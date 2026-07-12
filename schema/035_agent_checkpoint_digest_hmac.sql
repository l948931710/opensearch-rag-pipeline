-- schema/035_agent_checkpoint_digest_hmac.sql
-- 库：fuling_operation
-- P1-2（重评审计 2026-07-11）：agent_checkpoint.state_digest 从裸 sha256（防损坏的
--   完整性）升级为带密钥 HMAC（真实性）——能写表的攻击者篡改 state_blob 后重算
--   sha256 同行存储即可通过旧校验；HMAC 没有密钥算不出。
--   新摘要格式 `hmac1:<64hex>` = 70 字符，原列 CHAR(64) 放不下 → 加宽 VARCHAR(80)。
--
-- ⚠️ 部署顺序：本迁移必须在带 HMAC 编码的代码**之前** apply（加宽是纯放大、旧行为
--   零影响；反之新代码写 70 字符进 CHAR(64) 会 1406 让挂起整体失败）。
--   密钥：RAG_AGENT_CHECKPOINT_KEY（缺省从 RAG_SESSION_SIGNING_KEY 派生）；
--   静态加密 RAG_AGENT_CHECKPOINT_ENCRYPT 默认 off（混合版本窗口后再开）。
--   MODIFY 幂等语义由 apply_migration.py 的 information_schema 预检 + 重复执行 no-op 保障。

ALTER TABLE agent_checkpoint
  MODIFY COLUMN state_digest VARCHAR(80) NOT NULL COMMENT 'hmac1:<hmac_sha256>（带密钥真实性）或历史裸 sha256';
