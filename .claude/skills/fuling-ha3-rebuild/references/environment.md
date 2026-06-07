# Environment & concrete facts

> Secrets are **not** stored here. Credentials live in `~/Downloads/opensearch-rag-pipeline/.env`
> (DashScope) and `.env.production` (RDS/OSS/HA3), and are also hardcoded in the DataWorks node
> scripts. The bundled helper scripts read them from those `.env` files. **Rotate them after a
> rebuild** — they tend to end up in chat transcripts and `.mcp.json`.

## Repo
- Path: `~/Downloads/opensearch-rag-pipeline` (Python). Pipeline core: `opensearch_pipeline/`.
- Key modules: `dataworks_orchestrator.py` (stage entrypoints + `run_stage` / `run_stage_drained`),
  `pipeline_nodes.py` (DAG node impls, PII detection, embedding, HA3 push, deactivation),
  `chunker.py` (chunking incl. step-card / xlsx-layout methods), `retriever.py` (serving-side hybrid
  query), `config.py` (config + the production "no Gemini" guard).
- Run locally with no external services: `make sim` (simulate mode). **Always validate code edits in
  `make sim` before deploying.**

## Alibaba Cloud — DataWorks
- Region: **cn-hangzhou**. Workspace: **`default_workspace_6na2`**, project id **`609583`**, single
  environment (no dev/prod split).
- Resource group (runs the nodes): **`data_process`**, Serverless `CommonV2`, 500 CU,
  identifier `Serverless_res_group_783676587918209_787400821137602`, VPC `vpc-bp1acxagvp6mtss2rnvcq`.
- MCP server: **`alibabacloud-dataworks-mcp-server`** (`npx -y …`) configured in repo `.mcp.json`,
  env `REGION` + `ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET`. Loads only at Claude Code startup.
- **Stage nodes** (manual PyODPS3 nodes, run in DataStudio IDE — NOT scheduled tasks):
  | Stage | Node name | Node id | Notes |
  |---|---|---|---|
  | 1 | `opensearch_stage1_canonicalize1` | `6258545097132126018` | extract/OCR/VLM → canonical |
  | 2 | `opensearch_stage2_safe_chunk` | `5000522562719311104` | classify/redact/chunk |
  | 3 | **`清理stage3`** | `6270472233860755482` | HA3 cleanup + embed/push/swap — **use this** |
  | 3 (broken) | `opensearch_stage3_push_index` | `5037820758500522302` | **DO NOT USE** — refs missing `opensearch_pipeline.zip` |
- Code archive resource: **`opensearch_pipeline_production.zip`**, resource id **`8937691206218419696`**
  (type Archive). Re-upload via DataStudio console; OpenAPI presigned-URL upload is blocked by the guard.

## RDS (MySQL) — `fuling_knowledge`
- Public endpoint (laptop-reachable, in `.env.production`): `rm-bp15j7wekd5738f093o.rwlb.rds.aliyuncs.com`.
- Intranet endpoint (used by nodes): `rm-bp15j7wekd5738f09.rwlb.rds.aliyuncs.com` (`100.x` VPC).
  **Confirm these are the same instance** (public + VPC addresses) before trusting laptop reads/writes.
- Tables: `document_meta`, `document_version`, `chunk_meta`, `document_sensitive_finding`,
  `opensearch_bulk_job`, `review_task`. The `002_feedback_system` tables live in DB `fuling_operation`.
- Key columns: `document_version.{content_process_status, chunk_status, index_status, canonical_json_key,
  raw_key, raw_key_hash, version_no, file_ext, retry_count, status}`;
  `chunk_meta.{is_active, index_status, embedding_status, version_no, chunk_type}`.
- ⚠️ Production has `UNIQUE KEY uk_raw_key_hash (raw_key_hash)` — **not** in repo `schema/001`.

## HA3 / OpenSearch 向量检索版
- Instance id: `ha-cn-kgl4slr1n01`. User: `<RAG_HA3_USER — in .env.production>`. Table: **`fuling_kb_chunks`**. PK field: `id`.
- **Intranet API入口**: `ha-cn-kgl4slr1n01.ha.aliyuncs.com` → `100.x` VPC address, **in-VPC only**.
- **Public 公网域名**: `ha-cn-kgl4slr1n01.public.ha.aliyuncs.com` → public IP, **HTTP / port 80**
  (not HTTPS/443), requires 公网访问 enabled + IP on the allowlist. The HA3 SDK `Config` accepts
  `protocol="HTTP"`.
- Hybrid config (`config.py` defaults): `hybrid_fusion="weighted"`, `knn_weight=0.7`, `text_weight=0.3`,
  `text_search_field="chunk_text"` (BM25 inverted index), `hybrid_knn_top_k=100`. Serving score
  thresholds (high=8/medium=5) are calibrated to **weighted** fusion — they break under RRF.

## OSS
- Bucket: **`fuling-knowledge-base`**. Internal endpoint `oss-cn-hangzhou-internal.aliyuncs.com`
  (VPC-only). **Public** endpoint `oss-cn-hangzhou.aliyuncs.com` is laptop-reachable for reads/writes
  with the OSS keys (used by `verify_oss_canonical.py`).
- Prefixes: `raw/<dept>/…` (sources; quarantine under `…/_quarantine/`), `processing/canonical/{doc_id}/v{N}/`,
  `processing/assets/…`, `rag-ready/…`, `processing/cache/vlm_cache.json`, and the bulk-job staging prefix.

## Models (DashScope / Bailian)
- LLM `qwen3.6-plus`, OCR `qwen-vl-ocr-latest`, VLM `qwen3-vl-plus`, embedding **`text-embedding-v4`**
  (dim 1024, dense+sparse via the **native** endpoint `/api/v1/services/embeddings/text-embedding/…`;
  compatible-mode drops sparse). Production guard hard-fails if any model resolves to Gemini.
