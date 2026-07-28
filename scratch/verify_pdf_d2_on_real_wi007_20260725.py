#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF-D2 的**本机补充证据**：在真实 FL-XS-WI-007 上核对图↔步骤绑定（opt-in）。

CI 内的永久回归由合成夹具承担（`tests/test_pdf_column_gap_binding.py`，不依赖仓外
数据）。本脚本只是"合成件确实代表了真实故障"的旁证，**刻意不放进 tests/**：它依赖
仓外 GT 语料，进 `make test` 会引入环境相关的成败。

预期（GT: gt_pdf_analysis.json）：步骤3 = {26,27,28}，步骤4 = {29}。
PDF-D2 之前两者恰好对调。

用法:
    python3 scratch/verify_pdf_d2_on_real_wi007_20260725.py [路径]
默认找 ~/Downloads/opensearch-rag-data/eval_samples/documents/pdf_xs_wi_007.pdf，
找不到就退出码 3（跳过，不算失败）。
"""
import contextlib
import io
import os
import sys

DEFAULT = os.path.expanduser(
    "~/Downloads/opensearch-rag-data/eval_samples/documents/pdf_xs_wi_007.pdf")
EXPECTED = {3: [26, 27, 28], 4: [29]}


def main(argv):
    path = argv[1] if len(argv) > 1 else DEFAULT
    if not os.path.exists(path):
        print(f"⏭  跳过：找不到 {path}（仓外 GT 语料，非失败）")
        return 3

    os.environ.setdefault("RAG_ENV", "test")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from eval_harness.binding.ingestion_binding import _extract_and_chunk, _gv

    with contextlib.redirect_stdout(io.StringIO()):
        chunks = _extract_and_chunk("pdf_xs_wi_007", "pdf", path)

    cards = {}
    for c in chunks:
        if _gv(c, "chunk_type") != "step_card":
            continue
        extra = _gv(c, "extra") or {}
        refs = sorted(r.get("image_index") for r in (extra.get("image_refs") or []))
        if refs:
            cards.setdefault(extra.get("step_no"), refs)

    ok = True
    print(f"真实文档: {path}\n")
    for step, want in EXPECTED.items():
        got = cards.get(step)
        mark = "✅" if got == want else "❌"
        if got != want:
            ok = False
        print(f"  {mark} 步骤{step}: 期望 {want}  实得 {got}")
    print("\n全部步骤卡的图集:")
    for step in sorted(cards, key=lambda s: (s is None, s)):
        print(f"    step_no={step}: {cards[step]}")
    if not ok:
        print("\n❌ 与 GT 不符 —— PDF-D2 可能回归，或该文档版面已变。")
        return 1
    print("\n✅ 与 GT 一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
