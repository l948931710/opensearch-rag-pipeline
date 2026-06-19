"""Frozen-baseline regression gating for the eval harness (EVAL item 1).

Compares a run's metrics to a committed baseline PER layer + subset (not just total recall), and ONLY
when the run REGIME matches the baseline's (eval-set sha, code commit, models, reranker, fusion mode,
threshold version) — so a delta is never computed across different run conditions. A per-metric drop
beyond `delta` is a FAIL (caught even when the absolute threshold still passes). Higher-is-better vs
lower-is-better is inferred from the metric name.

Read-only. The baseline file is small + committed; freeze with `run_eval baseline-freeze`.
"""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

# metric-name fragments whose VALUE is better when LOWER (rates / latencies / drift)
_LOWER_BETTER = ("refus", "leak", "dangling", "orphan", "dup", "fabricat", "latency", "drift",
                 "p95", "p99", "miss", "error")
DEFAULT_DELTA = 0.03


def _direction(path: str) -> str:
    return "lower" if any(t in path.lower() for t in _LOWER_BETTER) else "higher"


# ── advisory/trend baseline metrics ───────────────────────────────────────────────────────────
# The smallest explicit mechanism (the schema stores flat {path: float} with no per-metric advisory
# flag): a registry of metric paths whose baseline DELTA is REPORTED (visible) but must NEVER hard-
# block --strict. It mirrors the advisory:True gates in report.py::build_gates — today the only
# advisory metric extract_metrics yields is the L4-srv orphan rate (its absolute gate is soft/trend:
# referenced-only rendering means unreferenced candidate images aren't shown to users, so a high
# orphan rate on photo-dense docs is a trend signal, not a defect). Everything else — recall, jaccard,
# dup, marker_validity, dangling, judge scores, refusal/leak rates — stays a FULLY blocking hard
# metric. Keep this in sync with the advisory:True gates in report.py (test pins the exact set).
ADVISORY_METRICS = frozenset({"l4srv.orphan_rate"})


def _is_advisory(path: str) -> bool:
    return path in ADVISORY_METRICS


def extract_metrics(results: Dict) -> Dict[str, float]:
    """Flatten the comparable metrics across layers + subsets → {path: float}. The subset breakdowns
    (by_module / by_source / by_difficulty, per-format, ACL public count) are what let a local
    regression surface even when the aggregate still clears the bar."""
    m: Dict[str, float] = {}

    def put(k, v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            m[k] = float(v)

    l1 = results.get("l1") or {}
    rk = l1.get("ranking") or {}
    for k in ("recall@1", "recall@5", "mrr", "ndcg@5", "ndcg@10"):
        put(f"l1.ranking.{k}", rk.get(k))
    for grp in ("by_module", "by_source", "by_difficulty"):
        for sub, d in (l1.get(grp) or {}).items():
            if isinstance(d, dict):
                for k in ("recall@1", "recall@5", "mrr"):
                    put(f"l1.{grp}.{sub}.{k}", d.get(k))
    put("l1.n_positive_public", l1.get("n_positive_public"))  # ACL/public coverage signal

    l3 = (results.get("l3") or {}).get("deterministic") or {}
    pos = l3.get("positive") or {}
    for k in ("over_refusal_rate", "source_leak_rate", "mean_keyword_coverage"):
        put(f"l3.{k}", pos.get(k))

    ing = ((results.get("l4") or {}).get("ingestion") or {}).get("deterministic") or {}
    for fmt in ("pdf", "xlsx", "docx", "pptx"):
        put(f"l4ing.jaccard.{fmt}", ing.get(f"binding_jaccard_{fmt}"))  # 图文 ingestion subset
    put("l4ing.img_dup_p95", ing.get("img_dup_factor_p95"))
    srv = (results.get("l4") or {}).get("aggregate") or {}
    for k in ("marker_validity", "dangling_ref_rate", "orphan_rate"):
        put(f"l4srv.{k}", srv.get(k))

    j = (results.get("judge") or {}).get("aggregate") or {}
    posj = j.get("positives") or {}
    for k in ("faithfulness", "correctness", "completeness"):
        put(f"judge.{k}", (posj.get(k) or {}).get("mean"))
    return m


def regime_of(results: Dict) -> Dict:
    return (results.get("meta") or {}).get("regime") or {}


_REGIME_KEYS = ("eval_set_sha", "fusion", "rerank_enable", "llm_model",
                "embedding_model", "reranker_models", "threshold_version")


def regime_matches(base_regime: Dict, cur_regime: Dict) -> Tuple[bool, List[str]]:
    diffs = [k for k in _REGIME_KEYS if base_regime.get(k) != cur_regime.get(k)]
    return (not diffs, diffs)


def compare(baseline: Dict, results: Dict, delta: float = DEFAULT_DELTA) -> Dict:
    """Return regression gate(s). Regime mismatch → a single N/A gate (expected_na: can't compare
    across regimes) — NOT a free pass on the real check, just a loud 'refreeze for this regime'."""
    base_regime = baseline.get("regime") or {}
    ok, diffs = regime_matches(base_regime, regime_of(results))
    if not ok:
        return {"baseline regression (regime)": {
            "target": "baseline regime must match run regime to compare",
            "value": f"REGIME MISMATCH on {diffs} — baseline not comparable",
            "pass": None, "na_reason": "expected_na",
            "notes": "refreeze the baseline for the current regime (run_eval baseline-freeze)"}}

    cur = extract_metrics(results)
    base_m = baseline.get("metrics") or {}
    delta = baseline.get("delta", delta)
    # Split regressions by classification: HARD deltas block --strict; ADVISORY/trend deltas are
    # reported visibly but never block (registry above). An advisory metric that REGRESSES must not
    # silently become a hard blocker; a hard metric that regresses must still fail strict mode.
    hard_reg: List[str] = []
    adv_reg: List[str] = []
    hard_n = adv_n = 0
    for path, bv in base_m.items():
        cv = cur.get(path)
        if cv is None:
            continue  # metric absent this run; coverage/not-executed handled by the strict guards
        regressed = (cv < bv - delta) if _direction(path) == "higher" else (cv > bv + delta)
        if _is_advisory(path):
            adv_n += 1
            if regressed:
                adv_reg.append(f"{path}: {bv}→{cv}")
        else:
            hard_n += 1
            if regressed:
                hard_reg.append(f"{path}: {bv}→{cv}")
    # hard gate FIRST (callers that take .values()[0] expect the blocking gate)
    gates = {"baseline regression (hard metrics, no drop > delta)": {
        "target": f"no HARD per-metric regression > {delta} vs frozen baseline ({hard_n} compared)",
        "value": (f"{len(hard_reg)} regressed: {hard_reg[:8]}" if hard_reg else f"clean ({hard_n} hard metrics)"),
        "pass": (len(hard_reg) == 0)}}
    # advisory/trend gate — emitted only when advisory metrics are actually compared; VISIBLE
    # (pass reflects drift) but advisory=True so _strict_failures skips it (never blocks).
    if adv_n:
        gates["baseline regression (advisory/trend — visible, non-blocking)"] = {
            "target": f"advisory: trend metrics may drift, never blocks ({adv_n} tracked)",
            "value": (f"{len(adv_reg)} advisory drift: {adv_reg[:8]}" if adv_reg else f"clean ({adv_n} advisory metrics)"),
            "pass": (len(adv_reg) == 0),
            "advisory": True}
    return gates


def freeze(results: Dict, path: str, delta: float = DEFAULT_DELTA) -> Dict:
    base = {
        "frozen_at": (results.get("meta") or {}).get("timestamp"),
        "run_id": (results.get("meta") or {}).get("run_id"),
        "delta": delta,
        "regime": regime_of(results),
        "metrics": extract_metrics(results),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False, indent=1, default=str)
    return base
