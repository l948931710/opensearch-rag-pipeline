# ═══════════════════════════════════════════════════════════════
# 016 — user_feedback (message_id,user_id) 去重 + 唯一键补建（存量库补救）
# ═══════════════════════════════════════════════════════════════
# 在 fuling_operation 数据库上执行。幂等，可重复跑。
#
# 背景：002_feedback_system.sql 在 CALL _add_unique_if_not_exists('user_feedback',
#   'uk_message_user', ...) 之前【未先去重】。若 002 首次执行时 user_feedback 存量已有
#   相同 (message_id, user_id) 多行（上线 002 前合法：同一用户对同一消息多次反馈各成一行），
#   ADD UNIQUE 会报 ERROR 1062 中止，DDL autocommit 使 002 半途而废、唯一键始终建不上、
#   依赖 ON DUPLICATE KEY UPDATE 的反馈写路径无法去重。
#
# ⚠️ 为何单独出一个迁移而非改 002：迁移工具按文件名记录已执行版本，【已 apply 过 002 的
#   生产环境不会重跑 002】——仅修改 002 对既有生产无追溯效果。002 里已同步补上去重（供全新
#   安装/重跑），本文件则负责【已上线环境】的补救：先去重、再幂等补唯一键。两者都跑到即安全。
#
# 关键判断留给运维：
#   - 若你的生产 002 当年成功建上了 uk_message_user（说明当时无重复对），本迁移是 no-op（安全）。
#   - 若 002 当年跑到 uk_message_user 就 1062 中止（唯一键没建上），本迁移完成补救。
# 去重范式同 003_user_role_unique.sql：保留最新一行（updated_at 最大，平手取 id 最大）。
#
# ⚠️ 本 SQL 未在开发沙箱执行验证（无本地 MySQL）；上线前请在预演库 dry-run（先 SELECT 统计
#   将被删除的重复行数），确认符合预期后再在生产执行。
# ═══════════════════════════════════════════════════════════════

USE fuling_operation;

-- 1. 去重：凡存在更新的同 (message_id,user_id) 行，删除较旧的那行（每对恰好保留 1 行）。
--    只删真正的重复对；002 的 L234-240 backfill 已把 NULL 填成互不相同的 legacy_/unknown_。
DELETE uf FROM user_feedback uf
JOIN user_feedback newer
  ON newer.message_id = uf.message_id
 AND newer.user_id    = uf.user_id
 AND (newer.updated_at > uf.updated_at
      OR (newer.updated_at = uf.updated_at AND newer.id > uf.id));

-- 2. 幂等补唯一键（已存在则跳过；补救 002 因 1062 未能建上的情形）
DROP PROCEDURE IF EXISTS _uf_add_unique_if_not_exists;
DELIMITER $$
CREATE PROCEDURE _uf_add_unique_if_not_exists()
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME   = 'user_feedback'
          AND INDEX_NAME   = 'uk_message_user'
    ) THEN
        ALTER TABLE `user_feedback` ADD UNIQUE INDEX `uk_message_user` (`message_id`, `user_id`);
    END IF;
END$$
DELIMITER ;

CALL _uf_add_unique_if_not_exists();
DROP PROCEDURE IF EXISTS _uf_add_unique_if_not_exists;
