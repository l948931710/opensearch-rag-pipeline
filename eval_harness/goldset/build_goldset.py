"""Build the unified gold set for the live HA3 RAG eval.

Sources (the user asked for BOTH):
  1. Official 200-case template  ->  scratch/xlsx_goldset_raw.json  (dumped from
     一期模型评测集和评测模板_v2.0(1).xlsx; 评测集 sheet)
  2. JSON retrieval set (51)     ->  eval_samples/ground_truth/text_eval_queries.json
  3. Multimodal set (19)         ->  eval_samples/ground_truth/gt_answer_images.json
     (+ text_eval_doc_registry.json for target_doc -> Chinese-name mapping)

Each gold case is resolved against the LIVE document inventory (document_meta JOIN active
chunk_meta). A positive case is `live_scorable` only if at least one of its expected docs
resolves to a doc actually in the live index — so retrieval recall is never unfairly charged
for a document that was never indexed. Negatives are always scorable (they test interception).

Outputs (eval_harness/goldset/):
  golden_full.json        - every unified case with resolution metadata
  golden_50.json          - the stratified ~50 representative live run-set
  resolution_report.json  - inventory size, resolved/unresolved counts, coverage table
"""
from __future__ import annotations

import json
import os
import random
from collections import defaultdict, Counter
from typing import Dict, List

from .. import envboot  # noqa: F401
from ..matching import parse_expected_docs, title_similarity

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
EVAL_DATA = os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples/ground_truth")

XLSX_RAW = os.path.join(ROOT, "scratch", "xlsx_goldset_raw.json")
JSON_TEXT = os.path.join(EVAL_DATA, "text_eval_queries.json")
JSON_MM = os.path.join(EVAL_DATA, "gt_answer_images.json")
JSON_REG = os.path.join(EVAL_DATA, "text_eval_doc_registry.json")

# target_doc key -> Chinese doc name (registry + known eval-corpus names) for JSON resolution
DOC_NAME_MAP = {
    "zicai_manual": "富岭U8+资材部操作手册", "renshi_manual": "富岭U8+人事部操作手册",
    "hr_xiaofang": "A50消防安全知识", "admin_gongzuofu": "工作服管理规定",
    "clean_lapian": "设备清扫基准书-拉片机", "pdf_sop": "注塑成品收货报检作业指导书",
    "pdf_it_install": "电脑安装作业指导书", "pdf_admin_visitor": "关于外来人员来访留宿相关规定",
    "docx_sop": "印刷产品检验作业指导书", "docx_water": "奶茶杯测水试验作业指导书",
    "docx_qc": "不干胶标签检验作业指导书", "docx_manual": "富岭U8+财务操作手册",
    "xlsx_sop": "电子天平使用作业指导书", "xlsx_spec": "纸杯产品规格书",
    "xlsx_inspect": "纸杯过程检验记录表", "pptx_training": "产品培训资料",
}

_MODULE_MAP = {"RAG检索": "rag_retrieval", "自然语言问答": "nlq", "来源标注": "source_attribution"}


def _norm_dept(d):
    return (d or "").strip() or None


def load_inventory() -> List[Dict]:
    from ..ha3live import doc_inventory
    return doc_inventory()


def resolve_docs(expected_names: List[str], inventory: List[Dict], thr: float = 0.6):
    """Return (resolved_doc_ids, best_matches) for the expected doc names vs live inventory."""
    resolved, matches = [], []
    for name in expected_names:
        best, best_s = None, 0.0
        for inv in inventory:
            s = max(title_similarity(name, inv.get("title") or ""),
                    title_similarity(name, inv.get("original_filename") or ""))
            if s > best_s:
                best_s, best = s, inv
        if best and best_s >= thr:
            resolved.append(best["doc_id"])
            matches.append({"expected": name, "title": best.get("title"),
                            "doc_id": best["doc_id"], "sim": round(best_s, 3),
                            "permission_level": best.get("permission_level"),
                            "owner_dept": best.get("owner_dept")})
        else:
            matches.append({"expected": name, "title": None, "doc_id": None,
                            "sim": round(best_s, 3), "permission_level": None,
                            "owner_dept": None})
    return resolved, matches


def build_records(inventory: List[Dict]) -> List[Dict]:
    records: List[Dict] = []

    # 1) xlsx 200
    xlsx = json.load(open(XLSX_RAW, encoding="utf-8"))
    for r in xlsx:
        qid = str(r.get("用例编号") or "").strip()
        if not qid:
            continue
        module = _MODULE_MAP.get(r.get("评测模块"), r.get("评测模块"))
        subtype = (r.get("评测子类型") or "").strip()
        kind = "negative" if subtype.startswith("负例") else "positive"
        expected_names = parse_expected_docs(r.get("预期来源文档"))
        resolved, matches = resolve_docs(expected_names, inventory)
        records.append({
            "qid": qid, "source": "xlsx", "module": module, "subtype": subtype,
            "dept": _norm_dept(r.get("所属部门")), "query": (r.get("用户问题(Query)") or "").strip(),
            "kind": kind, "expected_docs": expected_names, "expected_doc_ids": resolved,
            "resolution": matches, "answer_points": (r.get("标准答案要点") or "").strip(),
            "pass_criteria": (r.get("通过标准") or "").strip(), "keyword_gt": [],
            "difficulty": None, "expect_images": False, "expected_images": [],
            "expected_permission": sorted({m["permission_level"] for m in matches
                                           if m.get("doc_id") and m.get("permission_level")}),
            "live_scorable": (kind == "negative") or bool(resolved),
        })

    # 2) JSON text set (51)
    mm = {q["query_id"]: q for q in json.load(open(JSON_MM, encoding="utf-8")).get("queries", [])}
    text = json.load(open(JSON_TEXT, encoding="utf-8")).get("queries", [])
    for q in text:
        qid = "J-" + q["query_id"]
        target = q.get("target_doc")
        name = DOC_NAME_MAP.get(target, target)
        kind = q.get("kind", "positive")
        resolved, matches = resolve_docs([name], inventory) if kind == "positive" else ([], [])
        mmq = mm.get(q["query_id"], {})
        records.append({
            "qid": qid, "source": "json_text", "module": "rag_retrieval_json",
            "subtype": q.get("source", ""), "dept": None, "query": q["query"], "kind": kind,
            "expected_docs": [name] if kind == "positive" else [],
            "expected_doc_ids": resolved, "resolution": matches, "answer_points": "",
            "pass_criteria": "Top5召回目标文档", "keyword_gt": q.get("keyword_gt", []),
            "difficulty": q.get("difficulty"),
            "expect_images": bool(mmq.get("expect_images")),
            "expected_images": mmq.get("expected_images", []),
            "expected_permission": sorted({m["permission_level"] for m in matches
                                           if m.get("doc_id") and m.get("permission_level")}),
            "live_scorable": (kind == "negative") or bool(resolved),
        })

    return records


def stratified_50(records: List[Dict], n: int = 50, seed: int = 7) -> List[Dict]:
    """Diversity-first selection across module/dept/difficulty/kind, live-scorable only.

    Floors: keep a healthy share of negatives (interception) and at least a few image queries.
    """
    rng = random.Random(seed)
    pool = [r for r in records if r["live_scorable"]]
    negs = [r for r in pool if r["kind"] == "negative"]
    poss = [r for r in pool if r["kind"] == "positive"]
    imgs = [r for r in poss if r["expect_images"]]

    rng.shuffle(negs); rng.shuffle(poss); rng.shuffle(imgs)

    n_neg = min(len(negs), max(10, round(n * 0.24)))   # ~12 negatives
    n_img = min(len(imgs), 4)
    n_pos = n - n_neg

    chosen, seen = [], set()

    def take(r):
        if r["qid"] not in seen:
            seen.add(r["qid"]); chosen.append(r)

    for r in imgs[:n_img]:
        take(r)

    # round-robin positives across (module, dept, difficulty) buckets for diversity
    buckets = defaultdict(list)
    for r in poss:
        key = (r["module"], r.get("dept"), r.get("difficulty"))
        buckets[key].append(r)
    keys = list(buckets.keys()); rng.shuffle(keys)
    while len([c for c in chosen if c["kind"] == "positive"]) < n_pos:
        progressed = False
        for k in keys:
            if buckets[k]:
                r = buckets[k].pop()
                if r["qid"] not in seen:
                    take(r); progressed = True
                if len([c for c in chosen if c["kind"] == "positive"]) >= n_pos:
                    break
        if not progressed:
            break

    for r in negs[:n_neg]:
        take(r)

    return chosen[:n]


def main():
    print("Loading live document inventory (read-only RDS)...")
    inventory = load_inventory()
    print(f"  live docs with active chunks: {len(inventory)}")

    records = build_records(inventory)
    pos = [r for r in records if r["kind"] == "positive"]
    pos_scorable = [r for r in pos if r["live_scorable"]]
    report = {
        "inventory_size": len(inventory),
        "total_cases": len(records),
        "by_source": dict(Counter(r["source"] for r in records)),
        "by_module": dict(Counter(r["module"] for r in records)),
        "by_kind": dict(Counter(r["kind"] for r in records)),
        "positives": len(pos),
        "positives_live_scorable": len(pos_scorable),
        "positives_unresolved": len(pos) - len(pos_scorable),
        "unresolved_examples": [
            {"qid": r["qid"], "expected_docs": r["expected_docs"]}
            for r in pos if not r["live_scorable"]
        ][:25],
    }

    chosen = stratified_50(records)
    report["selected_50"] = {
        "n": len(chosen),
        "by_module": dict(Counter(r["module"] for r in chosen)),
        "by_dept": dict(Counter(r.get("dept") for r in chosen)),
        "by_kind": dict(Counter(r["kind"] for r in chosen)),
        "by_difficulty": dict(Counter(r.get("difficulty") for r in chosen)),
        "image_queries": sum(1 for r in chosen if r["expect_images"]),
    }

    json.dump(records, open(os.path.join(HERE, "golden_full.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(chosen, open(os.path.join(HERE, "golden_50.json"), "w"),
              ensure_ascii=False, indent=1)
    json.dump(report, open(os.path.join(HERE, "resolution_report.json"), "w"),
              ensure_ascii=False, indent=2)

    print("\n=== RESOLUTION REPORT ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nWrote golden_full.json ({len(records)}), golden_50.json ({len(chosen)}), "
          f"resolution_report.json -> {HERE}")


if __name__ == "__main__":
    main()
