-- 005_cross_doc_dedup_index.sql
-- Index on document_version.canonical_sha256 — HARD PRECONDITION for enabling RAG_DEDUP_CROSS_DOC.
-- The cross-document content-dedup check (node_build_canonical) does a canonical_sha256 lookup on
-- the Stage-1 hot path; without this index every canonical build is a full table scan.
--
-- Additive / non-destructive. ⚠️ NOT YET APPLIED — gated prod DDL. Apply via the
-- information_schema-guarded scratch/apply_migration_005.py (RW token), same discipline as 003/004.
-- MySQL 8.0 has no CREATE INDEX IF NOT EXISTS; the apply script guards on information_schema.STATISTICS.

CREATE INDEX idx_canonical_sha256 ON document_version (canonical_sha256);
