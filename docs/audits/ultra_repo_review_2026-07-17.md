# Ultra Repo Review — full-repository code review
**Date:** 2026-07-17 · **Branch:** claude/ontology-p0 · **Scope:** `/code-review ultra, all repo`

## How this was run
Orchestrated as a multi-agent review: the repo (~114k LOC Python + ~20k LOC Vue/TS) was split into 31 scoped
finders (subsystem × dimension), each briefed on the project's known/intentional decisions (default-off flags,
prod-write guards, in-memory sessions, HA3 no-2PC, the tracked plaintext IP endpoint, PII quarantine). Every
P0–P2 finding was to be adversarially verified. **The run hit the account session limit after 21 of 88 agents**,
so 10 subsystems never ran and the automated verification pass did not complete. The findings below were then
**re-verified by hand** (reading the cited code directly) for the P0 and the highest-impact P1s.

## Coverage & verification status
**Reviewed (finders completed):** api/serving · routes (kb_console, agent, ontology, kb_access, contribution,
console) · auth/identity · retrieval · generation · dingtalk · agent-runtime (run_store, executor, tools/policy,
approvals, outbox/worker) · pipeline_nodes (both lenses) · chunker · extraction (text + image) · reconcile/lease ·
db/clients · config/guards.

**Reviewed in the continuation run (2026-07-17, run 2 — Opus 4.8; see "## Continuation" below):** `ontology/` package
internals · ops-misc (qa_logger, retention, audit_log, spot_checker, admin_notify, kb_upload, access_grants,
dept_ancestry) · dataworks-dag (dag_engine, dag_definitions, dataworks_nodes/) · frontend-core · frontend-manage ·
frontend-views · miniapp · schema-migrations · supply-chain · test-safety-gates. **Coverage is now complete — every
subsystem has been finder-reviewed, and run-1's five ⏳ P1s were adversarially re-verified** (verdicts inlined at each
P1 above and summarized in the Continuation).

**Verification legend:** ✅ CONFIRMED = re-verified by hand this session against the cited code. ⏳ = finder-reported,
well-evidenced, but not independently re-verified here — treat as high-confidence-pending-confirmation.

---

## Summary
- **1 P0 (confirmed):** a `dept_admin` can publish a **company-wide PUBLIC** KB document with `approval_status=APPROVED`
  and **no kb_admin approval**, by putting a trailing slash in `category_dept` — the sanitizer validates the cleaned
  value but the raw value is baked into the object key, shifting the permission path segment so the pipeline reads it
  back as `public`. Live in production, no feature flag. This is the headline issue.
- **13 unique P1s** (8 confirmed by hand). Highest-impact: a crash on the general-answer path under the multi-instance
  Redis limiter; a hardcoded DingTalk card-callback secret that neuters signature enforcement; raw question/answer PII
  printed to logs on every card send; an OSS write method (`copy_object`) missing from the prod-write guard; a
  missing-CAS ledger write that clobbers the `PENDING_DELETE` handshake and re-pushes restricted docs with stale ACL.
- **27 P2s, 30 P3s** — robustness, concurrency-under-rollout-flags, and maintainability.

Most P1/P2 concurrency and data-loss findings sit behind **default-off rollout flags** (durable worker, ingest lease,
general-ability mode, VLM rebuild) — they are latent today but are on the stated activation path, so they should be
fixed *before* those flags flip. The P0, the DingTalk log leak, the chunker drop, and the OSS-guard gap are **not**
flag-gated.

---

### P0 — Critical

#### [P0] dept_admin publishes company-wide PUBLIC KB doc, bypassing kb_admin approval, via slash-injected category_dept on contribution accept
- **Location:** `opensearch_pipeline/routes/contribution.py:851` · **Category:** security
- **Verification (manual, this session):** ✅ **CONFIRMED** — Reproduced empirically: build_raw_key("marketing/",...,perm="dept_internal") -> "raw/marketing//internal/..." and perm_from_raw_key -> "public". authorize_upload sanitizes the slash so requires_kb_admin_approval=False; _materialize_contribution then writes permission_level=public, approval_status=APPROVED, content_process_status=NOT_STARTED. Live (contribution feature in prod, no flag).
- **Description:** In kb_contribution_accept, final_dept is only .strip()ed (line 827). authorize_upload validates a sanitized COPY (regex strips '/'), so category_dept='marketing/' passes as 'marketing' with level=dept_internal and requires NO kb_admin approval. But the RAW 'marketing/' is fed to build_raw_key, shifting the permission path segment; _finish_contribution_ingestion then derives permission via perm_from_raw_key(raw_key) which returns 'public'. The doc is materialized permission_level='public', approval_status='APPROVED', content_process_status='NOT_STARTED' and ingested company-wide, defeating the P2-16 gate that requires kb_admin approval for public. (Same unsanitized value also lets a malformed owner_dept e.g. 'marketing!' silently make a dept_internal doc unretrievable.)
- **Evidence:** contribution.py:827 final_dept=(req.category_dept or cur_dept or '').strip() (no sanitize); :843 authorize_upload(kb, final_dept, ...) validates a _SANITIZE_RE-stripped copy; :851-852 build_raw_key(final_dept,...); :428 permission_level=kb_upload.perm_from_raw_key(raw_key). Reproduced: build_raw_key('marketing/',...perm='dept_internal') -> 'raw/marketing//internal/...' and perm_from_raw_key -> 'public'.
- **Suggested fix:** Sanitize final_dept with kb_authz.sanitize_owner_depts/authorize_upload's canonical output and persist that canonical value (used for both build_raw_key and category_dept), never the raw request string.

### P1 — High

#### [P1] B6 reconcile replays stale approval decisions from earlier suspend cycles onto new pending calls
- **Location:** `opensearch_pipeline/agent_runtime/approval_store.py:488` · **Category:** security
- **Verification (continuation run 2, Opus 4.8):** ✅ **CONFIRMED — P1** (both adversarial lenses). Traced end-to-end: `list_decided_unresumed` has no latest-request guard; an approved request never leaves status `approved` (no consumed/resumed transition), so it permanently satisfies the scan; the reaper redrives it unconditionally; `_verify_persisted_decision` matches only decision-direction + `final_args_digest` against the CURRENT pending call, never the request's `call_id`. See "Continuation → Carried-P1 verdicts".
- **Description:** list_decided_unresumed selects any decided approval_request whose run is currently suspended, with no check that it is the run's latest request. A multi-approval run (approve request 1, resume, suspend again on request 2) permanently matches the scan, since decided requests never change status. The reaper's _reconcile_decided then redrives the OLD decision: if the new pending call has the same tool+args digest, _verify_persisted_decision passes and the call executes with no human decision on request 2 — approve-once, auto-approve identical calls forever. With differing args it causes perpetual per-cycle claim churn racing the real approver. Behind agent/ontology-tool flags that are intended to be enabled; B6 itself has no flag.
- **Evidence:** approval_store.py:483-490 joins ar.status IN ('approved',...) with r.status='suspended' only; routes/agent.py:1551-1558 redrives after checking only run status=='suspended'; executor.py:313-321 verifies just decision direction and final_args_digest against the CURRENT pending call, never comparing the request's call_id to the checkpoint's pending call_id.
- **Suggested fix:** Restrict the scan to the run's most recent approval_request (exclude runs with a newer pending request), and have _verify_persisted_decision also match the approval_request.call_id against the checkpoint's pending call_id.

#### [P1] Recovery scanner can claim a freshly-enqueued command and race the fast path into double execution
- **Location:** `opensearch_pipeline/agent_runtime/dispatch_outbox.py:134` · **Category:** concurrency
- **Verification (continuation run 2, Opus 4.8):** ✅ **CONFIRMED, severity ↓ P2** (both lenses). The race is real and unmitigated — no `message_id` uniqueness anywhere (`qa_logger.py:36` says so explicitly), so the re-drive after ThreadBusy yields a second run + duplicate SUCCESS row + double LLM cost. Both verifiers re-rated **P2**: the window is a sub-ms race against a 60s scanner tick (low probability), bounded/self-healing (attempts cap), no data corruption, and behind default-off `RAG_AGENT_DURABLE_DISPATCH`. Fix before that flag ships. See "Continuation → Carried-P1 verdicts".
- **Description:** claim_next selects any status='queued' row with no minimum-age/grace filter, so a scanner tick (every web replica + the Stage D worker, 60s interval) can claim a command in the ms window between the fast path's enqueue commit and its claim_specific. The fast path then logs the lost claim and direct-dispatches anyway (routes/agent.py:824-831), while the scanner's recover_fn gets ThreadBusy → DispatchRetryLater, and after the first run finishes re-submits the same question: two runs, duplicate answer in session history under the same message_id, double LLM cost. Behind RAG_AGENT_DURABLE_DISPATCH (default off) but staging-validated and intended for prod.
- **Evidence:** dispatch_outbox.py:134 `WHERE (status='queued' OR (status='claimed' AND lease_expires_at < NOW(3)))` — no age filter. routes/agent.py:827-831: on claim_for_request False, logs "他方抢先？——直通执行" and still runs executor.submit. routes/agent.py:1475-1476: recover's ThreadBusy → DispatchRetryLater keeps the claim for later re-drive.
- **Suggested fix:** Have claim_next only pick queued rows older than a grace age (e.g. created_at < NOW() - lease_s), and/or make the fast path not direct-dispatch after losing the claim.

#### [P1] Unbounded client session_id silently drops qa_session_log audit rows (caller-controlled audit evasion)
- **Location:** `opensearch_pipeline/api.py:290` · **Category:** security
- **Verification (continuation run 2, Opus 4.8):** ✅ **CONFIRMED — P1** (both lenses). `AskRequest.session_id` has no `max_length` (siblings cap at 128); a non-`miniapp:`-prefixed id bypasses the only length gate (`api.py:791`), is returned verbatim by `session_store.py:250`, and INSERTs untruncated into `qa_session_log.session_id VARCHAR(128)`. Under MySQL 8 strict mode (RDS default) a >128-char value raises errno 1406, which the `qa_logger` fallback ladder (only handles 1054/1146) swallows — answer served, audit row silently dropped. Even without strict mode it silently truncates. See "Continuation → Carried-P1 verdicts".
- **Description:** AskRequest.session_id has no max_length, unlike conversation_id (line 291) and SessionClearRequest.session_id (line 1716), both capped at 128. The value flows untruncated into log_qa_session and INSERTs into qa_session_log.session_id VARCHAR(128). Under MySQL strict mode (RDS default; db.py sets no sql_mode) a >128-char value fails the INSERT with 1406, and log_qa_session swallows all exceptions — the answer is still served but the audit row is silently lost. Any caller can thus exempt their own queries from the audit trail used for 溯源, feedback ownership, gap mining, and abuse analysis; feedback on those messages also breaks. Arbitrarily long IDs are additionally stored as in-memory session-store keys (count-capped at 500 but size-uncapped).
- **Evidence:** api.py:290 session_id Field has no max_length; qa_logger.py:407 passes session_id untruncated into base_vals; schema/001_opensearch_pipeline.sql:339 session_id VARCHAR(128); qa_logger.py:351 docstring: all exceptions caught, never raised; the 1054 fallback (qa_logger.py:466-477) does not cover 1406.
- **Suggested fix:** Add max_length=128 to AskRequest.session_id (matching SessionClearRequest) and defensively truncate session_id/user-controlled strings in build_qa_log_kwargs before INSERT.

#### [P1] Oversized step_card body is never split and gets dropped wholesale by validation
- **Location:** `opensearch_pipeline/chunker.py:1197` · **Category:** correctness
- **Verification (manual, this session):** ✅ **CONFIRMED** — Split guard requires len(parts)>1 (text-only step never split) and core_parts=[parts[0]] always keeps full step_text; a page-absorbing step exceeds the 2000-token validator cap and node_validate_chunks drops it. Live (step routing, no flag). Trigger = pathological single step.
- **Description:** The step_card length guard only triggers when there are supplemental parts (annotation/captions/OCR): a text-only step is never split, and even in the split branch core_parts always keeps the full step_text. A step group that swallows many pages (the documented failure mode in the line-855 comment: content absorbed into the previous step when the next anchor is missing) easily exceeds the 2000-token validator cap, and node_validate_chunks drops the whole chunk — that step's content vanishes from the index. No flag gates this; step routing is live in production.
- **Evidence:** chunker.py:1197 `if len(final_chunk_text) > self.max_chunk_chars and len(parts) > 1:` (no split when parts==1); :1201 `core_parts = [parts[0]]` keeps full step_text; pipeline_nodes.py:4761 `if chunk.token_count > 2000: issues.append("too_many_tokens")` then the chunk goes to invalid, not valid.
- **Suggested fix:** Run step_text itself through _split_long_text (emitting is_step_continuation cards) whenever the assembled card would exceed max_chunk_chars, mirroring the existing 1800-token parent budget.

#### [P1] Pooled connections never enter SteadyDB transaction mode — deadlock mid-transaction causes silent partial commit
- **Location:** `opensearch_pipeline/db.py:207` · **Category:** correctness
- **Verification (manual, this session):** ✅ **CONFIRMED (mechanism)** — Pool autocommit=False, _get_db_conn returns GuardedDBConnection without begin(). DBUtils 3.1.2 tough_method re-executes a single failed statement when _transaction is False; begin() docstring: "during a transaction ... all errors will be raised". Older pipeline_nodes multi-statement ledger writes lack begin() -> deadlock mid-txn can persist only the tail. Trigger (deadlock at wrong stmt) is rare.
- **Description:** The pool is built with autocommit=False but _get_db_conn never calls begin(), so DBUtils SteadyDB treats every statement as individually replayable. On OperationalError/InternalError (pymysql maps deadlock 1213 to OperationalError; 1205 falls to InternalError — both in SteadyDB's default failure classes) the tough cursor silently re-executes only the failing statement, possibly on a fresh connection, while the server has already rolled back all earlier statements of the in-flight transaction. The caller's commit() then persists only the tail — a torn ledger write, e.g. document_meta updated without its document_version row.
- **Evidence:** db.py:207 autocommit=False; db.py:53 returns GuardedDBConnection with no begin(). Installed dbutils SteadyDBCursor._get_tough_method retries single statement when con._transaction is False. Multi-statement txn: pipeline_nodes.py:287-317 (loop of INSERT/UPDATE, one commit at 317). Only spot_checker.py:646/666/722 call conn.begin() in the package.
- **Suggested fix:** Call conn.begin() in _get_db_conn (or provide a mandatory transactional acquire helper for write paths) so SteadyDB raises the original error instead of replaying statements mid-transaction.

#### [P1] Hardcoded default apiSecret defeats card-callback signature verification
- **Location:** `opensearch_pipeline/dingtalk_bot.py:295` · **Category:** security
- **Verification (manual, this session):** ✅ **CONFIRMED** — Literal default "fuling_card_cb" at dingtalk_bot.py:295 (verify) and dingtalk_card.py:200 (register); repo was public so value is known. When sig enforcement is turned on with the env var unset, HMACs are forgeable.
- **Description:** The card-callback signature secret falls back to the literal "fuling_card_cb" when DINGTALK_CARD_CALLBACK_API_SECRET is unset, in both verification and the registration call that tells DingTalk what to sign with. The repo has been public, so this value is known. Once RAG_DINGTALK_CARD_SIG_REQUIRED=true is enabled (the stated Track-1 plan), an attacker can still mint valid HMACs (algorithm is timestamp+secret only), making the enforcement flag security theater; only the Track-2 message_id ownership check remains.
- **Evidence:** dingtalk_bot.py:295 `os.environ.get("DINGTALK_CARD_CALLBACK_API_SECRET", "fuling_card_cb")`; same default at dingtalk_card.py:200 in register_card_callback; sign computed from `f"{ts}\n{secret}"` at lines 311-313, so knowing the secret suffices to forge.
- **Suggested fix:** Require the env var (refuse to register/verify with the built-in default, or generate a random secret at deploy) and rotate the registered apiSecret.

#### [P1] Raw user question/answer printed unredacted to application logs on every card send
- **Location:** `opensearch_pipeline/dingtalk_card.py:416` · **Category:** security
- **Verification (manual, this session):** ✅ **CONFIRMED** — print("[CARD DEBUG] cardParamMap=...") at l.416 dumps card_param_map which contains "question"(l.505) and "answer"(l.506) to stdout on every card deliver; violates the project _q_for_log redaction rule. Leftover debug print.
- **Description:** _post_card_deliver prints the full cardParamMap (800 chars) to stdout on every interactive/streaming card delivery. cardParamMap contains the raw question and answer (send_interactive_card lines 503-506, create_streaming_card 620-622). This bypasses the project's own mandatory log-redaction rule — dingtalk_bot._q_for_log states all log points printing question text must pass PII redaction because questions can contain ID/phone numbers and SAE logs are long-retained. Fires in production config on every bot answer.
- **Evidence:** dingtalk_card.py:416 `print(f"[CARD DEBUG] cardParamMap={json.dumps(...)[:800]}")`; card_param_map includes "question": question and "answer" (503-506); redaction policy documented at dingtalk_bot.py:328-331.
- **Suggested fix:** Route the debug print through the same redact_query_text helper (as _body_for_log does) or drop the cardParamMap dump.

#### [P1] GuardedBucket write guard misses copy_object, which the real OSS archive path uses
- **Location:** `opensearch_pipeline/env_guard.py:258` · **Category:** security
- **Verification (manual, this session):** ✅ **CONFIRMED** — _WRITE_METHODS omits copy_object; __getattr__ passes it through unguarded; pipeline_nodes.py:7094 calls bucket.copy_object on the GuardedBucket. delete_object IS guarded so the archive move half-completes under PROD-RO/config-drift.
- **Description:** GuardedBucket only intercepts put_object/put_object_from_file/delete_object/batch_delete_objects/append_object; every other attribute passes through __getattr__ unguarded. oss2's copy_object writes a new object into the bucket, and the real (non-simulated) bulk-job archive path calls it on the guarded bucket. A RAG_READONLY=true PROD-RO session or a dev config pointed at the prod bucket can therefore write to production OSS without any ack, bypassing the prod-write defense this class exists to enforce (the adjacent delete_object is guarded, so the operation also half-completes).
- **Evidence:** env_guard.py:258 _WRITE_METHODS tuple lacks copy_object (and multipart methods). pipeline_nodes.py:7094 calls bucket.copy_object(config.oss.bucket_name, source_path, target_key) then bucket.delete_object(...) on the bucket returned by clients.py:203 return GuardedBucket(bucket, ...).
- **Suggested fix:** Add copy_object (and multipart upload methods) to _WRITE_METHODS, or invert to a read-method allowlist so unknown methods are guarded by default.

#### [P1] Transient run/daily budget exhaustion terminally quarantines healthy documents
- **Location:** `opensearch_pipeline/extraction/cost_breaker.py:463` · **Category:** correctness
- **Verification (manual, this session):** ✅ **CONFIRMED** — quarantine_for_cost fires on ANY deny reason at l.463, not distinguishing transient run/daily-budget trips from doc-intrinsic caps; healthy docs get terminally quarantined once shared budget exhausts. Behind rebuild.enabled (default off, rollout target).
- **Description:** gate_vlm_rebuild with quarantine_on_deny=True quarantines on ANY deny reason, without distinguishing doc-intrinsic denials (per-doc cap/budget) from shared-state denials (run budget tripped, daily ledger cap). Once run_budget_rmb (default 200) is exhausted mid-backfill, every subsequent PDF with even one <30-char page (blank/cover pages escalate too) is set publish_status=QUARANTINED, content_process_status=FAILED, retry_count=3 (terminal, never re-claimed) and kb_type flipped to 'private'. Healthy docs are permanently removed from ingestion for a condition that clears next run. Behind rebuild.enabled (default off) but the flag is the Increment-1 rollout target.
- **Evidence:** try_reserve deny reasons include daily cap (lines 214-219) and run budget (221-241); line 463 quarantine_for_cost runs on any deny; quarantine sets retry_count=3 (line 389, comment line 362: prevents DAG-1 re-claim) and kb_type='private' (line 396); vlm_rebuilder.py:210 also sets cost_quarantined so downstream skips the doc.
- **Suggested fix:** Only quarantine when the deny reason is doc-intrinsic (gate 1/2/2b); on run/daily budget denial leave content_process_status re-claimable so the doc retries next run.

#### [P1] takeover_where_sql trusts residual lease columns — flag-on sweeper can steal a live flag-off holder's lock in minutes
- **Location:** `opensearch_pipeline/ingest_lease.py:107` · **Category:** concurrency
- **Verification (continuation run 2, Opus 4.8):** ⚠️ **MECHANISM CONFIRMED, net-new-P1 REFUTED → treat as P3 prerequisite.** The accuracy lens CONFIRMED the code exactly as described (lease-death arm fires on `lease_expires_at < NOW(3)` alone; flag-off claims never stamp/clear lease columns; a laundered residual lease is stealable by a flag-on sweeper). The reachability lens **REFUTED it as a live P1 and re-rated P3**: it only bites at **concurrency > 1 / overlapping runs**, which is not the current single-instance deployment, and `docs/ingest_lease_fencing_scope_2026-07-17.md` §3.5 already documents this exact residual window as an *accepted, tracked prerequisite* to enabling `workers>1` (kill switch = revert to status quo). Net: not a new bug — a known gate on the workers>1 rollout. See "Continuation → Carried-P1 verdicts".
- **Description:** The lease-death arm fires on lease_expires_at < NOW(3) alone, with no progress check. Flag-off claims/terminal writes never stamp or clear lease columns (claim_set_sql/clear_set_sql return ""), so a row carrying an expired lease from a crashed flag-on run, later legitimately claimed by a flag-off run (stage-2 claim accepts LOADING rows with no age/lease predicate), is instantly judged dead by any flag-on sweeper/takeover. The live flag-off holder — which has no fencing — keeps writing unfenced while another run re-claims: duplicate concurrent ingestion and torn chunk_meta writes, worse than the legacy 2h floor. Mixed fleet is the current deployment (DataWorks stage-2/3 nodes flag-on, laptop/other runs off), so this is reachable now.
- **Evidence:** ingest_lease.py:107 predicate has no updated_at guard on the lease arm; :74-75/:89-91 return empty fragments when flag off; sweeper applies predicate at dataworks_orchestrator.py:631-633; pipeline_nodes.py:1564 claims LOADING rows without any lease/age check. Docstring line 99 claims '新旧混跑无缝'.
- **Suggested fix:** Only treat an expired lease as death when updated_at has not advanced past lease_expires_at (fall back to the 2h age arm otherwise), or make claims/terminal writes clear-and-restamp lease columns unconditionally regardless of the flag.

#### [P1] Deactivate failure path clobbers console PENDING_DELETE handshake (missing CAS)
- **Location:** `opensearch_pipeline/pipeline_nodes.py:6082` · **Category:** concurrency
- **Verification (manual, this session):** ✅ **CONFIRMED** — Deactivate-failure UPDATE sets index_status=FAILED with no AND index_status=PROCESSING CAS and no lease clear; sibling paths 6208/7340 carry that CAS with comments naming the PENDING_DELETE-clobber -> stale-ACL-repush hazard; FAILED is stage-3 claimable. Two finders converged.
- **Description:** When the HA3 old-chunk delete fails, node_deactivate_old_chunks unconditionally sets document_version.index_status='FAILED' for each current (doc_id, version_no) with no state guard. The success path (line 6208) and node_update_index_status (line 7340) both CAS on index_status='PROCESSING' precisely so a console-issued PENDING_DELETE (retire / visibility→restricted handshake) is never overwritten. This failure path destroys that token; since FAILED is stage-3 claimable, the next run re-pushes the restricted doc's chunks with stale ACL and the reconcile-based HA3 delete never fires.
- **Evidence:** Line 6082-6085: UPDATE document_version SET index_status='FAILED' WHERE doc_id=%s AND version_no=%s — no AND index_status='PROCESSING'. Contrast line 6212 and 7344 (CAS'd, with comments describing the exact PENDING_DELETE-overwrite hazard). reindex_states.py:81 lists FAILED as STAGE3_CLAIMABLE.
- **Suggested fix:** Add AND index_status='PROCESSING' to the failure-path document_version UPDATE (same CAS as the success path), leaving PENDING_DELETE rows untouched.

#### [P1] Crash between chunk_meta commit and status closure wedges whole stage-2 batches via the unfrozen-rechunk guard
- **Location:** `opensearch_pipeline/pipeline_nodes.py:5472` · **Category:** correctness
- **Verification (continuation run 2, Opus 4.8):** ✅ **CONFIRMED — P1** (both lenses), reachable at **default flags**. Chunk rows commit on one connection (`5293`); per-doc `content_process_status='DONE'` closure runs on a separate connection (`5468`+, commits at `5472-5482`). A crash between them leaves chunk rows present with the doc stuck PROCESSING; `_reset_stale_stage2_locks` (2h age arm, works flag-off) flips it to FAILED retry+1; the loader re-selects FAILED&retry<3 with no chunk-existence exemption and re-batches it; the unfrozen-rechunk guard (`1519-1531`, `_docs_with_existing_chunks` matches ANY chunk row) then `raise()`s for the WHOLE batch before the preempt — stranding healthy co-batched docs at LOADING until retry_count=3 → permanent FAILED. Not gated by `RAG_INGEST_LEASE_ENABLE`. See "Continuation → Carried-P1 verdicts".
- **Description:** node_write_chunk_meta commits chunk rows in one transaction (line 5293) and writes content_process_status='DONE' per doc in later separate transactions (5472-5482; a DB error there raises at 5494, skipping remaining docs). A crash/kill in this window leaves chunk rows present with the doc stuck PROCESSING; the orchestrator stale sweep resets it to FAILED-retry, and on re-claim node_classify_and_risk_assess's unfrozen-rechunk guard (1519-1531) raises for the WHOLE batch before claiming. Repeated runs strand co-batched healthy docs at LOADING, burning their retry budget to permanent FAILED — a spontaneous stage-2 outage requiring a same-day manual override token.
- **Evidence:** Line 5293 conn.commit() (chunk insert) vs 5472-5494 per-doc closure transactions; 1519-1531 raises when any target has chunk_meta rows and no frozen routing; dataworks_orchestrator.py:626-638 resets stale LOADING/PROCESSING to FAILED retry+1, and the loader (line 220) re-selects FAILED&retry<3 with no chunk-existence exemption.
- **Suggested fix:** Exempt crash-resume retries from the whole-batch block (e.g., auto-accept targets whose stored _chunk_set_hash matches the recomputed one, or per-doc skip instead of whole-batch raise), or fold the DONE closure into the chunk-insert transaction.

#### [P1] Deactivate-failure handler overwrites PENDING_DELETE handshake (missing PROCESSING CAS)
- **Location:** `opensearch_pipeline/pipeline_nodes.py:6082` · **Category:** correctness
- **Verification (manual, this session):** ✅ **CONFIRMED** — Deactivate-failure UPDATE sets index_status=FAILED with no AND index_status=PROCESSING CAS and no lease clear; sibling paths 6208/7340 carry that CAS with comments naming the PENDING_DELETE-clobber -> stale-ACL-repush hazard; FAILED is stage-3 claimable. Two finders converged.
- **Description:** When the HA3 old-version delete fails, the exception handler writes document_version.index_status='FAILED' unconditionally. Both sibling finalize paths use a CAS (only PROCESSING→terminal) precisely because the console can set PENDING_DELETE (visibility-restrict/retire delete handshake) mid-run; overwriting it to FAILED makes the version re-claimable (FAILED is in STAGE3_CLAIMABLE_INDEX_STATUS), so the next loader re-pushes the restricted document to HA3 with stale permission and the reconcile delete never runs. The handler also skips the lease clear/fence used elsewhere.
- **Evidence:** Lines 6082-6085: UPDATE document_version SET index_status='FAILED' WHERE doc_id=%s AND version_no=%s — no index_status='PROCESSING' predicate. Contrast CAS at 6208-6213 and 7340-7345, whose comments (6203-6207, 7335-7337) name this exact PENDING_DELETE-clobber → stale-permission re-push risk; reindex_states.py:81 confirms FAILED is claimable.
- **Suggested fix:** Add AND index_status='PROCESSING' (plus ingest_lease.clear_set_sql()) to the failure-path document_version UPDATE, matching the CAS used at 6208 and 7340.

#### [P1] RedisRateLimiter lacks admit_general — general-ability quota path raises AttributeError on redis backend
- **Location:** `opensearch_pipeline/rate_limiter.py:718` · **Category:** correctness
- **Verification (manual, this session):** ✅ **CONFIRMED** — RedisRateLimiter (l.718) defines admit_ask/admit_aux/charge_llm_call but NOT admit_general; api.py:877 & dingtalk_bot.py:752 call LIMITER.admit_general unguarded. redis backend + general-ability mode -> AttributeError -> 500.
- **Description:** ServingRateLimiter defines admit_general (line 460) for the T2/T3 general-answer daily quota, but RedisRateLimiter — the backend required for multi-instance deployment (RAG_RATE_LIMIT_BACKEND=redis; cloud Redis already provisioned as the workers>1 prerequisite) — never implements it. api.py:877 and dingtalk_bot.py:752 call LIMITER.admit_general unguarded (only the downstream LLM call is wrapped), so with redis backend plus RAG_GENERAL_ABILITY_MODE enabled — both flags on the active rollout path — every general-intent question raises AttributeError instead of a quota decision (HTTP 500 / bot handler error).
- **Evidence:** rate_limiter.py:733 docstring claims interface parity with ServingRateLimiter ('api.py 无感切换'), yet the class defines only admit_ask(779)/admit_aux(843)/charge_llm_call(863). admit_general exists only at line 460 (memory backend); api.py:877 calls it with no try/except.
- **Suggested fix:** Implement admit_general on RedisRateLimiter (INCR-with-check Lua on a rl:gen:<actor>:<day> key, TTL to Beijing midnight), mirroring the memory backend's semantics.

### P2 — Medium

#### [P2] Attempts-exhausted resume commands are unreachable by all three closers — permanent non-terminal zombies
- **Location:** `opensearch_pipeline/agent_runtime/dispatch_outbox.py:242` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** A resume command (run_id always set at enqueue) that burns 3 attempts on DispatchRetryLater (run stuck running after holder death, admission full, identity blips) is excluded from every convergence path: claim_next filters attempts < max, sweep_exhausted filters run_id IS NULL, list_bound_expired filters kind='submit'. It stays 'claimed' with an expired lease forever, is counted by backlog_count permanently (drifting readiness/alerting), and retention only deletes terminal rows — the outbox invariant that every command reaches a terminal state is violated.
- **Evidence:** dispatch_outbox.py:242 `WHERE run_id IS NULL AND attempts >= %s`; :136 `AND attempts < %s`; :266 `AND run_id IS NOT NULL AND kind='submit'`; :281 backlog_count counts any claimed-expired row. retention.py:27: agent_dispatch_command deletion is 终态-only. No other code touches the table.
- **Suggested fix:** Add a resume branch to the recovery scan that closes exhausted resume commands by run terminal state (done if run terminal, else failed with last_error).

#### [P2] bind_and_done swallows CAS failures silently — stolen lease mid-dispatch yields an invisible duplicate run
- **Location:** `opensearch_pipeline/agent_runtime/durable_dispatcher.py:73` · **Category:** concurrency
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** bind_run/complete are holder-CAS updates returning bool, but bind_and_done ignores both return values; its warning only fires on exceptions. If dispatch outlives the 120s lease (renew_lease exists at dispatch_outbox.py:161 but has zero production callers) and another scanner steals the claim, the original's bind silently no-ops, the run it already started keeps running, and the thief re-executes — duplicate answer with no log line anywhere. Same silence applies when sweep_exhausted marks a still-in-flight final attempt failed while its run actually succeeds.
- **Evidence:** durable_dispatcher.py:73-74: `self._outbox.bind_run(...)` and `complete(...)` results discarded; except at :75 only catches exceptions, not rowcount-0 CAS misses. dispatch_outbox.py:190 bind_run WHERE requires status='claimed' AND lease_holder=%s. Grep shows no caller of renew_lease outside tests.
- **Suggested fix:** Check bind_run's return value and log an ERROR (and skip the complete) when the CAS misses, and renew the lease around long recover_fn work.

#### [P2] Crash-reaped or relay-degraded runs never close their event stream; cross-replica SSE consumers hang up to 30 minutes
- **Location:** `opensearch_pipeline/agent_runtime/event_relay.py:132` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** stream_run_events exits only on __end__, a run_completed/run_failed frame, or the 1800s default deadline. In the crash scenarios the relay exists for, no terminal frame is ever published: (a) executor SIGKILL → reaper flips running→failed with a DB-only UPDATE, never touching Redis; (b) any mid-run publish error latches _dead=True and silently drops all later frames including the terminal frame and __end__. A reconnecting consumer on GET /runs/{id}/events replays partial frames then blocks on XREAD tailing for up to 30 minutes, holding the SSE connection, even though the route already read terminal durable status. Flag defaults off but this is the intended-on cross-instance replay path (PR-3 staging graduated it).
- **Evidence:** event_relay.py:144-148 — loop returns only on _END_TYPE or _TERMINAL_TYPES; line 130 default timeout 1800s. run_store.py:583-587 reaper failed-UPDATE is DB-only. event_relay.py:88-91 _dead=True stops all publishes. routes/agent.py:1858 computes terminal but only guards the missing-stream case (1859).
- **Suggested fix:** When the durable run status is terminal, bound tailing (replay-only or short grace timeout after last frame), or have the reaper/route append a closure frame to the stream.

#### [P2] Double _release() when pool.submit fails but work item was already enqueued
- **Location:** `opensearch_pipeline/agent_runtime/executor.py:231` · **Category:** concurrency
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** When ThreadPoolExecutor.submit raises after enqueuing the work item (thread-creation failure, the exact 'dispatch_maybe_scheduled' case the P0-02 fix acknowledges), submit()/resume() except-paths call self._release() while a warm worker still drives _drive_gen, whose finally also calls _release(). One _acquire gets two releases: _active undercounts permanently, eroding the max_concurrent admission cap and letting drain()'s _active<=0 wait exit while a run is still executing. The state-machine side was fixed but not slot accounting.
- **Evidence:** executor.py:215/341 set dispatch_maybe_scheduled (item may be enqueued, 'warm worker 仍会驱动'); executor.py:231 and :361 unconditionally self._release() in the except path; executor.py:617 _drive_gen finally releases again for the same admitted run.
- **Suggested fix:** When dispatch_maybe_scheduled is true, skip the except-path _release() (ownership including slot release passes to the possibly-running driver, with reaper as fallback).

#### [P2] Suspend-persist failure path calls failure callback without D3 ownership fencing
- **Location:** `opensearch_pipeline/agent_runtime/executor.py:531` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** Every other failure path gates _notify_failure on a successful running→failed CAS as proof of ownership (D3 fencing, lines 567-571, 588-592, 596-600, 950-953), explicitly to prevent re-INSERTing a qa_session_log row after purge/reap/cancel took the run. The suspend-failure path instead uses _safe_transition (result swallowed) and calls _notify_failure unconditionally — so when suspend_run_atomic returned ok=False precisely because ownership was lost, the failure-side durable write still fires, violating the invariant (including post-purge data reinsertion).
- **Evidence:** executor.py:528-531: after transition_checked(running→suspended) fails, _safe_transition(running→failed) ignores its result and self._notify_failure(handle, "挂起状态落库失败") runs unconditionally; contrast fenced pattern at executor.py:567-571.
- **Suggested fix:** Gate the callback: only call _notify_failure if _transition_checked(run_id, "running", "failed") returns True, matching the D3 pattern used everywhere else.

#### [P2] Empty state_digest bypasses checkpoint HMAC verification even with REQUIRE_HMAC on
- **Location:** `opensearch_pipeline/agent_runtime/executor.py:410` · **Category:** security
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _verify_checkpoint returns immediately when state_digest is falsy, before the RAG_AGENT_CHECKPOINT_REQUIRE_HMAC anti-downgrade check. The HMAC feature's stated threat model is an attacker who can write agent_checkpoint; such an attacker can NULL the digest column and tamper the blob, and resume proceeds with zero verification — even in the hardened REQUIRE_HMAC mode built specifically to block digest-downgrade attacks. Tampered checkpoints let the attacker inject messages/pending tool calls executed under the run owner's identity. Flag is intended for production enablement.
- **Evidence:** executor.py:409-411: `digest = getattr(cp, "state_digest", None); if not digest: return` short-circuits before the REQUIRE_HMAC downgrade rejection at executor.py:426-430, which only fires for non-empty bare-sha256 digests.
- **Suggested fix:** When a checkpoint key is present (or REQUIRE_HMAC is on), reject checkpoints with a missing/empty digest instead of skipping verification.

#### [P2] send_ops_alert treats HTTP 200 as success; DingTalk reports failures via errcode in a 200 body, so alerts are silently lost
- **Location:** `opensearch_pipeline/alerting.py:73` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** The DingTalk custom-robot API returns HTTP 200 with a JSON body {"errcode":N,"errmsg":...} for sign mismatch (310000), keyword filtering, and server-side rate limiting (130101). send_ops_alert only checks resp.status and never parses the body, so a misconfigured RAG_OPS_ALERT_SECRET or a burst exceeding DingTalk's 20-msg/min robot limit makes every ops alert (parity drift, SLO breach, cost-breaker trips — 15+ call sites) vanish while the function returns True and logs nothing. The whole alert channel can be dead with zero signal, defeating its purpose.
- **Evidence:** alerting.py:72-77: `with urllib.request.urlopen(req, ...) as resp: ok = 200 <= resp.status < 300` — response body never read; `return ok` after only an HTTP-status warning. Callers include reconcile.py:919 (RDS↔HA3 parity drift, severity=critical).
- **Suggested fix:** Parse the response JSON and treat errcode != 0 as failure with a logger.error including errcode/errmsg.

#### [P2] /api/ready is unauthenticated and un-rate-limited yet issues live RDS and HA3 queries per hit
- **Location:** `opensearch_pipeline/api.py:593` · **Category:** performance
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** readiness_check runs a real RDS SELECT 1 (pool checkout) and a live HA3 vector query on every request, plus a Redis ping when any redis backend is configured, with no _enforce_rate_limit call and no caching of these three probes (only the sub-checks in readiness.py are TTL-cached). On the public domain, an anonymous flood of /api/ready bypasses the strict anon cost controls applied to every other backend-touching endpoint, consuming DB pool connections and the 120-token AnyIO thread pool; under load this can starve real traffic and even flip the LB's own readiness probes to 503, withdrawing healthy instances.
- **Evidence:** api.py:592-593 @app.get("/api/ready") def readiness_check() — no _enforce_rate_limit anywhere in the body; RDS probe at api.py:620-622 (cur.execute("SELECT 1")); HA3 client.query at api.py:636-641; readiness.py caches only sub-checks (agent_tables 60s etc.), not these probes.
- **Suggested fix:** Cache the RDS/HA3/Redis probe results for a few seconds (single-flight) and/or apply the aux IP rate limit to /api/ready while whitelisting the LB source.

#### [P2] clause_chunk loses page_num and source provenance for all clause-routed documents
- **Location:** `opensearch_pipeline/chunker.py:1847` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _chunk_by_clause concatenates all paragraphs into full_text and creates every clause_chunk without page_num or source: page_num is always None (HA3 gets page_num=0 via to_ha3_doc line 263) and source defaults to "native" even for OCR text. The no-match fallback path (lines 1758-1766) has the same gap. All policy/regulation documents (制度/规定/规范 routing in node_chunk_documents) therefore lose page-level citation and OCR provenance, while the step path carefully preserves both and the same function already tracks per-image offsets (#F-clause-img) — block page info is available but discarded.
- **Evidence:** chunker.py:1847-1855 _create_chunk called with only section_title/metadata — no page_num, no source; same at 1833-1841 and 1758-1766. to_ha3_doc line 263: `"page_num": self.page_num or 0`.
- **Suggested fix:** Track (offset, page_num, source) per paragraph alongside full_len (as already done for images) and stamp each clause_chunk from the block covering its seg_start.

#### [P2] Environment label is open-set and inconsistently case-normalized across guard layers
- **Location:** `opensearch_pipeline/config.py:632` · **Category:** security
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _validate_environment_target_consistency lowercases the label but only matches known values; any unrecognized label (e.g. 'dev', 'prod', a typo) matches no branch and skips all label↔target cross-checks — a 'dev'-labeled box can read prod RDS with no read_only_ack. Separately, the production security guards use exact match (config.environment in ("production","staging")) and env_guard uses cfg.environment == "production", so 'Production' passes the lowercased cross-check as prod yet silently skips the PII-redact, self-approval, REQUIRE_AUTH/ACL_FAIL_CLOSED posture, and label-triggered vendor guards.
- **Evidence:** config.py:627 lowercases; branches at 632 ("development","local",""), 646 ("staging","test"), 682 ("production") cover only known labels — no else. config.py:1005 `config.environment in ("production", "staging")` is exact-match on the raw value; env_guard.py:191 `cfg.environment == "production"` likewise.
- **Suggested fix:** Normalize (strip+lower) config.environment once in load_config and fail-fast on any label outside the known set.

#### [P2] DAG-3 failure rollback resets PROCESSING→FAILED with no lease fence and without clearing lease columns
- **Location:** `opensearch_pipeline/dataworks_orchestrator.py:587` · **Category:** concurrency
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** The orchestrator's post-failure lock rollback bypasses the PR-4 lease protocol entirely: WHERE only checks index_status='PROCESSING'. If this run stalled past TTL and another holder took the doc over (epoch+1), the rollback clobbers the new holder's live PROCESSING lock, making the doc claimable by a third run mid-flight (wasted work, duplicate HA3 pushes; epoch fences bound but don't prevent the churn). Even in the benign case it leaves lease_holder/lease_expires_at set on a FAILED row, violating the '租约列非空 ⇔ 有人自认在跑' invariant (ingest_lease.py:87-88) and seeding the residual-lease landmine of the P1 finding. The in-node FAILED path (pipeline_nodes.py:7340-7345) is lease-aware; this backstop was missed.
- **Evidence:** dataworks_orchestrator.py:585-589: UPDATE document_version SET index_status='FAILED' WHERE doc_id=%s AND version_no=%s AND index_status='PROCESSING' — no fence_where_sql, no clear_set_sql, executed on a fresh connection after DAG failure.
- **Suggested fix:** Append the LeaseSet fence (holder+epoch) to the rollback WHERE and clear_set_sql to the SET, skipping keys already discarded as LeaseLost.

#### [P2] maxcached=5 vs maxconnections=20 causes per-request connection churn under serving concurrency, amplified by newly enabled RDS TLS
- **Location:** `opensearch_pipeline/db.py:195` · **Category:** performance
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** The pool ceiling was raised to 20 for serving/classify concurrency, but the idle cache stays at maxcached=5. dbutils PooledDB.cache() hard-closes any returned connection when 5 are already idle, so whenever concurrency oscillates above 5 each request beyond the cache pays a full TCP + MySQL auth handshake — and since P0-02 enabled RDS SSL, a TLS handshake too. The docstring's 'RDS 侧成本≈0' claim covers idle-connection count, not this reconnect churn on the serving hot path.
- **Evidence:** db.py:194-196 mincached=2, maxcached=5, maxconnections=_max_conn (default 20); db.py:137 claims 'maxcached=5 不变…RDS 侧成本≈0'; installed dbutils PooledDB.cache: 'else: con.close()' when idle cache full; db.py:208 pymysql_ssl_args wires TLS.
- **Suggested fix:** Raise maxcached to match maxconnections (or expose it alongside RAG_DB_POOL_MAX) so connections are reused rather than torn down under load.

#### [P2] Wildcard "*" passthrough in ACL normalizer grants ALL permission groups, bypassing the fail-closed whitelist
- **Location:** `opensearch_pipeline/dingtalk_identity.py:257` · **Category:** security
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _normalize_dept_to_codes is documented as the single fail-closed ACL boundary (unknown -> [], everything filtered by _VALID_ACL_GROUPS). But the passthrough branch sets mapped=[key], and the next line expands ANY input equal to "*" to sorted(_VALID_ACL_GROUPS) BEFORE the whitelist filter. It cannot distinguish the trusted ancestry sentinel from an arbitrary external department name. A DingTalk department literally named "*" (its name is cached raw into user_role.dept_code at line 417, unsanitized) — or a seed-script typo — silently grants every user in it read access to all dept_internal docs company-wide.
- **Evidence:** Line 256 `mapped = [key]` (passthrough of unknown dept name); line 257-258 `if "*" in mapped: mapped = sorted(_VALID_ACL_GROUPS)`. dept_name is written to user_role.dept_code unsanitized at line 417, and read back through this same function at line 352/509. sanitize_owner_depts would strip "*" but is not applied on this path.
- **Suggested fix:** Only expand "*" to all groups when it arrives via the trusted ancestry sentinel (a dedicated constant/flag), not for arbitrary passthrough department names, so external/DingTalk-sourced "*" is dropped by the whitelist like any other unknown value.

#### [P2] is_stream_active() reports thread liveness, not WSS connectivity — STREAM cards lose clicks during connect/reconnect windows
- **Location:** `opensearch_pipeline/dingtalk_stream_runner.py:140` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _running.set() executes at thread start, before client.start_forever() has established the WSS connection, and stays set during start_forever's internal 3s-backoff reconnect gaps. dingtalk_card._assemble_delivery_payload uses is_stream_active() to choose callbackType=STREAM, whose own comment warns that STREAM cards created while the client isn't actually connected silently lose button clicks. So every deploy/restart and every network blip creates a window where feedback/handoff clicks on newly delivered cards are dropped with no fallback.
- **Evidence:** dingtalk_stream_runner.py:140-143 `_running.set()` then `client.start_forever()`; is_stream_active() at 55-57 checks only the event and thread.is_alive(); consumed at dingtalk_card.py:384 with warning comment at 380-382.
- **Suggested fix:** Set/clear the active flag from the SDK's on-connect/on-disconnect callbacks (or verify an established session) instead of at thread start.

#### [P2] XLSX annotation enrichment assumes drawingN.xml maps to sheet N, silently losing annotation bindings
- **Location:** `opensearch_pipeline/extraction/image_extraction_utils.py:672` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _enrich_xlsx_annotations derives sheet_idx = drawing_num - 1, but OOXML drawing files are numbered by creation order among sheets that have drawings (the real mapping is via worksheet .rels). In a workbook whose first sheet(s) have no images (e.g., a cover/index sheet), sheet 2's drawing is drawing1.xml, so sheet_idx=0 while the assets carry page_num=2; _find_asset_by_md5 requires page_num == sheet_idx+1, so every annotation (①②③ numbers/labels) fails to bind — silently. Lost annotation_num degrades step-card image binding from the explicit 'annotation 显式绑定' primary path to weaker positional heuristics.
- **Evidence:** image_extraction_utils.py:668-672 computes sheet_idx from the drawing filename; lines 655-657 gate the MD5 match on `asset.page_num == sheet_idx + 1` (so the claimed MD5 fallback does not survive the misalignment); image_relation_classifier.py:107-108 shows has_annotation drives the confidence-1.0 primary binding.
- **Suggested fix:** Resolve the sheet-to-drawing mapping via xl/worksheets/_rels/sheetN.xml.rels (drawing relationship target) instead of the drawing filename number.

#### [P2] Partial OCR page failures reported as DONE with no signal, silently losing page content
- **Location:** `opensearch_pipeline/extraction/ocr_client.py:298` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _real_pdf_ocr aggregates to FAILED only when ALL pages fail; any partial failure (e.g., a mid-run 429 burst exhausting post_json_with_retry on some pages of a scanned doc) yields status DONE with those pages' text silently absent. No warning, page list, or failure count is recorded anywhere — failed pages produce text="" which to_blocks() drops, and downstream only reacts to ocr_status=='FAILED'. The doc completes and is never re-picked, so the missing pages are permanently absent from the index while the ledger says extraction succeeded.
- **Evidence:** ocr_client.py:298 agg_status FAILED only if all(p.status=='FAILED'); lines 397-403 failed pages get text=""; unified_extractor.py:2362-2363 stores only the aggregate status; pipeline_nodes.py:5405 checks only _ocr_status=='FAILED'.
- **Suggested fix:** Surface per-page failures (e.g., append a warning with failed page numbers and/or an ocr_failed_pages count) so partial results are auditable and can gate DONE.

#### [P2] Unparseable 200 OCR response returns "" and is cached permanently as the page's text
- **Location:** `opensearch_pipeline/extraction/ocr_client.py:477` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _call_ocr_api treats a 200 response whose body fails extract_vlm_text parsing (KeyError/IndexError — e.g., DashScope error/refusal payload shapes without choices) as a successful empty OCR: it returns "" instead of raising. _ocr_one then marks the page DONE and writes "" into the persistent SQLite page cache keyed by model+rendered-bytes (line 389). All future re-ingests of that document serve the empty text from cache with zero API calls, so a one-time response-shape anomaly becomes a permanent, silent loss of that page with no retry path short of deleting the cache.
- **Evidence:** ocr_client.py:475-477 `except (KeyError, IndexError): return ""` after a 200; lines 387-391 cache page_text unconditionally, including ""; cache hit path (lines 377-382) returns DONE without calling the API.
- **Suggested fix:** Raise (page FAILED, uncached) when a 200 body lacks the expected content path, and skip caching empty OCR results.

#### [P2] Budget-capped/cost-denied images vanish from assets with no NEEDS_REVIEW signal
- **Location:** `opensearch_pipeline/extraction/unified_extractor.py:1945` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** When RAG_FUNNEL_MAX_IMAGES caps need_vlm_hashes (line 1855) or the cost breaker DENY empties it (line 1876), the skipped hashes never enter hash_to_result, so Phase 3 drops those images entirely — no asset record, no ocr_text block. Unlike funnel exceptions and degraded results, these skips are not counted in vlm_degraded_count (line 2036), so the document finalizes DONE instead of NEEDS_REVIEW. After raising the budget, nothing marks these docs for rescan — images are permanently absent with only a stdout log. Both flags default off but are the intended cost-control mechanism.
- **Evidence:** Line 1945: `if file_hash not in hash_to_result: continue` silently drops capped/denied hashes; line 2036 counts only `degraded_asset_count + funnel_exception_count`; pipeline_nodes.py:5519 gates NEEDS_REVIEW solely on vlm_degraded_count.
- **Suggested fix:** Count cap/DENY-skipped unique images into vlm_degraded_count (or a dedicated skipped counter that also drives NEEDS_REVIEW) so budget-affected docs self-heal on rescan.

#### [P2] XLSX/PPTX blanket except finalizes partially-extracted documents as complete
- **Location:** `opensearch_pipeline/extraction/unified_extractor.py:1343` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _extract_xlsx wraps the entire multi-sheet loop in one try; an exception on sheet N (corrupt drawing, bad cell) keeps blocks from sheets 1..N-1 and appends a warning, and _extract_pptx (line 1621) does the same mid-deck. The 0-chunk suspected-failure guard (pipeline_nodes.py:5407-5416) only inspects warnings when zero chunks were produced, so any partial output finalizes chunk_status/content_process_status as DONE — the remaining sheets/slides silently never reach the index, violating the 'suspected failure must be queryable' intent that guard was built for.
- **Evidence:** Line 1343-1344: `except Exception as e: warnings.append(f"Failed to extract Excel file: {e}")` after partial block accumulation; pipeline_nodes.py:5383 comment scopes the failure-warning check to the 0-chunk path only.
- **Suggested fix:** On mid-extraction exceptions, set a partial-extraction flag on ExtractionResult (or reuse the warn-scan outside the 0-chunk branch) so partially extracted docs land NEEDS_REVIEW instead of DONE.

#### [P2] refund() after real billed VLM calls lets actual spend escape run/daily budget caps
- **Location:** `opensearch_pipeline/extraction/vlm_rebuilder.py:472` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** maybe_refine_tables makes one billed VLM call per page (line 434-435), then if every table fails the fidelity/correspondence gates (refined==0) it refunds the full reservation. maybe_rebuild_pdf similarly refunds when all pages return empty blocks (line 262) — those 200-responses are still billed. refund() also un-trips the breaker and debits the shared daily ledger, so run_total_rmb/daily ledger undercount real spend. A batch of mangled-table or image-only PDFs can each burn up to doc_budget in billed calls and refund it, making run_budget_rmb/daily_budget_rmb not bound actual DashScope spend — defeating the breaker's stated purpose.
- **Evidence:** vlm_rebuilder.py:429-435 renders+calls VLM per page before line 471-472 refunds when refined==0; line 260-263 refunds when added==0; cost_breaker.py:269-270 un-trips _run_tripped on refund and line 272 subtracts from the shared daily ledger.
- **Suggested fix:** Only refund pages where no billable API call occurred (e.g., render failure or transport exception), tracking billed-call count per doc instead of refunding the whole estimate.

#### [P2] _vlm_reconstruct_page has no retry: single transient 429/5xx silently drops a page's rebuilt content
- **Location:** `opensearch_pipeline/extraction/vlm_rebuilder.py:149` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** The rebuild/refine paths call the VLM with a bare requests.post — unlike ocr_client, which routes through post_json_with_retry precisely because this DashScope account has documented 429 fragility. The per-page loop fires calls back-to-back, so a transient 429/timeout on one page returns [] and that page is silently omitted while the doc completes with extract_method '+vlm_rebuild' looking successful; in the refine path a mangled native table is silently kept. The reserved cost for the failed page is also not refunded unless every page fails.
- **Evidence:** vlm_rebuilder.py:149-150 bare requests.post; 165-167 any exception → return []; contrast ocr_client.py:468-471 using post_json_with_retry, with lines 352-355 noting the account's recorded 429 fragility and shared QPS with this same funnel.
- **Suggested fix:** Route _vlm_reconstruct_page through post_json_with_retry (or vlm_retry's bounded compress-on-retry) so transient 429/5xx don't silently drop rebuilt pages.

#### [P2] Deterministic-calc quota exemption is documented but never implemented — calc queries burn general quota and are denied when quota is exhausted
- **Location:** `opensearch_pipeline/general_answerer.py:203` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** The stated contract is that deterministic calculator answers are quota-free (caller exempts via model=="calc"), and rate_limiter's docstring repeats "确定性计算…不经此配额". But the caller checks and increments the daily general quota BEFORE answer_general runs the calculator, and no caller ever inspects model=="calc" or refunds. So arithmetic queries (routed to ROUTE_OFFICE by pre_route) consume quota units, and once quota is exhausted a zero-LLM "3+5=?" gets the quota-refusal message. An LLM exception also leaves the unit consumed. Behind RAG_GENERAL_ABILITY_MODE, which is being rolled out.
- **Evidence:** general_answerer.py:197 promises "免配额——调用方按 model==\"calc\" 判断免计"; api.py:877 calls LIMITER.admit_general before answer_general (api.py:886); rate_limiter.py:490 increments self._daily[key] at admission; grep for "calc" in api.py finds no exemption/refund.
- **Suggested fix:** In _execute_general_llm, run try_deterministic_calc (tier==office) before admit_general, or refund the quota unit when result model=="calc" or answer_general raises.

#### [P2] Classification persistence writes document_meta/document_version without lease fencing
- **Location:** `opensearch_pipeline/pipeline_nodes.py:1878` · **Category:** concurrency
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** PR-4 fences the classify FAILED terminal write (1813-1827: fence_where_sql + check_fenced_write) but the success-path persists (document_meta category/permission_level/kb_type at 1878-1888; document_version classification fields at 1890-1898; also the frozen-maintenance 1673-1681 and contribution 1705-1715 paths) run unfenced on per-thread connections. After a TTL takeover, a stalled zombie holder's late write lands last-writer-wins, leaving document_meta category/permission divergent from the chunk set the new holder actually chunked and indexed (LLM classification is run-to-run nondeterministic — the documented 79-vs-47 family-flip class). Only matters once RAG_INGEST_LEASE_ENABLE is on, which is the stated multi-worker rollout plan.
- **Evidence:** Lines 1873-1899: UPDATE document_meta ... / UPDATE document_version ... classification_status='CONTENT_CLASSIFIED' with no fence_where_sql/check_fenced_write, versus the FAILED path at 1813-1825 which fences and handles LeaseLost.
- **Suggested fix:** Apply the same fenced-write pattern (fence_where_sql on the document_version UPDATE, verify before the document_meta write in the same transaction) to the CONTENT_CLASSIFIED, frozen-maintenance, and contribution persist paths.

#### [P2] HA3 delete helper treats real-SDK 2xx as success without per-doc error parsing; loose 'not found' idempotency matching
- **Location:** `opensearch_pipeline/pipeline_nodes.py:6690` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _ha3_push_delete_request declares success from getattr(resp,'status_code',200) — the real HA3 SDK response has no status_code, so every response defaults to 200 and the string body is never parsed for per-document errors. The push side added exactly this parsing after the 96-doc silent-loss incident (2xx with errors embedded in a str body). A doc-level-rejected delete is thus counted as success, then chunk_meta flips is_active=0/DELETED and dv goes SUCCESS: the old version stays permanently live in HA3 (old permission fields) with no PENDING_DELETE reconcile queued. The non-2xx branch also accepts any error text containing 'not found'/'no_op' as idempotent success.
- **Evidence:** Lines 6690-6695: status_code=getattr(resp,'status_code',200); is_success=(200<=status_code<300); body never JSON-parsed on success. Contrast 6822-6857 in _push_chunks_to_ha3: str body parsed for per-doc errors, comment citing the 96-case silent loss. Substring idempotency at 6702/6710-6713. RDS flip that trusts this at 6175-6180.
- **Suggested fix:** Parse the string body for per-document errors like _push_chunks_to_ha3 does and raise (or queue PENDING_DELETE) on any non-idempotent error; restrict idempotent detection to exact error codes.

#### [P2] Parity failure leaves document_version stuck in PROCESSING — FAILED chunks not re-drainable until 2h takeover
- **Location:** `opensearch_pipeline/pipeline_nodes.py:7544` · **Category:** correctness
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** _persist_parity_failed_and_raise writes chunk_meta FAILED (PARITY_DROP/UNKNOWN/DRIFT) then raises, but never resets document_version.index_status from PROCESSING to FAILED — unlike node_update_index_status (7340) and the deactivate failure handler (6082), which do. The stage-3 loader excludes chunks whose dv is PROCESSING, and node_acquire_index_lock only claims NOT_INDEXED/FAILED, so every parity failure stalls that document's retry for the full 2h stale-lock takeover window (or lease TTL) instead of the intended next-round re-drain, despite comments claiming next-round reload.
- **Evidence:** Lines 7499-7544: only chunk_meta UPDATEs (via _fail_chunks_with_retry_budget) plus chunk_status='NEEDS_REVIEW' for DEAD; no document_version index_status write before raise. Loader predicate dataworks_orchestrator.py:461 (dv.index_status != 'PROCESSING' OR takeover); claim set reindex_states.py:81; comment 7320-7321 asserts next-round reload.
- **Suggested fix:** In the same transaction, CAS the affected (doc_id,version_no) rows from PROCESSING to FAILED (with lease clear) before raising, mirroring node_update_index_status.

#### [P2] /api/agent/approve discloses run status and approval decisions before any authorization check
- **Location:** `opensearch_pipeline/routes/agent.py:1005` · **Category:** security
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** agent_approve fetches any run and, for non-suspended runs, executes the idempotent-replay check and 409 branch before authorization (_authorize_approver only runs at line 1028, and only on the suspended path). Any authenticated employee holding a run_id can learn: run existence, its live status (409 detail embeds status), and the stored approval decision via same-direction guessing of outcome.kind (no idempotency key needed for a 202 replay). This breaks the repo's 'invisible == nonexistent' invariant enforced by every other run endpoint. Flag RAG_AGENT_ENABLE is default-off but is the rollout target; run_ids are unguessable UUIDs, which limits practical impact.
- **Evidence:** agent.py:1005-1010: status!='suspended' -> _replay_decided_response(req, run) then 409 with f"run 非挂起态（{run.get('status')}）"; replay (lines 903-912) matches same_key OR same_direction and returns 202 with dec decision+status. Contrast agent_run_status:1691-1695 and agent_run_detail:1719-1723 which 404 non-owners.
- **Suggested fix:** Before the suspended-status branch, apply the same owner-or-kb_admin (or approver-scope) gate used by agent_run_detail and return 404 to unauthorized callers.

#### [P2] owner_dept authorized on sanitized value but persisted/used raw
- **Location:** `opensearch_pipeline/routes/kb_console.py:2255` · **Category:** security
- **Verification:** ⏳ Not independently re-verified (finder-reported; verification pass hit the session limit). Evidence below is the finder's.
- **Description:** kb_upload_url takes owner = req.owner_dept.strip() with no sanitization, then passes it to authorize_upload (which internally strips it via _SANITIZE_RE before the whitelist/managed check) but persists and path-encodes the RAW value: build_raw_key(owner,...) and the document_meta INSERT (line 2450) both use the un-sanitized owner. The value that passes authorization is therefore not the value stored or fed downstream. Injected separators (e.g. owner='s/ales' sanitizes to a managed dept 'sales') land document_meta.owner_dept='s/ales' while _dept_from_raw_key (positional parts[1]) derives chunk owner_dept='s' — a mismatch that drives HA3 dept_internal permission filtering. Today it fails closed (self-harm, not escalation), but validate-one-value/use-another is fragile.
- **Evidence:** kb_console.py:2255 `owner = (req.owner_dept or "").strip()` (no sanitize); build_raw_key(owner,...) at 2298 and INSERT ...VALUES(...,owner,...) at 2450 use it raw. authorize_upload sanitizes first: kb_authz.py:288 `owner = _SANITIZE_RE.sub("", ...)`. _dept_from_raw_key returns parts[1]: pipeline_nodes.py:1099.
- **Suggested fix:** Sanitize owner_dept once (kb_authz._SANITIZE_RE) at the top of kb_upload_url and use that single canonical value for authorization, raw_key construction, the token, and the document_meta INSERT.

### P3 — Suggestions (finder-reported, not individually re-verified)

| # | File:Line | Category | Title |
|---|-----------|----------|-------|
| 1 | `opensearch_pipeline/routes/kb_console.py:2573` | maintainability | kb_approve / kb_reject audit is post-commit and swallow-on-error |
| 2 | `opensearch_pipeline/routes/ontology.py:207` | maintainability | _raise_mutation_denied maps authz denials to HTTP status by matching Chinese substrings of the reason text |
| 3 | `opensearch_pipeline/routes/contribution.py:1422` | security | Any dept_admin can globally suppress another department's knowledge gap (no owner scoping on gap dismiss) |
| 4 | `opensearch_pipeline/auth_token.py:113` | security | Session and upload tokens share one signing key by default; cross-type forgery is blocked only by an in-payload typ claim that verify_payload itself does not enforce |
| 5 | `opensearch_pipeline/agent_tools/knowledge_search.py:279` | correctness | _spec_arms_estimate reads nonexistent get_config().retrieval — speculative-search cost telemetry always reports 1 arm |
| 6 | `opensearch_pipeline/content_blocks_builder.py:321` | correctness | Image map is built from the full retrieval list while context/sources use the truncation-aware subset — hallucinated or history-mimicked <<IMG:N>> can render images from chunks the LLM never saw, absent from sources |
| 7 | `opensearch_pipeline/intent_router.py:306` | security | Untrusted document titles are concatenated into the triage classifier prompt with no data/instruction boundary |
| 8 | `opensearch_pipeline/dingtalk_bot.py:1436` | correctness | Failed feedback DB write is confirmed as processed, so identical retry clicks are swallowed by replay dedup |
| 9 | `opensearch_pipeline/dingtalk_bot.py:1543` | maintainability | Dead card-rebuild code path and stale fail-open docstring |
| 10 | `opensearch_pipeline/agent_runtime/events.py:109` | security | RunSuspended internal payloads (state_messages/remaining_calls) rely on manual stripping instead of exclude=True |
| 11 | `opensearch_pipeline/agent_runtime/executor.py:485` | correctness | tool_calls budget double-counted for an approved call across suspend/resume |
| 12 | `opensearch_pipeline/agent_runtime/executor.py:454` | correctness | Pre-suspension retrieval sources never persisted to qa_session_log for approval runs |
| 13 | `opensearch_pipeline/agent_runtime/tool_executor.py:335` | security | Idempotency-hit replay leaves no tool_invocation row and no compliance audit record |
| 14 | `opensearch_pipeline/agent_runtime/sanitize.py:27` | security | sanitize_args does not scrub dict keys or non-string scalars, bypassing the args_json PII contract |
| 15 | `opensearch_pipeline/agent_runtime/tool_executor.py:193` | correctness | Obligation handlers rebuild ToolResult without artifacts, silently severing the sources channel |
| 16 | `opensearch_pipeline/agent_runtime/policy.py:131` | security | Baseline policy pre-grants dormant sql.readonly.* scope to all users on all channels with no obligations |
| 17 | `opensearch_pipeline/agent_runtime/operation_reconciler.py:71` | performance | Auto-reconcile scan window can be permanently head-of-line blocked by unprocessable uncertain rows |
| 18 | `opensearch_pipeline/agent_runtime/llm_log_outbox.py:110` | concurrency | drain_llm_log can report drained while the last batch is popped but unflushed |
| 19 | `opensearch_pipeline/chunker.py:1234` | correctness | step_card continuation chunks are emitted before their main step card, inverting order and prev/next chain |
| 20 | `opensearch_pipeline/chunker.py:1407` | correctness | Step mode never runs _dedup_table_chunks, so repeated page-header tables survive as duplicate chunks |
| 21 | `opensearch_pipeline/chunker.py:1031` | maintainability | Latent infinite recursion in the step-mode __wrapped__ fallback |
| 22 | `opensearch_pipeline/extraction/unified_extractor.py:1600` | correctness | PPTX fallback slide title is applied one slide too late |
| 23 | `opensearch_pipeline/extraction/unified_extractor.py:1658` | correctness | _extract_text re-derives file_ext without the dispatcher's normalization, silently skipping HTML/CSV conversion |
| 24 | `opensearch_pipeline/extraction/text_extractor.py:96` | correctness | Markdown table separator-row 'skip' branch is a no-op; separator noise enters table blocks |
| 25 | `opensearch_pipeline/reconcile.py:474` | correctness | run_parity_check with --hi reports every doc above the bound as VANISHED and fires false critical alerts |
| 26 | `opensearch_pipeline/ingestion_resume.py:23` | correctness | Resume report's stale-lock predicate hard-codes 2h, diverging from the lease-aware reclaim the drain actually performs |
| 27 | `opensearch_pipeline/clients.py:66` | maintainability | Duplicate HA3 client factory already drifted: timeout hardening applied only to clients.py copy, not the serving singleton in retriever |
| 28 | `opensearch_pipeline/config.py:750` | correctness | _env_bool does not strip whitespace, so RAG_READONLY with stray whitespace silently fails open |
| 29 | `opensearch_pipeline/run_simulation.py:558` | security | run_simulation prod-search guard checks only the first truthy target and omits instance_id |
| 30 | `opensearch_pipeline/readiness.py:452` | correctness | Live DashScope readiness probe hardcodes the public domain, diverging from the production VPC endpoint |

---

## What looks good (verified solid)
The reviewed subsystems are, on the whole, carefully built and already hardened by prior audits. Independently confirmed-solid areas:

- **Auth/token layer** — HMAC-SHA256 with `compare_digest`, `exp` + `typ` segregation, MIN_IAT/revocation levers; request-body `user_id`/`user_dept` never trusted for authz (identity resolved from Bearer token + live DB re-read, fail-closed).
- **SQL** across routes — uniformly parameterized; only trusted enum/constant fragments are interpolated; pagination bounds clamped everywhere.
- **Retrieval ACL boundary** — single shared `_build_permission_filter`, fail-closed empty-group normalization, umbrella expansion from frozen taxonomy; threshold↔fusion coupling actively auto-recalibrated.
- **Agent-runtime core** — `run_store`/`approval_store`/`dispatch_outbox` use `_begin()` to pin multi-statement transactions against SteadyDB single-statement replay (the exact hazard the db.py P1 is about), FOR UPDATE + CAS fencing, same-transaction audit/outbox.
- **Generation** — referenced-only citation invariant, truncation-aware sources, strict AST calculator (no `eval`), fail-closed intent routing.
- **Supply chain** — no committed secrets; Dockerfile pins base images by digest, installs with `--require-hashes` against a lockfile, runs as non-root.
- **Test prod-write gates** — collection-phase hard gate + autouse per-test re-check of live config; covers xdist workers.
- **Frontend XSS** — `renderMd` escapes HTML *before* markdown transform and allowlists http(s) links, so `v-html` on LLM/corpus content is safe.
- **Miniapp** — production uses the HTTPS domain; plaintext base URL is dev-only and DEV-gated.

## Verdict: **Request changes**
Fix the **P0 contribution privilege-escalation** before anything else (it is live and unflagged). Land the four other
non-flag-gated P1s (DingTalk secret default, card-log PII leak, `copy_object` guard gap, chunker oversized-step drop)
next. Sweep the flag-gated concurrency/data-loss P1s (`db.py` begin, pipeline `6082` CAS, `cost_breaker` quarantine,
`ingest_lease` takeover, `approval_store` replay, `dispatch_outbox` claim race, `pipeline 5472` crash-wedge) before the
durable-worker / ingest-lease / general-ability flags are enabled in production. Then re-run the finders for the 4
subsystems that never got reviewed (ontology internals, ops-misc, dataworks-dag, frontend-manage) once the session
limit resets.

---

## Continuation (2026-07-17, run 2 — Opus 4.8): the 10 remaining subsystems + carried-P1 verification

The run-1 review hit the account session limit after 21/88 agents. This continuation finished the job: 10 finders over
the never-reached subsystems (each P0–P2 finding adversarially verified as it completed) plus a 2-lens adversarial
verify of the 5 P1s run 1 reported but had not independently confirmed. Method identical to run 1 (finders must cite
`file:line` they actually read; every P0–P2 gets ≥1 refute pass, P0/P1 get 2 lenses). Partway through, the Fable-5 quota
tripped and failed 13 verifiers; the run was then **resumed on Opus 4.8** (cached agents replayed, the 13 re-ran) and
**all 44 agents completed, 0 failed targets**. Coverage is now complete — every subsystem in the repo has been reviewed.

### Coverage now complete
Reviewed in this continuation: `ontology/` internals · ops-misc (qa_logger, audit_log, retention, spot_checker,
admin_notify, kb_upload, access_grants, dept_ancestry) · dataworks-dag (dag_engine, dag_definitions, dataworks_nodes/) ·
frontend-core · frontend-manage · frontend-views · miniapp · schema-migrations · supply-chain · test-safety-gates.
Nothing left un-run.

### Carried-P1 verdicts (run-1's five ⏳ items — now adversarially verified)

| Finding | Run-1 sev | Final verdict |
|---|---|---|
| `approval_store.py:488` — stale approval replay onto new pending call | P1 | ✅ **CONFIRMED P1** (both lenses) |
| `pipeline_nodes.py:5472` — crash-window wedges whole stage-2 batch | P1 | ✅ **CONFIRMED P1** (both lenses; reachable at default flags) |
| `api.py:290` — unbounded `session_id` drops audit rows | P1 | ✅ **CONFIRMED P1** (both lenses) |
| `dispatch_outbox.py:134` — recovery scanner races fast path → double run | P1 | ✅ **CONFIRMED, ↓ P2** (real but sub-ms race, self-healing, flag-gated) |
| `ingest_lease.py:107` — flag-on sweeper steals a live flag-off lease | P1 | ⚠️ **mechanism CONFIRMED, net-new-P1 REFUTED → P3** (documented `workers>1` prerequisite, not live at concurrency-1) |

Net: 3 of the 5 hold as P1, one drops to P2 (fix before the durable-dispatch flag ships), and one is not a live bug but
a known, already-tracked precondition to enabling multiple ingest workers. Full reasoning is inlined at each finding's
**Verification** line above.

### New findings from the 10 subsystems — CONFIRMED (12 × P2, 4 × P3)

The single most important one is the **systemic root of the run-1 P0**:

#### [P2 → P0 root cause] `perm_from_raw_key` fails open to `public`; `build_raw_key` never validates its segments
- **Location:** `opensearch_pipeline/kb_upload.py:123` · **Category:** security · **Verdict:** ✅ CONFIRMED (both lenses; final P2)
- The choke point documented as the authority for retry/reconcile/resume permission resolution **fails open**: `build_raw_key` (`kb_upload.py:108-110`) interpolates `owner_dept` with zero validation, so a `/`-containing value (e.g. trailing-slash `"marketing/"`) shifts every path segment, and `perm_from_raw_key` (`116-123`) reads `parts[2]` and **falls through to `return "public"`** — the most permissive level — for any structurally malformed key. This is the *mechanism* behind the confirmed run-1 P0 (`contribution.py` trailing-slash escalation), and it contradicts the repo's own fail-closed convention twice (`retriever.py:750` and `kb_authz.normalize_permission_level` both default missing/unknown → `restricted`).
- **Scope nuance from the reachability lens:** only a slash producing an empty `parts[2]` escalates (empty or `!`-suffixed depts resolve to `dept_internal`, not public), and the *sole reachable exposure today* is the run-1 P0 whose primary fix lives at the caller. So this is best treated as **defense-in-depth for the P0** rather than an independent live escalation — but it is the correct place to fail closed so future call sites can't reintroduce the P0.
- **Fix:** reject empty/`/`-containing `owner_dept`/`doc_id`/`upload_id` in `build_raw_key`, and make `perm_from_raw_key` return `restricted` (or raise) on any structurally malformed key.

Other CONFIRMED P2s (all verified, one refute lens each):

| File:Line | Cat | Finding |
|---|---|---|
| `console-app/src/components/manage/AgentApprovalQueue.vue:73` | security | **Edit-then-approve executes server-redacted args:** the "改参并批准" editor pre-fills from `proposed_args`, which the backend stores **PII-redacted** ("原文不出库"); `kind=edited` runs `edited_args` verbatim (no merge with the checkpoint original), so an approver tweaking one field silently submits masked placeholders as the authoritative execution args on a high-risk write. Flag-gated (`RAG_AGENT_ENABLE`, off) but on the rollout path. |
| `fuling-rag-miniapp/pages/chat/chat.js:166` | security | **Shared-device conversation bleed:** `ensureOwner` (clear-on-account-change) is called only in `onLoad`'s login-success path; `_restoreLast()` renders the prior user's cached conversations before login, and the `_ask`/drawer/settings login paths never re-check ownership — a second account's Q&A is shown and `_persistCurrent`-ed into the first owner's local store on a shared device. |
| `schema/009_acl_projection_outbox.sql:34` | security | **ACL revocation can be silently lost:** one row per doc, resurrect-on-duplicate, **no generation/epoch column**; a drain marks the row done unconditionally (`access_grants.py:273`, no `done_at IS NULL`/seq CAS), so a revocation that resurrects the row mid-drain is erased, leaving stale `allowed_depts` in HA3 until the capped full-scan reconcile. Read-side fail-closed re-check limits it to index residue, not disclosure. |
| `.github/workflows/ci.yml:12` | security/ops | **Blocking CI never runs on the branch that ships:** `ci.yml`/`frontend.yml` trigger `push` only on `main` (+ `pull_request`). Production work lands via direct pushes to `claude/ontology-p0` (packages are cut from it) with no PR, so tests, lint, **gitleaks, and CVE audits never execute on the shipped code**. |
| `tests/conftest.py:34` | security | **Prod-write test gate has holes:** `_prod_target_violations` checks only `rds.host` and `alibaba_vector.endpoint` — never OSS bucket (`RAG_SIMULATE_OSS=false` + prod bucket creds passes both gates) nor a remote OpenSearch host. The only OSS backstop is `GuardedBucket`, whose write list omits `copy_object` (run-1 P1). |
| `console-app/src/composables/useAuth.ts:229` | correctness | **401 reauth storm:** `reauth()` has no in-flight dedup, so N concurrent 401s (App.vue's 5 parallel preloads, ManageView's 3 poll calls) fire N `requestAuthCode` + N token exchanges, and each distinct token re-triggers a full identity-scope store wipe, clobbering just-reloaded data. |
| `console-app/src/composables/useAuth.ts:20` | correctness | **Malformed deep-link → permanent blank page:** `decodeURIComponent` on raw URL params with no try/catch, run at module top-level (`boot/capture.ts`) before Vue mounts; a mangled `?token=abc%` throws `URIError` during import, aborting the whole app with no error UI. |
| `console-app/src/composables/useAsk.ts:433` | correctness | **Legacy `stop()` falsely finalizes agent messages:** conversation switch/new/remove call legacy `stop()` when `asking=true` (shared with the agent transport) with no `m.agent` guard, painting a cancelled-error card on an agent run that keeps executing server-side; retry then spawns a duplicate run. |
| `fuling-rag-miniapp/pages/chat/chat.js:232` | correctness | **"清空会话" permanently breaks restore:** `session_reset_at` marker is set once and never cleared; `lastResetAt` re-inits to `0` each launch, so `resetAt !== lastResetAt` is true forever after one clear — every cold start silently wipes the restored conversation. |
| `dataworks_nodes/ontology_backfill_node.py:41` | correctness | **Ontology DataWorks nodes are dead on arrival:** both pin `requests==2.32.3` (Requires-Python ≥3.8) but the DataWorks executor is py3.7, so `pip` fails at step 0. Sibling stage nodes gained a `sys.version_info` pin branch on 07-17; these two paste-sources were missed. |
| `dataworks_nodes/scan_oss_sync_keys.py:253` | correctness | **Dedup leaves duplicate live docs:** in fix mode, when the target `raw_key_hash` is already owned, the current record is retired with a **status-only** `document_version.status='superseded'` — its `chunk_meta` stays `is_active=1/INDEXED` and its HA3 PKs stay live, and no reconciler covers this shape, so both doc_ids return in retrieval permanently. |
| `opensearch_pipeline/dataworks_orchestrator.py:661` | correctness | **Stage-1 no-progress false abort (↓P3):** the drain counter counts `_quarantine` rows the scanner never claims, so it can spuriously declare no-progress and abort / head-of-line wedge stage-1. |

CONFIRMED but re-rated **P3** by verification: `ontology/packing_math.py:245` (positive container capacity reported for cartons that can't physically fit), `ontology/store.py:1469` (stewardship/attribute-source writes bypass `_check_caller`, no audit trail), `spot_checker.py:686` (writes the LLM's out-of-vocab `'internal'` verbatim into `permission_level`).

### PLAUSIBLE (1)
- **`requirements.txt:13` — production SAE install path is unpinned/unhashed** (supply-chain, P2). One lens fully CONFIRMED, one UNCERTAIN. The SAE buildImage/serving path installs `requirements.txt` (floor-only `>=`, no hashes, over a third-party Aliyun mirror — git history `02961fa`/`e77040f` confirms it is the live path), while **every** integrity gate (`--require-hashes` lockfile, pip-audit, SBOM, trivy) targets `requirements-prod.lock` / the Dockerfile — a path SAE does not use. Partial mitigation: `ci.yml:194-196` runs a fresh-resolve `pip_audit` over the same pyproject prod extras. ⚠️ **Internal tension to resolve:** the `requirements.txt:15` refuter argued the Dockerfile lock *is* the install source; the truth depends on whether SAE serving uses the zip/buildpack path (`requirements.txt`) or the Docker path — worth a 5-minute confirmation against the actual SAE deploy config, since the answer decides whether this is a real zero-integrity-verification prod path.

### REFUTED (3)
- **`access_grants.py:249` — outbox attempts-cap starvation** → REFUTED. The mechanic is accurate (drain is oldest-first, no attempts predicate; failing rows keep `done_at=NULL` and can clog the head), but the *impact* ("newer grants stop projecting") is false: `reconcile_allowed_depts(commit=True)` runs a full-scan over all approved doc_ids every stage-3 pre-drain, immediately after the outbox drain, independently re-projecting drift. Worth a P3 note for the head-of-line clog, not a security gap.
- **`requirements.txt:15` — 07-17 trim "dropped" redis/jsonschema** → REFUTED. `git log -S` shows neither package was ever in `requirements.txt` (the trim removed only extraction deps); and `requirements-prod.lock` (the Dockerfile install source) contains `redis==8.0.1` + `jsonschema==4.26.0`. Both the causal claim and the runtime-failure conclusion are wrong. (See the `requirements.txt:13` tension above about which path SAE actually installs.)
- **`scripts/corpus_cleanup.py:39` — defaults to prod-write, bypasses `PROD_RW_ACK`** → REFUTED. Literal facts true, but two in-repo controls defeat the conclusion: `RAG_ENV=test` remaps to `prod_ro` with **read-only DB creds** (`fuling_ro`), so the `--commit` UPDATEs are denied by MySQL privileges; and the `env_guard` prod-write assertion (`RAG_ENVIRONMENT=staging` + prod host + `fuling_knowledge`) fires first. The write cannot land on production.

### P3 suggestions (16, finder-reported, not individually re-verified)

| # | File:Line | Cat | Title |
|---|---|---|---|
| 1 | `opensearch_pipeline/ontology/store.py:474` | correctness | `update_golden` provenance diverges RDS (deep-merge) vs Memory (replace), leaving stale stamp fields on RDS |
| 2 | `opensearch_pipeline/ontology/packing_math.py:135` | maintainability | `CalcRule.applicable_categories` parsed but never enforced by any consumer |
| 3 | `opensearch_pipeline/access_grants.py:191` | correctness | `materialize` unchanged-check unions per-chunk `allowed_depts`, masking partially-projected versions |
| 4 | `dataworks_nodes/ops_health_monitor_node.py:38` | correctness | installs unpinned `pypdf`/`pdfplumber` without the py3.7 pin branch its siblings have |
| 5 | `console-app/src/composables/useKb.ts:758` | security | `openDocPreview` placeholder tab keeps `window.opener` before cross-origin nav (reverse tabnabbing) |
| 6 | `console-app/src/composables/useDialog.ts:22` | correctness | single `_resolve` slot: overlapping dialogs orphan the first promise, dropping the awaited action |
| 7 | `console-app/src/composables/useAuth.ts:243` | correctness | after in-session token expiry with failed reauth, UI stays "logged in" showing only generic load errors |
| 8 | `console-app/src/components/manage/OpsMetricsPanel.vue:174` | correctness | paints NULL `answer_rate` as "0.0%" on the 最新答问率 stat card |
| 9 | `console-app/src/components/manage/FeedbackTrend.vue:58` | correctness | still labels the 30-day `feedback_total` as "累计" after backend #10 made it window-scoped |
| 10 | `console-app/src/components/manage/AccessRequestModal.vue:12` | correctness | wipes the typed reason before the request settles, losing it on failure |
| 11 | `console-app/src/composables/useAsk.ts:440` | correctness | `retry()` during an in-flight stream deletes the error card without re-asking |
| 12 | `console-app/src/lib/mdIncr.ts:63` | correctness | incremental streaming renderer splits multi-line blockquotes, diverging from the authoritative renderer |
| 13 | `schema/001_opensearch_pipeline.sql:337` | maintainability | dead legacy `qa_session_log`/`user_feedback`/`escalation_ticket` copies in `fuling_knowledge`, recreated every fresh env |
| 14 | `schema/022_agent_runtime.sql:29` | correctness | agent-family identity columns are half the width of the canonical identity columns they must hold |
| 15 | `scripts/dataworks_stage3_with_cleanup.py:2` | security | legacy stage-3 cleanup node pip-installs fully unpinned deps at production runtime |
| 16 | `tests/conftest.py:162` | correctness | `_LOCAL_STACK_SERIAL_MODULES` hand-curated with no enforcement; real-DB modules already exist outside it |

### What looks good (continuation — verified solid)
- **Ontology core** — `store.py` compound writes are transactional; `ids.py` ULID mint/decode round-trips; `authz.py` `can_read_object`/`acl_read_predicate_sql` are a faithful fail-closed Python↔SQL pair; `resolve.py` auto-activation is HMAC-signed manifest with date/env/source-sha256 binding.
- **ops-misc** — `audit_log` dual-mode writer sound; `dept_ancestry` nearest-ancestor handles cycles/self-parent/max-hops; `qa_logger` PII redaction ordering + 1054 optional-column ladder correct; `retention` dry-run default + double-gate commit + liveness guards.
- **dataworks-dag** — `dag_engine` Kahn topo-sort with cycle/unresolved-dep detection; `dag_definitions` DAG-3 enforces the never-disappear invariant; stage-2/3 claim uses single-step `FOR UPDATE … SKIP LOCKED` + the shared `ingest_lease.takeover_where_sql`.
- **frontend-core/views** — `markdown.ts`+`mdIncr.ts` escape-then-whitelist confirmed end-to-end; token captured before router import and scrubbed from URL; `identityScope` reset discipline uniform across all module stores; `sseDecoder` handles split frames, multi-byte UTF-8, `[DONE]`, bad JSON.
- **frontend-manage** — chart primitives handle empty/zero/NaN; `DocTable` bulk-op confirm counts match execution; action queues have no optimistic-without-rollback; authz-driven rendering mirrors server enforcement.
- **miniapp** — Bearer token header-only, single-shot 401 re-login, token in memory never in `dd` storage; prod `BASE_URL` HTTPS with compile-time-gated dev plaintext; kb-upload web-view deliberately excludes the bearer token.
- **schema-migrations** — idempotency via `information_schema`+`PREPARE` guards; explicit `utf8mb4_unicode_ci`; destructive dedup DELETEs run after NULL backfills; apply-order numerically consistent.
- **supply-chain** — Dockerfile digest-pinned + hash-verified lock install + non-root; `ci.yml` security job strong *where it runs* (full-history gitleaks, blocking pip-audit); dependabot covers actions/pip/npm; no committed secrets.
- **test-safety-gates** — collection gate executes in every xdist worker; no nested `conftest.py`; `local_stack` refuses non-local hosts; stress-harness Makefile targets strip inherited `RAG_*` and pin `127.0.0.1`.

### Updated verdict — still **Request changes**, priorities unchanged at the top
The continuation did not surface anything above the run-1 P0. Fix order stands: (1) the run-1 **P0** contribution
escalation — and while there, fail-close its root `kb_upload.build_raw_key`/`perm_from_raw_key`; (2) the non-flag-gated
P1s from run 1; (3) the **three re-confirmed P1s** (`approval_store:488`, `pipeline_nodes:5472` — reachable at default
flags, `api.py:290`) plus `dispatch_outbox:134` (now P2) **before the durable-dispatch / general-ability flags flip**;
(4) `ingest_lease:107` stays a **precondition to enabling `workers>1`**, already tracked in the fencing scope doc — not
a hotfix. New non-flag surfaces worth landing soon: the **CI-branch gap** (`ci.yml:12` — the shipped branch is
ungated, including secret scan), the **prod-write test-gate holes** (`conftest.py:34`), and confirming the **SAE install
path** for `requirements.txt:13`.
