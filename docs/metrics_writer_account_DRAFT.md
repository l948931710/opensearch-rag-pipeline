# DRAFT — least-privilege metrics-writer account for OBS-5 `qa_rollup`

> ⚠️ **DRAFT / NOT APPLIED.** This documents a proposed DB account + config so the OBS-5 nightly
> `qa_rollup` (the only **write** job in `ops_monitor`) can run in a standing cron **without**
> `fuling_admin` creds sitting in a scheduled job. **Do not run the SQL or create the account until
> reviewed.** (User decision pending — drafted 2026-06-17.)

## Why

The daily LaunchAgent runs the **read-only** reconcilers (`reconcile_ha3` / `reconcile_oss`) under
`prod_ro` (account `fuling_ro`) — zero write risk. `qa_rollup` (OBS-5) is different: it **writes**
`fuling_operation.qa_daily_metrics`. Putting `fuling_admin` (full RW) in a standing cron would mean a
leaked cron env = full prod write. A dedicated least-privilege account caps the blast radius to the
one table the job touches.

## Exact footprint (verified against `opensearch_pipeline/qa_rollup.py`)

| op | object | why |
|----|--------|-----|
| SELECT | `fuling_operation.qa_session_log` | `run_rollup` reads one Beijing-day's rows to aggregate |
| INSERT, UPDATE, (SELECT) | `fuling_operation.qa_daily_metrics` | `_upsert_daily` `INSERT … ON DUPLICATE KEY UPDATE` |
| — | (a `SELECT DATE(UTC_TIMESTAMP() …)` — no table) | resolves "yesterday" without host-clock skew |

Nothing else. No `fuling_knowledge`. No `DELETE`. No DDL. No other tables. `compute_daily_metrics` is
a pure function (no I/O).

## Proposed grants (DRAFT SQL — review + run manually as `fuling_admin` when ready)

```sql
-- Restrict the host to the cron box's egress IP if it's static; else '%' + rely on the RDS IP
-- whitelist (recommended) to bound where these creds can connect from.
CREATE USER 'fuling_metrics'@'%' IDENTIFIED BY '<STRONG_RANDOM_PASSWORD>';

-- read the source, write ONLY the rollup table (the entire blast radius)
GRANT SELECT                 ON fuling_operation.qa_session_log  TO 'fuling_metrics'@'%';
GRANT SELECT, INSERT, UPDATE ON fuling_operation.qa_daily_metrics TO 'fuling_metrics'@'%';
-- deliberately NO: DELETE, DROP/ALTER/CREATE, *.* , fuling_knowledge.*, any other fuling_operation table
FLUSH PRIVILEGES;

-- verify the grant is exactly the above:
SHOW GRANTS FOR 'fuling_metrics'@'%';
```

## Config overlay (`.env.metrics` — DRAFT, do not commit secrets)

`qa_rollup` connects via `_get_db_conn` (the config pool), and writes `qa_daily_metrics` **unqualified**
→ the connection's default DB must be `fuling_operation`.

```dotenv
# write-capable overlay for the OBS-5 qa_rollup cron ONLY (least-privilege account)
RAG_SIMULATE=false
RAG_RDS_HOST=rm-bp15j7wekd5738f093o.rwlb.rds.aliyuncs.com
RAG_RDS_PORT=3306
RAG_RDS_USER=fuling_metrics
RAG_RDS_PASSWORD=<STRONG_RANDOM_PASSWORD>
RAG_RDS_DATABASE=fuling_operation          # so unqualified qa_daily_metrics resolves
RAG_READONLY=false                          # this job must write
# DashScope key required ONLY to satisfy the production config guard (qa_rollup does NOT call DashScope):
DASHSCOPE_API_KEY=<key>
```

### The one wart to resolve at creation time (env_guard / config guard)

To get a **write-capable** pool against the prod RDS, `env_guard._pool_readonly_declared` requires the
config `environment` to be **production** — otherwise a non-prod label pointing at a prod target is
forced `SESSION READ ONLY` (unless a *same-day* `RAG_DESTRUCTIVE_PROD_ACK`, which a standing cron
can't hardcode). But `environment=production` then trips the **production security guard**
(`config.py`), which hard-raises if no DashScope key is set / if any model resolves to Gemini — hence
the `DASHSCOPE_API_KEY` above even though `qa_rollup` never calls it.

Two ways to handle (decide at creation):
1. **Accept the wart** — put a (any valid) DashScope key in `.env.metrics`; the DB-layer grant is the
   real least-privilege protection regardless of the app guard. Simplest, no code change.
2. **Small code accommodation (future)** — give `qa_rollup` a dedicated guarded RW connection
   (mirroring `prod_access.get_prod_rw_conn`) so it doesn't load the full production config guard.
   Cleaner, but a code change — out of scope for "draft only".

## Scheduling (separate from the read-only agent)

Add a SECOND LaunchAgent / crontab entry for the write job (keep it OFF the read-only `prod_ro` agent):

```bash
# nightly, after the read-only monitor; write-capable metrics account
RAG_ENV=metrics /usr/bin/python3 -m opensearch_pipeline.ops_monitor --only qa_rollup
# or just the rollup module:  RAG_ENV=metrics /usr/bin/python3 -m opensearch_pipeline.qa_rollup
```
(macOS: a second plist `com.fuling.qa-rollup`, same FDA'd `/usr/bin/python3`. Back-fill history once
interactively: `… qa_rollup --date <YYYY-MM-DD>` per day before scheduling.)

## Checklist before this goes live (none done yet)
- [ ] DBA review of the grants above
- [ ] Create `fuling_metrics` + grants (run the SQL as admin)
- [ ] RDS IP-whitelist the cron host; restrict the user host if the IP is static
- [ ] Author `.env.metrics` (secrets out of git)
- [ ] One interactive back-fill run, confirm a `qa_daily_metrics` row lands
- [ ] Add the second (write) agent; confirm SLO verdict + OBS-4 alert path
