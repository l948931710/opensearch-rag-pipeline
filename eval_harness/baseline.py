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
# "midsentence"：P2-25 新增 l6.boundary.midsentence_cut_rate（切句率,越低越好）
_LOWER_BETTER = ("refus", "leak", "dangling", "orphan", "dup", "fabricat", "latency", "drift",
                 "p95", "p99", "miss", "error", "midsentence")
DEFAULT_DELTA = 0.03


def _direction(path: str) -> str:
    return "lower" if any(t in path.lower() for t in _LOWER_BETTER) else "higher"


# ── advisory/trend baseline metrics ───────────────────────────────────────────────────────────
# The smallest explicit mechanism (the schema stores flat {path: float} with no per-metric advisory
# flag): a registry of metric paths whose baseline DELTA is REPORTED (visible) but must NEVER hard-
# block --strict. It mirrors the advisory:True gates in report.py::build_gates — the advisory
# metrics extract_metrics yields are:
#   - l4srv.orphan_rate: its absolute gate is soft/trend (referenced-only rendering means
#     unreferenced candidate images aren't shown to users, so a high orphan rate on photo-dense
#     docs is a trend signal, not a defect);
#   - l4srv.answer_image_rate (#F-mm1, 2026-07-01): brand-new端到端出图率主指标 — 项目惯例
#     新指标先 advisory 跑两轮锁分布再议升 hard（LLM 非确定性下小样本波动大,冒然 hard 会
#     把噪声当回退阻断发布）;
#   - l4srv.marker_distinctness (#F-mm13b): report 侧一直是 advisory 闸（复用捆包是
#     addressability 限制非缺陷）,但此前不进 baseline → chunker 改动使捆包恶化无 delta
#     告警。入 baseline 后恶化可见,仍不阻断（与 report advisory 语义对齐）;
#   - judge.mm.image_relevance (#F-mm13a): caption-based 语义贴题率,judge 主观性+面板
#     方差,advisory trend 起步。
# Everything else — recall, jaccard, dup, marker_validity, dangling, judge scores, refusal/leak
# rates — stays a FULLY blocking hard metric. Keep this in sync with the advisory:True gates in
# report.py (test pins the exact set).
ADVISORY_METRICS = frozenset({
    "l4srv.orphan_rate", "l4srv.answer_image_rate",
    "l4srv.marker_distinctness", "judge.mm.image_relevance",
})


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
    for k in ("marker_validity", "dangling_ref_rate", "orphan_rate", "answer_image_rate",
              "marker_distinctness"):
        put(f"l4srv.{k}", srv.get(k))

    j = (results.get("judge") or {}).get("aggregate") or {}
    posj = j.get("positives") or {}
    for k in ("faithfulness", "correctness", "completeness"):
        put(f"judge.{k}", (posj.get(k) or {}).get("mean"))
    # mm judge（#F-mm13a）：语义贴题率 advisory trend
    jmm = (results.get("judge_mm") or {}).get("aggregate") or {}
    put("judge.mm.image_relevance", (jmm.get("image_relevance") or {}).get("mean"))

    # ── P2-25：L0 索引健康 / L2 校准 / L5 权限 / L6 chunk 质量 / judge 负例族 ─────────
    # 此前 baseline 只捕获 l1 / l3 部分 / l4 / judge 正例——以下层级的回退完全没有差量网。
    # 兼容性：老 baseline.json 缺这些键时,compare 只遍历 baseline 里已有的指标（视为
    # 无基线,不阻断）,并由末尾的 informational coverage 闸提示 refreeze;新 freeze 起自动纳入。
    # 只取比率/因子类标量,不取全库计数——L6 跑在活语料上,语料自然增长会让计数漂移误报。
    l0 = results.get("l0") or {}
    g2 = l0.get("G2_dense_self_query") or {}
    if g2.get("total"):
        put("l0.dense_self_query_rate", (g2.get("healthy") or 0) / g2["total"])
    g3 = l0.get("G3_sparse_self_query") or {}
    if g3.get("total"):
        put("l0.sparse_presence_rate", (g3.get("ok") or 0) / g3["total"])
    put("l0.vector_fidelity_cos_mean", (l0.get("G4_vector_fidelity") or {}).get("cos_mean"))

    l2 = results.get("l2") or {}
    put("l2.frac_high", l2.get("frac_高"))
    put("l2.frac_at_least_med", l2.get("frac_at_least_中"))
    put("l2.separation_auc_offtopic", l2.get("separation_auc_offtopic"))
    put("l2.n_offtopic_neg", l2.get("n_offtopic_neg"))  # 负例覆盖信号（同 n_positive_public 先例）

    l5 = results.get("l5") or {}
    n5 = l5.get("n_gated_docs_tested")
    if n5:  # applicable=False（全 public 语料）时无此键 → 优雅跳过
        put("l5.public_exclusion_rate", (l5.get("public_exclusion_ok") or 0) / n5)
        put("l5.authorized_visibility_rate", (l5.get("authorized_visibility_ok") or 0) / n5)
        put("l5.n_gated_docs_tested", n5)

    l6 = results.get("l6") or {}
    fam = l6.get("families") or {}
    put("l6.boundary.midsentence_cut_rate", (fam.get("boundary") or {}).get("midsentence_cut_rate"))
    put("l6.self_containedness.dangling_anaphor_rate",
        (fam.get("self_containedness") or {}).get("dangling_anaphor_rate"))
    put("l6.dedup.near_dup_cross_factor", (fam.get("dedup") or {}).get("near_dup_cross_factor"))
    put("l6.image_binding.img_dup_factor_p95",
        (fam.get("image_binding") or {}).get("img_dup_factor_p95"))
    put("l6.routing.routing_match_rate", (fam.get("routing") or {}).get("routing_match_rate"))
    # L6 chunk-judge 代表桶 pass 率（仅 merge 过 chunk 面板的 run 才有该键）
    put("l6.judge_chunk.repr_pass_rate_ge4",
        ((l6.get("judge_chunk") or {}).get("representative") or {}).get("pass_rate_overall_ge4"))

    # judge 负例族（拒答质量 + 负例造假率）与正例造假率——四个正例均值硬门之外的回退信号
    neg = j.get("negatives") or {}
    put("judge.negatives.fabrication_rate", neg.get("fabrication_rate"))
    put("judge.negatives.overall", (neg.get("overall") or {}).get("mean"))
    put("judge.positives_fabrication_rate", j.get("positives_fabrication_rate"))
    return m


def regime_of(results: Dict) -> Dict:
    return (results.get("meta") or {}).get("regime") or {}


# P2-24：judge 指纹入 regime——judge 是 faithfulness/correctness/completeness/fabrication
# 四个答案质量硬门的产出者,judge 模型/rubric 升级即换 regime,差量比较必须拒绝跨 judge 对比。
# VLM 指纹哨兵(2026-07-20 xlsx 绑定漂移 P2)：vlm_model / vlm_cache_version 同理——
# visual_summary 是 l4ing.jaccard.* 与 l6.image_binding 的输入,VLM 换代或缓存版本提升
# = 全部图 caption 重掷,分数不可与前次直接比较(0.8917→0.7167 曾烧一晚二分定位)。
# l4_ingestion_evaluator_version(2026-07-25 M3)：L4-ing 的 GT→产出卡匹配判定链自身
# 就会移动 l4ing.jaccard.*。code_commit 虽被 _regime() 记录却刻意不在本元组里（它对
# 任何无关提交都变，进闸即噪声源），结果"改了尺子"与"改了管线"在差量网里此前完全
# 无法区分。本键**刻意不进** _LENIENT_REGIME_KEYS：老 baseline 缺该键即 mismatch，
# 强制重冻——跨口径静默比较比 N/A 危险得多。
_REGIME_KEYS = ("eval_set_sha", "fusion", "rerank_enable", "llm_model",
                "embedding_model", "reranker_models", "threshold_version",
                "judge_model", "judge_rubric_version",
                "vlm_model", "vlm_cache_version", "l4_gt_sha",
                "l4_ingestion_evaluator_version", "funnel_policy",
                "l6_evaluator_version",
                "l1_matcher_version",
                "l4_serving_evaluator_version",
                "l4_serving_set_sha")
# P2-24 向后兼容宽容窗口：judge_model / judge_rubric_version 是 2026-07 新增 regime 键,
# 存量 baseline.json 里没有——老基线缺该键（None）视为匹配,新 freeze 起自动带上;
# 一旦 baseline 里有值,就按普通键严格比较。refreeze 需在用户机器上跑 live eval
# （沙箱 403 打不到生产 HA3）,所以不能因新键让存量基线立即失效。
# vlm_model / vlm_cache_version(2026-07-20)沿用同一窗口。
# l4_gt_sha(2026-07-20):L4 GT 在 repo 外数据仓,GT 重标对基线网原本隐形(clean 战役
# 当天实证);CI 无数据仓时现值为 None——lenient 窗口双向覆盖(老基线缺键 / 现值不可算)。
_LENIENT_REGIME_KEYS = frozenset({"judge_model", "judge_rubric_version",
                                  "vlm_model", "vlm_cache_version", "l4_gt_sha"})


# 双向宽容键:任一侧为 None 即视为匹配——l4_gt_sha 在无数据仓的机器(CI)上现值恒 None,
# 单向宽容会让"基线有值+CI 现值 None"误判 mismatch,整个差量网被 N/A 掉。
_BILATERAL_LENIENT_KEYS = frozenset({"l4_gt_sha"})


def regime_matches(base_regime: Dict, cur_regime: Dict) -> Tuple[bool, List[str]]:
    diffs = [k for k in _REGIME_KEYS
             if not (k in _LENIENT_REGIME_KEYS and base_regime.get(k) is None)
             and not (k in _BILATERAL_LENIENT_KEYS and cur_regime.get(k) is None)
             and base_regime.get(k) != cur_regime.get(k)]
    return (not diffs, diffs)


def compare(baseline: Dict, results: Dict, delta: float = DEFAULT_DELTA) -> Dict:
    """Return regression gate(s). Regime mismatch → a single N/A gate — NOT a free pass on the real
    check, just a loud 'refreeze for this regime'.

    P2-23：mismatch 的 na_reason 从 expected_na 改为 regime_mismatch——非 strict 路径语义不变
    （pass=None → 报告里仍是 N/A,容忍跨 regime 比较不可行）,但发布门（merge --strict）把它当
    硬失败：否则默认 goldset（golden_full）对上冻结在 golden_50 的 baseline 时,唯一的指标下降
    差量网会被静默关闭而 exit 0。"""
    base_regime = baseline.get("regime") or {}
    ok, diffs = regime_matches(base_regime, regime_of(results))
    if not ok:
        hint = ("goldset 与冻结基线不匹配：要么用 baseline 的 goldset 跑发布门,"
                "要么显式 refreeze (run_eval baseline-freeze)"
                if "eval_set_sha" in diffs else
                "refreeze the baseline for the current regime (run_eval baseline-freeze)")
        return {"baseline regression (regime)": {
            "target": "baseline regime must match run regime to compare",
            "value": f"REGIME MISMATCH on {diffs} — baseline not comparable",
            "pass": None, "na_reason": "regime_mismatch",
            "notes": hint}}

    cur = extract_metrics(results)
    base_m = baseline.get("metrics") or {}
    delta = baseline.get("delta", delta)
    # ── 按量纲分档容差（2026-08-02 Sam 拍板）：全局 delta(0.03) 是按 0-1 比率标定的；
    # 套在 5 分制评委均分上=要求满刻度 0.6% 的稳定性，远小于面板抽样噪声（同一 run 三评委
    # 负例均分 4.000/4.212/4.424 极差 0.42；同日两独立 run 逐题分数一致——量具噪声非行为
    # 回归）。5 分制均分族用 0.25（满刻度 5%，仍能抓真实退化）；比率类（fabrication_rate
    # 等 0-1 指标）不在此列，维持全局 delta。
    _scale5 = ("judge.faithfulness", "judge.correctness", "judge.completeness",
               "judge.negatives.overall", "judge.mm.image_relevance")
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
        d = 0.25 if path in _scale5 else delta
        regressed = (cv < bv - d) if _direction(path) == "higher" else (cv > bv + d)
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
    # ── P2-25 informational：本 run 有、冻结基线没有的指标（新增指标族 / 老基线）——
    # 视为无基线：可见但绝不阻断（advisory + expected_na 双保险,_strict_failures 都跳过）,
    # 仅提示 refreeze 后这些指标才进差量网。
    uncovered = sorted(set(cur) - set(base_m))
    if uncovered:
        gates["baseline coverage (informational — metrics not in frozen baseline)"] = {
            "target": f"informational: {len(uncovered)} run metrics lack a frozen baseline",
            "value": f"uncovered (first 8): {uncovered[:8]}",
            "pass": None, "na_reason": "expected_na", "advisory": True,
            "notes": "refreeze (run_eval baseline-freeze) to extend the regression net to these"}
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
