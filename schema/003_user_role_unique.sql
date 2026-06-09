# ═══════════════════════════════════════════════════════════════
# 003 — user_role.user_id 唯一键迁移（存量库）
# ═══════════════════════════════════════════════════════════════
# 在 fuling_knowledge 数据库上执行。幂等，可重复跑。
#
# 背景：user_role 原本只有普通索引 idx_user_id，没有唯一键 ——
#   1) dingtalk_identity 的 INSERT ... ON DUPLICATE KEY UPDATE 实际从不触发去重，
#      缓存未命中竞态下可能写入同一 user_id 的多行；
#   2) dept_code 驱动 HA3 dept_internal 权限过滤，SELECT ... LIMIT 1 无排序时
#      取哪一行是不确定的（手工种入的部门映射可能被旧行盖住）。
# 本迁移先按「最新行优先」去重（updated_at 最大，平手取 id 最大），
# 再加 UNIQUE KEY uk_user_id 并移除冗余的普通索引。
# ═══════════════════════════════════════════════════════════════

USE fuling_knowledge;

-- 1. 去重：凡存在更新的同 user_id 行，删除较旧的那行（每个 user_id 恰好保留 1 行）
DELETE ur FROM user_role ur
JOIN user_role newer
  ON newer.user_id = ur.user_id
 AND (newer.updated_at > ur.updated_at
      OR (newer.updated_at = ur.updated_at AND newer.id > ur.id));

-- 2. 加唯一键（幂等：已存在则跳过）
DROP PROCEDURE IF EXISTS _ur_add_unique_if_not_exists;
DELIMITER $$
CREATE PROCEDURE _ur_add_unique_if_not_exists()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'user_role'
          AND INDEX_NAME   = 'uk_user_id'
    ) THEN
        ALTER TABLE `user_role` ADD UNIQUE INDEX `uk_user_id` (`user_id`);
    END IF;

    -- 3. 移除被唯一键取代的普通索引（幂等）
    IF EXISTS (
        SELECT 1 FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'user_role'
          AND INDEX_NAME   = 'idx_user_id'
    ) THEN
        ALTER TABLE `user_role` DROP INDEX `idx_user_id`;
    END IF;
END$$
DELIMITER ;

CALL _ur_add_unique_if_not_exists();
DROP PROCEDURE IF EXISTS _ur_add_unique_if_not_exists;
