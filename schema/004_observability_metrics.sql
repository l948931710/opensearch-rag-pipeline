-- ════════════════════════════════════════════════════════════════════════════
-- 004_observability_metrics.sql — Phase-2 OBS-3 + OBS-5 (roadmap-to-8 dim10)
--
-- BOTH ADDITIVE / NULL-SAFE / IDEMPOTENT-on-apply (same discipline as 003):
--   * OBS-3 adds NULL-default metric columns to pipeline_run — existing rows/reads/writes unaffected;
--     run_finish backfills them opportunistically on the next ingest run.
--   * OBS-5 creates a brand-new rollup table — touches nothing existing; the nightly rollup populates
--     it from a read of qa_session_log.
--   * No data rebuild. No behavior change until the OBS-3 plumbing / OBS-5 rollup run.
--
-- ⚠️ TWO DATABASES:
--   * pipeline_run lives in  fuling_knowledge  (created by 003).
--   * qa_session_log lives in fuling_operation (002_feedback_system) → qa_daily_metrics goes there too.
--   The apply script (scratch/apply_migration_004.py) connects to each DB for its section and guards
--   the ALTER via information_schema (MySQL 8.0 has no ADD COLUMN IF NOT EXISTS). CREATE TABLE IF NOT
--   EXISTS is natively idempotent.
-- ════════════════════════════════════════════════════════════════════════════

-- ─────────────────────────────────────────────────────────────────────────────
-- OBS-3 — per-run ingestion metrics on pipeline_run (DB: fuling_knowledge)
-- Feeds the embed-fail-rate signal (the P0 class) + partial-batch / deactivation visibility over
-- time. embed_fail_rate = embedding_failed_chunks / NULLIF(embedded_chunks+embedding_failed_chunks,0).
-- ─────────────────────────────────────────────────────────────────────────────
-- @DB fuling_knowledge
ALTER TABLE pipeline_run
    ADD COLUMN embedded_chunks         INT DEFAULT NULL COMMENT 'OBS-3: chunks successfully embedded this run'         AFTER chunks_written,
    ADD COLUMN embedding_failed_chunks INT DEFAULT NULL COMMENT 'OBS-3: chunks that failed/omitted embedding (P0 signal)' AFTER embedded_chunks,
    ADD COLUMN chunks_deactivated      INT DEFAULT NULL COMMENT 'OBS-3: old-version chunks deactivated this run'        AFTER embedding_failed_chunks,
    ADD COLUMN docs_failed             INT DEFAULT NULL COMMENT 'OBS-3: doc-versions that failed this run'              AFTER chunks_deactivated;

-- ─────────────────────────────────────────────────────────────────────────────
-- OBS-5 — nightly QA serving-quality rollup + SLO verdicts (DB: fuling_operation)
-- One row per business day. Percentiles are computed in Python (MySQL 8.0 lacks PERCENTILE_CONT).
-- tz_shift_hours records the Pacific→Beijing bucketing offset (qa_session_log.created_at is stored in
-- the SAE container's Pacific wall-clock; +15h ⇒ Beijing business day — see qa-log-analytics gotcha).
-- ─────────────────────────────────────────────────────────────────────────────
-- @DB fuling_operation
CREATE TABLE IF NOT EXISTS qa_daily_metrics (
    metric_date          DATE         NOT NULL PRIMARY KEY COMMENT 'Beijing business day (after tz_shift)',
    total_queries        INT          DEFAULT 0,
    success_count        INT          DEFAULT 0,
    refusal_count        INT          DEFAULT 0   COMMENT 'answer_status REFUSAL* or risk_blocked',
    no_result_count      INT          DEFAULT 0   COMMENT 'opensearch_hit_count=0 / NO_RESULT',
    error_count          INT          DEFAULT 0   COMMENT 'answer_status ERROR/exception',
    risk_blocked_count   INT          DEFAULT 0,
    p50_latency_ms       INT          DEFAULT NULL,
    p95_latency_ms       INT          DEFAULT NULL,
    avg_top_score        DECIMAL(10,4) DEFAULT NULL,
    distinct_users       INT          DEFAULT 0,
    distinct_sessions    INT          DEFAULT 0,
    single_chat_count    INT          DEFAULT 0,
    group_chat_count     INT          DEFAULT 0,
    answer_rate          DECIMAL(6,4) DEFAULT NULL COMMENT 'success_count / total_queries',
    no_result_rate       DECIMAL(6,4) DEFAULT NULL,
    error_rate           DECIMAL(6,4) DEFAULT NULL,
    slo_ok               TINYINT(1)   DEFAULT NULL COMMENT '1=all SLOs met, 0=>=1 breach',
    slo_breaches_json    JSON         DEFAULT NULL COMMENT '[{slo,threshold,value}] for breached SLOs',
    tz_shift_hours       INT          DEFAULT NULL COMMENT 'Pacific→Beijing offset used for bucketing',
    computed_at          DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_slo_ok (slo_ok)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
