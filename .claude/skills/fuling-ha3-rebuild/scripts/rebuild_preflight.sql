-- ============================================================
-- Fuling HA3 rebuild — PRE-FLIGHT INSPECTION (READ-ONLY)
-- Target: RDS database `fuling_knowledge`
-- Run in DMS, or via a DataWorks SQL/PyODPS node (in-VPC).
-- Nothing here mutates data. Review output before Phase 2 (seeding).
-- ============================================================

-- [A] Corpus size: active docs vs Stage-1 LIMIT 100 → number of Stage-1 cycles needed.
SELECT COUNT(DISTINCT doc_id) AS active_docs
FROM document_version WHERE status = 'active';

-- [B] Pipeline-state distribution across active versions.
SELECT content_process_status, index_status, COUNT(*) AS n
FROM document_version WHERE status = 'active'
GROUP BY content_process_status, index_status
ORDER BY n DESC;

-- [C] Latest version per doc (the basis for the +1 version bump).
SELECT doc_id, MAX(version_no) AS cur_ver, COUNT(*) AS versions
FROM document_version WHERE status = 'active'
GROUP BY doc_id ORDER BY doc_id;

-- [D] STALE LOCKS from any interrupted run (would block re-runs). Expect 0 rows.
SELECT doc_id, version_no, content_process_status, index_status, updated_at
FROM document_version
WHERE status = 'active'
  AND (content_process_status IN ('PROCESSING','LOADING') OR index_status = 'PROCESSING')
ORDER BY updated_at;

-- [E] Active chunks currently serving (total + per doc/version).
SELECT COUNT(*) AS active_chunks_total FROM chunk_meta WHERE is_active = 1;
SELECT doc_id, version_no, COUNT(*) AS chunks
FROM chunk_meta WHERE is_active = 1
GROUP BY doc_id, version_no ORDER BY doc_id, version_no;

-- [F] Leftover bulk jobs (cosmetic, not blocking).
SELECT job_id, index_name, status, total_chunks, created_at
FROM opensearch_bulk_job WHERE status = 'PENDING' ORDER BY created_at;

-- [G] Quarantine versions — EXCLUDED from the rebuild per instruction (count only).
SELECT COUNT(*) AS quarantine_versions
FROM document_version
WHERE status = 'active' AND raw_key LIKE '%/\_quarantine/%';

-- [H] Top versions not yet SUCCESS — if any, a rebuild may already be seeded/in-flight.
SELECT dv.doc_id, dv.version_no, dv.content_process_status, dv.index_status
FROM document_version dv
JOIN (SELECT doc_id, MAX(version_no) mv FROM document_version WHERE status='active' GROUP BY doc_id) m
  ON dv.doc_id = m.doc_id AND dv.version_no = m.mv
WHERE dv.status = 'active' AND dv.index_status <> 'SUCCESS'
ORDER BY dv.doc_id;
