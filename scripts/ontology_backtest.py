# -*- coding: utf-8 -*-
"""
ontology_backtest.py — 身份消解 GT 回测（本体 P0 PR14；外评 S9 验收分层）。

输入 GT CSV（gate ③ 的 50–100 标注对；列契约见下）→ 对每条跑 OntologyResolver.resolve
（**纯读**，绝不落库）→ 输出 S9 分层指标 + **false-merge 率单列硬门** + would-auto 仿真
（auto_eligible 纯函数——不经 may_auto_activate 的 ack 闸，评估≠放行）。

GT CSV 列契约（UTF-8，带表头）：
  namespace     命名空间（u8 / customer:<名> / …）
  raw_code      原始编号/名称
  expected_ref  期望的 canonical_ref（FLP-…）；**留空 = 该编号在本体中不应有对应对象**

判定口径（S9）：
  - exact_correct    resolve→resolved 且 ref 一致
  - false_merge      resolved 但 ref 不一致 / GT 说不存在却 resolved —— **硬门单列**
  - candidate_hit    未 resolved，但期望对象在候选列表里（HITL 可救）
  - candidate_miss   有候选但期望对象不在其中
  - unresolved_miss  期望存在却完全无候选（false-split/unresolved 桶）
  - true_negative    GT 说不存在且未 resolved（有候选记 candidate_noise，计 HITL 率）
  would-auto 仿真：auto_eligible 判定若放行 auto 会自动铸什么——auto_wrong>0 即硬门破。

出口（供 Sam 签发 AUTO_ACK）：打印 GT 文件 sha256 + manifest 骨架（source_sha256/
environment/date 已填；gt_summary 用本轮指标；**签名密钥是 Sam 的**，本工具不签名）。

Usage:
  python scripts/ontology_backtest.py gt.csv                  # 按当前 RAG_ENV 的 store 回测
  python scripts/ontology_backtest.py gt.csv --max-false-merge-rate 0 --out report.json
退出码：0=过门；2=硬门破（false-merge 超限 / auto_wrong>0）；3=输入/环境错误。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from typing import Any, Dict, List

sys.path.insert(0, ".")


def _read_gt(path: str) -> List[Dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"namespace", "raw_code", "expected_ref"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"GT CSV 缺少必需列：{sorted(missing)}（现有：{reader.fieldnames}）")
        rows = []
        for row in reader:
            if not (row.get("raw_code") or "").strip():
                continue
            rows.append({"namespace": row["namespace"].strip(),
                         "raw_code": row["raw_code"].strip(),
                         "expected_ref": (row.get("expected_ref") or "").strip().upper()})
        return rows


def run_backtest(store, rows: List[Dict[str, str]], *, tau_table=None) -> Dict[str, Any]:
    """纯读回测核心（store 注入供单测/staging 复用）。返回完整报告 dict。"""
    from opensearch_pipeline.ontology.resolve import OntologyResolver, TauTable, auto_eligible
    tau = tau_table or TauTable.from_env(strict=True)   # 写 worker 同款 strict：τ 配坏即断
    resolver = OntologyResolver(store, tau_table=tau)

    verdicts: List[Dict[str, Any]] = []
    strata: Dict[tuple, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    counters: Dict[str, int] = defaultdict(int)

    for row in rows:
        ns, raw, expected = row["namespace"], row["raw_code"], row["expected_ref"]
        try:
            res = resolver.resolve(ns, raw, intent="read")
        except ValueError as e:
            verdicts.append({**row, "verdict": "input_error", "detail": str(e)})
            counters["input_error"] += 1
            continue
        cand_refs = [(c.canonical_ref or "").upper() for c in res.candidates]
        method = res.method or (res.candidates[0].method if res.candidates else None)
        if expected:
            if res.status == "resolved":
                verdict = ("exact_correct"
                           if (res.canonical_ref or "").upper() == expected else "false_merge")
            elif expected in cand_refs:
                verdict = "candidate_hit"
            elif res.candidates:
                verdict = "candidate_miss"
            else:
                verdict = "unresolved_miss"
        else:
            if res.status == "resolved":
                verdict = "false_merge"           # GT 判不存在却给出正式映射
            elif res.candidates:
                verdict = "candidate_noise"       # 无中生有候选：HITL 噪音，非硬门
            else:
                verdict = "true_negative"
        counters[verdict] += 1
        strata[(ns, method or "-")][verdict] += 1

        winner = auto_eligible(res.candidates, intent="read", namespace=ns, tau_table=tau)
        would_auto = None
        if winner is not None:
            would_auto = (winner.canonical_ref or "").upper()
            counters["would_auto"] += 1
            if expected and would_auto == expected:
                counters["auto_correct"] += 1
            else:
                counters["auto_wrong"] += 1       # 含 GT 判不存在却 would-auto
        verdicts.append({**row, "verdict": verdict, "status": res.status,
                         "got_ref": res.canonical_ref, "method": method,
                         "confidence": res.confidence, "would_auto": would_auto})

    n_expected = sum(1 for r in rows if r["expected_ref"])
    n_negative = len(rows) - n_expected
    resolved_total = counters["exact_correct"] + counters["false_merge"]
    hitl = (counters["candidate_hit"] + counters["candidate_miss"]
            + counters["candidate_noise"])
    metrics = {
        "n_rows": len(rows), "n_expected": n_expected, "n_negative": n_negative,
        "exact_correct": counters["exact_correct"], "false_merge": counters["false_merge"],
        "false_merge_rate": (round(counters["false_merge"] / len(rows), 4) if rows else 0.0),
        "resolve_precision": (round(counters["exact_correct"] / resolved_total, 4)
                              if resolved_total else None),
        "resolve_recall": (round(counters["exact_correct"] / n_expected, 4)
                           if n_expected else None),
        "candidate_hit": counters["candidate_hit"],
        "candidate_miss": counters["candidate_miss"],
        "unresolved_miss": counters["unresolved_miss"],
        "unresolved_rate": (round(counters["unresolved_miss"] / n_expected, 4)
                            if n_expected else None),
        "true_negative": counters["true_negative"],
        "candidate_noise": counters["candidate_noise"],
        "hitl_rate": (round(hitl / len(rows), 4) if rows else 0.0),
        "would_auto": counters["would_auto"], "auto_correct": counters["auto_correct"],
        "auto_wrong": counters["auto_wrong"],
        "input_error": counters["input_error"],
    }
    per_stratum = {f"{ns}|{m}": dict(v) for (ns, m), v in sorted(strata.items())}
    return {"metrics": metrics, "strata": per_stratum, "verdicts": verdicts}


def _manifest_skeleton(gt_path: str, metrics: Dict[str, Any]) -> Dict[str, Any]:
    from datetime import date

    from opensearch_pipeline.config import get_config
    h = hashlib.sha256()
    with open(gt_path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return {
        "op": "ontology-auto-activation",
        "date": date.today().isoformat(),
        "docset": f"GT 标注对（gate ③）：{gt_path}",
        "gt_summary": (f"backtest：n={metrics['n_rows']} exact={metrics['exact_correct']} "
                       f"false_merge={metrics['false_merge']} "
                       f"auto_wrong={metrics['auto_wrong']}"),
        "signer": "<Sam 填写>",
        "source_sha256": h.hexdigest(),
        "environment": get_config().environment,
    }


def main(argv=None, *, store=None) -> int:
    p = argparse.ArgumentParser(description="身份消解 GT 回测（S9 分层指标 + false-merge 硬门）")
    p.add_argument("gt_csv", help="GT 标注对 CSV（namespace,raw_code,expected_ref）")
    p.add_argument("--max-false-merge-rate", type=float, default=0.0,
                   help="false-merge 率硬门（默认 0——任何误合并都破门）")
    p.add_argument("--out", default=None, help="完整报告 JSON 输出路径（含逐条 verdict）")
    p.add_argument("--show", type=int, default=10, help="打印前 N 条非 correct 的 verdict")
    args = p.parse_args(argv)

    try:
        rows = _read_gt(args.gt_csv)
    except (OSError, ValueError) as e:
        print(f"❌ GT 读取失败：{e}", file=sys.stderr)
        return 3
    if not rows:
        print("❌ GT 为空", file=sys.stderr)
        return 3

    if store is None:
        from opensearch_pipeline.config import get_config
        from opensearch_pipeline.ontology.store import RDSOntologyStore
        cfg = get_config()
        print(f"目标库：{cfg.rds.host}/{cfg.rds.ontology_database}（纯读回测，零写库）")
        store = RDSOntologyStore()

    try:
        report = run_backtest(store, rows)
    except ValueError as e:
        print(f"❌ 回测中断：{e}", file=sys.stderr)
        return 3
    m = report["metrics"]

    print("── S9 分层指标 ──")
    for k, v in m.items():
        print(f"  {k:22s} {v}")
    print("── 分层（namespace|method）──")
    for k, v in report["strata"].items():
        print(f"  {k:28s} {json.dumps(v, ensure_ascii=False)}")
    bad = [v for v in report["verdicts"]
           if v["verdict"] not in ("exact_correct", "true_negative")]
    for v in bad[: max(0, args.show)]:
        print(f"  · {v['verdict']:16s} {v['namespace']}:{v['raw_code']} "
              f"期望={v['expected_ref'] or '∅'} 得到={v.get('got_ref')}")
    if len(bad) > args.show:
        print(f"  …（其余 {len(bad) - args.show} 条见 --out 报告）")

    manifest = _manifest_skeleton(args.gt_csv, m)
    print("── AUTO_ACK manifest 骨架（签名密钥是 Sam 的，本工具不签名）──")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"GT sha256：{manifest['source_sha256']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({**report, "manifest_skeleton": manifest}, f,
                      ensure_ascii=False, indent=2, default=str)
        print(f"完整报告 → {args.out}")

    broken = []
    if m["false_merge_rate"] > args.max_false_merge_rate:
        broken.append(f"false_merge_rate {m['false_merge_rate']} > {args.max_false_merge_rate}")
    if m["auto_wrong"] > 0:
        broken.append(f"auto_wrong={m['auto_wrong']}（would-auto 铸错对象）")
    if broken:
        print("❌ 硬门破：" + "；".join(broken), file=sys.stderr)
        return 2
    print("✅ 硬门过（false-merge/auto_wrong 零容忍口径）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
