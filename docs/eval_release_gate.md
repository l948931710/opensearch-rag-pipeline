# Eval release gate (the dim9 closed loop) — PRE-DEPLOY placement

> **WIRED, pending activation.** Placement decided 2026-06-17: a **pre-deploy gate** (option 2) — the
> mandatory first step of every serving/ingestion release; a non-zero exit BLOCKS the rollout. Invoke
> via `make release-gate` (→ `deploy/eval_release_gate.sh`). On-demand at deploy time, so the
> laptop-sleep objection doesn't apply (you're awake deploying) — but the box you deploy from still
> needs the prerequisites below (prod-read creds + Alibaba reachability + data repo + authed claude).
> Items 1–4 + ① (inter-judge gate) + ② (judge-calibration draft) hardened the framework; THIS gate
> enforces it. Two activation steps remain (§First run). Artifacts: `deploy/eval_release_gate.sh`,
> `eval_harness/run_judge.py`, `run_eval baseline-freeze`/`--baseline`, `make release-gate` /
> `make eval-baseline-freeze`.

## Why this exists
Without an automated, blocking `run_eval --strict` + judge-merge, dim9 isn't 8 — the gates exist but
nothing enforces them recurringly. This gate runs the full L0–L6 eval, auto-judges answer quality with
a Claude panel, compares to a frozen baseline, and **exits non-zero to block a release**.

## Where it must NOT run
Not a personal **Mac LaunchAgent** — sleep, flaky network, and local-only creds make it an unreliable
release gate (and `~/Downloads` + TCC bit us already). Pick one of:
1. **Dedicated self-hosted CI runner inside the Alibaba VPC** (recommended) — a persistent box with
   prod-read creds + the data repo + claude CLI; the GitHub Actions eval job targets it (the public CI
   runner can't reach prod). Blocks PRs/merges.
2. ✅ **CHOSEN — Deploy-pipeline pre-deploy step**: `make release-gate` as the mandatory first step of
   the SAE serving / DataWorks ingestion release procedure; non-zero exit BLOCKS the rollout. Best
   "blocks release" fit. (Caveat vs option 1: it only runs at deploy time, so it does not catch drift
   *between* releases — pair with an occasional manual run or add option 3 later for drift monitoring.)
3. **Cloud cron node** (ECS/function) — nightly run + OBS-4 alert on failure. Easiest, but monitoring
   not a hard gate (a regression can still ship; you just get paged).

## Prerequisites on the runner
- prod-**read** creds the eval forces live (envboot): DashScope key, HA3 endpoint/instance/access, RDS
  read (fuling_ro). **Read-only** — the eval never writes prod.
- Alibaba VPC reachability / IP-whitelist for HA3 + RDS + DashScope.
- The out-of-repo data repo (`~/.../opensearch-rag-data/eval_samples`) for L4/L6, or `EVAL_L4_*`/`EVAL_L6_*` envs.
- `claude` CLI authed (for the auto-judge). Set `RAG_CLAUDE_BIN`.

## Flow (why `run` is NOT strict)
Because the new strict semantics make `run --strict` **intentionally fail pre-judge** (answer
correctness can't be certified without the judge), the gate is the **merge** strict:
```
run_eval run              (no --strict)         → report.json + judge_bundle*.json
run_judge                 (Claude panel x3)     → judge_verdicts*.json
run_eval merge --strict --baseline             → EXIT CODE = the gate
```
`deploy/eval_release_gate.sh` does all of this; its exit code is the gate.

## First run: freeze the baseline
The regression gate needs a frozen, regime-tagged baseline. After the FIRST clean gate:
```
RAG_PY=… python -m eval_harness.run_eval baseline-freeze \
    --results /tmp/eval_release_*/report.json --baseline eval_harness/goldset/baseline.json
```
Commit `baseline.json`. Refreeze whenever the regime changes (eval-set / models / reranker / **fusion** /
thresholds) — the gate refuses to compare across regimes (N/A "refreeze"), so a stale baseline never
silently passes or fails.

## What blocks (strict failure semantics — implemented + tested)
- any hard gate `pass==False`; L6 `NO_GO_DEFECT`; EVAL-2 manifest drift;
- `not_executed` N/A — a HARD gate that couldn't run (sample/config shortfall, e.g. L4-serving N<5);
- **answer correctness not judged** (L3 ran, no judge merge);
- **fusion ≠ weighted** (calibration-regime guard);
- **per layer/subset baseline regression > delta** (even if the absolute threshold still passes).
- `expected_na` (e.g. L5 on the all-public corpus) does **not** block.

## Still open AFTER this runner (to fully trust the numbers — beyond items 1–4)
- **Judge calibration (② DRAFTED)**: inter-judge stdev is now GATED (① `1e9df48`, L3 + L6 panels).
  Judge-vs-human validity: `eval_harness/judge_calibration.py` + `docs/judge_calibration_DRAFT.md` are
  ready — awaiting ~20–50 human labels to activate that gate (the auto-judge is Claude-vs-Qwen, no
  self-grading, but its absolute validity is unanchored until then).
- **Image-binding circularity**: the strict "which-image" key is the extractor's own `image_index`
  (acknowledged, bounded) — re-derive from a raw-reconstructible coordinate for a held-out subset.
- **Gold-set de-skew**: add IT/production/quality/sales/marketing depts, typed hard-query classes,
  ≥5 image + more negative cases (so `not_executed` shortfalls stop firing legitimately).

## Cost note
Auto-judge = panels × ceil(items/batch) claude calls. golden_full (251) × 3 panels × batch 20 ≈ 39
calls/run. Tune `--panels` / `--batch`; nightly is fine, per-PR may want a smaller goldset.
