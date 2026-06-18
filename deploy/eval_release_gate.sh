#!/usr/bin/env bash
# eval_release_gate.sh — DRAFT release gate for the HA3 RAG eval (dim9 closed-loop).
#
# Host-agnostic: a self-hosted CI runner INSIDE the Alibaba VPC, a deploy-pipeline pre-deploy step,
# or a long-running cloud cron node. NOT a personal Mac LaunchAgent (sleep/network/local-creds make it
# an unreliable release gate). Exit code IS the gate: non-zero => block the release.
#
# Flow (matches the strict semantics — `run --strict` intentionally fails pre-judge, so the gate is the
# MERGE strict, with judge verdicts + baseline regression all enforced together):
#   1) preflight: creds + Alibaba reachability + out-of-repo data + claude CLI
#   2) run_eval run      (NO --strict) → report.json + judge_bundle*.json
#   3) run_judge         → judge_verdicts*.json (auto Claude panel)
#   4) run_eval merge --strict --baseline → EXIT CODE = release gate
#
# Required env (set on the runner; never commit secrets):
#   prod-READ creds the eval needs (envboot forces live): DashScope key, HA3 endpoint/instance/creds,
#   RDS read (fuling_ro). Plus:
#   RAG_REPO (repo root)             RAG_PY (python with deps)        RAG_CLAUDE_BIN (authed claude)
#   RAG_EVAL_GOLDSET (default golden_full.json)   RAG_EVAL_BASELINE (default goldset/baseline.json)
#   RAG_EVAL_DATA (~/.../eval_samples for L4/L6)  RAG_EVAL_PANELS (default 3)
set -uo pipefail

REPO="${RAG_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${RAG_PY:-python3}"
GOLDSET="${RAG_EVAL_GOLDSET:-$REPO/eval_harness/goldset/golden_full.json}"
BASELINE="${RAG_EVAL_BASELINE:-$REPO/eval_harness/goldset/baseline.json}"
PANELS="${RAG_EVAL_PANELS:-3}"
RUNDIR="${RAG_EVAL_OUTDIR:-/tmp/eval_release_$(date +%Y%m%d_%H%M%S)}"
LAYERS="${RAG_EVAL_LAYERS:-l0,l1,l2,l3,l4,l5,l6}"
cd "$REPO" || { echo "FATAL: cannot cd $REPO"; exit 3; }

bl_arg=(); [ -f "$BASELINE" ] && bl_arg=(--baseline "$BASELINE")

echo "== [1/4] preflight =="
command -v "${RAG_CLAUDE_BIN:-claude}" >/dev/null 2>&1 || { echo "FATAL: claude CLI not found (judge merge needs it)"; exit 3; }
"$PY" -c "import opensearch_pipeline, eval_harness" 2>/dev/null || { echo "FATAL: package not importable by $PY"; exit 3; }
[ -f "$GOLDSET" ] || { echo "FATAL: goldset missing: $GOLDSET"; exit 3; }
[ -f "$BASELINE" ] || echo "WARN: no baseline ($BASELINE) — regression gate skipped; freeze one after the first clean gate:
  $PY -m eval_harness.run_eval baseline-freeze --results $RUNDIR/report.json --baseline $BASELINE"

echo "== [2/4] run_eval run (no strict) → $RUNDIR =="
"$PY" -m eval_harness.run_eval run --goldset "$GOLDSET" --layers "$LAYERS" --outdir "$RUNDIR" "${bl_arg[@]}" || true

echo "== [3/4] auto-judge (Claude panel x$PANELS) =="
"$PY" -m eval_harness.run_judge --bundle "$RUNDIR/judge_bundle.json" \
      --out "$RUNDIR/judge_verdicts.json" --panels "$PANELS" --rubric answer || { echo "FATAL: answer judge failed"; exit 3; }
if [ -f "$RUNDIR/judge_bundle_chunk.json" ]; then
  "$PY" -m eval_harness.run_judge --bundle "$RUNDIR/judge_bundle_chunk.json" \
        --out "$RUNDIR/judge_verdicts_chunk.json" --panels "$PANELS" --rubric chunk || echo "WARN: chunk judge failed"
fi

echo "== [4/4] run_eval merge --strict (THE gate) =="
"$PY" -m eval_harness.run_eval merge --results "$RUNDIR/report.json" \
      --verdicts "$RUNDIR/judge_verdicts.json" --strict "${bl_arg[@]}"
gate=$?
echo "== release gate exit=$gate (0=ship, non-zero=BLOCK; report: $RUNDIR/report.md) =="
exit $gate
