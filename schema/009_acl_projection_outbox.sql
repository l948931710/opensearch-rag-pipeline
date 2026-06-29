-- ════════════════════════════════════════════════════════════════════════════
-- 009_acl_projection_outbox.sql — Phase D allowed_depts 投影 outbox（撤销/审批必达重物化）
--
-- ADDITIVE / IDEMPOTENT-on-apply（与 003/004/006/008 同纪律）：新建一张表，不改既有列/行、
-- 不动检索路径、不重建 HA3。CREATE TABLE IF NOT EXISTS 原生幂等。
--
-- 为何需要：跨部门授权 approve/reject/revoke（kb_access_request.status，= 权威）改后，需把该 doc 的
--   allowed_depts 投影（chunk_meta.allowed_depts + index_status='NOT_INDEXED' = stage-3 outbox）重算。
--   decide 端点内联调 materialize 是【best-effort】：可能抛异常或撞 2h PROCESSING 反抢锁（skipped_locked）
--   而漏标脏 → 权威已变但投影滞后 → HA3 残留（撤销方向 = 机密性回归尾）。此前只靠 allowed_depts_reconcile
--   全扫兜底（capped 200/轮、仅 stage-3）。本表把【受影响的具体 doc】持久入队，让 stage-3 outbox drain
--   定向、幂等重试至成功，与全扫 reconcile 互补（定向必达 + 全扫兜底）。读侧另有 fail-closed 复核即时拒绝。
--
-- 语义：decide 端点【同事务】enqueue（权威变更与投影意图原子提交——enqueue 失败则整笔回滚，绝不出现
--   权威已改而无 outbox 行的撕裂）。drain（access_grants.drain_acl_projection_outbox，stage-3 pre-drain）
--   逐行幂等 materialize：成功/unchanged → 标 done_at；skipped_locked/失败 → attempts++ 留待下轮。
--
-- 一行一 doc（UNIQUE doc_id）：重复 enqueue 走 ON DUPLICATE KEY UPDATE 复活（done_at=NULL, attempts=0）；
--   不留历史（审计在 audit_log）。done_at IS NULL = 待处理。
--
-- ⚠️ DB：fuling_knowledge（与 kb_access_request / chunk_meta / document_meta 同库）。
-- ════════════════════════════════════════════════════════════════════════════

-- @DB fuling_knowledge
CREATE TABLE IF NOT EXISTS kb_acl_projection_outbox (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    doc_id      VARCHAR(128) NOT NULL COMMENT '待重物化 allowed_depts 投影的文档（fuling_knowledge.document_meta.doc_id）',
    reason      VARCHAR(64)  DEFAULT NULL COMMENT '入队原因（approved/rejected/revoked 等，审计）',
    attempts    INT          NOT NULL DEFAULT 0 COMMENT 'drain 重试次数（skipped_locked/失败累加）',
    last_error  VARCHAR(512) DEFAULT NULL COMMENT '最近一次 drain 失败原因（成功置空）',
    enqueued_at DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '入队时间（drain 按此 FIFO）',
    updated_at  DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    done_at     DATETIME     DEFAULT NULL COMMENT '投影落实时间（NULL=待处理；非空=已完成，drain 跳过）',
    UNIQUE KEY uniq_doc (doc_id),         -- 一行一 doc；重复 enqueue 走 ON DUPLICATE 复活
    INDEX idx_pending (done_at)           -- drain 扫 done_at IS NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  -- ⚠️ COLLATE 必须显式 = utf8mb4_unicode_ci，与全库一致（见 008 注释；doc_id 可能与 document_meta JOIN）。
  COMMENT='Phase D allowed_depts 投影 outbox：decide 同事务入队 + stage-3 幂等 drain（撤销必达）';
