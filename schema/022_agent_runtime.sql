-- schema/022_agent_runtime.sql
-- 库：fuling_operation
-- 企业级 Agent Runtime durable 表族（v2 报告 §4/§6；WS1-2）。
-- 取号：re-baseline 从 022 起（017–021 已 apply 生产，见 schema/README.md）。
-- 分工：Redis 承热态（会话窗/限流/去重/锁）；RDS 承不可丢事实（run/审批/审计/回执/checkpoint）。
-- 状态机：agent_run.status 单向迁移，复用 kb_access FOR UPDATE + CAS（见 run_store.py）。
-- ⚠️ 执行宿主/checkpoint 字段级结构（B1/B2/B4）随「从长计议」拍板后再定；本表只持久化 blob 字节。

-- ── 工具注册表（报告 §4；元数据落 DB，进程内只是缓存）──────────────────────────
CREATE TABLE IF NOT EXISTS tool_registry (
  tool_name        VARCHAR(64)  NOT NULL,
  version          VARCHAR(16)  NOT NULL,
  spec_json        JSON         NOT NULL,          -- ToolSpec 全量（含 input/output schema）
  risk_level       VARCHAR(16)  NOT NULL,
  permission_scope VARCHAR(64)  NOT NULL,
  owner_team       VARCHAR(64)  NOT NULL,
  status           ENUM('active','deprecated','disabled') NOT NULL DEFAULT 'active',
  registered_by    VARCHAR(64)  NOT NULL,
  created_at       DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (tool_name, version),
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 运行主表（血缘 / 状态机 / 心跳）报告 §6 ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_run (
  run_id              CHAR(32)     NOT NULL,
  thread_id           VARCHAR(160) NOT NULL,
  conversation_id     VARCHAR(128) NULL,
  user_id             VARCHAR(64)  NOT NULL,
  channel             VARCHAR(16)  NOT NULL,       -- dingtalk|console|miniapp|api
  agent_profile       VARCHAR(64)  NOT NULL,
  -- B1 决策(b) + B3：resuming = 审批已批、执行宿主尚未接手的中间态（回调 CAS suspended→resuming
  -- 后立即 ACK，续跑由 B1 有界执行器消费；decided-but-not-resumed 对账扫此态，见 run_store）。
  status              ENUM('running','suspended','resuming','succeeded','failed','cancelled','expired') NOT NULL,
  parent_run_id       CHAR(32)     NULL,           -- 子 run 血缘（子 Agent，P2+）
  parent_reason       VARCHAR(32)  NULL,
  acl_groups_snapshot JSON         NOT NULL,       -- 审计快照；resume 时不用它授权（重新解析）
  model_profile       VARCHAR(64)  NULL,
  prompt_version      VARCHAR(32)  NULL,
  git_sha             VARCHAR(40)  NULL,
  -- B8：预算消耗跟踪落 durable（caps 在 RunBudget/ctx，消耗须跨 suspend/resume 持久，不放 frozen ctx）
  turns_used          INT          NOT NULL DEFAULT 0,
  tool_calls_used     INT          NOT NULL DEFAULT 0,
  tokens_used         INT          NOT NULL DEFAULT 0,
  heartbeat_at        DATETIME(3)  NULL,           -- 僵尸 run 回收判据（心跳扫描）
  started_at          DATETIME(3)  NOT NULL,
  ended_at            DATETIME(3)  NULL,
  PRIMARY KEY (run_id),
  KEY idx_thread (thread_id, started_at),
  KEY idx_status_hb (status, heartbeat_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 步骤 trace（即表；每 turn 一步）报告 §6 ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_step (
  run_id            CHAR(32) NOT NULL,
  step_no           INT      NOT NULL,
  kind              ENUM('model_call','tool_call','approval','compaction','system') NOT NULL,
  payload_json      JSON     NOT NULL,             -- 脱敏后；大对象放 digest+OSS 引用
  tokens_prompt     INT      NULL,
  tokens_completion INT      NULL,
  latency_ms        INT      NULL,
  created_at        DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (run_id, step_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 挂起 checkpoint（跨天审批/崩溃恢复）报告 §6 ─────────────────────────────────
CREATE TABLE IF NOT EXISTS agent_checkpoint (
  run_id        CHAR(32)   NOT NULL,
  checkpoint_id CHAR(32)   NOT NULL,
  state_blob    MEDIUMBLOB NOT NULL,               -- msgpack + 可插拔加密（loop 层负责编解码）
  state_digest  CHAR(64)   NOT NULL,
  created_at    DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (run_id, checkpoint_id),
  KEY idx_run_created (run_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ── 工具调用（幂等键 / 回执 / 对账）报告 §6 ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS tool_invocation (
  invocation_id       CHAR(32) NOT NULL,
  run_id              CHAR(32) NOT NULL,
  step_no             INT      NOT NULL,
  tool_name           VARCHAR(64) NOT NULL,
  tool_version        VARCHAR(16) NOT NULL,
  args_json           JSON     NOT NULL,           -- 脱敏后入 JSON，原文只留 digest
  args_digest         CHAR(64) NOT NULL,
  idempotency_key     VARCHAR(96) NULL,
  status              ENUM('proposed','denied','pending_approval','executing',
                           'succeeded','failed','compensated') NOT NULL,
  policy_decision     VARCHAR(16) NOT NULL,
  policy_id           VARCHAR(64) NULL,
  approval_request_id CHAR(32) NULL,
  result_digest       CHAR(64) NULL,
  receipt_json        JSON     NULL,
  started_at          DATETIME(3) NULL,
  ended_at            DATETIME(3) NULL,
  error_text          TEXT     NULL,
  PRIMARY KEY (invocation_id),
  UNIQUE KEY uk_tool_idem (tool_name, idempotency_key),   -- 写型工具幂等兜底
  KEY idx_run (run_id, step_no),
  KEY idx_status (status, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
