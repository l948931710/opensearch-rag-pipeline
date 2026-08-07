-- ════════════════════════════════════════════════════════════════════════════
-- 067_contribution_node_axis.sql — 贡献域归属迁到 node 轴（M1）
-- 库：fuling_operation（kb_contribution 的归属库，见 schema/README.md 矩阵）
-- 方案：docs/contribution_node_axis_migration_2026-08-06.md（Claude-Codex 四轮 APPROVE）
--
-- ── 为什么需要这一列 ─────────────────────────────────────────────────────────
-- 贡献域的八个消费点全部只认组码 `category_dept`（`dept_admin_grant.managed_owner_dept`），
-- 而 2026-07-27 起的节点授权（`dept_admin_node_grant`，schema/060）是**另一根轴**：
-- 只被 node 授权的管理员在贡献域**看不见队列、审不了、采纳不成、也收不到通知**。
-- 本列把 node 归属显式落到贡献行上，判定按轴分流。
--
-- ── 这一列的语义（轴隔离，不是"两个都试一遍"）──────────────────────────────
--   · NULL      = 组码轴历史行 ⇒ 判定**只**看 `category_dept`（与改动前逐字节一致）
--   · 非 NULL   = node 轴行     ⇒ 判定**只**看本列（`category_dept_id ∈ 管辖后代集`）
-- 两支互斥，**绝不 OR 交叉**：交叉会让 node 行的 `category_dept` 残值继续命中旧组码
-- 管理员（与 060 阶段 B 的 `owner_dept` 残值越权同型；设计稿 §3.5）。
-- 组码 → dept_id 是**一对多**，代码侧任何位置都不得从 `category_dept` 反推本列
-- （存量迁移只走 ID 白名单，见方案 M8）。
--
-- ── 兼容性 ───────────────────────────────────────────────────────────────────
-- 纯加列（可空、无默认值变化）+ 加索引；存量行全 NULL ⇒ 行为逐字节不变。
-- 复合索引列序对齐既有 `idx_dept_review (category_dept, review_status)` —— 审核队列的
-- node 腿谓词形状与组码腿同构。
-- 代码侧带 capability 探测（`_contrib_node_axis_present`，1054/1146 → 恒 legacy 分支），
-- **先部署后 apply 与先 apply 后部署都安全**。
--
-- 幂等守卫（与 060/062/063/064 同款 PREPARE 形态）：裸 `ADD COLUMN` / `ADD INDEX` 重跑会
-- ERROR 1060 / 1061，而 apply_migration 的台账在 checksum 一致时是**放行重跑**的
-- ⇒ 必须自带幂等。MySQL 8 的 `ALTER TABLE ... ADD COLUMN` 不支持 `IF NOT EXISTS`。
-- ════════════════════════════════════════════════════════════════════════════

-- @DB fuling_operation
SET @c = (SELECT COUNT(*) FROM information_schema.COLUMNS
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kb_contribution'
            AND COLUMN_NAME = 'category_dept_id');
SET @ddl = IF(@c = 0,
    "ALTER TABLE kb_contribution ADD COLUMN category_dept_id BIGINT NULL COMMENT '归属组织节点（node 轴权威）。NULL=组码轴行，判定回落 category_dept'",
    'SELECT 1');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

SET @i = (SELECT COUNT(*) FROM information_schema.STATISTICS
          WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'kb_contribution'
            AND INDEX_NAME = 'idx_dept_id_status');
SET @ddl = IF(@i = 0,
    'ALTER TABLE kb_contribution ADD INDEX idx_dept_id_status (category_dept_id, review_status)',
    'SELECT 1');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ── 台账（F-35 纪律：schema 文件 + schema_migrations 一行，缺一即事故预备役）──
-- ⚠️ **不要手写 INSERT**（与 062/063 同款纪律）：`schema_migrations` 的 PK 是 `filename`、
--    并无 `description` 列，且 checksum 由 apply_migration.py 计算写入
--    （032 起的「同名异 checksum」漂移检测依赖它）。一律走：
--        python scripts/apply_migration.py schema/067_contribution_node_axis.sql
