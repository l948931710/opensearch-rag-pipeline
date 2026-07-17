-- ════════════════════════════════════════════════════════════════════════════
-- 045_agent_tool_operation.sql — PR-3 Stage C：HIGH_WRITE 工具操作台账（回执查证真相）
--
-- @DB fuling_operation（staging 为 fuling_operation_stg）
--
-- 动机（PR-3 立项 §3 Stage C，docs/pr3_durable_worker_design_2026-07-17_DRAFT.md）：
--   写型工具超时/进程崩溃后「副作用是否已发生」此前无处可问——invocation 落 uncertain
--   只能人工对账（/api/agent/invocations 核实后处置）。本表是操作级 durable 回执：
--   工具把台账行与副作用**写进同一个事务**（operation_writer 游标回调，形态同 Stage B
--   insert_command_tx），于是「台账行存在 ⇔ 副作用已 commit」成为可查证事实——
--   check_operation(operation_id) 按此三态作答（applied / not_applied / unknown），
--   uncertain 对账从人工升级为自动查证（operation_reconciler，随 reaper 周期跑）。
--
-- 语义：
--   · operation_id = tool_invocation.invocation_id（executor 注入 ctx.operation_id 透传
--     下游；同键重试复用同一 invocation 行 ⇒ 同一 operation_id）；
--   · **PK 即 fencing**：僵尸线程与对账放行后的重试并发提交时，同 operation_id 的台账
--     INSERT 至多一个 commit——输家整事务回滚，读现行台账回执按幂等成功返回（绝无双副作用）；
--   · receipt_json 与工具成功返回的 receipt 同构（含 requested_by/approved_by 等主体字段，
--     敏感级与 tool_invocation.receipt_json 同级）——自动对账把它补进 invocation 行，
--     幂等命中/运行中心据此呈现真实结果；
--   · 无 status 列：行=已提交的操作（不存在"进行中"的台账行），行不存在=未提交。
--
-- retention：随 F-36 纳管（tool_operations 作业，与 tool_invocation 同窗 12 月
--   RAG_RETENTION_AGENT_TRACE_MONTHS）；purge_subject 经 run_id→agent_run.user_id 覆盖。
--
-- ADDITIVE / 幂等：仅新建表。未接线工具/flag 关闭时代码不读写本表——可先 apply 后部署。
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS agent_tool_operation (
  operation_id  CHAR(32)     NOT NULL COMMENT '= tool_invocation.invocation_id（executor 铸、ctx 透传）',
  tool_name     VARCHAR(64)  NOT NULL COMMENT '写入方工具（查证时校验，错配=unknown 绝不自动处置）',
  run_id        CHAR(32)     DEFAULT NULL COMMENT '溯源 + purge_subject 归属链',
  receipt_json  JSON         DEFAULT NULL COMMENT '操作回执（与工具成功返回的 receipt 同构）',
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '=副作用 commit 时刻',
  PRIMARY KEY (operation_id),
  KEY idx_tool_created (tool_name, created_at),
  KEY idx_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='PR-3 Stage C 工具操作台账（同事务回执；行存在⇔副作用已提交）';
