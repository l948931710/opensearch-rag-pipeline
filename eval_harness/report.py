"""Consolidated report: PASS/FAIL gates + executive summary in JSON / Markdown / HTML."""
from __future__ import annotations

import html
import json
import os
from typing import Dict


def _g(d, *path, default=None):
    for p in path:
        if not isinstance(d, dict):
            return default
        d = d.get(p, default)
    return d


def build_gates(r: Dict) -> Dict:
    """Top-line pass/fail gates with the xlsx rubric targets where applicable."""
    gates = {}

    l0 = r.get("l0")
    if l0:
        gates["index_health (L0)"] = {"target": "all gates pass", "value": l0.get("PASS"),
                                      "pass": bool(l0.get("PASS"))}

    l1 = r.get("l1")
    if l1 and l1.get("ranking"):
        r5 = _g(l1, "ranking", "recall@5")
        gates["retrieval recall@5 (L1)"] = {
            "target": ">= 0.85 (xlsx 正例召回率@5)", "value": r5,
            "ci": _g(l1, "ranking", "recall@5_ci"),
            "pass": (r5 is not None and r5 >= 0.85)}
        # source attribution is the 来源标注 module specifically (doc-level top-1 accuracy)
        src_r1 = _g(l1, "by_module", "source_attribution", "recall@1")
        if src_r1 is not None:
            gates["source attribution recall@1 (L1, 来源标注)"] = {
                "target": ">= 0.95 (xlsx 来源标注)", "value": src_r1,
                "pass": src_r1 >= 0.95}
        # xlsx RAG retrieval module (clean gold) — the authoritative recall signal
        rag_r5 = _g(l1, "by_module", "rag_retrieval", "recall@5")
        if rag_r5 is not None:
            gates["xlsx RAG retrieval recall@5 (L1, clean gold)"] = {
                "target": ">= 0.85", "value": rag_r5, "pass": rag_r5 >= 0.85}

    l2 = r.get("l2")
    if l2:
        gates["score calibration (L2)"] = {"target": "labels still fit",
                                           "value": l2.get("thresholds_ok"),
                                           "pass": bool(l2.get("thresholds_ok"))}

    l3 = r.get("l3", {}).get("deterministic") if r.get("l3") else None
    if l3:
        rl = l3.get("reasoning_leak_count")
        gates["thinking-off verified (L3)"] = {"target": "0 reasoning leaks", "value": rl,
                                               "pass": (rl == 0)}
        orr = _g(l3, "positive", "over_refusal_rate")
        gates["positive over-refusal (L3)"] = {"target": "<= 0.10 (hard refusals)", "value": orr,
                                               "pass": (orr is not None and orr <= 0.10)}
        # negative interception is judged authoritatively by the Claude panel
        # (appropriate_refusal / fabrication); the rule-based proxy is a diagnostic only.
        sl = _g(l3, "positive", "source_leak_rate")
        gates["answer source-leak (L3)"] = {"target": "<= 0.05", "value": sl,
                                            "pass": (sl is not None and sl <= 0.05)}
        # 完整性确定性门：gold 关键词覆盖率。基线 0.723（run_predeploy_q36）；
        # 2026-06-11 实证 prompt 格式规则可悄悄吃掉相关细则（规则4 条目化后
        # 路程假 0/3 丢失），该回归不触发任何旧门 —— 此门专为拦它。
        kc = _g(l3, "positive", "mean_keyword_coverage")
        gates["answer keyword-coverage (L3, 完整性)"] = {
            "target": ">= 0.70", "value": kc,
            "pass": (kc is not None and kc >= 0.70)}

    # ── L4 闸(两支柱)2026-06-12 加入 ─────────────────────────────
    # 注:所有新闸首轮 soft 跑两轮锁档分布后再决定升 hard(plan Day 5-6)
    l4 = r.get("l4") if r.get("l4", {}).get("applicable") else None
    if l4:
        # — L4-ingestion 支柱(逐格式 Jaccard + over-attach)—
        ing = (l4.get("ingestion") or {}).get("deterministic") or {}
        # 2026-06-13 D7 升 hard:5 轮 byte-equal 后 chunker 已 deterministic
        # XLSX 升到 0.85 hard(实测 0.8636,1.4pp 缓冲;task chip 修了 step5/6 anchor 互换)
        # DOCX 0.95 hard(D6 设定,实测 0.9847 走 strict_fixture)
        # PDF 保 soft(1 doc 11 chunks 样本太小,3 个 PDF chunker bug 未修)
        # PPTX 保 soft(待 GT 补)
        for fmt, threshold, is_hard in (
            ("pdf", 0.70, False),
            ("xlsx", 0.85, True),     # ⬆ D7 hard
            ("docx", 0.95, True),
            ("pptx", 0.75, False),
        ):
            v = ing.get(f"binding_jaccard_{fmt}")
            if v is not None:
                gate_entry = {
                    "target": f">= {threshold}" + (" hard (D7 升档)" if is_hard else " soft"),
                    "value": round(v, 4),
                    "pass": (v >= threshold),
                }
                # DOCX 来源标注:strict_fixture 时口径是 micro-accuracy(SOP-only)
                # 而非 Jaccard,审计/复盘要能一眼看出
                src = (ing.get("per_fmt", {}).get(fmt, {}) or {}).get("_source")
                if fmt == "docx" and src == "strict_fixture":
                    gate_entry["notes"] = "per-image micro-acc via fuling_chunk_exp fixture (SOP-only)"
                gates[f"binding {fmt} Jaccard (L4-ing)"] = gate_entry
        dup = ing.get("img_dup_factor_p95")
        if dup is not None:
            gates["img_dup_factor p95 (L4-ing, 全格式)"] = {
                "target": "<= 1.20 hard (>1.5 是已知 over-attach bug)",
                "value": round(dup, 4),
                "pass": (dup <= 1.20),
            }
        # — L4-serving 支柱(LLM 标记摆放质量)—
        # 样本太小(N<5)自动降级:plan 抢救点,2-3 题样本上 marker_validity/orphan
        # 等指标 noise 极大,FAIL 不具诊断价值,改 N/A 仅 trend 监控
        srv = l4.get("aggregate") or {}
        n_srv = srv.get("n_answers_with_images") or 0
        srv_degraded = n_srv < 5
        mv = srv.get("marker_validity")
        if mv is not None:
            # 0.95 hard 守底 + 0.98 soft 追(双阈值,plan 抢救点)
            gates["<<IMG:N>> marker validity (L4-srv)"] = {
                "target": (f">= 0.95 hard / >= 0.98 soft (N={n_srv} 太小,trend 监控)"
                           if srv_degraded else ">= 0.95 hard / >= 0.98 soft"),
                "value": round(mv, 4),
                "pass": None if srv_degraded else (mv >= 0.95),
            }
        dn = srv.get("dangling_ref_rate")
        if dn is not None:
            gates["dangling 口惠图但卡片无图 (L4-srv)"] = {
                "target": (f"<= 0.05 hard (N={n_srv} 太小,trend 监控)"
                           if srv_degraded else "<= 0.05 hard (扩正则后)"),
                "value": round(dn, 4),
                "pass": None if srv_degraded else (dn <= 0.05),
            }
        op = srv.get("orphan_rate")
        if op is not None:
            gates["orphan rate (L4-srv, trend 监控)"] = {
                "target": (f"<= 0.30 soft (N={n_srv} 太小,仅 trend)"
                           if srv_degraded else "<= 0.30 soft"),
                "value": round(op, 4),
                "pass": None if srv_degraded else (op <= 0.30),
            }

    l5 = r.get("l5")
    if l5:
        if l5.get("applicable") is False:
            gates["permission filtering (L5)"] = {"target": "n/a (no gated docs)",
                                                  "value": "not exercised", "pass": None}
        else:
            gates["permission filtering (L5)"] = {"target": "no leak + injection-safe",
                                                  "value": l5.get("PASS"), "pass": bool(l5.get("PASS"))}

    j = r.get("judge", {}).get("aggregate") if r.get("judge") else None
    if j:
        # 2026-06-12 修:value=None(本轮 bundle 不含 positive 类 case,如 L4-only run)
        # 应当 N/A(pass=None),不应误判 FAIL
        def _ge(v, t): return None if v is None else (v >= t)
        def _le(v, t): return None if v is None else (v <= t)

        faith = _g(j, "positives", "faithfulness", "mean")
        corr = _g(j, "positives", "correctness", "mean")
        gates["answer faithfulness (Claude, L3)"] = {"target": ">= 4.0 / 5",
                                                     "value": faith,
                                                     "pass": _ge(faith, 4.0)}
        gates["answer correctness (Claude, L3)"] = {"target": ">= 4.0 / 5",
                                                    "value": corr,
                                                    "pass": _ge(corr, 4.0)}
        # 完整性质量门：评审面板 completeness（四维中历史最弱，4.29@predeploy）
        comp = _g(j, "positives", "completeness", "mean")
        gates["answer completeness (Claude, L3)"] = {"target": ">= 4.0 / 5",
                                                     "value": comp,
                                                     "pass": _ge(comp, 4.0)}
        fab = j.get("positives_fabrication_rate")
        gates["positive fabrication (Claude, L3)"] = {"target": "<= 0.05", "value": fab,
                                                      "pass": _le(fab, 0.05)}
        nfab = _g(j, "negatives", "fabrication_rate")
        if nfab is not None:
            gates["negative fabrication (Claude, L3)"] = {"target": "<= 0.10", "value": nfab,
                                                          "pass": nfab <= 0.10}
        # L4-ingestion Claude binding 维度(2026-06-12 加入,UNIFIED-L4 plan)
        ib_mean = _g(j, "binding", "image_binding", "mean")
        ib_n = _g(j, "binding", "n")
        if ib_mean is not None and (ib_n or 0) >= 5:   # N<5 时不计闸(plan 抢救点:样本太小自动降级)
            gates["image binding (Claude, L4-ing)"] = {
                "target": ">= 4.0 / 5 soft (N>=5 才计闸)",
                "value": ib_mean,
                "pass": ib_mean >= 4.0,
            }
    return gates


def _md(r: Dict, gates: Dict) -> str:
    L = []
    meta = r.get("meta", {})
    L.append("# HA3 RAG — End-to-End Evaluation Report\n")
    L.append(f"- **Run**: {meta.get('run_id','?')}  |  **Table**: `{meta.get('table','?')}`  "
             f"|  **Generated**: {meta.get('timestamp','?')}")
    L.append(f"- **Gold cases run**: {meta.get('n_cases','?')}  "
             f"|  **LLM**: `{meta.get('llm_model','?')}` (thinking OFF)  "
             f"|  **Judge**: Claude panel (independent of generator)")
    L.append(f"- **Env**: {meta.get('rag_environment','?')}, simulate={meta.get('simulate','?')}, "
             f"endpoint=`{meta.get('ha3_endpoint','?')}` (read-only)\n")

    n_pass = sum(1 for g in gates.values() if g["pass"] is True)
    n_fail = sum(1 for g in gates.values() if g["pass"] is False)
    L.append(f"## Verdict: {n_pass} passed / {n_fail} failed / "
             f"{sum(1 for g in gates.values() if g['pass'] is None)} n-a\n")
    L.append("| Gate | Target | Value | Result |")
    L.append("|---|---|---|---|")
    for name, g in gates.items():
        mark = "✅ PASS" if g["pass"] is True else ("❌ FAIL" if g["pass"] is False else "➖ N/A")
        L.append(f"| {name} | {g['target']} | {g['value']} | {mark} |")
    L.append("")

    if r.get("l0"):
        l0 = r["l0"]
        L.append("## L0 — Index Health\n")
        L.append(f"- status/docCount: {json.dumps(l0.get('G0_status_doccount'), ensure_ascii=False)}")
        L.append(f"- dense self-query: {json.dumps(l0.get('G2_dense_self_query'), ensure_ascii=False)}")
        L.append(f"- sparse self-query: {json.dumps(l0.get('G3_sparse_self_query'), ensure_ascii=False)}")
        L.append(f"- vector fidelity (drift): {json.dumps(l0.get('G4_vector_fidelity'), ensure_ascii=False)}")
        if l0.get("duplicate_content_diagnostic"):
            L.append(f"- duplicate-content diagnostic: {json.dumps(l0.get('duplicate_content_diagnostic'), ensure_ascii=False)}")
        L.append("")

    if r.get("l1"):
        l1 = r["l1"]
        L.append("## L1 — Retrieval Ranking\n")
        L.append(f"- scorable positives: {l1.get('n_positive_public')} public / "
                 f"{l1.get('n_positive_scorable')} total  |  permission-gated excluded: "
                 f"{l1.get('n_permission_gated')}  |  negatives: {l1.get('n_negative')}")
        L.append(f"- **ranking (single-target)**: {json.dumps(l1.get('ranking'), ensure_ascii=False)}")
        if l1.get("ranking_multidoc"):
            L.append(f"- ranking (multi-doc, single-rank proxy): {json.dumps(l1.get('ranking_multidoc'), ensure_ascii=False)}")
        if l1.get("content_hit_rate") is not None:
            L.append(f"- content-hit rate (keyword GT in retrieved context, robust to mislabeled gold): "
                     f"{l1.get('content_hit_rate')} over {l1.get('n_content_hit_cases')} cases")
        L.append(f"- by module: {json.dumps(l1.get('by_module'), ensure_ascii=False)}")
        L.append(f"- by source: {json.dumps(l1.get('by_source'), ensure_ascii=False)}")
        L.append(f"- by difficulty: {json.dumps(l1.get('by_difficulty'), ensure_ascii=False)}")
        L.append(f"- latency (ms): {json.dumps(l1.get('latency_ms'), ensure_ascii=False)}\n")

    if r.get("l2"):
        L.append("## L2 — Score Calibration\n")
        for k, v in r["l2"].items():
            if k == "notes":
                for n in v:
                    L.append(f"  - ⚠️ {n}")
            else:
                L.append(f"- {k}: {json.dumps(v, ensure_ascii=False)}")
        L.append("")

    if r.get("l3"):
        L.append("## L3 — Answer Quality (deterministic)\n")
        L.append(f"```json\n{json.dumps(r['l3']['deterministic'], ensure_ascii=False, indent=1)}\n```\n")
    if r.get("judge"):
        L.append("## L3 — Answer Quality (Claude panel)\n")
        L.append(f"```json\n{json.dumps(r['judge']['aggregate'], ensure_ascii=False, indent=1)}\n```\n")

    if r.get("l4") and r["l4"].get("applicable"):
        l4 = r["l4"]
        # — Ingestion 支柱 —
        ing = l4.get("ingestion")
        if ing:
            L.append("## L4-ingestion — 摄入侧图文绑定精度(逐格式 Jaccard)\n")
            L.append(f"```json\n{json.dumps(ing.get('deterministic') or {}, ensure_ascii=False, indent=1)}\n```\n")
            n_docs = len(ing.get("per_doc") or [])
            n_judge = len(ing.get("judge_bundle_binding") or [])
            L.append(f"- evaluated docs: {n_docs}  |  judge_bundle_binding items: {n_judge}\n")
        # — Serving 支柱 —
        if l4.get("serving_applicable", True):
            L.append("## L4-serving — `<<IMG:N>>` 摆放质量(LLM 行为)\n")
            L.append(f"```json\n{json.dumps(l4.get('aggregate', {}), ensure_ascii=False, indent=1)}\n```\n")

    if r.get("l5"):
        L.append("## L5 — Permission Filtering\n")
        L.append(f"```json\n{json.dumps(r['l5'], ensure_ascii=False, indent=1)}\n```\n")

    return "\n".join(L)


def write(r: Dict, outdir: str) -> Dict:
    os.makedirs(outdir, exist_ok=True)
    gates = build_gates(r)
    r["gates"] = gates
    json.dump(r, open(os.path.join(outdir, "report.json"), "w"),
              ensure_ascii=False, indent=1, default=str)
    md = _md(r, gates)
    open(os.path.join(outdir, "report.md"), "w").write(md)
    html_doc = ("<!doctype html><meta charset=utf-8><title>HA3 RAG Eval</title>"
                "<style>body{font:14px/1.5 -apple-system,system-ui,sans-serif;max-width:1000px;"
                "margin:2rem auto;padding:0 1rem}pre{background:#f6f8fa;padding:1rem;overflow:auto;"
                "border-radius:6px}table{border-collapse:collapse}td,th{border:1px solid #ddd;"
                "padding:4px 8px}</style><pre>" + html.escape(md) + "</pre>")
    open(os.path.join(outdir, "report.html"), "w").write(html_doc)
    return gates
