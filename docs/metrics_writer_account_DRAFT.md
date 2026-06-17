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

## Config overlay (`.env.metrics` — gitignored; do not commit secrets)

`qa_rollup` connects via `_get_db_conn` (the config pool) and writes `qa_daily_metrics` **unqualified**
→ the connection's default DB must be `fuling_operation`. The base `.env` (loaded first) already
provides the shared DashScope key + HA3 config, so `.env.metrics` is just `.env.production` with the
RDS overlay swapped to the least-privilege account — **three edited lines**:

```bash
cp .env.production .env.metrics        # .env.metrics is now gitignored
# then edit exactly three lines in .env.metrics:
#   RAG_RDS_USER=fuling_metrics
#   RAG_RDS_PASSWORD=<the password you set in CREATE USER>
#   RAG_RDS_DATABASE=fuling_operation       # was fuling_knowledge
# keep RAG_ENVIRONMENT=production (inherited from .env.production); do NOT set RAG_READONLY=true.
```

### Why it works (guards auto-satisfied — the wart is smaller than first thought)

- **Write-capable pool:** `env_guard._pool_readonly_declared` makes the pool writable only when
  `environment=production` (else a non-prod label → prod RDS is forced `SESSION READ ONLY` without a
  *same-day* ack a cron can't hold). `.env.production` already sets `RAG_ENVIRONMENT=production`, so the
  copy gives a write-capable pool.
- **Production config guard** (requires a DashScope key; models must resolve to Qwen not Gemini): met
  **automatically by the base `.env`** (shared DashScope key + HA3 endpoint/table) — you do NOT add a
  key to `.env.metrics`. `qa_rollup` never calls DashScope/HA3; they're present only so the guard passes.
- **DB-layer least-privilege is the real protection:** even though the config is "production-shaped",
  the `fuling_metrics` account can physically only SELECT `qa_session_log` + write `qa_daily_metrics`.
- (A future code cleanup could give `qa_rollup` a dedicated guarded RW connection so it doesn't load the
  full production config at all — nice-to-have, not needed for this.)

## Scheduling (separate from the read-only agent — write-agent already built)

`deploy/com.fuling.qa-rollup.plist` is committed and ready: a SECOND LaunchAgent (label
`com.fuling.qa-rollup`) that runs `ops_monitor --only qa_rollup` under `RAG_ENV=metrics`, daily 02:50
(after the read-only monitor at 02:30), reusing the same FDA'd `/usr/bin/python3`. It is **NOT
installed** — load it only AFTER the account + `.env.metrics` exist (else `RAG_ENV=metrics` falls back
to `.env` only → wrong RDS). Install:
```bash
cp deploy/com.fuling.qa-rollup.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.fuling.qa-rollup.plist
launchctl kickstart gui/$(id -u)/com.fuling.qa-rollup   # test
```
Back-fill history once interactively before (or after) scheduling:
```bash
RAG_ENV=metrics /usr/bin/python3 -m opensearch_pipeline.qa_rollup --date <YYYY-MM-DD>   # per past day
```

## Checklist

**Prepared (Claude — done):**
- [x] Exact minimal grants verified against `qa_rollup.py` (one-table blast radius)
- [x] `.env.metrics` added to `.gitignore` (secrets can't leak)
- [x] Write-agent `deploy/com.fuling.qa-rollup.plist` committed (built, NOT installed)
- [x] Config-guard path confirmed: base `.env` auto-satisfies the production guard; `.env.metrics` = `cp` + 3 lines

**You must run (credential / access-control steps Claude is not permitted to perform):**
- [ ] DBA review of the grants
- [ ] `openssl rand -base64 24` → password; run the `CREATE USER` + `GRANT` SQL as `fuling_admin`
- [ ] RDS IP-whitelist the cron host; restrict the user host if its egress IP is static
- [ ] `cp .env.production .env.metrics`, set the 3 RDS lines (user/password/database)

**Then Claude can verify (on your go):**
- [ ] back-fill run + confirm a `qa_daily_metrics` row lands + SLO verdict is sane
- [ ] install + kickstart `com.fuling.qa-rollup`; confirm it writes via launchd
