# -*- coding: utf-8 -*-
"""sensitive_guard_ab.py — v2 敏感查询 guard 的零 LLM 离线 A/B（2026-08-02）。

用法:
  python -m eval_harness.sensitive_guard_ab                      # golden_full 全量
  python -m eval_harness.sensitive_guard_ab --goldset PATH --out PATH

对金集全部 query 直调 intent_router.classify_sensitive_v2（纯判定，不看 flag——
flag 门控语义由 tests/test_intent_router.py 钉死），输出：

  · 正例误伤清单 —— **铁门：必须为空**，非空 exit 1（guard-flip A/B 方法论
    [[guard-flip-ab-verdict-2026-07-09]]：护栏类改动先 guard 层 A/B；guard 命中
    = 确定性话术零生成，故无需再对翻转 case 跑生成探针——不存在生成侧损益）；
  · 负例拦截清单（qid + family），并按 golden_gated_proposal.json 的
    refusal_class taxonomy 分组命中率（taxonomy 尚未并入 golden_full，
    此处只读映射作诊断维度）。

flag off 臂无需实跑：sensitive_guard_route 关闸恒 None（单测钉死），off 臂
逐字节等于现状。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GOLDSET = os.path.join(HERE, "goldset", "golden_full.json")
PROPOSAL_TAXONOMY = os.path.join(HERE, "goldset", "golden_gated_proposal.json")


def _load_refusal_class_map(path: str) -> dict:
    """qid → refusal_class（taxonomy 提案只读映射；文件缺失 → 空映射，不阻塞）。"""
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        return {it["qid"]: it["refusal_class"] for it in items
                if it.get("qid") and it.get("refusal_class")}
    except (OSError, ValueError, KeyError):
        return {}


def run_ab(goldset_path: str) -> dict:
    from opensearch_pipeline.intent_router import classify_sensitive_v2

    with open(goldset_path, encoding="utf-8") as f:
        gold = json.load(f)
    taxonomy = _load_refusal_class_map(PROPOSAL_TAXONOMY)

    pos_hits, neg_hits = [], []
    n_pos = n_neg = 0
    by_class: dict = {}
    for g in gold:
        q = g.get("query")
        if not q:
            continue
        kind = g.get("kind")
        cat = classify_sensitive_v2(q)
        row = {"qid": g.get("qid"), "query": q, "family": cat}
        if kind == "negative":
            n_neg += 1
            rc = taxonomy.get(g.get("qid"), "untagged")
            stats = by_class.setdefault(rc, {"n": 0, "intercepted": 0})
            stats["n"] += 1
            if cat is not None:
                stats["intercepted"] += 1
                neg_hits.append({**row, "refusal_class": rc})
        else:
            n_pos += 1
            if cat is not None:
                pos_hits.append(row)

    return {
        "goldset": os.path.basename(goldset_path),
        "n_positive": n_pos, "n_negative": n_neg,
        "positive_misfires": pos_hits,           # 铁门：必须为空
        "negative_intercepted": neg_hits,
        "negative_interception_by_refusal_class": {
            rc: {**s, "rate": round(s["intercepted"] / s["n"], 3)}
            for rc, s in sorted(by_class.items())
        },
        "ok": not pos_hits,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goldset", default=DEFAULT_GOLDSET)
    ap.add_argument("--out", default=None, help="结果 JSON 落盘路径（可选）")
    args = ap.parse_args(argv)

    res = run_ab(args.goldset)
    print(f"[sensitive_guard_ab] goldset={res['goldset']} "
          f"positives={res['n_positive']} negatives={res['n_negative']}")
    print(f"  正例误伤（铁门，必须为空）: {len(res['positive_misfires'])}")
    for r in res["positive_misfires"]:
        print(f"    ✗ {r['qid']} [{r['family']}] {r['query']}")
    print(f"  负例拦截: {len(res['negative_intercepted'])}/{res['n_negative']}")
    for r in res["negative_intercepted"]:
        print(f"    ✓ {r['qid']} [{r['family']}/{r['refusal_class']}] {r['query']}")
    print("  按 refusal_class（taxonomy 提案，诊断维度）:")
    for rc, s in res["negative_interception_by_refusal_class"].items():
        print(f"    {rc}: {s['intercepted']}/{s['n']} = {s['rate']}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print(f"  → {args.out}")
    if not res["ok"]:
        print("  ❌ 正例误伤非空 —— guard 规则不得进入开启评审", file=sys.stderr)
        return 1
    print("  ✅ 零正例误伤")
    return 0


if __name__ == "__main__":
    sys.exit(main())
