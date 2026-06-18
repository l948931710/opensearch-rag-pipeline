"""② Judge calibration — anchor the 3× Claude panel against HUMAN labels.

> DRAFT: the code (template generator + consistency math + gate) is ready and tested; the missing
> input is ~30–50 human-labeled items. Run `build_template` → label by hand → `compare` → the gate.

The answer-quality gates (faithfulness/correctness/completeness >= 4.0, fabrication) come from a 3×
Claude panel chosen because answers are Qwen-generated (avoids self-grading). But their ABSOLUTE
validity is unverified against humans — the panel could be systematically lenient/harsh and still
"pass". This module measures judge-vs-human agreement and turns it into a gate:

  build_template(bundle, n, out)  → sample N judged items into a blank human-labeling template.
       The Claude scores are deliberately NOT included (avoid anchoring the human labeler).
  compare(human_labels, panels)   → per-dimension MAE + within-±1 rate + fabrication P/R/F1 +
       an overall `calibrated` verdict (judge mean per qid = mean over the 3 panels).
  calibration_gate(calib)          → a report gate: uncalibrated judge (MAE too high / fabrication
       F1 too low) FAILS; too few labels → pass:None na_reason 'not_executed' (must be resolved,
       not silently passed) so a --strict run that REQUESTS calibration can't pass unmeasured.

Re-run when the judge model or rubric_version changes (the calibration is regime-specific, like the
eval baseline). Thresholds via env: RAG_JUDGE_CAL_MAE_MAX (0.75), RAG_JUDGE_CAL_F1_MIN (0.70),
RAG_JUDGE_CAL_MIN_N (20).
"""
from __future__ import annotations

import json
import os
import random
from typing import Dict, List

# 1-5 numeric dims the human labels + we compare against the panel mean.
SCORE_DIMS = ("faithfulness", "correctness", "completeness", "relevance")
MAE_MAX = float(os.environ.get("RAG_JUDGE_CAL_MAE_MAX", "0.75"))   # per-dim mean abs err on 1-5
F1_MIN = float(os.environ.get("RAG_JUDGE_CAL_F1_MIN", "0.70"))     # fabrication-detection F1
MIN_N = int(os.environ.get("RAG_JUDGE_CAL_MIN_N", "20"))           # min labeled items to gate


def build_template(bundle: List[Dict], n: int, out_path: str, *, seed: int = 0) -> List[Dict]:
    """Sample N items (stratified by kind) from a judge bundle into a blank human-labeling template.
    Claude scores are intentionally omitted to avoid anchoring the labeler."""
    pos = [b for b in bundle if (b.get("kind") or "positive") != "negative"]
    neg = [b for b in bundle if (b.get("kind") or "positive") == "negative"]
    rng = random.Random(seed)
    rng.shuffle(pos)
    rng.shuffle(neg)
    n_neg = min(len(neg), max(1, n // 4)) if neg else 0
    picks = neg[:n_neg] + pos[: max(0, n - n_neg)]
    tmpl = []
    for b in picks:
        tmpl.append({
            "qid": b.get("qid"),
            "kind": b.get("kind", "positive"),
            "question": b.get("question") or b.get("query"),
            "answer": b.get("answer"),
            "context_for_labeler": b.get("context") or b.get("context_snippet"),
            "gold_for_labeler": b.get("gold") or b.get("gold_points"),
            # human fills these (1-5 ints; booleans). Leave null = unlabeled (skipped in compare).
            "human": {d: None for d in SCORE_DIMS} | {"fabricated": None, "appropriate_refusal": None},
        })
    if out_path:
        json.dump(tmpl, open(out_path, "w"), ensure_ascii=False, indent=1)
    return tmpl


def _panel_means(panels: List[Dict]) -> Dict[str, Dict]:
    """qid → {dim: mean over panels, 'fabricated': majority-vote bool}."""
    by_qid: Dict[str, Dict[str, list]] = {}
    for p in panels:
        for v in (p.get("verdicts") or p if isinstance(p, list) else p.get("verdicts", [])):
            q = v.get("qid")
            if q is None:
                continue
            slot = by_qid.setdefault(q, {d: [] for d in SCORE_DIMS} | {"fabricated": []})
            for d in SCORE_DIMS:
                if isinstance(v.get(d), (int, float)):
                    slot[d].append(float(v[d]))
            if v.get("fabricated") is not None:
                slot["fabricated"].append(1.0 if v["fabricated"] else 0.0)
    out = {}
    for q, slot in by_qid.items():
        out[q] = {d: (sum(slot[d]) / len(slot[d]) if slot[d] else None) for d in SCORE_DIMS}
        fb = slot["fabricated"]
        out[q]["fabricated"] = (sum(fb) / len(fb) >= 0.5) if fb else None
    return out


def compare(human_labels: List[Dict], panels: List[Dict]) -> Dict:
    """Judge-vs-human agreement on the overlapping qids. Returns per-dim MAE/within-±1 + fabrication
    P/R/F1 + overall `calibrated`. Only items with a non-null human label for a dim count for it."""
    cmean = _panel_means(panels)
    per_dim = {d: {"abs_errs": [], "within1": 0, "n": 0} for d in SCORE_DIMS}
    tp = fp = fn = tn = 0
    n_items = 0
    for h in human_labels:
        q = h.get("qid")
        hum = h.get("human") or {}
        cj = cmean.get(q)
        if not cj:
            continue
        n_items += 1
        for d in SCORE_DIMS:
            hv, cv = hum.get(d), cj.get(d)
            if isinstance(hv, (int, float)) and isinstance(cv, (int, float)):
                e = abs(hv - cv)
                per_dim[d]["abs_errs"].append(e)
                per_dim[d]["within1"] += 1 if e <= 1.0 else 0
                per_dim[d]["n"] += 1
        hf, cf = hum.get("fabricated"), cj.get("fabricated")
        if hf is not None and cf is not None:
            if cf and hf:
                tp += 1
            elif cf and not hf:
                fp += 1
            elif not cf and hf:
                fn += 1
            else:
                tn += 1
    dims = {}
    for d, s in per_dim.items():
        nn = s["n"]
        dims[d] = {"mae": round(sum(s["abs_errs"]) / nn, 3) if nn else None,
                   "within1_rate": round(s["within1"] / nn, 3) if nn else None, "n": nn}
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else (0.0 if (tp + fp + fn) else None)
    return {"n_items": n_items, "per_dim": dims,
            "fabrication": {"precision": prec, "recall": rec,
                            "f1": round(f1, 3) if f1 is not None else None,
                            "tp": tp, "fp": fp, "fn": fn, "tn": tn}}


def calibration_gate(calib: Dict) -> Dict:
    """report gate. PASS = enough labels AND every measured key-dim MAE <= MAE_MAX AND fabrication
    F1 >= F1_MIN. Too few labels → pass:None na_reason 'not_executed' (a requested-but-unmeasured
    calibration must not silently pass under --strict)."""
    n = calib.get("n_items", 0)
    if n < MIN_N:
        return {"target": f">= {MIN_N} human-labeled items overlapping the panel",
                "value": f"only {n} labeled", "pass": None, "na_reason": "not_executed",
                "notes": "author more human calibration labels (judge validity unverified)"}
    maes = {d: (calib["per_dim"].get(d) or {}).get("mae") for d in ("faithfulness", "correctness")}
    f1 = (calib.get("fabrication") or {}).get("f1")
    bad_mae = [d for d, m in maes.items() if m is not None and m > MAE_MAX]
    f1_bad = (f1 is not None and f1 < F1_MIN)
    ok = (not bad_mae) and (not f1_bad)
    return {"target": f"faithfulness/correctness MAE <= {MAE_MAX} & fabrication F1 >= {F1_MIN} (vs human)",
            "value": f"MAE={maes}, fabrication_F1={f1}", "pass": bool(ok),
            **({} if ok else {"notes": f"miscalibrated judge: high-MAE dims={bad_mae}, f1_low={f1_bad}"})}
