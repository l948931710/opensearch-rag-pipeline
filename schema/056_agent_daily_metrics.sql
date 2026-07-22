-- 056_agent_daily_metrics.sql — Agent 权威监控面持久表(Majors γ3/M9,2026-07-21)
-- 库：fuling_operation
--
-- 背景(07-21 评审 M9,codex 共识 docs/majors_remediation_plan_2026-07-21_DRAFT.md)：
--   通用 qa_rollup 不取 model_name、通用看板显式排除 agent 行(kb_console
--   model_name<>'agent')——agent 的延迟/可用率/队列健康在权威 SLO 面**完全缺席**。
--   本表是 agent_health 作业按日 UPSERT 的持久权威面(与 serving 进程内 reaper 探针
--   正交：探针与进程同生死,本表是 ops_monitor/DataWorks 调度的库内事实)。
--
--   按日一行(北京日界,与 qa_rollup run_rollup 同约定)：当日聚合(可用率/延迟分位)
--   + agent_health 采集时点的八维瞬时快照。RAG_AGENT_HEALTH_ENABLE=on 时作业写入
--   (flag 默认 off → 本表零写入,存在即可,readiness approval 无关本表)。
--
-- 可用率口径(codex v4 blocker：M9 必须有 agent 独立可用率)：
--   success_rate = runs_succeeded / NULLIF(runs_total,0)；agent_run 终态计数。
-- 延迟口径：active_latency=各腿活跃执行耗时累计(不含审批等待,054 列)；
--   ttr=ended_at−started_at 墙钟(含审批等待,只进本表不进通用 p95)。
-- 八维(采集时点瞬时,NULL=该维 N/A：relay/worker 依赖对应 flag)：
--   dispatch_backlog / approval_pending+oldest_age / stale_running /
--   uncertain_invocations+oldest_age / relay_miss_rate / worker_heartbeat_age /
--   cancellation_latency_p95 / recovery_convergence_p95。
--
-- 全语句自幂等(CREATE TABLE IF NOT EXISTS)，可重放。

CREATE TABLE IF NOT EXISTS agent_daily_metrics (
  metric_date              DATE          NOT NULL COMMENT '北京日界(与 qa_rollup 同约定)',
  -- 可用率(当日 agent_run 终态聚合)
  runs_total               INT           NOT NULL DEFAULT 0,
  runs_succeeded           INT           NOT NULL DEFAULT 0,
  runs_failed              INT           NOT NULL DEFAULT 0,
  runs_cancelled           INT           NOT NULL DEFAULT 0,
  runs_expired             INT           NOT NULL DEFAULT 0,
  success_rate             DECIMAL(5,4)  NULL COMMENT 'succeeded/total；total=0 时 NULL',
  error_rate               DECIMAL(5,4)  NULL COMMENT 'failed/total',
  -- 延迟分位(当日)
  active_latency_p50_ms    BIGINT        NULL COMMENT '活跃执行耗时 p50(不含审批等待,054)',
  active_latency_p95_ms    BIGINT        NULL,
  ttr_p50_ms               BIGINT        NULL COMMENT 'time-to-resolution p50(墙钟,含审批)',
  ttr_p95_ms               BIGINT        NULL,
  -- 八维瞬时快照(采集时点；NULL=该维 N/A)
  dispatch_backlog         INT           NULL COMMENT 'durable 命令积压(backlog_count)',
  approval_pending         INT           NULL,
  approval_oldest_age_s    BIGINT        NULL,
  stale_running            INT           NULL COMMENT '心跳龄超阈的 running',
  uncertain_invocations    INT           NULL,
  uncertain_oldest_age_s   BIGINT        NULL,
  relay_miss_rate          DECIMAL(5,4)  NULL COMMENT '终态帧缺失率(relay off=NULL)',
  worker_heartbeat_age_s   BIGINT        NULL COMMENT 'dispatcher 心跳龄(dispatch off=NULL)',
  cancellation_latency_p95_s INT         NULL COMMENT 'cancel_requested→ended p95',
  recovery_convergence_p95_s INT         NULL COMMENT 'outbox created→run ended p95(代理口径)',
  -- 裁决
  slo_verdict              VARCHAR(16)   NULL COMMENT 'ok / warn / breach',
  computed_at              DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (metric_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
