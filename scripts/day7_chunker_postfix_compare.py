#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
day7_chunker_postfix_compare.py — Day 7 N 连跑 deterministic 比对器

读 N 个 run outdir 下的 report.json,抽 l4.ingestion.deterministic:
  - per_fmt (pdf / xlsx / docx) mean_jaccard / std_jaccard
  - per_doc / per_chunk jaccard 列表
  - img_dup_factor p95/max
  - binding_jaccard_{fmt} 顶层快捷字段

输出:
  --out          compare.json     完整结构化对比
  --report       compare.md       人读 markdown(verdict + 4 张表)
  --final-report D7_chunker_postfix_report.md  最终报告(若 PASS 才写,
                                                避免与 D6_hard_lock_report.md 冲突)

退出码:
  0  = ALL_EQUAL ✅ — N 轮 per_chunk byte-equal + per_fmt mean std = 0
  2  = STD_OK ⚠️   — per_chunk 有飘但 per_fmt mean std ≤ 0.02 → 局部修
  1  = DRIFT ❌    — per_fmt mean std > 0.02 → 回炉

设计要点(与 A-runbook verdict 对齐 P0/P1 amendments):
  * per_chunk key 取 gt_label(ingestion_binding.py L208/218/231 实际字段)
  * _safe_std len<2 返 None(不是 0.0),避免单点 chunk 假 byte-equal
  * verdict 门槛:byte-equal 是理想,std ≤ 0.02 也判 OK(用户要求)
  * 最终 D7_chunker_postfix_report.md 仅 PASS 时写,不和 D6_hard_lock_report.md 重名
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ── 阈值 ─────────────────────────────────────────────────────
MEAN_STD_PASS = 0.0       # per_fmt mean across runs 完全相等 → PASS
MEAN_STD_PARTIAL = 0.02   # ≤2pp → PARTIAL(可接受 micro-noise)
DUP_P95_HARD = 1.20       # D6 锁档闸
PER_CHUNK_STD_TOL = 0.0   # per_chunk 必须 byte-equal


# ── IO ───────────────────────────────────────────────────────
def _load_report(outdir: str) -> Optional[Dict[str, Any]]:
    p = os.path.join(outdir, "report.json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _extract_determ(report: Dict[str, Any]) -> Dict[str, Any]:
    """从 report.json 抽 l4.ingestion.deterministic;兼容老结构。"""
    l4 = report.get("l4") or report.get("layers", {}).get("l4") or {}
    ingestion = l4.get("ingestion") or l4.get("ingestion_binding") or {}
    return ingestion.get("deterministic") or ingestion.get("metrics") or {}


def _extract_per_doc(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    l4 = report.get("l4") or report.get("layers", {}).get("l4") or {}
    ingestion = l4.get("ingestion") or l4.get("ingestion_binding") or {}
    return ingestion.get("per_doc") or []


# ── 统计 ─────────────────────────────────────────────────────
def _safe_std(xs: List[float]) -> Optional[float]:
    """len<2 返 None(P1 amendment:避免被当 'stable' 误判)。"""
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if len(xs) < 2:
        return None
    return statistics.stdev(xs)


def _safe_mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not xs:
        return None
    return sum(xs) / len(xs)


def _fmt_num(v: Optional[float], digits: int = 4) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float) and math.isnan(v):
        return "nan"
    return f"{v:.{digits}f}"


# ── 对比 ─────────────────────────────────────────────────────
def compare_per_fmt(determs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    all_fmts = set()
    for d in determs:
        for f in (d.get("per_fmt") or {}).keys():
            all_fmts.add(f)
    for fmt in sorted(all_fmts):
        means: List[Optional[float]] = []
        stds: List[Optional[float]] = []
        n_docs: List[Optional[int]] = []
        for d in determs:
            pf = (d.get("per_fmt") or {}).get(fmt) or {}
            means.append(pf.get("mean_jaccard"))
            stds.append(pf.get("std_jaccard"))
            n_docs.append(pf.get("n_docs"))
        valid_means = [m for m in means if m is not None]
        std_across = _safe_std(valid_means)
        out[fmt] = {
            "means_per_run": means,
            "std_jaccard_per_run": stds,
            "n_docs_per_run": n_docs,
            "mean_across_runs": _safe_mean(valid_means),
            "std_across_runs": std_across,
            "spread_across_runs": (max(valid_means) - min(valid_means)) if len(valid_means) >= 2 else 0.0,
        }
    return out


def compare_dup_p95(determs: List[Dict[str, Any]]) -> Dict[str, Any]:
    p95s = [d.get("img_dup_factor_p95") for d in determs]
    maxs = [d.get("img_dup_factor_max") for d in determs]
    return {
        "p95_per_run": p95s,
        "max_per_run": maxs,
        "all_under_hard": all(
            (p is None or p <= DUP_P95_HARD) for p in p95s
        ),
        "hard_threshold": DUP_P95_HARD,
        "note": "排除 degraded doc(与 ingestion_binding.py L313-316 一致)",
    }


def _per_chunk_index(per_doc_list: List[Dict[str, Any]]) -> Dict[str, float]:
    """打平 per_doc → {f'{label}::{gt_label}': jaccard}。

    P0 amendment:key 取 gt_label(ingestion_binding.py L208/218/231 实际字段),
    回退 chunk_label/chunk_id,最后才 fallback chunk_i。
    """
    idx: Dict[str, float] = {}
    for d in per_doc_list:
        label = d.get("label") or d.get("doc_id") or "?"
        per_chunk = d.get("per_chunk") or []
        for i, c in enumerate(per_chunk):
            ck = c.get("gt_label") or c.get("chunk_label") or c.get("chunk_id") or f"chunk_{i}"
            j = c.get("jaccard")
            idx[f"{label}::{ck}"] = j
    return idx


def compare_per_chunk(reports: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    """逐 chunk 跨 N 轮 jaccard 列表 → top-N 飘动 chunk + 计 byte-equal 数。"""
    indices = [_per_chunk_index(_extract_per_doc(r)) for r in reports]
    all_keys = set()
    for idx in indices:
        all_keys.update(idx.keys())
    rows: List[Dict[str, Any]] = []
    n_equal = 0
    n_total = 0
    for k in sorted(all_keys):
        vals = [idx.get(k) for idx in indices]
        present = [v for v in vals if v is not None]
        if not present:
            continue
        n_total += 1
        std = _safe_std(present)
        spread = (max(present) - min(present)) if len(present) >= 2 else 0.0
        # byte-equal:必须 N 轮都出现 + std 为 0
        if std == 0.0 and len(present) == len(vals):
            n_equal += 1
        rows.append({
            "key": k,
            "vals_per_run": vals,
            "stdev": std,
            "spread": spread,
            "n_present": len(present),
        })
    # std=None(只出 1 轮)排在最后,真飘动(std>0)排在前
    def _sort_key(r):
        s = r["stdev"]
        if s is None:
            return (1, 0.0, 0.0)
        return (0, -s, -r["spread"])
    rows.sort(key=_sort_key)
    return rows, n_equal, n_total


# ── 渲染 ─────────────────────────────────────────────────────
def _md_per_fmt_table(per_fmt: Dict[str, Dict[str, Any]], n_runs: int) -> str:
    lines = []
    header = "| fmt | " + " | ".join(f"run{i+1}" for i in range(n_runs)) + " | mean | std↓ | spread↓ |"
    sep = "|" + "---|" * (n_runs + 4)
    lines.append(header)
    lines.append(sep)
    for fmt, agg in per_fmt.items():
        cells = [_fmt_num(m) for m in agg["means_per_run"]]
        lines.append(
            f"| {fmt} | " + " | ".join(cells)
            + f" | {_fmt_num(agg['mean_across_runs'])} | {_fmt_num(agg['std_across_runs'])}"
            + f" | {_fmt_num(agg['spread_across_runs'])} |"
        )
    return "\n".join(lines)


def _md_top_drift_table(rows: List[Dict[str, Any]], n_runs: int, top_n: int = 10) -> str:
    if not rows:
        return "(无 per_chunk 数据)"
    drifted = [r for r in rows if (r["stdev"] is not None and r["stdev"] > 0)][:top_n]
    if not drifted:
        return "(✅ 全部 per_chunk byte-equal,无飘动)"
    lines = []
    header = "| chunk_key (label::gt_label) | " + " | ".join(f"run{i+1}" for i in range(n_runs)) + " | stdev | spread |"
    sep = "|" + "---|" * (n_runs + 3)
    lines.append(header)
    lines.append(sep)
    for r in drifted:
        cells = [_fmt_num(v) for v in r["vals_per_run"]]
        key_short = r["key"]
        if len(key_short) > 60:
            key_short = key_short[:57] + "..."
        lines.append(
            f"| {key_short} | " + " | ".join(cells)
            + f" | {_fmt_num(r['stdev'])} | {_fmt_num(r['spread'])} |"
        )
    return "\n".join(lines)


def _md_dup_table(dup: Dict[str, Any], n_runs: int) -> str:
    lines = []
    lines.append("| metric | " + " | ".join(f"run{i+1}" for i in range(n_runs)) + " | hard |")
    lines.append("|" + "---|" * (n_runs + 2))
    lines.append(
        "| img_dup_factor_p95 | "
        + " | ".join(_fmt_num(v, 3) for v in dup["p95_per_run"])
        + f" | ≤{dup['hard_threshold']} |"
    )
    lines.append(
        "| img_dup_factor_max | "
        + " | ".join(_fmt_num(v, 3) for v in dup["max_per_run"])
        + " | — |"
    )
    lines.append("")
    lines.append(f"_{dup['note']}_")
    return "\n".join(lines)


def _md_baseline_delta(per_fmt: Dict[str, Dict[str, Any]], baseline: Dict[str, Any]) -> str:
    b_pf = (baseline.get("per_fmt") or {})
    if not b_pf:
        return "(无 D6 baseline per_fmt)"
    lines = ["| fmt | D6 mean | D7 mean | Δ |", "|---|---|---|---|"]
    for fmt, agg in per_fmt.items():
        bm = (b_pf.get(fmt) or {}).get("mean_jaccard")
        dm = agg["mean_across_runs"]
        delta = (dm - bm) if (bm is not None and dm is not None) else None
        lines.append(f"| {fmt} | {_fmt_num(bm)} | {_fmt_num(dm)} | {_fmt_num(delta)} |")
    return "\n".join(lines)


def _render_md(
    n_runs: int,
    per_fmt_cmp: Dict[str, Dict[str, Any]],
    dup_cmp: Dict[str, Any],
    chunk_rows: List[Dict[str, Any]],
    n_equal_chunks: int,
    n_total_chunks: int,
    verdict_md: str,
    args,
    baseline: Optional[Dict[str, Any]] = None,
) -> str:
    parts = [
        "# Day 7 chunker post-fix verify — compare\n",
        f"*generated: {datetime.now():%Y-%m-%d %H:%M}* — runs={n_runs}\n",
        "",
        verdict_md,
        "",
        "## 表 1 — per_fmt mean_jaccard 跨 N 轮",
        _md_per_fmt_table(per_fmt_cmp, n_runs),
        "",
        f"- ALL_EQUAL 条件:std_across_runs ≤ {args.mean_std_pass}(byte-equal)",
        f"- STD_OK 上限:std_across_runs ≤ {args.mean_std_partial}",
        "",
        "## 表 2 — top-10 飘动 per_chunk",
        _md_top_drift_table(chunk_rows, n_runs, top_n=10),
        "",
        f"- per_chunk 总数: {n_total_chunks};byte-equal: {n_equal_chunks};飘动: {n_total_chunks - n_equal_chunks}",
        "- chunk_key 取 ingestion_binding.per_chunk.gt_label(P0 amendment)",
        "",
        "## 表 3 — img_dup_factor",
        _md_dup_table(dup_cmp, n_runs),
        "",
    ]
    if baseline:
        parts.extend([
            "## 表 4 — vs D6 baseline",
            _md_baseline_delta(per_fmt_cmp, baseline),
            "",
        ])
    return "\n".join(parts)


# ── main ─────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True,
                    help="逗号分隔的 run outdir 列表(每个含 report.json)")
    ap.add_argument("--out", required=True, help="compare.json 路径")
    ap.add_argument("--report", required=True, help="compare.md 路径(scratch)")
    ap.add_argument("--final-report", default=None,
                    help="D7_chunker_postfix_report.md 路径(仅 ALL_EQUAL/STD_OK 时写)")
    ap.add_argument("--baseline-d6", default=None,
                    help="D6 baseline report.json,用于算 Δ(可选)")
    ap.add_argument("--mean-std-pass", type=float, default=MEAN_STD_PASS)
    ap.add_argument("--mean-std-partial", type=float, default=MEAN_STD_PARTIAL)
    args = ap.parse_args()

    run_dirs = [r.strip() for r in args.runs.split(",") if r.strip()]
    if len(run_dirs) < 2:
        print("[!] 至少需要 2 个 run outdir", file=sys.stderr)
        sys.exit(2)

    reports: List[Dict[str, Any]] = []
    missing: List[str] = []
    for rd in run_dirs:
        r = _load_report(rd)
        if r is None:
            missing.append(rd)
        else:
            reports.append(r)
    if missing:
        print(f"[!] 缺 report.json 的 outdir({len(missing)}):", file=sys.stderr)
        for m in missing:
            print(f"      - {m}", file=sys.stderr)
        sys.exit(1)

    determs = [_extract_determ(r) for r in reports]
    n_runs = len(reports)
    per_fmt_cmp = compare_per_fmt(determs)
    dup_cmp = compare_dup_p95(determs)
    chunk_rows, n_equal_chunks, n_total_chunks = compare_per_chunk(reports)

    # ── verdict ──────────────────────────────────────────────
    fmt_stds = [v["std_across_runs"] for v in per_fmt_cmp.values() if v["std_across_runs"] is not None]
    fmt_std_max = max(fmt_stds) if fmt_stds else 0.0
    per_chunk_all_equal = (n_total_chunks > 0 and n_equal_chunks == n_total_chunks)
    dup_ok = dup_cmp["all_under_hard"]

    if fmt_std_max <= args.mean_std_pass and per_chunk_all_equal and dup_ok:
        verdict = "ALL_EQUAL"
        exit_code = 0
        verdict_md = (
            "## ✅ Day 7 verdict: ALL_EQUAL — chunker 已确定\n\n"
            f"- per_fmt mean across {n_runs} runs std = {_fmt_num(fmt_std_max)}\n"
            f"- per_chunk byte-equal: {n_equal_chunks}/{n_total_chunks}\n"
            f"- img_dup_factor_p95 全部 ≤ {DUP_P95_HARD}: {dup_ok}\n\n"
            "**下一步**:在最后一轮 outdir 跑 1 次 panel,看 image_binding mean ≥ 4.0?"
        )
    elif fmt_std_max <= args.mean_std_partial and dup_ok:
        verdict = "STD_OK"
        exit_code = 2
        verdict_md = (
            "## ⚠️ Day 7 verdict: STD_OK — per_fmt mean std ≤ 0.02,可接受 micro-noise\n\n"
            f"- per_fmt mean std = {_fmt_num(fmt_std_max)}(≤ {args.mean_std_partial})\n"
            f"- per_chunk byte-equal: {n_equal_chunks}/{n_total_chunks}\n"
            f"- img_dup_factor_p95 全部 ≤ {DUP_P95_HARD}: {dup_ok}\n\n"
            "**下一步**:看 top-N 飘动 chunk 表,局部排查后重跑 N 连。panel 仍建议跑(信号 > 噪声)。"
        )
    else:
        verdict = "DRIFT"
        exit_code = 1
        verdict_md = (
            "## ❌ Day 7 verdict: DRIFT — chunker 仍非确定\n\n"
            f"- per_fmt mean std = {_fmt_num(fmt_std_max)}(> {args.mean_std_partial})\n"
            f"- per_chunk byte-equal: {n_equal_chunks}/{n_total_chunks}\n"
            f"- img_dup_factor_p95 全部 ≤ {DUP_P95_HARD}: {dup_ok}\n\n"
            "**下一步**:回炉看 ThreadPool 顺序 / asset 排序;panel 不跑(噪声叠噪声)。"
        )

    # ── 渲染 md ──────────────────────────────────────────────
    baseline = None
    if args.baseline_d6 and os.path.exists(args.baseline_d6):
        with open(args.baseline_d6, "r", encoding="utf-8") as fh:
            baseline_report = json.load(fh)
            baseline = _extract_determ(baseline_report)

    md = _render_md(
        n_runs=n_runs,
        per_fmt_cmp=per_fmt_cmp,
        dup_cmp=dup_cmp,
        chunk_rows=chunk_rows,
        n_equal_chunks=n_equal_chunks,
        n_total_chunks=n_total_chunks,
        verdict_md=verdict_md,
        args=args,
        baseline=baseline,
    )

    # compare.md(scratch,每轮都写)
    os.makedirs(os.path.dirname(args.report), exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as fh:
        fh.write(md)

    # D7_chunker_postfix_report.md(reports/,仅 PASS/STD_OK 时写;
    # 文件名与 D6_hard_lock_report.md 不冲突)
    if args.final_report and verdict in ("ALL_EQUAL", "STD_OK"):
        os.makedirs(os.path.dirname(args.final_report), exist_ok=True)
        with open(args.final_report, "w", encoding="utf-8") as fh:
            fh.write("<!-- 由 day7_chunker_postfix_compare.py 自动生成 -->\n")
            fh.write(f"<!-- verdict={verdict} | exit_code={exit_code} -->\n\n")
            fh.write(md)
            fh.write("\n\n---\n")
            fh.write("\n## 后续手写部分(panel mean / 升 hard 决策 / D8 计划)\n\n")
            fh.write("> 这部分由人接着补:在最后一轮 outdir 上跑 panel 后,把 image_binding mean、\n")
            fh.write("> top-3 评委分歧、是否升 hard 写下来,锁档完成。\n")

    # ── 落 json ──────────────────────────────────────────────
    payload = {
        "generated": datetime.now().isoformat(),
        "n_runs": n_runs,
        "run_dirs": run_dirs,
        "per_fmt": per_fmt_cmp,
        "img_dup": dup_cmp,
        "per_chunk": {
            "n_total": n_total_chunks,
            "n_byte_equal": n_equal_chunks,
            "top_drift": chunk_rows[:20],
        },
        "verdict": verdict,
        "exit_code": exit_code,
        "thresholds": {
            "mean_std_pass": args.mean_std_pass,
            "mean_std_partial": args.mean_std_partial,
            "dup_p95_hard": DUP_P95_HARD,
        },
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    # ── 控制台简表 ──────────────────────────────────────────
    print("")
    print("──────────────────────────────────────────────────────────────")
    print(f" Day 7 compare — verdict: {verdict}")
    print("──────────────────────────────────────────────────────────────")
    print(f"  reports:              {n_runs} 个 outdir")
    print(f"  per_fmt std_max:      {_fmt_num(fmt_std_max)}")
    print(f"  per_chunk byte-equal: {n_equal_chunks}/{n_total_chunks}")
    print(f"  dup_p95 全过闸:        {dup_ok}")
    print(f"  scratch compare md:   {args.report}")
    print(f"  scratch compare json: {args.out}")
    if args.final_report and verdict in ("ALL_EQUAL", "STD_OK"):
        print(f"  final report:         {args.final_report}")
    print("")

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
