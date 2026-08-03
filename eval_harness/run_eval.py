"""Orchestrator for the end-to-end HA3 RAG eval.

Phases:
  run    : execute layers L0-L5 live (read-only), write results.json + judge_bundle.json
           + a preliminary report (deterministic gates only).
  merge  : load results.json + judge_verdicts.json (authored by the Claude panel),
           compute judge aggregates, write the FINAL report with all gates.

Usage:
  python -m eval_harness.run_eval run   [--goldset PATH] [--layers l0,l1,l2,l3,l4,l5] [--limit N]
  python -m eval_harness.run_eval merge --results PATH --verdicts PATH

Everything that touches HA3/RDS is read-only. Answer generation runs with thinking OFF.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from . import envboot
from .ha3live import install_into_retriever

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GOLDSET = os.path.join(HERE, "goldset", "golden_50.json")


def _ts():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _load_goldset(path):
    cases = json.load(open(path, encoding="utf-8"))
    return cases


# The retrieval regime the L1/L2/L3 score thresholds were calibrated on. RRF (or an un-accounted
# fusion change) invalidates the absolute thresholds even when gates still "pass" → the guard fails.
CALIBRATION_FUSION = "weighted"

# Production serves with the routed reranker ON (verified read-only 2026-06-18: docs/audits/L6_*_
# 2026-06-15 record RAG_RERANK_ENABLE=true, and live qa_session_log.top_score sits in the 0-1 rerank
# band — NOT the 7.7/5.8 weighted scale). The eval MUST run in the same rerank state or its L1/L2
# numbers don't represent production: L2 switches to the rerank 0.9/0.8 bands when rerank is on
# (l2_calibration.py), so a rerank-OFF run scores against the wrong thresholds. Override ONLY to
# deliberately evaluate/recalibrate a different regime.
CALIBRATION_RERANK = os.environ.get(
    "RAG_EVAL_CALIBRATION_RERANK", "true").strip().lower() in ("1", "true", "yes", "on")


def _cfg_get(cfg, name, default=None):
    """hybrid_fusion / rerank_enable live on cfg or cfg.alibaba_vector depending on version."""
    if hasattr(cfg, name):
        return getattr(cfg, name)
    av = getattr(cfg, "alibaba_vector", None)
    if av is not None and hasattr(av, name):
        return getattr(av, name)
    return default


def _l4_gt_files() -> list:
    """L4-ingestion GT 文件解析——run 流程与 regime 指纹共用同一来源,保证指纹
    覆盖的就是 L4 实际消费的文件(EVAL_L4_GT_FILES 覆盖 → 指纹跟着覆盖走)。"""
    env = os.environ.get("EVAL_L4_GT_FILES")
    if env:
        return [p for p in env.split(",") if p.strip()]
    data = os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples")
    return [p for p in [
        os.path.join(data, "ground_truth", "gt_pdf_analysis.json"),
        os.path.join(data, "ground_truth", "gt_xlsx_pptx_analysis.json"),
        os.path.join(data, "ground_truth", "gt_docx_analysis.json"),
    ] if os.path.exists(p)]


def _l4_ingestion_evaluator_version() -> "str | None":
    """L4-ingestion 判定口径的语义版本（单一来源在 binding.ingestion_binding）。

    import 失败 fail-open 成 None——与 harness 其余 regime helper 同款，绝不让指纹
    采集把整轮 eval 打挂。"""
    try:
        from .binding.ingestion_binding import L4_INGESTION_EVALUATOR_VERSION
        return L4_INGESTION_EVALUATOR_VERSION
    except Exception:
        return None


def _l4_gt_sha() -> "str | None":
    """L4 GT 组合指纹:basename+内容,按名排序(顺序无关)。数据仓不可达 → None
    (CI 无数据仓;走 baseline._LENIENT_REGIME_KEYS 宽容窗口)。"""
    import hashlib
    try:
        blobs = sorted((os.path.basename(p), open(p, "rb").read()) for p in _l4_gt_files())
    except Exception:
        return None
    if not blobs:
        return None
    h = hashlib.sha256()
    for name, blob in blobs:
        h.update(name.encode("utf-8")); h.update(b"\0"); h.update(blob); h.update(b"\0")
    return h.hexdigest()[:16]


def _l6_evaluator_version() -> "str | None":
    """L6 判定口径的语义版本（单一来源在 layers.l6_chunk_quality）。import 失败 fail-open。"""
    try:
        from .layers.l6_chunk_quality import L6_EVALUATOR_VERSION
        return L6_EVALUATOR_VERSION
    except Exception:
        return None


def _l4_serving_set_sha() -> "str | None":
    """L4-serving 专用题集的内容指纹。解析路径必须与实际加载逻辑同源（默认/自定义/
    置空关闭/文件不存在四种情况），否则指纹与真正跑的题集脱节。"""
    import hashlib
    try:
        p = os.environ.get("EVAL_L4_SERVING_GOLDSET")
        if p is None:
            p = os.path.join(HERE, "goldset", "golden_l4_serving.json")
        if not p:
            return "disabled"                      # 显式置空 = 不并入
        if not os.path.exists(p):
            return "missing"
        return hashlib.sha256(open(p, "rb").read()).hexdigest()[:16]
    except Exception:
        return None


def _l4_serving_evaluator_version() -> "str | None":
    """L4-serving 判定口径的语义版本（单一来源在 mm_answer_metrics）。import 失败 fail-open。"""
    try:
        from .mm_answer_metrics import L4SRV_EVALUATOR_VERSION
        return L4SRV_EVALUATOR_VERSION
    except Exception:
        return None


def _l1_matcher_version() -> "str | None":
    """L1 标题匹配口径的语义版本（单一来源在 matching.py）。import 失败 fail-open。"""
    try:
        from .matching import MATCHER_VERSION
        return MATCHER_VERSION
    except Exception:
        return None


def _funnel_policy():
    """当前漏斗弃图判据的标签（选项 C）。**历史判据回 None 而不是空串**。

    它进的是 `_REGIME_KEYS` 的严格段（不在 lenient 窗口里）。历史判据回 None 是为了让
    存量 baseline（根本没有这个键、取值 `None`）不至于凭空多一条失配。
    ⚠️ **2026-07-26 起 C 默认 ON**，所以默认姿态下本函数返回的是 `"c1"` 而非 None，
    与任何历史基线**硬失配** —— 这正是要的（跨判据比较必须强制重冻），但"默认姿态下该键
    完全隐形"那句已随翻默认过期，此处订正。
    """
    try:
        from opensearch_pipeline.ingest_flags import effective_funnel_policy_version
        return effective_funnel_policy_version() or None
    except Exception:
        return None


def _regime(cfg, goldset_path: str) -> dict:
    """Run-condition fingerprint stamped into meta + the baseline, so deltas are never compared across
    different eval-set / code / model / reranker / fusion / threshold conditions."""
    import hashlib
    try:
        sha = hashlib.sha256(open(goldset_path, "rb").read()).hexdigest()[:16]
    except Exception:
        sha = "unknown"
    try:
        from opensearch_pipeline.versions import git_commit
        commit = git_commit()
    except Exception:
        commit = "unknown"
    av = getattr(cfg, "alibaba_vector", None)
    rag = getattr(cfg, "rag", cfg)
    rerank_on = bool(_cfg_get(cfg, "rerank_enable", False))
    thr = (f"sh={getattr(rag,'score_threshold_high',None)},sm={getattr(rag,'score_threshold_medium',None)},"
           f"rh={getattr(rag,'rerank_score_threshold_high',None)},rm={getattr(rag,'rerank_score_threshold_medium',None)}")
    # P2-24：judge 指纹入 regime——judge 模型 + answer rubric 版本变更即换 regime,
    # baseline 差量比较拒绝跨 judge 对比。graceful degradation：导入失败不破坏 run
    # （老 baseline 缺这两键时 baseline._LENIENT_REGIME_KEYS 宽容跳过,兼容窗口见彼处）。
    try:
        from .run_judge import JUDGE_MODEL as judge_model
    except Exception:
        judge_model = None
    try:
        from .judge import JUDGE_RUBRIC_VERSION as judge_rubric_version
    except Exception:
        judge_rubric_version = None
    # VLM 指纹哨兵(2026-07-20 xlsx 绑定漂移 P2):visual_summary 是 L4-ing 绑定与 L6 图
    # 指标的输入,VLM 模型或缓存版本变更 = 全部图 caption 重掷,l4ing.* 分数不可与前次
    # 直接比较(0.8917→0.7167 那次烧了一晚二分才定位到缓存失效)。入 regime 后,跨 VLM
    # 代对比在 baseline.compare 处直接 N/A 报「refreeze」,不再伪装成分数回归。
    # 老 baseline 缺这两键 → baseline._LENIENT_REGIME_KEYS 宽容窗口(与 judge 键同款)。
    try:
        ocr_cfg = getattr(cfg, "ocr", None)
        vlm_model = (getattr(ocr_cfg, "vlm_model", None)
                     or getattr(ocr_cfg, "model", None)) if ocr_cfg is not None else None
    except Exception:
        vlm_model = None
    vlm_cache_version = os.environ.get("RAG_VLM_CACHE_VERSION", "2").strip() or "bare"
    return {
        "eval_set_sha": sha,
        "fusion": _cfg_get(cfg, "hybrid_fusion", None),
        "rerank_enable": rerank_on,
        "llm_model": cfg.llm.model,
        "embedding_model": cfg.embedding.model,
        "reranker_models": (f"{getattr(av,'rerank_text_model',None)}/{getattr(av,'rerank_vl_model',None)}"
                            if rerank_on and av is not None else None),
        "threshold_version": thr,
        "judge_model": judge_model,
        "judge_rubric_version": judge_rubric_version,
        # v2 敏感 guard 姿态（2026-08-02）：guard on/off 直接改写负例拦截与生成路径
        # （guard 命中的 case 根本不生成），两种姿态的 run 不可比 —— 进 regime 且
        # 走严格键（baseline._REGIME_KEYS，不进宽容窗口），姿态一变强制重冻。
        "sensitive_query_guard": bool(_cfg_get(getattr(cfg, "rag", None), "sensitive_query_guard", False)),
        "sensitive_prompt_guard": bool(_cfg_get(getattr(cfg, "rag", None), "sensitive_prompt_guard", False)),
        "vlm_model": vlm_model,
        "vlm_cache_version": vlm_cache_version,
        # L4-GT 指纹哨兵(2026-07-20):L4-ing 绑定分的 GT 在 repo 外数据仓,不在金集
        # sha 覆盖面内——GT 重标会静默移动 l4ing.* 而基线网无感知(clean 战役当天实证:
        # 液压泵/电机螺丝两行 GT 修正对基线完全隐形)。老 baseline 缺键走宽容窗口。
        "l4_gt_sha": _l4_gt_sha(),
        # L4-ing 评测口径指纹(2026-07-25 M3)：GT→产出卡的匹配判定链本身会移动
        # l4ing.jaccard.*，而 code_commit 虽被记录却**不在** _REGIME_KEYS 里
        # (它对任何无关提交都变，进闸就是噪声源)——于是"改了尺子"和"改了管线"在
        # 差量网里此前完全无法区分。本键只在判定口径变化时手工 bump，
        # **不进** _LENIENT_REGIME_KEYS：跨口径比较必须硬失败、强制重冻。
        "l4_ingestion_evaluator_version": _l4_ingestion_evaluator_version(),
        # L6 判定口径指纹（2026-07-27）：与上面 L4 那条同款用途 —— 改**尺子**会移动 l6.*，
        # 而 code_commit 不在 _REGIME_KEYS 里，不进 regime 就无法与"管线变好了"区分。
        "l6_evaluator_version": _l6_evaluator_version(),
        # L1 标题归一口径指纹（2026-07-27）：gold_doc_rank 只看归一后的标题，改口径直接移动
        # 全部 l1.* recall —— 与上面两条同款，不进 regime 就无法与"检索变好了"区分。
        "l1_matcher_version": _l1_matcher_version(),
        # L4-serving 判定口径指纹（2026-07-27）：orphan 分母从"全部带图 chunk"收窄到
        # "标记真的进了 context 的图"，会移动 l4srv.orphan_rate —— 同 l4_ingestion/l6/l1
        # 三条先例，改尺子必须与"模型放图变好了"可区分。
        "l4_serving_evaluator_version": _l4_serving_evaluator_version(),
        # L4-serving 的**专用题集**指纹：_regime 的 eval_set_sha 只哈希主 goldset，而 L4
        # 运行时会并入 golden_l4_serving.json——改那份题集,旧基线仍被判"可比"。
        "l4_serving_set_sha": _l4_serving_set_sha(),
        # 漏斗弃图判据指纹(2026-07-26 选项 C)：它决定**哪些图能进知识库**，是 l4ing.*
        # 与 L6 图指标的上游输入 —— 换判据后的分数与换判据前的不可直接比较（更要命的是
        # 方向会骗人：救回的图进 Jaccard 并集只会压低分，看起来像回归其实是修复）。
        # **不进** _LENIENT_REGIME_KEYS：跨判据比较必须硬失败、强制重冻。
        "funnel_policy": _funnel_policy(),
        "code_commit": commit,
    }


def _regime_guard(regime: dict) -> dict:
    """Fail-closed if the active retrieval regime != the regime the thresholds were calibrated on
    (== production serving regime). Pins BOTH fusion and rerank state: L2 uses different score bands
    for rerank-on vs -off, so a rerank mismatch makes L1/L2 absolute numbers non-representative."""
    active_fusion = regime.get("fusion")
    active_rerank = bool(regime.get("rerank_enable"))
    fusion_ok = active_fusion == CALIBRATION_FUSION
    rerank_ok = active_rerank == CALIBRATION_RERANK
    mismatch = [m for m, ok in (("fusion", fusion_ok), ("rerank", rerank_ok)) if not ok]
    return {"expected_fusion": CALIBRATION_FUSION, "active_fusion": active_fusion,
            "expected_rerank": CALIBRATION_RERANK, "active_rerank": active_rerank,
            "rerank_enable": active_rerank,
            "match": (fusion_ok and rerank_ok), "mismatch": mismatch}


def _strict_enabled(args) -> bool:
    return bool(getattr(args, "strict", False)) or \
        os.environ.get("RAG_EVAL_STRICT", "").lower() in ("1", "true", "yes")


def _strict_failures(gates: dict, results: dict, *, requested_layers=None) -> list:
    """Hard-fail reasons under --strict. A gate's ``pass``:
      True → ok ; False → fail ; None → depends on ``na_reason``:
        - 'not_executed'    (sample / config / verdict shortfall — should have run but didn't) → FAIL.
          Never silently pass an unmeasured HARD gate.
        - 'regime_mismatch' (P2-23: baseline regime != run regime, 差量回归网整体失效) → FAIL.
          非 strict 路径仍容忍为 N/A——只有发布门要硬失败。
        - 'expected_na'     (genuinely inapplicable by design, e.g. L5 with no gated docs) → ok.
    Plus: L6 NO_GO_DEFECT; EVAL-2 manifest drift; answer-correctness-not-judged (L3 ran but no judge
    merge); and L6 requested but un-runnable. (L6 NO_GO_INCOMPLETE_EVIDENCE already FAILs via its
    verdict gate's pass=False — once L6 is actually in the run.)"""
    fails = []
    for name, g in (gates or {}).items():
        p = g.get("pass")
        if g.get("advisory"):
            continue  # advisory/diagnostic gate (e.g. L6-soft) — visible in the report, never blocks
        if p is False:
            fails.append(name)
        elif p is None and g.get("na_reason") == "not_executed":
            fails.append(f"{name} [not_executed]")
        elif p is None and g.get("na_reason") == "regime_mismatch":
            # P2-23：goldset/regime 与冻结基线不一致 → 发布门硬失败,不再静默放行
            fails.append(f"{name} [regime_mismatch: 用 baseline 的 goldset/regime 跑门,或显式 "
                         f"refreeze (run_eval baseline-freeze)]")
    if (results.get("l6") or {}).get("state") == "NO_GO_DEFECT":
        fails.append("l6:NO_GO_DEFECT")
    _errs = ((results.get("l4") or {}).get("ingestion") or {}).get("deterministic", {}).get("errors") or []
    if [e for e in _errs if isinstance(e, str) and e.startswith("manifest_drift::")]:
        fails.append("l4:manifest_drift")
    # answer-correctness MUST be judged: L3 ran (answers generated) but no judge merge → a strict run
    # cannot certify correctness. This is a FAIL, not "judge not enabled".
    if results.get("l3") and not (results.get("judge") or {}).get("aggregate"):
        fails.append("answer_correctness:not_judged (merge a judge_verdicts.json)")
    # L6 was requested but could not be evaluated at all → not a silent skip.
    if requested_layers and "l6" in requested_layers and (results.get("l6") or {}).get("applicable") is False:
        fails.append("l6:not_executed (requested but applicable=False)")
    return fails


def _enforce_strict(gates: dict, results: dict, strict: bool, *, requested_layers=None):
    """EVAL-1: convert advisory gates into a blocking exit code (the CI gate, EVAL-3, relies on this)."""
    if not strict:
        return
    fails = _strict_failures(gates, results, requested_layers=requested_layers)
    if fails:
        print(f"\n❌ STRICT: hard gate failure(s): {fails} → exit 1")
        sys.exit(1)
    print("\n✅ STRICT: all hard gates passed")


def phase_run(args):
    install_into_retriever()  # force public-HTTP client into the production retriever
    from opensearch_pipeline.config import get_config
    cfg = get_config()

    cases = _load_goldset(args.goldset)
    if args.limit:
        cases = cases[: args.limit]
    layers = set(args.layers.split(","))
    outdir = args.outdir or os.path.join(HERE, "reports", f"run_{_ts()}")
    os.makedirs(outdir, exist_ok=True)

    meta = {
        "run_id": os.path.basename(outdir), "timestamp": _ts(),
        "n_cases": len(cases), "goldset": args.goldset, "layers": sorted(layers),
        "llm_model": cfg.llm.model, "embedding_model": cfg.embedding.model,
        **envboot.facts(),
    }
    results = {"meta": meta}
    regime = _regime(cfg, args.goldset)
    results["meta"]["regime"] = regime
    results["regime_guard"] = _regime_guard(regime)  # item 3: fusion/calibration consistency gate
    print(f"== EVAL RUN {meta['run_id']} | table={meta['ha3_table']} | n={len(cases)} ==")
    print(json.dumps({k: meta[k] for k in ("ha3_endpoint", "llm_model", "rag_environment", "simulate")},
                     ensure_ascii=False))

    if "l0" in layers:
        print("\n[L0] index health ...")
        from .layers import l0_index_health
        results["l0"] = l0_index_health.run()
        print(f"   L0 PASS={results['l0'].get('PASS')}")

    if "l1" in layers:
        print("\n[L1] retrieval ranking ...")
        from .layers import l1_retrieval
        results["l1"] = l1_retrieval.run(cases, top_k=10)
        print(f"   ranking={json.dumps(results['l1'].get('ranking'), ensure_ascii=False)}")

    if "l2" in layers and "l1" in results:
        print("\n[L2] score calibration ...")
        from .layers import l2_calibration
        results["l2"] = l2_calibration.run(results["l1"])
        print(f"   thresholds_ok={results['l2'].get('thresholds_ok')}")

    if "l3" in layers:
        print("\n[L3] answer quality (thinking OFF) + judge bundle ...")
        from .layers import l3_answer
        results["l3"] = l3_answer.run(cases, top_k=7)
        json.dump(results["l3"]["judge_bundle"],
                  open(os.path.join(outdir, "judge_bundle.json"), "w"),
                  ensure_ascii=False, indent=1)
        print(f"   deterministic={json.dumps(results['l3']['deterministic'], ensure_ascii=False)}")

    if "l4" in layers:
        print("\n[L4] multimodal (ingestion + serving) ...")
        from .layers import l4_multimodal
        # L4-ingestion DOCX strict 路径默认开(make eval / DataWorks / CI 自动闭环)
        # 显式 export EVAL_L4_DOCX_BINDING_ENABLE=false 可关 — setdefault 不覆盖
        os.environ.setdefault("EVAL_L4_DOCX_BINDING_ENABLE", "true")
        # L4-ingestion 触发:env EVAL_L4_GT_FILES(逗号分隔)+ EVAL_L4_DOCS_DIR
        # 默认指向 eval_samples/ground_truth + documents(repo 外仓)
        _data = os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples")
        gt_files = _l4_gt_files()   # 与 regime 指纹同源(l4_gt_sha)
        docs_dir = os.environ.get("EVAL_L4_DOCS_DIR") or os.path.join(_data, "documents")
        # EVAL-2: image-manifest dir for GT preflight; default mirrors the existing CLI heuristic
        manifest_dir = os.environ.get("EVAL_L4_MANIFEST_DIR") or os.path.join(_data, "scratch", "eval_manifest")
        # ── L4-serving 专用题集并入（#F-mm1c, 2026-07-01）───────────────────
        # golden_full 的图 case live_scorable 仅 2 题 → L4-srv 硬闸恒 N<5 not_executed;
        # 25 题专用 golden_l4_serving.json 此前是零引用孤儿。默认并入（qid 去重,
        # 只影响 serving 支柱）;EVAL_L4_SERVING_GOLDSET=<path> 换题集、置空一键关回。
        _srv_gs = os.environ.get("EVAL_L4_SERVING_GOLDSET")
        if _srv_gs is None:
            _srv_gs = os.path.join(HERE, "goldset", "golden_l4_serving.json")
        serving_cases = cases
        if _srv_gs and os.path.exists(_srv_gs):
            # serving 题集是 L4-serving 标注的权威（query_breadth/expected_images 只维护在
            # 这里）：与主 goldset 重叠的 qid 用 serving 版覆盖（只影响 l4 输入,l1/l3 仍用
            # 原 cases）,其余追加。
            _srv_by_qid = {c.get("qid"): c for c in _load_goldset(_srv_gs)}
            _known_qids = {c.get("qid") for c in cases}
            _extra = [c for q, c in _srv_by_qid.items() if q not in _known_qids]
            _n_override = sum(1 for c in cases if c.get("qid") in _srv_by_qid)
            serving_cases = [_srv_by_qid.get(c.get("qid"), c) for c in cases] + _extra
            if _extra or _n_override:
                print(f"   L4-serving goldset 并入: +{len(_extra)} 新增, {_n_override} 覆盖标注 "
                      f"(from {_srv_gs})")
        elif _srv_gs:
            print(f"   ⚠ EVAL_L4_SERVING_GOLDSET 指向不存在的文件,跳过: {_srv_gs}")
        results["l4"] = l4_multimodal.run(
            serving_cases,
            gt_files=gt_files if gt_files else None,
            docs_dir=docs_dir if os.path.isdir(docs_dir) else None,
            manifest_dir=manifest_dir if os.path.isdir(manifest_dir) else None,
        )
        if results["l4"].get("applicable"):
            # serving bundle(可能为空)
            if results["l4"].get("judge_bundle_mm"):
                json.dump(results["l4"]["judge_bundle_mm"],
                          open(os.path.join(outdir, "judge_bundle_mm.json"), "w"),
                          ensure_ascii=False, indent=1)
            # ingestion bundle(新:L4-ingestion Claude image_binding 评审用)
            if results["l4"].get("judge_bundle_binding"):
                json.dump(results["l4"]["judge_bundle_binding"],
                          open(os.path.join(outdir, "judge_bundle_binding.json"), "w"),
                          ensure_ascii=False, indent=1)
        ing_det = (results["l4"].get("ingestion") or {}).get("deterministic", {})
        print(f"   applicable={results['l4'].get('applicable')}  "
              f"ingestion_pdf={ing_det.get('binding_jaccard_pdf')}  "
              f"ingestion_xlsx={ing_det.get('binding_jaccard_xlsx')}  "
              f"dup_p95={ing_det.get('img_dup_factor_p95')}")

    if "l5" in layers:
        print("\n[L5] permission filtering ...")
        from .layers import l5_permission
        results["l5"] = l5_permission.run()
        print(f"   {json.dumps({k: results['l5'].get(k) for k in ('applicable','PASS')}, ensure_ascii=False)}")

    if "l6" in layers:
        print("\n[L6] chunk-artifact content quality (read-only) ...")
        from .layers import l6_chunk_quality
        results["l6"] = l6_chunk_quality.run(d7_json_path=os.environ.get("EVAL_L6_D7_JSON"))
        if results["l6"].get("applicable") and results["l6"].get("judge_bundle_chunk"):
            json.dump(results["l6"]["judge_bundle_chunk"],
                      open(os.path.join(outdir, "judge_bundle_chunk.json"), "w"),
                      ensure_ascii=False, indent=1)
        print(f"   verdict={results['l6'].get('state')}  go_no_go={results['l6'].get('go_no_go')}  "
              f"d7={results['l6'].get('d7_source')}")

    if args.baseline and os.path.exists(args.baseline):
        from . import baseline as _bl
        results["baseline_gates"] = _bl.compare(
            json.load(open(args.baseline, encoding="utf-8")), results)
    from . import report
    gates = report.write(results, outdir)
    print(f"\n== PRELIMINARY REPORT -> {outdir}/report.md ==")
    for name, g in gates.items():
        mark = ("PASS" if g["pass"] is True else "FAIL" if g["pass"] is False
                else "N/A!" if g.get("na_reason") in ("not_executed", "regime_mismatch")
                else "N/A")
        print(f"   [{mark}] {name}: {g['value']}")
    print(f"\nNext: judge the bundle (Claude panel) -> {outdir}/judge_verdicts.json, then:\n"
          f"  python -m eval_harness.run_eval merge --results {outdir}/report.json "
          f"--verdicts {outdir}/judge_verdicts.json")
    _enforce_strict(gates, results, _strict_enabled(args), requested_layers=layers)
    return outdir


def phase_merge(args):
    results = json.load(open(args.results, encoding="utf-8"))
    verdicts = json.load(open(args.verdicts, encoding="utf-8"))
    # 合并 L3 bundle(judge_bundle.json)+ L4-binding bundle(judge_bundle_binding.json,
    # 2026-06-12 新增)以提供完整 kind_by_qid。任一缺失视为不存在,不阻断 merge。
    outdir = os.path.dirname(args.results)
    bundle: list = []
    for fname in ("judge_bundle.json", "judge_bundle_binding.json"):
        bp = os.path.join(outdir, fname)
        if os.path.exists(bp):
            try:
                bundle.extend(json.load(open(bp, encoding="utf-8")))
            except Exception:
                pass

    from .judge import merge_panel
    panels = verdicts["panels"] if isinstance(verdicts, dict) and "panels" in verdicts else verdicts
    results["judge"] = merge_panel(bundle, panels)

    # L6 chunk-judge merge (separate rubric/buckets) — convention-based files next to results.
    # judge_bundle_chunk.json (from phase_run) + judge_verdicts_chunk.json (from the panel).
    cb = os.path.join(outdir, "judge_bundle_chunk.json")
    cv = os.path.join(outdir, "judge_verdicts_chunk.json")
    if os.path.exists(cb) and os.path.exists(cv):
        from .judge import merge_chunk_panel
        chunk_bundle = json.load(open(cb, encoding="utf-8"))
        cverd = json.load(open(cv, encoding="utf-8"))
        cpanels = cverd["panels"] if isinstance(cverd, dict) and "panels" in cverd else cverd
        results.setdefault("l6", {})["judge_chunk"] = merge_chunk_panel(chunk_bundle, cpanels)

    # mm judge merge（#F-mm13a）：L4-serving 图文贴题率语义通道——judge_bundle_mm.json
    # 此前每轮落盘却从未被消费（run_judge 无 mm rubric、gate 不跑它）。同目录约定：
    # judge_bundle_mm.json（phase_run 产）+ judge_verdicts_mm.json（panel 产）都在才合并。
    mb = os.path.join(outdir, "judge_bundle_mm.json")
    mv = os.path.join(outdir, "judge_verdicts_mm.json")
    if os.path.exists(mb) and os.path.exists(mv):
        from .judge import merge_mm_panel
        mm_bundle = json.load(open(mb, encoding="utf-8"))
        mverd = json.load(open(mv, encoding="utf-8"))
        mpanels = mverd["panels"] if isinstance(mverd, dict) and "panels" in mverd else mverd
        results["judge_mm"] = merge_mm_panel(mm_bundle, mpanels)

    # P3-11 judge 校准门接线：report.py 的 calibration_gate 只在 results['judge_calibration']
    # 存在时出现，而此前【无任何编排路径写它】——门只活在文档片段里，每次自动 gate 都
    # 静默缺席。现按同目录约定（judge_calibration_labels.json）或 RAG_JUDGE_CAL_LABELS 显式
    # 路径拾取人工标注并现场 compare；无标注时打印显式 NOT-ACTIVE（不插 not_executed 门——
    # 人工标注是稀缺产物而非每轮必备，插门会让所有无标注的 strict 跑恒挂）。
    cal_path = (os.environ.get("RAG_JUDGE_CAL_LABELS")
                or os.path.join(outdir, "judge_calibration_labels.json"))
    if os.path.exists(cal_path):
        from .judge_calibration import compare as _cal_compare
        _labels = json.load(open(cal_path, encoding="utf-8"))
        if isinstance(_labels, dict) and "labels" in _labels:
            _labels = _labels["labels"]
        results["judge_calibration"] = _cal_compare(_labels, panels)
        results["judge_calibration"]["labels_path"] = cal_path
    else:
        results.setdefault("meta", {})["judge_calibration_status"] = (
            "NOT ACTIVE — no human labels; judge 绝对效度无锚。生成标注模板见 "
            "judge_calibration.build_template，标注后放 outdir/judge_calibration_labels.json "
            "或设 RAG_JUDGE_CAL_LABELS")
        print("   [note] judge-calibration gate NOT ACTIVE (no human labels) — "
              "四个答案质量硬门当前建立在未经人工校准的 judge 上")

    if args.baseline and os.path.exists(args.baseline):
        from . import baseline as _bl
        results["baseline_gates"] = _bl.compare(
            json.load(open(args.baseline, encoding="utf-8")), results)
    from . import report
    outdir = os.path.dirname(args.results)
    gates = report.write(results, outdir)
    print(f"== FINAL REPORT -> {outdir}/report.md ==")
    for name, g in gates.items():
        mark = ("PASS" if g["pass"] is True else "FAIL" if g["pass"] is False
                else "N/A!" if g.get("na_reason") in ("not_executed", "regime_mismatch")
                else "N/A")
        print(f"   [{mark}] {name}: {g['value']}")
    _enforce_strict(gates, results, _strict_enabled(args),
                    requested_layers=set((results.get("meta") or {}).get("layers") or []))
    return outdir


def phase_baseline_freeze(args):
    """Freeze the current run's report.json into a committed baseline (regime-tagged)."""
    from . import baseline as _bl
    results = json.load(open(args.results, encoding="utf-8"))
    out = args.baseline or os.path.join(HERE, "goldset", "baseline.json")
    base = _bl.freeze(results, out)
    print(f"== BASELINE FROZEN -> {out} ==")
    print(f"   regime: {json.dumps(base['regime'], ensure_ascii=False)}")
    print(f"   metrics: {len(base['metrics'])} (delta={base['delta']})")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["run", "merge", "baseline-freeze"])
    ap.add_argument("--goldset", default=DEFAULT_GOLDSET)
    ap.add_argument("--layers", default="l0,l1,l2,l3,l4,l5,l6")  # L6 now in default (item 4)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--outdir", default="")
    ap.add_argument("--results", default="")
    ap.add_argument("--verdicts", default="")
    ap.add_argument("--baseline", default="",
                    help="path to a frozen baseline.json; enables per-layer/subset regression gating")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on any hard gate FAIL / not_executed / L6 NO_GO_DEFECT / "
                         "unjudged correctness (CI gate); also enabled by RAG_EVAL_STRICT=true")
    args = ap.parse_args()
    if args.phase == "run":
        phase_run(args)
    elif args.phase == "merge":
        phase_merge(args)
    else:
        phase_baseline_freeze(args)


if __name__ == "__main__":
    main()
