#!/usr/bin/env python3
"""dump_anchor_gt_candidates.py — Step C+ A.

跑 4 PDF doc 的 `_extract_and_chunk`(OFF 模式,因 OFF/ON topology 一致, OFF chunks
已覆盖所有 anchor key),dump 候选 chunks 给可视化标注 widget 用.

输出 2 个文件:
  - scratch/anchor_gt_candidates_<ts>.json — 留痕完整 dump
  - scratch/anchor_gt_widget_data.js       — window.ANCHOR_CANDIDATES = {...}; 给 widget 嵌入

每 chunk 字段:
  anchor_key    : 五元组 (chunk_type, step_no, sub_no, section_no, seq_no)
  chunk_type    : step_card / text_chunk / ...
  step_no/sub_no/section_no/section_path/page_num
  chunk_text_excerpt: 前 150 字(控总大小,widget_code maxLength=512KB)
  image_refs[]  : {image_index, page_num, visual_summary[60], ocr_text[50], oss_key}
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from eval_harness.binding.ingestion_binding import _extract_and_chunk  # noqa: E402
from eval_harness.chunker_ab import _chunk_to_sig                       # noqa: E402


DOC_POOL: List[Tuple[str, str]] = [
    ("pdf_sop",
     str(Path("~/Downloads/opensearch-rag-data/eval_samples/documents/pdf_sop.pdf").expanduser())),
    ("pdf_xs_wi_007",
     str(Path("~/Downloads/opensearch-rag-data/eval_samples/documents/pdf_xs_wi_007.pdf").expanduser())),
    ("pdf_it_xxh_003",
     str(Path("~/Downloads/opensearch-rag-data/eval_samples/documents/pdf_it_xxh_003.pdf").expanduser())),
    ("admin_lodging",
     str(ROOT / "fuling_chunk_exp" / "admin_关于外来人员来访留宿相关规定.pdf")),
]

CHUNK_TEXT_EXCERPT_LEN = 150
VS_LEN = 60
OCR_LEN = 50


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    except Exception:
        return "unknown"


def _gv(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def dump_one_doc(label: str, path: str) -> Dict[str, Any]:
    """对一个 PDF doc 跑 _extract_and_chunk → dump chunks."""
    if not Path(path).exists():
        return {"error": f"file not found: {path}", "chunks": []}

    with contextlib.redirect_stdout(io.StringIO()):
        chunks = _extract_and_chunk(label, "pdf", path)

    out_chunks = []
    for c in chunks:
        anchor_key, _sig = _chunk_to_sig(c)
        key_list = list(anchor_key)
        chunk_text = (_gv(c, "chunk_text") or "")
        extra = _gv(c, "extra") or {}
        page_num = (_gv(c, "page_num") or extra.get("page_num") or
                    extra.get("page"))

        image_refs_raw = extra.get("image_refs") or []
        img_dumps = []
        for r in image_refs_raw:
            if not isinstance(r, dict):
                continue
            img_dumps.append({
                "image_index": r.get("image_index"),
                "page_num": r.get("page_num") or r.get("page"),
                "visual_summary": (r.get("visual_summary") or "")[:VS_LEN],
                "ocr_text": (r.get("ocr_text") or "")[:OCR_LEN].replace("\n", " "),
                "oss_key": r.get("oss_key") or r.get("source_image"),
            })

        out_chunks.append({
            "anchor_key": key_list,
            "chunk_type": _gv(c, "chunk_type"),
            "step_no": extra.get("step_no"),
            "sub_no": extra.get("sub_no") or extra.get("sub_step_no"),
            "section_no": extra.get("section_no"),
            "section_path": list(extra.get("section_path") or []),
            "page_num": page_num,
            "chunk_text_excerpt": chunk_text[:CHUNK_TEXT_EXCERPT_LEN].replace("\n", " "),
            "image_refs": img_dumps,
        })

    return {
        "n_chunks": len(out_chunks),
        "n_step_cards": sum(1 for c in out_chunks if c["chunk_type"] == "step_card"),
        "chunks": out_chunks,
    }


def main():
    out = {
        "_meta": {
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "git_commit": _git_commit(),
            "arm": "off",
            "dump_script": "scratch/dump_anchor_gt_candidates.py",
            "n_docs": len(DOC_POOL),
            "schema_version": "v1",
        },
        "documents": {},
    }
    print(f"[dump] starting — {len(DOC_POOL)} PDFs")
    for label, path in DOC_POOL:
        print(f"  → {label}: {path}")
        result = dump_one_doc(label, path)
        out["documents"][label] = result
        if "error" in result:
            print(f"    ❌ {result['error']}")
        else:
            print(f"    ✓ {result['n_chunks']} chunks ({result['n_step_cards']} step_cards)")

    ts = time.strftime("%Y%m%d_%H%M%S")
    scratch = ROOT / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    json_path = scratch / f"anchor_gt_candidates_{ts}.json"
    json_path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    widget_data_path = scratch / "anchor_gt_widget_data.js"
    compact = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    widget_data_path.write_text(
        f"window.ANCHOR_CANDIDATES = {compact};\n", encoding="utf-8")

    json_size = json_path.stat().st_size
    js_size = widget_data_path.stat().st_size
    print(f"\n✓ JSON  → {json_path}  ({json_size/1024:.1f} KB)")
    print(f"✓ JS    → {widget_data_path}  ({js_size/1024:.1f} KB)")
    if js_size > 500 * 1024:
        print(f"⚠️  widget_data.js ({js_size/1024:.1f} KB) 超 500KB,可能超 widget 嵌入上限")
        print("    考虑缩短 chunk_text_excerpt / visual_summary 字段长度")
    else:
        print(f"   widget 嵌入空间充足(<500KB / 512KB 限制)")

    print("\n══ 逐 doc step/sub/sec 表 ══")
    for label, dd in out["documents"].items():
        if "error" in dd:
            print(f"  ❌ {label}: {dd['error']}")
            continue
        print(f"\n  ── {label} ── ({dd['n_chunks']} chunks, {dd['n_step_cards']} step_cards)")
        for c in dd["chunks"]:
            if c["chunk_type"] != "step_card":
                continue
            sn = c["step_no"]
            sub = c["sub_no"]
            sec = c["section_no"]
            page = c["page_num"]
            n_imgs = len(c["image_refs"])
            title = c["chunk_text_excerpt"][:60]
            print(f"    step={sn!s:>3} sub={sub!s:>4} sec={sec!s:>6} page={page!s:>2} "
                  f"imgs={n_imgs:>2}  {title}")


if __name__ == "__main__":
    main()
