# -*- coding: utf-8 -*-
"""probe_citation_x3.py — L3 引用倾向探针 + L4 builder 隔离（只读 + 真实 LLM 调用）。

分辨图缺失的三种失败形态：
  a) LLM 未输出 <<IMG:N>>            → 引用倾向（prompt/提示层）
  b) 输出但指向无图文档（被正确丢弃）→ 编号指向问题
  c) 指向带图文档但 blocks 仍缺      → builder（配额/近重/签名）
--synthetic 用"合成全引用"把 builder 与 LLM 方差隔离（图缺失判定树 Q7）。

用法（从仓库根运行）：
  python3 …/probe_citation_x3.py "问题" [-n 3] [--synthetic]
注意：每次重复一次真实 LLM 生成调用，按需控制 -n。
"""
import argparse
import os
import re
import sys

os.environ.setdefault("RAG_ENV", "prod_ro")
os.environ.setdefault("RAG_RERANK_ENABLE", "true")
os.environ.setdefault("RAG_LOW_CONFIDENCE_GUARD", "true")
os.environ.setdefault("RAG_MAX_CONTEXT_CHARS", "10000")

if not os.path.exists("opensearch_pipeline"):
    sys.exit("请从仓库根目录运行")
sys.path.insert(0, os.getcwd())

from opensearch_pipeline.retriever import retrieve_and_enrich  # noqa: E402
from opensearch_pipeline.llm_generator import generate_answer  # noqa: E402
from opensearch_pipeline.content_blocks_builder import build_content_blocks  # noqa: E402
from opensearch_pipeline.config import get_config  # noqa: E402

PAT = re.compile(r"<<IMG:(\d+)>>")
ap = argparse.ArgumentParser()
ap.add_argument("question")
ap.add_argument("-n", type=int, default=3, help="真实生成重复次数（默认 3）")
ap.add_argument("--synthetic", action="store_true", help="附加合成全引用隔离 builder")
args = ap.parse_args()

cfg = get_config()
chunks = retrieve_and_enrich(args.question, top_k=None, user_dept=None, cosurface_images=True)
with_img = {i + 1 for i, c in enumerate(chunks)
            if (c.get("image_refs") or []) or c.get("source_image")}
print(f"chunks={len(chunks)}, 带图文档编号={sorted(with_img)}")

if args.synthetic:
    synth = "步骤" + "".join(f"<<IMG:{n}>>" for n in sorted(with_img)) + "完"
    blocks = build_content_blocks(synth, chunks)
    caps = [(b.get("caption") or "")[:36] for b in blocks if b.get("type") == "image"]
    print(f"\n[合成全引用] → {len(caps)} 图（builder 隔离，上限={cfg.rag.max_answer_images}）:")
    for c in caps:
        print("   -", c)

for r in range(1, args.n + 1):
    out = generate_answer(args.question, chunks, max_context_chars=cfg.rag.max_context_chars)
    raw = out.get("answer", "")
    cited = [int(m) for m in PAT.findall(raw)]
    valid = [c for c in cited if c in with_img]
    blocks = build_content_blocks(raw, chunks)
    imgs = [b for b in blocks if b.get("type") == "image"]
    print(f"\nrun{r}: 标记={cited or '无'} 有效指向={sorted(set(valid)) or '无'} → blocks图={len(imgs)}")
    for b in imgs:
        print("   -", (b.get("caption") or "")[:36])
    if not cited:
        print("   答案前100字:", raw[:100].replace(chr(10), " "))
