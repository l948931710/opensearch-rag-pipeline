-- ════════════════════════════════════════════════════════════════════════════
-- 047_agent_run_cancel_request.sql — PR-3 Stage D：durable cancel（跨实例取消标记）
--
-- @DB fuling_operation（staging 为 fuling_operation_stg）
--
-- 动机（PR-3 §3 Stage D；routes/agent.py cancel 端点自注「跨实例 cancel 标记留 v2」）：
--   协作式取消此前只有进程内通道（RunHandle._cancel 旗标）——run 的驱动句柄不在
--   收到请求的实例上（多副本/实例重启后）只能 501，用户唯一出路是等 reaper 按心跳
--   把活 run 误判收尸。本列是 durable 取消意愿：任何副本可标记（CAS：仅非终态、
--   仅首标）；**驱动副本的 per-run 心跳 ticker（30s）顺带轮询**，命中即转进程内
--   协作旗标，沿既有轮边界收口机制落 cancelled 终态（阻塞中的模型/工具调用照旧
--   不被中断——与进程内 cancel 完全同语义，只是标记面变 durable）。
--
-- 语义：
--   · cancel_requested_at 非 NULL = 有人请求过取消（幂等：重复请求不覆写首标）；
--   · 标记 ≠ 承诺：持有者已死的 run 等 reaper 按心跳收尸（failed），标记只对活
--     持有者生效——诚实语义，端点文案如实说明；
--   · cancel_requested_by 记发起人（本人或 kb_admin，端点门禁已裁决）。
--
-- ADDITIVE：两列 NULL 默认，存量行零回填；旧代码零读写——可先 apply 后部署。
--   非幂等 DDL：重复执行由 scripts/apply_migration.py 的 information_schema 预检跳过。
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE agent_run
  ADD COLUMN cancel_requested_at DATETIME(3) NULL
    COMMENT 'durable 取消标记（任何副本可写；驱动副本心跳 ticker 拾取）' AFTER heartbeat_at,
  ADD COLUMN cancel_requested_by VARCHAR(64) NULL
    COMMENT '取消发起人（本人/kb_admin，端点门禁裁决后写入）' AFTER cancel_requested_at;
