"""Claude judge panel — rubric, output schema, and verdict merge.

WHY Claude (not Qwen): the system generates answers with Qwen, so a Qwen judge would be
grading itself (self-evaluation bias). An INDEPENDENT model is the fair choice. We run a
panel of N independent Claude judges over the same blinded bundle and report inter-judge
agreement alongside the scores.

Fairness controls:
  - The judge sees ONLY: the question, the retrieved context, the gold answer points, and
    the answer — never the model name.
  - Anchored 1-5 rubric; faithfulness is scored against the PROVIDED context (penalizes
    fabrication beyond context), correctness against the gold points.
  - Negatives (unanswerable / out-of-corpus) are scored on correct refusal & no fabrication.
"""
from __future__ import annotations

from typing import Dict, List

from .metrics import bootstrap_ci, mean

# Rubric shown to each Claude judge (the Workflow injects per-item bundle data).
JUDGE_RUBRIC = """You are an impartial QA evaluator for an enterprise Chinese knowledge-base
assistant (manufacturing company). You are given, per item: the user QUESTION, the retrieved
CONTEXT passages the assistant was allowed to use, the GOLD answer points (reference), the
item KIND (positive = answerable from corpus; negative = should NOT be answerable), and the
assistant's ANSWER. Do not use outside knowledge; judge only against the provided context and
gold points. Score each dimension 1-5 (integers):

- faithfulness: is every claim in the ANSWER supported by the CONTEXT? 5 = fully grounded,
  1 = fabricated / contradicts context. (For negatives, an answer that invents facts = 1.)
- correctness: does the ANSWER agree with the GOLD points / facts? 5 = correct, 1 = wrong.
  (For negatives with no gold, a correct refusal = 5; a confident wrong answer = 1.)
- completeness: does it cover the key GOLD points the question asks for? 5 = complete,
  1 = misses everything. (For negatives, N/A -> set 3.)
- relevance: does it actually address the question (vs evasive/off-topic)? 5 = on-point.

Also set:
- fabricated (boolean): did the answer state facts not supported by context?
- appropriate_refusal (boolean): ONLY for negatives — did it correctly decline / say the
  info isn't available, instead of inventing an answer? (positives: set true.)
- overall (1-5): holistic answer quality given the kind.
- verdict: "pass" | "partial" | "fail".
- rationale: one sentence.
Return strictly the structured object."""

# JSON schema for the structured judge output (one object per bundle item).
VERDICT_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["qid", "faithfulness", "correctness", "completeness", "relevance",
                 "fabricated", "appropriate_refusal", "overall", "verdict", "rationale"],
    "properties": {
        "qid": {"type": "string"},
        "faithfulness": {"type": "integer", "minimum": 1, "maximum": 5},
        "correctness": {"type": "integer", "minimum": 1, "maximum": 5},
        "completeness": {"type": "integer", "minimum": 1, "maximum": 5},
        "relevance": {"type": "integer", "minimum": 1, "maximum": 5},
        "fabricated": {"type": "boolean"},
        "appropriate_refusal": {"type": "boolean"},
        "overall": {"type": "integer", "minimum": 1, "maximum": 5},
        "verdict": {"type": "string", "enum": ["pass", "partial", "fail"]},
        "rationale": {"type": "string"},
    },
}

_DIMS = ["faithfulness", "correctness", "completeness", "relevance", "overall"]


def merge_panel(bundle: List[Dict], panels: List[Dict]) -> Dict:
    """Merge N judge panels into per-query + aggregate scores with CIs + agreement.

    panels: [{"judge": str, "verdicts": [VERDICT_ITEM, ...]}, ...]
    """
    kind_by_qid = {b["qid"]: b["kind"] for b in bundle}
    # index verdicts: qid -> list of per-judge dicts
    by_qid: Dict[str, List[Dict]] = {}
    for p in panels:
        for v in p.get("verdicts", []):
            by_qid.setdefault(v["qid"], []).append(v)

    per_query = []
    overall_disagreements = []
    for qid, vs in by_qid.items():
        agg = {"qid": qid, "kind": kind_by_qid.get(qid), "n_judges": len(vs)}
        for d in _DIMS:
            vals = [v[d] for v in vs if d in v]
            agg[d] = round(mean(vals), 3) if vals else None
        # agreement on overall: stdev across judges (lower = more agreement)
        ov = [v["overall"] for v in vs if "overall" in v]
        if len(ov) > 1:
            m = sum(ov) / len(ov)
            sd = (sum((x - m) ** 2 for x in ov) / len(ov)) ** 0.5
            agg["overall_stdev"] = round(sd, 3)
            overall_disagreements.append(sd)
        agg["fabricated_any"] = any(v.get("fabricated") for v in vs)
        agg["verdicts"] = [v.get("verdict") for v in vs]
        agg["rationales"] = [v.get("rationale", "")[:200] for v in vs]
        per_query.append(agg)

    pos = [q for q in per_query if q["kind"] == "positive"]
    neg = [q for q in per_query if q["kind"] == "negative"]

    def agg_dim(rows, d):
        ci = bootstrap_ci([r[d] for r in rows if r.get(d) is not None])
        return {"mean": round(ci["mean"], 3) if ci["mean"] is not None else None,
                "ci": [round(ci["lo"], 3), round(ci["hi"], 3)] if ci["lo"] is not None else None,
                "n": ci["n"]}

    aggregate = {
        "n_judges": len(panels),
        "judges": [p.get("judge") for p in panels],
        "positives": {d: agg_dim(pos, d) for d in _DIMS},
        "negatives": {
            "overall": agg_dim(neg, "overall"),
            "faithfulness": agg_dim(neg, "faithfulness"),
            "fabrication_rate": round(mean([1.0 if q["fabricated_any"] else 0.0 for q in neg]), 3) if neg else None,
            "n": len(neg),
        },
        "positives_fabrication_rate": round(mean([1.0 if q["fabricated_any"] else 0.0 for q in pos]), 3) if pos else None,
        "mean_overall_interjudge_stdev": round(mean(overall_disagreements), 3) if overall_disagreements else None,
        "pass_rate_overall_ge4_positives": round(mean([1.0 if (q.get("overall") or 0) >= 4 else 0.0 for q in pos]), 3) if pos else None,
    }
    return {"aggregate": aggregate, "per_query": per_query}
