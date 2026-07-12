-- schema/024_agent_audit_log.sql
-- 库：fuling_operation
-- 企业级 Agent 合规审计日志（v2 报告 §6 全链 trace / §N 治理；WS1 收尾③）。
-- 与 tool_invocation（运营 trace，存 args 原文/回执）分工正交：本表是**安全合规审计**——
--   谁(user/roles/acl 快照) · 在哪(channel) · 对什么资源(action/permission_scope) · 何种裁决
--   (decision/policy_id) · 何种风险(risk_level)，只存 args **摘要**(sha256)不存原文(PII-safe)。
-- 写入语义（audit.py）：HIGH_WRITE 工具**写前审计 fail-closed**（审计不可写→阻断执行，绝不产生
--   无审计记录的高风险副作用）；READ_ONLY/LOW_WRITE **fail-open**（审计写失败仅告警、不阻断）。
-- APPEND-ONLY：仅 INSERT，无 UPDATE/DELETE（合规不可篡改；瘦身归 retention.py，不在 DDL 层）。
-- ADDITIVE / IDEMPOTENT：仅新建一张表。取号 024（README 台账续现值）。

CREATE TABLE IF NOT EXISTS agent_audit_log (
  audit_id            CHAR(32)     NOT NULL,
  run_id              CHAR(32)     NULL,             -- 归属 agent_run（同步问答可空）
  step_no             INT          NULL,             -- 对应 agent_step / tool_invocation 的步号
  request_id          VARCHAR(32)  NULL,             -- X-Request-Id
  event_type          VARCHAR(32)  NOT NULL,         -- tool_call / policy_decision / approval / run_lifecycle
  action              VARCHAR(96)  NOT NULL,         -- 工具 name@version 或裁决主体
  risk_level          VARCHAR(16)  NULL,             -- read_only / low_write / high_write
  decision            VARCHAR(32)  NOT NULL,         -- authorized / denied / require_approval / executed / failed
  policy_id           VARCHAR(64)  NULL,
  user_id             VARCHAR(64)  NOT NULL,         -- 责任主体（未知置 '-'）
  roles               VARCHAR(255) NULL,             -- 逗号连接的角色快照
  acl_groups_snapshot VARCHAR(512) NULL,             -- JSON 数组快照（仅审计，不作授权——铁律 5）
  channel             VARCHAR(16)  NULL,             -- dingtalk / console / miniapp / api
  args_digest         CHAR(64)     NULL,             -- sha256(args)：存摘要不存原文
  detail_json         TEXT         NULL,             -- obligations / reason / error 摘要等结构化补充
  created_at          DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (audit_id),
  KEY idx_run (run_id, step_no),                     -- 按 run 回放
  KEY idx_actor (user_id, created_at),               -- 按责任主体 × 时间查
  KEY idx_event (event_type, created_at)             -- 按事件类型 × 时间查（如全部 high_write 授权）
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
