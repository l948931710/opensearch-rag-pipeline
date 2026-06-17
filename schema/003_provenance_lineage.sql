-- ════════════════════════════════════════════════════════════════════════════
-- 003_provenance_lineage.sql — Phase-1 provenance/lineage migrations
-- DB: fuling_knowledge   Applied: 2026-06-16 (roadmap-to-8: L2 content-hash + L6prov run table)
--
-- BOTH ADDITIVE / NULL-SAFE / IDEMPOTENT-on-apply:
--   * existing rows, reads, and writes are completely unaffected
--   * existing 6690 active chunks / all document_version rows keep NULL in the new column
--   * no data rebuild; values are backfilled opportunistically on the next ingest/re-chunk touch
--
-- NOTE: MySQL 8.0 has no `ADD COLUMN IF NOT EXISTS`; the apply script
-- (scratch/apply_migration_003.py) guards the ALTER via information_schema so re-apply is a no-op.
-- CREATE TABLE IF NOT EXISTS is natively idempotent.
-- ════════════════════════════════════════════════════════════════════════════

-- ── L2: canonical-text content hash → content-based reprocess gate ──
-- document_version.checksum_sha256 (existing, raw-bytes) + this canonical-text hash let
-- node_scan_raw_files skip an unchanged re-ingest and force a changed one, instead of the
-- status-flag-only decision today.
ALTER TABLE document_version
    ADD COLUMN canonical_sha256 VARCHAR(64) DEFAULT NULL
    COMMENT 'sha256 of canonical text; L2 content-based reprocess/skip gate'
    AFTER checksum_sha256;

-- ── L6prov: per-run provenance (which run / code rev / model produced or retired what) ──
-- kb_audit_log (L5) is the per-doc event log; pipeline_run is the per-run header it joins to
-- via trace_id = '<git_commit>:<bizdate>'. Capstone for lineage_audit (dim7).
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id                  VARCHAR(64)  NOT NULL PRIMARY KEY,
    stage                   INT          DEFAULT NULL,
    bizdate                 VARCHAR(16)  DEFAULT NULL,
    git_commit              VARCHAR(64)  DEFAULT NULL,
    extractor_version       VARCHAR(64)  DEFAULT NULL,
    chunker_version         VARCHAR(64)  DEFAULT NULL,
    detector_version        VARCHAR(64)  DEFAULT NULL,
    embedding_model         VARCHAR(128) DEFAULT NULL,
    embedding_model_version VARCHAR(64)  DEFAULT NULL,
    llm_model               VARCHAR(128) DEFAULT NULL,
    status                  VARCHAR(32)  DEFAULT 'RUNNING',
    docs_processed          INT          DEFAULT NULL,
    chunks_written          INT          DEFAULT NULL,
    error_message           TEXT         DEFAULT NULL,
    started_at              DATETIME     DEFAULT CURRENT_TIMESTAMP,
    finished_at             DATETIME     DEFAULT NULL,
    INDEX idx_bizdate (bizdate),
    INDEX idx_started (started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
