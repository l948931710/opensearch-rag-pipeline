-- 063_visibility_intent.sql — C9 方案 B′：可见范围「意图」的版本级持久化
-- 库：fuling_knowledge（document_version 的归属库，见 schema/README.md 矩阵）
-- 拍板：docs/ops/c9_visibility_intent_signoff_2026-08-03.md（Sam 2026-08-03 选 B′）
--
-- ── 为什么需要这一列 ───────────────────────────────────────────────────────────
-- `kb_set_visibility` 改的可见范围会被 stage-2 **按 raw_key 路径覆盖回写**：
-- raw_key 的权限段才是 stage-2 的权威（resolve_permission_level 路径解析），
-- 于是管理员改完可见范围，下一轮入库就把它重置回上传路径隐含的值。
--
-- 机械根因（2026-08-03 核码坐实）：两个写方**锁的是不相交的行**——
--   · kb_set_visibility 只锁 document_meta（kb_console.py，FOR UPDATE）
--   · stage-2 认领只锁 document_version（dataworks_orchestrator.py，FOR UPDATE OF dv SKIP LOCKED）
-- ⇒ 彼此零互斥，覆盖不是偶发竞态而是必然。
--
-- ── 这一列的语义 ─────────────────────────────────────────────────────────────
-- nullable **canonical 权限值**（'public' / 'dept_internal' / 'restricted'），**不是布尔位**：
--   · NULL      = 无显式意图 ⇒ stage-2 沿用 raw_key 路径解析（历史行为，逐字节不变）
--   · 非 NULL   = 管理员显式声明过 ⇒ stage-2 loader 必须优先采用它
-- **只由显式管理员动作写**。⚠️ 绝不能反过来让「RDS 全局赢」：stage-1 自动注册的 INSERT
-- 根本不含 permission_level 列（落 schema 默认 'public'），DataWorks 批量注册更是硬编码
-- 'public' ⇒ 全局让 RDS 赢会**确定性破坏** raw/.../internal/ 与 restricted 文件。
--
-- ── 兼容性 ───────────────────────────────────────────────────────────────────
-- 纯加列、可空、无默认语义变化；未 apply 的环境走 capability 探测降级（先部署后 apply 安全，
-- 与 048/049/050/062 同款纪律）。
ALTER TABLE document_version
    ADD COLUMN permission_override VARCHAR(32) DEFAULT NULL
        COMMENT 'C9/B′：管理员显式声明的可见范围意图（canonical 值）；NULL=无意图，走 raw_key 路径解析';

-- 台账（schema/README.md 的 F-35 纪律）
INSERT INTO schema_migrations (version, description, applied_at)
VALUES ('063', 'C9/B-prime: document_version.permission_override（可见范围意图版本级持久化）', NOW())
ON DUPLICATE KEY UPDATE applied_at = VALUES(applied_at);
