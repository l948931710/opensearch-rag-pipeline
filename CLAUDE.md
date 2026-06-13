# CLAUDE.md

Guidance for Claude Code (and developers) working in this repository.

> The original `README.md` documents only the initial 4-DAG skeleton. The system has since grown VLM/multimodal handling, a DingTalk bot, a retrieval/serving layer, and a feedback system. **This file reflects the current scope.**

## What this is

An **Alibaba-Cloud-native enterprise RAG system** for **Zhejiang Fuling Plastics (浙江富岭塑胶 / 涪陵)**, a disposable-tableware & packaging manufacturer. It turns internal documents (SOP/作业指导书, U8+ ERP manuals, HR/admin policies, FAQs) into a self-service Q&A service: employees ask questions in **DingTalk (钉钉)** and a backend RAG pipeline answers from Fuling's own docs with **department-level permission filtering** and inline images.

Despite the project name, retrieval runs on **Alibaba's HA3 vector engine** ("OpenSearch 向量检索版", SDK `alibabacloud_ha3engine_vector`), **not** Elastic/AWS OpenSearch. The code also supports standard OpenSearch as a local-dev fallback. Models are **DashScope/Bailian** (Qwen LLM, `text-embedding-v4`, Qwen-VL OCR/VLM). Code, comments, and docs are largely in Chinese.

## Commands

```bash
# Install (extras: dev / production / ocr / api)
make install          # base
make dev              # + pytest, ruff, httpx
make prod             # + pymysql, opensearch-py, oss2, dashscope, ha3 vector sdk

# Run locally with NO external services (simulate mode)
make sim              # normal scenario
make sim-all          # all 4 scenarios: normal / sensitive / multi / version_update
make sim-dag1         # DAG 1 only;  make sim-dag2 = DAGs 1-2
make graph            # print DAG dependency graph

# Serving API
make api              # uvicorn opensearch_pipeline.api:app on :8000

# Tests / lint
make test             # pytest (testpaths = tests/)
make test-cov         # + coverage
make lint             # ruff (line-length 100, py39);  make lint-fix to autofix
```

**Simulate mode** (`RAG_SIMULATE=true`, the default) runs the whole pipeline with no OSS/RDS/OpenSearch/LLM — embeddings become hash vectors, OSS reads hit local files, HA3 returns a `MOCK_HA3_CLIENT`. Granular overrides: `RAG_SIMULATE_DB/OPENSEARCH/OSS/API`. **Always validate pipeline changes in simulate mode first.**

## Environments & config

All config is in `config.py` (`load_config()` → cached `get_config()`), read from `RAG_`-prefixed env vars. `RAG_ENV` selects an overlay: `.env` (shared keys/models) + `.env.{local|local_ab_*|staging|prod_ro}` (storage endpoints/creds). Six environment tiers (SIM / LOCAL-DEV / LOCAL-EVAL / STAGING / PROD-RO / PROD) — matrix, credential separation, and the cloud-console checklist live in **`docs/environment_design.md`**. `RAG_ENV=test` is a deprecated alias for `prod_ro`; production sets no `RAG_ENV` (SAE/DataWorks inject env vars directly).

- **Production security guard** (`config.py`): when `environment ∈ {production, staging}`, it **hard-raises** if no DashScope key is set or if any of LLM/OCR/Embedding would resolve to Google/Gemini. Production must use Alibaba Qwen — never Gemini.
- **Env↔target cross-validation** (`config.py::_validate_environment_target_consistency`): a dev label pointing at prod RDS/HA3 fingerprints hard-raises unless `RAG_ALLOW_REMOTE_DB/SEARCH=read_only_ack`. **Runtime destructive-op guard** (`env_guard.py`): non-production writes to prod targets need same-day `RAG_DESTRUCTIVE_PROD_ACK=<op>:<date>`; `RAG_READONLY=true` (PROD-RO) blocks all writes. Scripts reach prod **only** via `prod_access.py` (read-only session by default; RW requires same-day `PROD-RW:<date>` token) — never hand-parse `.env.production`.
- The overlay loads with `override=True` (file-wins): shell-exported vars are shadowed by the env file (banner lists them); `RAG_ALLOW_SHELL_OVERRIDE=VAR1,VAR2` is the per-var escape hatch.
- **Model names resolve at runtime**, not from dataclass defaults. With a DashScope key: LLM→`qwen3.6-plus`, OCR→`qwen-vl-ocr-latest`, VLM→`qwen3-vl-plus`, embedding→`text-embedding-v4`. The Gemini names in the dataclasses are only fallbacks. **Read the factory logic in `load_config()`, not the field defaults.**

## Architecture

### Ingestion — 4 DAGs (batch, runs on DataWorks)

A custom lightweight DAG engine (`dag_engine.py`, topo-sort + shared `context` dict). DAGs defined in `dag_definitions.py`; ~19 `node_*` functions implemented in `pipeline_nodes.py` (4,288 lines — the ingestion core).

1. **`raw_to_canonical`** — scan OSS `raw/` → register RDS metadata → extract text (+OCR fallback) → build `canonical/`.
2. **`canonical_to_safe_chunk`** — classify+risk (LLM) → detect PII (regex) → redact/quarantine → publish `rag-ready/` → chunk → validate → `write_chunk_meta`.
3. **`chunk_to_opensearch`** — acquire lock → embed → build bulk payload → push to HA3/OpenSearch → update index status → **deactivate old version chunks**.
4. **`retrieval_eval`** — eval only, **not wired into production** (only DAGs 1-3 are schedulable).

**⚠️ Critical safety invariant:** new chunks must be persisted to RDS **and** successfully indexed **before** old-version chunks are deactivated (`node_deactivate_old_chunks`). Reversing this makes a document vanish from search on any mid-failure. `node_update_index_status` aborts the DAG on index failure to protect this ordering. **Never reorder DAG 3 nodes or move deactivation earlier.**

Production entry: `dataworks_orchestrator.py --stage {1|2|3} --bizdate ${bizdate}` (DataWorks nodes shell out to this daily). It adds atomic row-claim preemption (`SET content_process_status='LOADING' ... LIMIT 100`), a 2-hour stale-lock guard, and rollback-on-failure. It deliberately `raise`s on partial load errors so DataWorks (which keys on exit code) marks the run failed.

### Serving — shared core, two frontends (online, runs on SAE)

Shared modules consumed by **both** `api.py` (FastAPI) and `dingtalk_bot.py`: `retriever.py`, `llm_generator.py`, `session_store.py`, `qa_logger.py`, `feedback_handler.py`, `content_blocks_builder.py`, `answer_flow.py` (**pure** bookkeeping: the single source for `qa_session_log` payloads via `build_qa_log_kwargs`, the history-append policy, and the NO_RESULT message — keep it side-effect-free; `log_qa_session`/`append_to_history` calls stay at the four call sites because tests monkeypatch those module-global names).

- **Retrieval** (`retriever.py`, `retrieve_and_enrich`, `top_k=7`): query-embed → HA3 **3-way hybrid** (Dense + Sparse in the kNN path, BM25 on `chunk_text`) → cover-page demotion → neighbor stitching (±1 from RDS) → step-card expansion. Fusion is **`weighted` (knn 0.7 / text 0.3)** by default — eval showed weighted > RRF. Permission filtering is **server-side in HA3** with the dept value whitelisted against filter-injection. A **routed learned reranker** (`reranker.py`: DashScope `qwen3-rerank` for text pools, `qwen3-vl-rerank` for image-bearing pools) is wired into `retrieve_and_enrich` but **OFF by default** behind `RAG_RERANK_ENABLE` (pool=20 → top_k=7; +10.5pp recall@1 on the 251-q gold set, see `eval_harness/reports/rerank_findings.md`). When rerank is on, 高/中/低 labels switch to rerank scores: `RAG_RERANK_SCORE_THRESHOLD_HIGH=0.9 / MEDIUM=0.8`.
- **Query embeddings must use the DashScope *native* API** (`output_type=dense&sparse`). OpenAI compatible-mode drops the sparse vector and silently tanks recall.
- **Answers** (`llm_generator.py`): Qwen via OpenAI compatible-mode. Context labels chunks 高/中/低 from `score_threshold_high=7.7 / medium=5.8` (recalibrated on the 251-q gold set) — **these are calibrated to weighted-fusion scores and break under RRF.** LLM is told not to emit its own source list; images are interleaved via `<<IMG:N>>` markers.

### Multimodal & step cards (current focus area)

For screenshot-heavy SOP/ERP docs:

- **VLM image funnel** (`image_funnel_processor.py`, called from `extraction/unified_extractor.py`): a 3-stage cascade (cheap heuristics → OCR text density → Qwen-VL semantic+safety audit) routing each image to `DISCARD` / `ROUTE_TO_TEXT` / `ROUTE_TO_VECTOR` / `QUARANTINE_SENSITIVE`. MD5-deduped, concurrent (`RAG_VLM_CONCURRENCY=8`), with a cross-document persistent cache (`scratch/vlm_cache.json` + OSS).
- **Step cards** (`chunker.py::_chunk_by_step`): procedural docs become one `procedure_parent` + per-step `step_card` chunks linked by `parent_chunk_id`/`step_no` (`schema/002_step_card_enhancement.sql`), each carrying its bound images (`image_refs_json`). The hard problem is **binding the right image to the right step** (DOCX uses exact positional `image_ref` blocks; PDF uses `page_num`; XLSX uses `anchor_row`/`figure_refs`).
- The `image_refs` dict shape (`oss_key`/`source_image`/`visual_summary`/`ocr_text`/`image_index`; xlsx additionally relies on `filename`+`anchor_row` as the strict identity for same-anchor disambiguation — both eval `jaccard` strict_key and chunker P2 anchor-aware fallback consume them) is a **load-bearing contract** across extractor → chunker → `content_blocks_builder` → DingTalk card. Preserve those keys end-to-end. Don't drop `filename`/`anchor_row` from `_img_entry` or any RDS→serving roundtrip — xlsx procedure_image_guide with multiple images at the same row will silently misbind without them.

## Layout

```
opensearch_pipeline/
  config.py                 # config center + production security guard
  dag_engine.py             # DAG executor
  dag_definitions.py        # the 4 DAGs
  pipeline_nodes.py         # ~19 node_* impls + DB pool, clients, classify, PII, embed, push, deactivate  (4288 lines)
  dataworks_orchestrator.py # production CLI (--stage / --bizdate)
  run_simulation.py         # local sim runner + test scenarios
  chunker.py                # DocumentChunker; modes: text/faq/clause/step  (2228 lines)
  content_blocks_builder.py # answer → DingTalk card content_blocks
  retriever.py              # HA3 hybrid retrieval + stitching + step expansion
  reranker.py               # routed DashScope rerank (text/VL), gated by RAG_RERANK_ENABLE
  api.py / llm_generator.py / session_store.py / qa_logger.py / feedback_handler.py   # serving
  dingtalk_bot.py / dingtalk_card.py / oss_url.py   # DingTalk integration
  image_funnel_processor.py / spot_checker.py       # VLM funnel; security re-audit
  extraction/               # unified_extractor + per-format (pdf/docx/text/xlsx) + ocr_client + image utils
schema/                     # RDS DDL (001 + 002_feedback_system + 002_step_card_enhancement)
dataworks_nodes/            # DataWorks stage node scripts
fuling_chunk_exp/           # real document corpus for chunking experiments
eval_harness/               # end-to-end HA3 RAG eval harness + reports
tests/  scratch/  docs/
```

RDS note: `001_*.sql` and step-card schema use DB `fuling_knowledge`; `002_feedback_system.sql` uses `fuling_operation`. Check which DB a table lives in.

## Gotchas / things to watch

- **Sessions are in-process in-memory** (`session_store.py`) — Dockerfile pins `--workers 1` for this reason; restart loses sessions. Swap for Redis to scale.
- **Open reliability gaps** (not yet fixed): no general lease/heartbeat on the optimistic locks (stage-3 has a 2h stale-`PROCESSING` takeover; stage-1/2 `content_process_status` has no age guard at all); cross-cloud split-brain on irreversible HA3 deletes (no 2-phase commit with RDS — `spot_checker.py`'s `PENDING_DELETE` reconciliation is the pattern to reuse); partial-batch failures strand fully-INDEXED docs with their old versions still active (dual versions served).
- **`.xls`** (legacy binary) is explicitly unsupported (clear warning, no silent failure); HTML is tag-stripped and CSV csv-parsed before chunking (`unified_extractor.py`).
- Qwen-VL endpoint routing (compat vs native) is centralized in `vlm_endpoint.py` — don't hand-build those URLs/payloads in callers.
- `work_report.md` is management-facing and somewhat spin-flavored (`weekly_changes.json` literally instructs reframing); trust the git log + `tests/eval/*.md` for the objective record.

## Conventions

- Heavy use of **graceful degradation**: optional deps imported lazily inside functions; image/OCR failures never break text extraction; logging/stitching/expansion all fail open. Preserve this — don't let an auxiliary failure break an answer.
- PII is stored as **SHA-256 hash + masked preview only** (`document_sensitive_finding`), never raw.
- `permission_level` is resolved by **path heuristics** (`restricted`/`internal` substrings in OSS keys), never by the LLM.
- Chunk strategy routing lives in `pipeline_nodes.py::node_chunk_documents` (brittle substring matching on category/title), not in the chunker.
