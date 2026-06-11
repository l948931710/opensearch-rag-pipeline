# -*- coding: utf-8 -*-
"""collect_questions.py — 批量复测采集（经 /api/ask，串行逐题）。

输入题集 json：[{"qid": "...", "query": "...", "expected_doc_ids": [...](可选),
               "expected_titles": [...](可选), "expect_images": bool(可选)}, ...]
输出逐题：rank（按 expected_doc_ids 或归一化标题匹配）/ no_result / guard /
图数与 captions / 延迟。25 题集 scratch/local_e2e_answers.json 可直接喂
（条目含 prod_old/local_new 历史字段会被忽略）。

用法（从仓库根运行，serving 须已起）：
  python3 …/collect_questions.py <题集.json> [--base http://127.0.0.1:8001] \
      [--out scratch/collect_<日期>.json] [--alias '{"旧doc_id":"等价doc_id"}']
"""
import argparse
import json
import re
import time

import requests

ap = argparse.ArgumentParser()
ap.add_argument("questions")
ap.add_argument("--base", default="http://127.0.0.1:8001")
ap.add_argument("--out", default="")
ap.add_argument("--alias", default="{}", help="doc_id 等价映射 json（退役孪生→保留版）")
args = ap.parse_args()

ALIAS = json.loads(args.alias)


def norm_id(doc_id):
    d = re.sub(r"^LOCALE2E(OLD)?_", "", (doc_id or ""))
    return ALIAS.get(d, d)


def norm_title(t):
    # 标题别名漂移防护：去"部"/空格/扩展名（pitfalls §标题匹配假阴）
    return re.sub(r"[部\s]|\.(docx|pdf|xlsx|pptx)$", "", str(t or ""), flags=re.I)


cases = json.load(open(args.questions, encoding="utf-8"))
results = []
for i, case in enumerate(cases, 1):
    qid, query = case.get("qid", f"q{i}"), case["query"]
    t0 = time.time()
    try:
        r = requests.post(f"{args.base}/api/ask",
                          json={"question": query, "user_id": "collect-questions"}, timeout=180)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        print(f"[{i:2d}/{len(cases)}] {qid}: ❌ {type(e).__name__}: {e}", flush=True)
        results.append({"qid": qid, "query": query, "error": str(e)})
        continue
    srcs = j.get("sources") or []
    rank = None
    exp_ids = {norm_id(e) for e in (case.get("expected_doc_ids") or [])}
    exp_titles = [norm_title(e) for e in (case.get("expected_titles") or case.get("expected_docs") or [])]
    for k, s in enumerate(srcs, 1):
        if (exp_ids and norm_id(s.get("doc_id")) in exp_ids) or \
           (exp_titles and any(e and e in norm_title(s.get("title")) for e in exp_titles)):
            rank = k
            break
    imgs = [b for b in (j.get("blocks") or []) if b.get("type") == "image"]
    entry = {"qid": qid, "query": query,
             "rank": rank, "no_result": bool(j.get("no_result")), "guard": j.get("guard"),
             "n_images": len(imgs),
             "captions": [(b.get("caption") or "")[:60] for b in imgs],
             "sources": [{k: s.get(k) for k in ("doc_id", "title", "section", "score", "level")} for s in srcs],
             "answer": j.get("answer", ""),
             "latency_ms": int((time.time() - t0) * 1000)}
    results.append(entry)
    print(f"[{i:2d}/{len(cases)}] {qid}: rank={rank} imgs={len(imgs)} "
          f"no_result={entry['no_result']} {entry['latency_ms']}ms", flush=True)

out = args.out or f"scratch/collect_{time.strftime('%Y%m%d_%H%M')}.json"
json.dump(results, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
n_ok = sum(1 for e in results if e.get("rank") == 1 and not e.get("no_result"))
print(f"\n✅ {len(results)} 题 → {out} | rank=1 且非拒答: {n_ok}")
