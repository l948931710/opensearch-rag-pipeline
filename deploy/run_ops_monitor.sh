#!/usr/bin/env bash
# run_ops_monitor.sh — cron/launchd wrapper for the standing health monitor (ops_monitor).
#
# Defaults to the READ-ONLY reconcilers (CS3 RDS↔HA3 / CS4 OSS↔RDS / CS4b raw_key↔OSS) under
# RAG_ENV=prod_ro — safe, no prod writes. Pass args through to choose jobs, e.g.
#   run_ops_monitor.sh --only reconcile_ha3 reconcile_oss reconcile_raw   (read-only, the default use)
#   run_ops_monitor.sh                                                    (all jobs incl. qa_rollup WRITE)
#
# Env (override per host):
#   RAG_REPO    repo root            (default: parent of this script's dir)
#   RAG_PY      python interpreter   (default: python3 — set to the one with prod deps)
#   RAG_OPS_ENV RAG_ENV to run under (default: prod_ro; use a WRITE-capable env only for qa_rollup)
#   RAG_OPS_LOG append log path      (default: <repo>/scratch/ops_monitor.log)
#   RAG_OPS_ALERT_WEBHOOK / RAG_OPS_ALERT_SECRET  — set to deliver OBS-4 alerts (else logged no-op)
#
# Exit code is ops_monitor's: 0 ok · 2 drift/SLO-breach (page) · 3 error.
set -uo pipefail

REPO="${RAG_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${RAG_PY:-python3}"
LOG="${RAG_OPS_LOG:-$REPO/scratch/ops_monitor.log}"
ENVNAME="${RAG_OPS_ENV:-prod_ro}"

cd "$REPO" || { echo "[$(date '+%F %T')] FATAL: cannot cd $REPO" >>"$LOG"; exit 3; }

# default to the read-only reconcilers if no job args given
if [ "$#" -eq 0 ]; then
  set -- --only reconcile_ha3 reconcile_oss reconcile_raw
fi

echo "[$(date '+%F %T')] ops_monitor start: env=$ENVNAME args='$*'" >>"$LOG"
RAG_ENV="$ENVNAME" "$PY" -m opensearch_pipeline.ops_monitor "$@" >>"$LOG" 2>&1
code=$?
echo "[$(date '+%F %T')] ops_monitor exit=$code" >>"$LOG"
exit $code
