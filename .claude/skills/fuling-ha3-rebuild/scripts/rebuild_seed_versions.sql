-- ============================================================
-- Fuling HA3 rebuild — SEED NEW VERSIONS  (MUTATING — review first)
-- Strategy: in-place VERSIONED SWAP, zero downtime.
--   Insert version_no+1 for each currently-live doc; Stage 1->3 then
--   re-extract/chunk/index the new version; Stage 3 deactivates the old
--   version's chunks AFTER the new ones are indexed. The old version keeps
--   serving until the swap completes.
-- Run in DMS / a DataWorks SQL node (in-VPC) AFTER reviewing rebuild_preflight.sql.
-- Excludes _quarantine. Optionally scope to one department via the LIKE filter.
--
-- IMPORTANT: do NOT delete old chunk_meta rows and do NOT wipe HA3 here.
--            node_deactivate_old_chunks needs the old rows to find old HA3 ids.
-- ============================================================

-- ── OPTIONAL: clear stale locks (ONLY if rebuild_preflight.sql [D] returned rows) ──
-- UPDATE document_version
-- SET content_process_status = CASE WHEN content_process_status IN ('PROCESSING','LOADING')
--                                   THEN 'NOT_STARTED' ELSE content_process_status END,
--     index_status = CASE WHEN index_status = 'PROCESSING' THEN 'FAILED' ELSE index_status END
-- WHERE status = 'active'
--   AND updated_at < NOW() - INTERVAL 2 HOUR
--   AND (content_process_status IN ('PROCESSING','LOADING') OR index_status = 'PROCESSING');

-- ── STEP 1 — PREVIEW (read-only): exactly which docs/versions will be seeded. ──
SELECT dv.doc_id, dv.version_no AS cur_ver, dv.version_no + 1 AS new_ver,
       dv.file_ext, dv.raw_key
FROM document_version dv
JOIN (SELECT doc_id, MAX(version_no) mv FROM document_version WHERE status='active' GROUP BY doc_id) m
  ON dv.doc_id = m.doc_id AND dv.version_no = m.mv
WHERE dv.status = 'active'
  AND dv.index_status = 'SUCCESS'                  -- only currently live & indexed docs
  AND dv.raw_key NOT LIKE '%/\_quarantine/%'       -- exclude quarantine
  -- AND dv.raw_key LIKE 'raw/sales/%'             -- OPTIONAL: scope to one dept
ORDER BY dv.doc_id;

-- ── STEP 2 — SEED (transaction). Review counts, then COMMIT or ROLLBACK. ──
START TRANSACTION;

INSERT INTO document_version
  (doc_id, version_no, bucket_name, raw_key, raw_key_hash, file_ext,
   gate_status, content_process_status, chunk_status, index_status, status)
SELECT dv.doc_id, dv.version_no + 1, dv.bucket_name, dv.raw_key, dv.raw_key_hash, dv.file_ext,
       'pending_clean', 'NOT_STARTED', 'NOT_STARTED', 'NOT_INDEXED', 'active'
FROM document_version dv
JOIN (SELECT doc_id, MAX(version_no) mv FROM document_version WHERE status='active' GROUP BY doc_id) m
  ON dv.doc_id = m.doc_id AND dv.version_no = m.mv
WHERE dv.status = 'active'
  AND dv.index_status = 'SUCCESS'
  AND dv.raw_key NOT LIKE '%/\_quarantine/%'
  -- AND dv.raw_key LIKE 'raw/sales/%'             -- OPTIONAL dept scope (must match STEP 1)
;
SELECT ROW_COUNT() AS versions_seeded;

-- Bump document_meta to the new latest version.
UPDATE document_meta dm
JOIN (SELECT doc_id, MAX(version_no) mv FROM document_version WHERE status='active' GROUP BY doc_id) m
  ON dm.doc_id = m.doc_id
SET dm.current_version_no = m.mv;

-- Confirm Stage 1 will pick these up (NOT_STARTED + canonical NULL).
SELECT COUNT(*) AS new_rows_ready_for_stage1
FROM document_version
WHERE status='active' AND content_process_status='NOT_STARTED' AND canonical_json_key IS NULL;

-- COMMIT;     -- run if versions_seeded / new_rows_ready_for_stage1 look correct
-- ROLLBACK;   -- run to undo (nothing committed)
