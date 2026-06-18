# DRAFT — judge calibration vs human (② "让分数真正可信")

> ✅ **Code ready, tested.** ❌ **Awaiting your input: ~20–50 human-labeled items.** Until those exist,
> the calibration gate is absent (no `results['judge_calibration']`), so it neither passes nor fails —
> it's simply unmeasured. Once you label, it becomes a real gate.

## Why

The answer-quality gates (faithfulness/correctness/completeness ≥ 4.0, fabrication ≤ 0.05) come from a
3× Claude panel. Without a human anchor they only show "Claude agrees with Claude" — the panel could be
systematically lenient/harsh and still pass. This calibration measures judge-vs-human agreement and
gates on it. (Note: ① already gates *inter-judge* disagreement; this gates judge-vs-*human* validity.)

## Workflow (3 steps)

```python
from eval_harness import judge_calibration as jc, judge
import json

# 1) After an eval run, sample N items into a BLANK human-labeling template (Claude scores omitted on
#    purpose — don't anchor the labeler). bundle = the run's judge_bundle.json.
bundle = json.load(open("eval_harness/reports/run_XXXX/judge_bundle.json"))
jc.build_template(bundle, n=40, out_path="eval_harness/reports/run_XXXX/human_calibration.json")

# 2) A human fills each item's "human": {faithfulness, correctness, completeness, relevance (1-5),
#    fabricated, appropriate_refusal (bool)}. Leave a dim null to skip it. Label WITHOUT looking at
#    the Claude verdicts. (Stratified: includes negatives so fabrication-detection is measured.)

# 3) Compare against the panel verdicts (the same panels merge consumes) → stash into results.json.
panels = json.load(open(".../judge_verdicts.json"))["panels"]
human  = json.load(open(".../human_calibration.json"))
calib  = jc.compare(human, panels)
# put calib under results["judge_calibration"] before report.write(...) — the gate auto-appears.
```

## The gate (`calibration_gate`)
- **PASS** when ≥ `MIN_N` (20) labeled items overlap the panel AND faithfulness/correctness **MAE ≤ 0.75**
  (1–5 scale, vs human) AND fabrication-detection **F1 ≥ 0.70**.
- **FAIL** when the judge is miscalibrated (MAE too high / F1 too low) → `--strict` blocks.
- **pass:None `not_executed`** when < `MIN_N` labels — deliberately NOT a silent pass: a `--strict` run
  that requests calibration with too few labels must not certify the judge.
- Thresholds via env: `RAG_JUDGE_CAL_MAE_MAX` / `RAG_JUDGE_CAL_F1_MIN` / `RAG_JUDGE_CAL_MIN_N`.

## Maintenance
Calibration is regime-specific (like the eval baseline) — re-label/re-run when the judge **model** or
**rubric_version** changes. Suggested cadence: a fresh ~40-item label set per judge-model bump.
