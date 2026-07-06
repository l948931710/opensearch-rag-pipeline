# -*- coding: utf-8 -*-
"""fidelity/run_fidelity.py — G6 抽取保真评测 runner（离线，默认零 API）。

用法（在仓库根目录）：
  # 1) 给一份新文档生成 GT 底稿（把当前抽取输出预填成 ref，人工【校对改错】而非从零转写）
  python -m eval_harness.fidelity.run_fidelity --stub eval_samples/docs/xxx.pdf \\
      >> eval_samples/fidelity_gt/stub_xxx.json

  # 2) 跑评测（默认 simulate 抽取，零 API；扫描件条目自动 SKIP 除非 --real）
  python -m eval_harness.fidelity.run_fidelity run
  python -m eval_harness.fidelity.run_fidelity run --real          # 含扫描件（付 OCR 费）
  python -m eval_harness.fidelity.run_fidelity run --strict        # 回归/硬缺测 → exit 1

  # 3) 冻结基线（首次全绿后；此后每次 run 自动与基线比对）
  python -m eval_harness.fidelity.run_fidelity run --freeze

基线分口径（v2，G6 双重 OCR 修复引入）：sim（默认，零 API，只评原生抽取路径）与
real（--real，含扫描件 hard 守卫）各存一段、各自 --freeze 各自比对——real 口径下
funnel 会把图内 OCR 文本追加进页文本（对 GT 的原生口径是加性噪声），两口径分数
不可互比；requires_real_ocr 的 hard 文档在 sim 跑里跳过不算缺测（守卫在 real 段）。

⚠️ --stub 的输出是【底稿不是真相】：抽取器自己的错误会原样出现在 ref_text 里，
人工必须对照原文档逐字校对后才算 GT（否则是自己给自己打分——binding GT 循环性
教训 G23 的同款陷阱）。校对完成前请保持 _doc_meta.degraded=true。

设计承接（见 __init__ docstring）：本层是 parser/OCR 换版的第一道回归网，也是
tier-3（全页 VLM 解析 / TEDS 表格）投入决策的度量前提。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Dict, List

from .gt_loader import DEFAULT_GT_PATH, FidelityDoc, load_gt
from .metrics import cer, char_f1, table_scores

BASELINE_PATH = os.path.join(os.path.dirname(__file__), "baseline.json")
DEFAULT_DELTA = 0.02
_TEXT_BLOCKS = ("paragraph", "heading", "list", "ocr_text")


def _extract(local_path: str, file_ext: str, real: bool):
    """跑一次离线抽取（不触 OSS/RDS）。返回 ExtractionResult。

    - simulate 下剥离全部 ocr_text 块：那是确定性桩文本（"[OCR page N: ...]"），
      不是抽取产物。simulate 语义 = 只评【原生】抽取路径；真实扫描件走
      requires_real_ocr + --real 才计分。
    - stdout 纪律由 main() 统一负责（整个工作阶段改道 stderr，含 config import 期
      的环境横幅——冒烟实测：仅包住 extract 调用不够，横幅在 import 时就打了）。
    """
    import tempfile
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor
    ux = UnifiedExtractor(simulate=not real)
    result = ux.extract({
        "doc_id": "FIDELITY_EVAL", "version_no": 1,
        "file_ext": file_ext, "raw_key": f"fidelity/{os.path.basename(local_path)}",
        "filename": os.path.basename(local_path), "local_path": local_path,
        "_tmp_dir": tempfile.mkdtemp(prefix="fidelity_"),
    })
    if not real:
        result.blocks = [b for b in result.blocks
                         if getattr(b, "block_type", "") != "ocr_text"]
    return result


def _page_text(blocks, page: int) -> str:
    """页级 hyp 文本：该页的非表格文本块按块序拼接。page=0 = 全文（DOCX/XLSX 无页码）。"""
    out = []
    for b in blocks:
        bt = getattr(b, "block_type", "")
        if bt not in _TEXT_BLOCKS:
            continue
        pg = getattr(b, "page_num", None)
        if page != 0 and pg != page:
            continue
        out.append(getattr(b, "text", "") or "")
    return "\n".join(out)


def _page_tables(blocks, page: int) -> List[str]:
    return [getattr(b, "text", "") or "" for b in blocks
            if getattr(b, "block_type", "") == "table"
            and (page == 0 or getattr(b, "page_num", None) == page)]


def score_doc(doc: FidelityDoc, result) -> Dict:
    """对单文档打分（纯函数：GT + ExtractionResult → 指标 dict）。"""
    page_rows = []
    for p in doc.pages:
        hyp = _page_text(result.blocks, p["page"])
        page_rows.append({
            "page": p["page"], "scope": p.get("scope", "body"),
            "char_f1": char_f1(p["ref_text"], hyp),
            "cer": cer(p["ref_text"], hyp),
            "ref_chars": len("".join(p["ref_text"].split())),
        })
    table_rows = []
    for t in doc.tables:
        cands = _page_tables(result.blocks, t["page"])
        best = max((table_scores(t["ref_grid"], md) for md in cands),
                   key=lambda s: s["cell_f1"], default=None)
        if best is None:
            best = {"cell_f1": 0.0, "grid_exact": False,
                    "ref_shape": [len(t["ref_grid"]),
                                  max((len(r) for r in t["ref_grid"]), default=0)],
                    "hyp_shape": [0, 0], "note": "该页无任何表格块"}
        table_rows.append({"page": t["page"], **best})

    ref_total = sum(r["ref_chars"] for r in page_rows) or 1
    return {
        "label": doc.label, "degraded": doc.degraded, "status": "SCORED",
        "pages": page_rows, "tables": table_rows,
        # 文档级聚合：按参考字符数加权（长页权重大）
        "char_f1": round(sum(r["char_f1"] * r["ref_chars"] for r in page_rows)
                         / ref_total, 4) if page_rows else None,
        "cer": round(sum(r["cer"] * r["ref_chars"] for r in page_rows)
                     / ref_total, 4) if page_rows else None,
        "table_cell_f1": (round(sum(t["cell_f1"] for t in table_rows) / len(table_rows), 4)
                          if table_rows else None),
        "grid_exact_rate": (round(sum(1 for t in table_rows if t["grid_exact"])
                                  / len(table_rows), 4) if table_rows else None),
    }


def run(gt_path: str, real: bool) -> Dict:
    docs = load_gt(gt_path)
    rows, skipped = [], []
    for doc in docs:
        reason = doc.verify_bytes()
        regime_skip = False
        if reason is None and doc.requires_real_ocr and not real:
            reason = "requires_real_ocr（--real 才计分，避免误用 simulate OCR 桩文本打分）"
            regime_skip = True  # 口径外跳过 ≠ 缺测：该文档的守卫在 real 段基线
        if reason:
            skipped.append({"label": doc.label, "status": "SKIP", "reason": reason,
                            "degraded": doc.degraded, "regime_skip": regime_skip})
            continue
        result = _extract(doc.local_path, doc.file_ext, real)
        rows.append(score_doc(doc, result))

    hard = [r for r in rows if not r["degraded"]]

    def _mean(key):
        vals = [r[key] for r in hard if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    return {
        "schema": "fidelity_report_v1", "real_ocr": real,
        "docs": rows + skipped,
        "hard_doc_labels": sorted(r["label"] for r in hard),
        "hard_skipped": sorted(s["label"] for s in skipped
                               if not s["degraded"] and not s["regime_skip"]),
        "corpus": {"char_f1": _mean("char_f1"), "cer": _mean("cer"),
                   "table_cell_f1": _mean("table_cell_f1"),
                   "grid_exact_rate": _mean("grid_exact_rate"),
                   "hard_docs": len(hard)},
    }


def compare_to_baseline(report: Dict, baseline: Dict, delta: float) -> Dict:
    """与冻结基线比对。文档集变了 → INCOMPARABLE（学 regime-mismatch：不给 GO）。"""
    if baseline.get("hard_doc_labels") != report["hard_doc_labels"]:
        return {"verdict": "INCOMPARABLE",
                "reason": "hard 文档集与基线不一致——先 --freeze 重冻或补齐缺失文档",
                "baseline_docs": baseline.get("hard_doc_labels"),
                "current_docs": report["hard_doc_labels"]}
    fails = []
    b, c = baseline["corpus"], report["corpus"]
    for key, direction in (("char_f1", +1), ("table_cell_f1", +1),
                           ("grid_exact_rate", +1), ("cer", -1)):
        if b.get(key) is None or c.get(key) is None:
            continue
        drop = (b[key] - c[key]) * direction
        if drop > delta:
            fails.append(f"{key}: {b[key]} → {c[key]} (回退 {drop:.4f} > δ={delta})")
    if report["hard_skipped"]:
        fails.append(f"hard 文档缺测: {report['hard_skipped']}（缺测不算过——L6 三态门同款语义）")
    return {"verdict": "REGRESSION" if fails else "PASS", "fails": fails, "delta": delta}


def _baseline_regime_section(baseline_raw: Dict, regime: str):
    """从基线文件取指定口径（"sim"/"real"）的段；无该段返回 None。

    v2 = {"schema": "fidelity_baseline_v2", "regimes": {"sim": {...}, "real": {...}}}。
    legacy v1（单段 + 顶层 real_ocr 布尔）按其 real_ocr 归入对应口径读取，
    首次 --freeze 时被迁移为 v2（另一口径的段保留）。
    """
    if "regimes" in baseline_raw:
        return baseline_raw["regimes"].get(regime)
    legacy_regime = "real" if baseline_raw.get("real_ocr") else "sim"
    return baseline_raw if legacy_regime == regime else None


def make_stub(path: str, real: bool) -> Dict:
    """生成 GT 底稿：抽取输出预填 ref（人工校对改错后才是 GT，见模块 docstring ⚠️）。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    result = _extract(path, ext, real)
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    pages_seen = sorted({getattr(b, "page_num", None) for b in result.blocks
                         if getattr(b, "page_num", None)})
    label = os.path.splitext(os.path.basename(path))[0]
    entry = {
        "_doc_meta": {"local_path": path, "doc_sha256": sha, "file_ext": ext,
                      "requires_real_ocr": False,
                      "degraded": True},  # 校对完成前必须保持 true
        "pages": ([{"page": p, "scope": "body", "ref_text": _page_text(result.blocks, p)}
                   for p in pages_seen]
                  or [{"page": 0, "scope": "body",
                       "ref_text": _page_text(result.blocks, 0)}]),
        "tables": [{"page": getattr(b, "page_num", None) or 0,
                    "ref_grid": [[c.strip() for c in ln.split("|")[1:-1]]
                                 for ln in (b.text or "").splitlines() if ln.strip()]}
                   for b in result.blocks if getattr(b, "block_type", "") == "table"],
    }
    return {"_meta": {"schema": "fidelity_v1",
                      "notes": "⚠️ STUB——ref 是抽取器输出的预填底稿，人工逐字校对前不是 GT"},
            label: entry}


def main(argv=None):
    ap = argparse.ArgumentParser(description="G6 抽取保真评测")
    sub = ap.add_subparsers(dest="cmd")
    runp = sub.add_parser("run")
    runp.add_argument("--gt", default=DEFAULT_GT_PATH)
    runp.add_argument("--real", action="store_true", help="真实 OCR/VLM（扫描件计分；付费）")
    runp.add_argument("--freeze", action="store_true", help="把本次结果冻结为基线")
    runp.add_argument("--strict", action="store_true", help="REGRESSION/INCOMPARABLE → exit 1")
    runp.add_argument("--delta", type=float, default=DEFAULT_DELTA)
    runp.add_argument("--out", default="")
    ap.add_argument("--stub", default="", help="给文档生成 GT 底稿 JSON（打到 stdout）")
    args = ap.parse_args(argv)

    # stdout 纪律：本 CLI 的 stdout 只输出 JSON（供 `> gt.json` 重定向与管道消费）。
    # 管线的全部进度 print（含 config import 期的"⚙️ 环境"横幅）在工作阶段整体改道
    # stderr——冒烟实测：横幅在 import 时就打了，仅包住 extract 调用不够。
    import contextlib
    real_stdout = sys.stdout

    with contextlib.redirect_stdout(sys.stderr):
        if args.stub:
            out = json.dumps(make_stub(args.stub, real=False), ensure_ascii=False, indent=2)
            print(out, file=real_stdout)
            return 0
        if args.cmd != "run":
            ap.print_help(file=sys.stderr)
            return 2

        report = run(args.gt, real=args.real)
        regime = "real" if args.real else "sim"
        baseline_raw: Dict = {}
        if os.path.exists(BASELINE_PATH):
            with open(BASELINE_PATH, encoding="utf-8") as f:
                baseline_raw = json.load(f)
        if args.freeze:
            # 只冻本口径的段；另一口径的段（含 legacy v1 迁移来的）原样保留
            regimes = dict(baseline_raw.get("regimes") or {})
            if baseline_raw and "regimes" not in baseline_raw:
                legacy_regime = "real" if baseline_raw.get("real_ocr") else "sim"
                regimes.setdefault(legacy_regime, {
                    "hard_doc_labels": baseline_raw.get("hard_doc_labels"),
                    "corpus": baseline_raw.get("corpus")})
            regimes[regime] = {"hard_doc_labels": report["hard_doc_labels"],
                               "corpus": report["corpus"]}
            with open(BASELINE_PATH, "w", encoding="utf-8") as f:
                json.dump({"schema": "fidelity_baseline_v2", "regimes": regimes},
                          f, ensure_ascii=False, indent=2)
            report["gate"] = {"verdict": "FROZEN", "regime": regime, "path": BASELINE_PATH}
        elif baseline_raw:
            section = _baseline_regime_section(baseline_raw, regime)
            if section:
                report["gate"] = compare_to_baseline(report, section, args.delta)
                report["gate"]["regime"] = regime
            else:
                report["gate"] = {"verdict": "NO_BASELINE", "regime": regime,
                                  "reason": f"基线无 {regime} 口径段——先 --freeze 冻结该口径"}

        out = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(out)

    print(out, file=real_stdout)
    verdict = (report.get("gate") or {}).get("verdict", "NO_BASELINE")
    if args.strict and verdict in ("REGRESSION", "INCOMPARABLE"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
