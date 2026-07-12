-- schema/025_approval_workflow.sql
-- 库：fuling_operation
-- WS3 审批闭环持久化（v2 报告 §5/§N「Approval Engine」；深度审查 2026-07-09 A 组 P1）。
-- 补齐「request→decision→invocation→audit 四表可关联回放」缺的前两环：
--   approval_request  = 一次 HIGH_WRITE/require_approval 挂起产生的待审批事实（executor 挂起侧写入）
--   approval_decision = 审批人处置事实（routes /api/agent/approve 决策侧写入，幂等键防重复决策）
-- 状态机复用 kb_access_request 范式：SELECT ... FOR UPDATE + from_status='pending' 单向 CAS，
-- first-valid-wins；重复决策由 uk_req_idem 幂等返回。
-- 脱敏纪律（与 022 tool_invocation.args_json 同）：proposed_args_json / edited_args_json 均为
-- **脱敏后**参数（agent_runtime/sanitize.py），原文只在 checkpoint（短留存 + 主体擦除覆盖）。
-- ⚠️ COLLATE 刻意不显式指定：与 022 五表同吃库默认（族内一致优先）；全族统一 COLLATE
-- 由后续专门迁移一次做齐（深度审查 schema 组），零散指定反而制造 1267 混排。

CREATE TABLE IF NOT EXISTS approval_request (
  request_id         CHAR(32)    NOT NULL,
  run_id             CHAR(32)    NOT NULL,
  call_id            VARCHAR(64) NOT NULL,           -- 挂起时的 pending_call.call_id（resume 放行凭据锚点）
  tool_name          VARCHAR(64) NOT NULL,
  tool_version       VARCHAR(16) NOT NULL DEFAULT '',
  proposed_args_json JSON        NOT NULL,           -- 脱敏后参数（审批队列渲染用）
  args_digest        CHAR(64)    NOT NULL,           -- 原文摘要（与 tool_invocation/audit 关联回放）
  render_summary     TEXT        NULL,               -- 队列/卡片文案（工具名/关键参数/影响面）
  requested_by       VARCHAR(64) NOT NULL,           -- 发起用户（职责分离：decided_by 不得等于它）
  requested_dept     VARCHAR(64) NULL,
  approver_scope     VARCHAR(64) NOT NULL,           -- 审批人裁决键（owner_dept；dept_admin managed 覆盖即可审）
  status ENUM('pending','approved','edited','rejected_feedback',
              'rejected_terminate','expired','cancelled') NOT NULL DEFAULT 'pending',
  expires_at         DATETIME(3) NOT NULL,           -- 过期=拒绝（沉默不是同意）；reaper 扫描置 expired
  created_at         DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  decided_at         DATETIME(3) NULL,
  PRIMARY KEY (request_id),
  KEY idx_status_expiry (status, expires_at),
  KEY idx_run (run_id),
  KEY idx_scope_status (approver_scope, status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS approval_decision (
  decision_id      CHAR(32)    NOT NULL,
  request_id       CHAR(32)    NOT NULL,
  decision ENUM('approved','edited','rejected_feedback','rejected_terminate') NOT NULL,
  edited_args_json JSON        NULL,                 -- EDITED：脱敏后的人工改参（真参走 resume 内存通道）
  reason           TEXT        NULL,
  decided_by       VARCHAR(64) NOT NULL,
  idempotency_key  VARCHAR(64) NOT NULL,             -- 客户端重试/回调重放幂等（uk 防重复决策）
  decided_at       DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (decision_id),
  UNIQUE KEY uk_req_idem (request_id, idempotency_key),
  KEY idx_req (request_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
