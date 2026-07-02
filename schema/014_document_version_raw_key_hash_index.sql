-- ════════════════════════════════════════════════════════════════════════════
-- 014_document_version_raw_key_hash_index.sql — raw_key_hash 回填 + 索引（perf#5）
--
-- @DB fuling_knowledge（staging 为 fuling_knowledge_stg）
--
-- 动机：上传注册（kb_register）与贡献物化（_materialize_contribution）的幂等检查按
-- raw_key（VARCHAR(1024) 无索引）等值查找，每次全表扫且随 document_version 增长恶化。
-- raw_key_hash CHAR(64) 已在全部写路径落值（api 自助注册 / contribution / pipeline
-- register，001 里建列时即为此预留）——先回填存量 NULL，再建索引，点查走索引。
--
-- 代码侧（同批次落地）谓词形态：
--   WHERE raw_key=%s AND (raw_key_hash=%s OR raw_key_hash IS NULL)
--   · raw_key 等值仍是权威判定（hash 碰撞免疫）；
--   · hash 等值 + IS NULL 走本索引的 ref_or_null 访问；
--   · 回填前/索引前的环境自动退化为与旧行为等价的正确全扫——无部署顺序风险，
--     本迁移带来的是纯性能收益（回填后 IS NULL 分支为空，点查 O(1)）。
--
-- 非 UNIQUE 的原因：与 007 idx_etag 同理——历史数据无法保证零重复（重灌/迁移期同
-- raw_key 多版本行合法存在：版本升级共用 raw/ 键的场景），先做普通索引；
-- 待 reconcile 清零重复后再考虑升 UNIQUE（那是行为变更，需单独迁移号）。
--
-- ADDITIVE / 幂等：UPDATE 只补 NULL；索引用 information_schema 守卫（apply 脚本）。
-- ⚠️ GATED PROD DDL——回填 UPDATE 是一次性全表写（行数~千级，秒级完成），选低峰执行；
-- apply 后向 fuling_knowledge.schema_migrations 记 '014'。
-- ════════════════════════════════════════════════════════════════════════════

-- 1) 回填存量 NULL（SHA2(...,256) 与代码侧 hashlib.sha256(raw_key.encode('utf-8')) 同值；
--    raw_key 为 NULL/空的行保持 NULL，代码谓词的 IS NULL 分支继续兜住）
UPDATE document_version
   SET raw_key_hash = SHA2(raw_key, 256)
 WHERE raw_key_hash IS NULL
   AND raw_key IS NOT NULL AND raw_key != '';

-- 2) 建索引（幂等守卫写在 apply 脚本里；裸 SQL 版本）：
CREATE INDEX idx_raw_key_hash ON document_version (raw_key_hash);

-- 回滚：
-- DROP INDEX idx_raw_key_hash ON document_version;   -- 回填数据保留无害（列本就该有值）
