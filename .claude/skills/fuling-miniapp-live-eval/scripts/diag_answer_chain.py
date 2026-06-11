# -*- coding: utf-8 -*-
"""diag_answer_chain.py — L2 检索/上下文链路探针（只读）。

对任意问题 dump：配置形态 → 检索 chunks 全表（类型/step/图数/rerank/分数/小节/归属文档）
→ guard 判定 → 实际 context headers → 文档分组与带图地图。
是图缺失判定树 Q4/Q5 与拒答判定树 Q2/Q3 的标准探针。

用法（从仓库根运行）：
  python3 .claude/skills/fuling-miniapp-live-eval/scripts/diag_answer_chain.py "问题文本"
  可选：--raw-top 100 --find "目标小节或文档关键词"   # 附加裸混检排名锚定
"""
import argparse
import os
import sys

os.environ.setdefault("RAG_ENV", "prod_ro")
os.environ.setdefault("RAG_RERANK_ENABLE", "true")
os.environ.setdefault("RAG_LOW_CONFIDENCE_GUARD", "true")
os.environ.setdefault("RAG_MAX_CONTEXT_CHARS", "10000")

if not os.path.exists("opensearch_pipeline"):
    sys.exit("请从仓库根目录运行（需能 import opensearch_pipeline）")
sys.path.insert(0, os.getcwd())

from opensearch_pipeline.retriever import retrieve_and_enrich, search_chunks  # noqa: E402
from opensearch_pipeline.llm_generator import _format_context, is_low_confidence_band  # noqa: E402
from opensearch_pipeline.config import get_config  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("question")
ap.add_argument("--raw-top", type=int, default=0, help=">0 时附加裸混检 topN 排名锚定")
ap.add_argument("--find", default="", help="在裸排名中定位含此关键词的 标题/小节")
args = ap.parse_args()

cfg = get_config()
print(f"[cfg] rerank={cfg.alibaba_vector.rerank_enable} pool={cfg.alibaba_vector.rerank_pool} "
      f"top_k={cfg.rag.default_top_k} max_ctx={cfg.rag.max_context_chars} "
      f"family_cap={cfg.rag.step_expand_family_cap} max_imgs={cfg.rag.max_answer_images} "
      f"guard={cfg.rag.low_confidence_guard}")

chunks = retrieve_and_enrich(args.question, top_k=None, user_dept=None, cosurface_images=True)
print(f"\n■ retrieve_and_enrich → {len(chunks)} chunks（>30 警惕扩展洪泛）")
for i, c in enumerate(chunks, 1):
    refs = c.get("image_refs") or []
    rr = c.get("rerank_score")
    print(f" [{i:2d}] {c.get('chunk_type','')[:14]:14s} step={c.get('step_no')!s:4s} "
          f"refs={len(refs)} rerank={f'{rr:.3f}' if isinstance(rr,(int,float)) else 'MISS'} "
          f"score={c.get('score', 0):.3f} | {(c.get('section_title') or '')[:20]:20s} "
          f"| {(c.get('title') or '')[:24]}")

has_rr = sum(1 for c in chunks if isinstance(c.get("rerank_score"), (int, float)))
print(f"\nguard=is_low_confidence_band → {is_low_confidence_band(chunks)} "
      f"(rerank_score 在场 {has_rr}/{len(chunks)})")

ctx = _format_context(chunks, max_chars=cfg.rag.max_context_chars)
heads = [ln for ln in ctx.splitlines() if ln.startswith("[文档")]
print(f"\n■ context = {len(ctx)} chars，headers {len(heads)} 条（截断看尾部缺谁）：")
for h in heads:
    print("  ", h[:108])

from collections import OrderedDict  # noqa: E402
docs = OrderedDict()
for i, c in enumerate(chunks, 1):
    d = docs.setdefault((c.get("doc_id"), c.get("title")), [])
    if (c.get("image_refs") or []) or c.get("source_image"):
        d.append((i, c.get("step_no"), len(c.get("image_refs") or []) or 1))
print(f"\n■ 文档分组（{len(docs)} 个）与带图地图：")
for (did, title), imgmap in docs.items():
    print(f"  {title}\n    doc_id={did} 带图(编号,step,图数)={imgmap}")

if args.raw_top:
    print(f"\n■ 裸混检 top{args.raw_top} 锚定（无 rerank/扩展）：")
    raw = search_chunks(args.question, top_k=args.raw_top)
    hit = None
    for i, c in enumerate(raw, 1):
        text = f"{c.get('title') or ''}|{c.get('section_title') or ''}"
        if args.find and args.find in text and hit is None:
            hit = (i, c.get("title"), c.get("section_title"))
        if i <= 10:
            print(f"  [{i}] {c.get('score'):.3f} {(c.get('title') or '')[:28]} | {(c.get('section_title') or '')[:22]}")
    if args.find:
        print(f"  关键词 '{args.find}' 首现排名: {hit}")
