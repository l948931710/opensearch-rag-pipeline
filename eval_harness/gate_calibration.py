# -*- coding: utf-8 -*-
"""gate_calibration.py — 离线标定「可答性闸门」阈值。

数据源：reports/rerank_ab_v2.json（251 题 × hybrid top-20 池 × 路由式重排实测分）。
闸门语义：重排后 top-1 rerank 分 < 阈值 ⇒ 判定知识库无法回答 ⇒ 走 NO_RESULT。
  - 拦截率（TPR）：负例（不可答）被闸门拦下的比例 —— 越高越好
  - 误拒率（FPR）：正例（可答）被误拦的比例 —— 即新增 over-refusal，必须很小

路由复现：n_img_in_pool>0 → qwen3-vl-rerank，否则 qwen3-rerank（与 reranker.py 一致，
rerank_route_vl=True）。VL 被跳过（无图）时回落文本分。

用法：python -m eval_harness.gate_calibration
"""
import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
AB_PATH = os.path.join(HERE, "reports", "rerank_ab_v2.json")


def routed_top1(p):
    """复现 reranker.py 的路由：池含图 → VL，否则文本；VL skip 时回落文本。"""
    models = p.get("models", {})
    vl = models.get("qwen3-vl-rerank", {})
    txt = models.get("qwen3-rerank", {})
    if p.get("n_img_in_pool", 0) > 0 and vl.get("top1") is not None:
        return float(vl["top1"]), "vl"
    if txt.get("top1") is not None:
        return float(txt["top1"]), "text"
    return None, None


def sweep(pos, neg, thresholds):
    rows = []
    for t in thresholds:
        tpr = sum(1 for s in neg if s < t) / len(neg)
        fpr = sum(1 for s in pos if s < t) / len(pos)
        rows.append({"threshold": round(t, 3), "neg_intercept": round(tpr, 4),
                     "pos_overrefusal": round(fpr, 4), "youden_j": round(tpr - fpr, 4)})
    return rows


def boot_ci(scores, t, below=True, n_boot=4000, seed=7):
    rng = random.Random(seed)
    n = len(scores)
    stats = []
    for _ in range(n_boot):
        sample = [scores[rng.randrange(n)] for _ in range(n)]
        frac = sum(1 for s in sample if (s < t) == below) / n
        stats.append(frac)
    stats.sort()
    return [round(stats[int(0.025 * n_boot)], 4), round(stats[int(0.975 * n_boot)], 4)]


def main():
    data = json.load(open(AB_PATH, encoding="utf-8"))
    pos, neg, pos_by_route, neg_by_route = [], [], {"text": [], "vl": []}, {"text": [], "vl": []}
    skipped = 0
    for p in data["per_query"]:
        s, route = routed_top1(p)
        if s is None:
            skipped += 1
            continue
        if p["kind"] == "positive":
            pos.append(s)
            pos_by_route[route].append(s)
        else:
            neg.append(s)
            neg_by_route[route].append(s)

    print(f"n_pos={len(pos)} n_neg={len(neg)} skipped={skipped}")
    for route in ("text", "vl"):
        ps, ns = pos_by_route[route], neg_by_route[route]
        pm = sum(ps) / len(ps) if ps else float("nan")
        nm = sum(ns) / len(ns) if ns else float("nan")
        print(f"route={route}: n_pos={len(ps)} (mean {pm:.3f})  n_neg={len(ns)} (mean {nm:.3f})")

    ths = [i / 200 for i in range(60, 200)]  # 0.30 .. 0.995
    rows = sweep(pos, neg, ths)

    print("\n— 关键工作点（全体，路由后分数）—")
    best_j = max(rows, key=lambda r: r["youden_j"])
    print(f"max Youden J: {best_j}")
    for cap in (0.0, 0.01, 0.02, 0.03, 0.05):
        ok = [r for r in rows if r["pos_overrefusal"] <= cap]
        if ok:
            best = max(ok, key=lambda r: (r["neg_intercept"], -r["threshold"]))
            print(f"max intercept s.t. over-refusal<={cap:.2f}: {best}")

    print("\n— 阈值细扫 0.55–0.80 —")
    for r in rows:
        if 0.55 <= r["threshold"] <= 0.80 and abs(r["threshold"] * 100 % 2.5) < 1e-9:
            print(r)

    # 负例分数明细（n=26，逐个看分布，便于人工判断拐点）
    print("\nneg scores sorted:", [round(s, 3) for s in sorted(neg)])
    print("pos scores below 0.8 (sorted):", [round(s, 3) for s in sorted(pos) if s < 0.8])

    # 推荐点的 bootstrap CI
    for t in (0.60, 0.65, 0.70, 0.75):
        ci_tpr = boot_ci(neg, t)
        ci_fpr = boot_ci(pos, t)
        tpr = sum(1 for s in neg if s < t) / len(neg)
        fpr = sum(1 for s in pos if s < t) / len(pos)
        print(f"t={t}: intercept={tpr:.3f} CI{ci_tpr}  over-refusal={fpr:.4f} CI{ci_fpr}")

    out = {"n_pos": len(pos), "n_neg": len(neg), "sweep": rows,
           "neg_scores": sorted(neg), "pos_scores_below_0.85": sorted(s for s in pos if s < 0.85)}
    out_path = os.path.join(HERE, "reports", "gate_calibration.json")
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nwrote", out_path)


if __name__ == "__main__":
    main()
