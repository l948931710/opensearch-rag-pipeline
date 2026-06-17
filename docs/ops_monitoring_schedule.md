# Ops monitoring schedule (CS3/CS4 reconcilers + OBS-5 QA rollup)

Single entry point: `python -m opensearch_pipeline.ops_monitor` runs all standing health jobs in one
invocation, each fail-open, each firing its own OBS-4 ops alert on drift/breach. Exit = worst sub-job
code (0 ok / 2 drift-or-SLO-breach / 3 error).

| Job | What it checks | Reads | Writes |
|-----|----------------|-------|--------|
| `reconcile_ha3` (CS3) | RDS active+INDEXED chunks ⇄ HA3 PKs; vanished docs | RDS, HA3 | none |
| `reconcile_oss` (CS4) | active-chunk image `oss_key`s ⇄ OSS objects | RDS, OSS | none |
| `qa_rollup` (OBS-5) | qa_session_log → qa_daily_metrics + SLO verdict | RDS | `qa_daily_metrics` UPSERT |

The two reconcilers are **read-only**. `qa_rollup` writes only to `qa_daily_metrics` (the fresh OBS-5
table) → it needs a **write-capable** env, not `prod_ro` (PROD-RO blocks the UPSERT).

## Status (2026-06-16)

- Code + migration 004 (OBS-3 columns + qa_daily_metrics) live on prod. SLOs ratified:
  answer_rate≥0.75 / no_result_rate≤0.15 / p95_latency_ms≤25000 / error_rate≤0.05 (env-overridable
  `RAG_SLO_*`).
- **Not yet scheduled.** The DataWorks prod project (`fuling_ai_kb_prod`, 609696) has only the
  `fuling_ai_kb_prod_root` VIRTUAL no-op task — the pipeline has always been laptop-driven, so the
  `opensearch_pipeline` package + creds are **not deployed to the serverless resource group**
  (`data_process`, Serverless_res_group_…137602). Until that deployment exists, schedule from an
  environment that already has the code + `.env` (the laptop).

## Path A — laptop / any host with the repo + .env (works today)

Add to crontab on a host that has the repo checked out and a write-capable prod `.env` (the same
place you run `dataworks_orchestrator.py`). Example: daily 02:30 Beijing.

```cron
# m h dom mon dow   command
30 2 * * *  cd /path/to/opensearch-rag-pipeline && \
            RAG_ENV=prod /usr/bin/env python -m opensearch_pipeline.ops_monitor \
            >> /var/log/rag_ops_monitor.log 2>&1
```

Requirements:
- `RAG_OPS_ALERT_WEBHOOK` (+ `RAG_OPS_ALERT_SECRET`) set so OBS-4 alerts actually deliver; without
  them the alert path is a logged no-op (jobs still run, exit codes still set).
- A **write-capable** prod env for `qa_rollup` (not `prod_ro`). If you only want the read-only
  reconcilers on a `prod_ro` box, run `--only reconcile_ha3 reconcile_oss`.
- For read-only-reconcilers-only nodes, exit 2 means real drift → page.

## Path B — DataWorks scheduled node (requires a one-time deployment)

Prerequisites that do **not** exist yet for this project:
1. **Code on the resource group.** Package `opensearch_pipeline` (+ deps) as a DataWorks Archive/File
   Resource, or bake a custom image, so a Shell node can `python -m opensearch_pipeline.ops_monitor`.
2. **Cred injection.** RDS / HA3 / OSS / DashScope + `RAG_OPS_ALERT_*` as resource-group env vars or
   a secret manager (prod sets no `RAG_ENV`; vars are injected directly — the same mechanism the
   stage orchestrator was designed for but which was never stood up).
3. A **Shell/CDH-Shell** node (not ODPS_SQL) depending on `fuling_ai_kb_prod_root`, daily cron,
   `rerunMode=FailureAllowed`, `runtimeResource` = `data_process`. NOTE: the DataWorks MCP
   `CreateNode` currently only supports `command=ODPS_SQL`, so the Shell node must be created in the
   DataWorks console (or via a SQL node that shells out, if your setup allows it).

Once 1–2 exist, the node body is just the Path-A command. Until then, Path A is the supported route.

## Path C — DataWorks custom image (the chosen production route)

Correction to Path B: the ingestion pipeline **is** authored on DataWorks — in `default_workspace_6na2`
(609583) as 5 `PYODPS3` stage nodes that load the package from the Archive resource
`opensearch_pipeline_production.zip`, on the official **Python 3.7** PyODPS pod. The monitor can't use
that pod directly: the import surface (`reconcile → retriever`, `qa_rollup → pipeline_nodes`) needs
the third-party deps (pymysql/oss2/dashscope/ha3 SDK) at the right ABI, and a 3.7-built zip won't
serve them on a newer Python. So the monitor runs on a **custom image** instead.

Two prerequisites are already handled in code:
- **Cred portability** (reconcile.py `_rds_conn`/`_oss_bucket`): the reconcilers prefer the dedicated
  read-only `fuling_ro` path (`prod_access`, laptop `.env` files) and **fall back to the config/env
  pool** when no `.env` file is present (a DataWorks pod). So the monitor reaches prod from injected
  env vars, no `.env` file needed. `qa_rollup` already used the config/env pool.
- **Self-contained node**: the image bakes the package (`COPY opensearch_pipeline` + `PYTHONPATH`), so
  the node script is a plain `import` + `ops_monitor.main([])` — no `##@resource_reference` boilerplate.

### Steps

1. **Build + push the image** from `deploy/dataworks_monitor.Dockerfile` (build context = repo root).
   - Fill the `BASE` arg with the **exact** official py311 pod-image tag from DataWorks
     console → 镜像管理 (region/version-pinned — a guessed tag fails the build).
   - The build's `pip install` needs network egress: the Serverless RG's VPC must have a NAT/proxy, or
     use the Aliyun PyPI mirror (already the default `PIP_INDEX`).
   - Push to a registry DataWorks can pull (Aliyun ACR). Register under 镜像管理 → 自定义镜像.
   - Limits: ≤10 GB/image, ≤10 images/tenant; custom images are **Serverless-RG only**.
2. **Point the `ops_health_monitor` node at the image** — in the DataStudio console (the MCP can't
   reliably update the node: its id `5203574917819388193` exceeds 2^53 and truncates as a float).
   Select the custom image in **BOTH** 运行配置 **and** 调度配置 — they must match (documented footgun).
3. **Replace the node script** with the clean version (image already has the code on `PYTHONPATH`):
   ```python
   import sys
   print("python:", sys.version)
   try:
       import opensearch_pipeline
       print("opensearch_pipeline:", opensearch_pipeline.__file__)
   except Exception as e:
       print("IMPORT FAILED:", type(e).__name__, e); raise
   from opensearch_pipeline.ops_monitor import main
   sys.exit(main([]))
   ```
4. **Set the cred env vars** on the node/workspace (NOT baked in the image):
   - The same `RAG_*` storage creds your stage nodes already use (RDS / HA3 / OSS). `qa_rollup` writes
     `qa_daily_metrics`, so the env must be **write-capable** (production creds, not `prod_ro`); the
     reconcilers only SELECT/list.
   - `RAG_OPS_ALERT_WEBHOOK` (+ `RAG_OPS_ALERT_SECRET`) — or OBS-4 alerts are a silent no-op.
   - Optional SLO overrides: `RAG_SLO_ANSWER_RATE_MIN` etc. (defaults already ratified).
5. **Validate**: 临时运行 (test run). Expect `opensearch_pipeline: /opt/rag/...` and the
   `[ops_monitor] reconcile_ha3: ok / reconcile_oss: ok / qa_rollup: ...` lines. Exit 0 ok / 2
   drift-or-SLO-breach / 3 error.
6. **Activate**: 提交/发布, then set `recurrence` Pause → Normal. Runs daily 02:30 Asia/Shanghai.

### Updating later
- Code change → rebuild + repush the image (code-in-image). Or switch to the zip-reference approach if
  you want code updates without image rebuilds.
- Dep change → rebuild the image.

## Verifying a run

```bash
# read-only reconcilers anywhere (safe on prod_ro):
RAG_ENV=prod_ro python -m opensearch_pipeline.ops_monitor --no-alert --only reconcile_ha3 reconcile_oss
# full run incl. qa_rollup (write-capable prod env):
RAG_ENV=prod python -m opensearch_pipeline.ops_monitor
```
