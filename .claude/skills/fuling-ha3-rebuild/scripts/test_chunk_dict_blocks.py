"""Reproduce the prod crash path: chunk_from_blocks with DICT blocks (canonical JSON shape)
through the two xlsx-layout methods. Also run with namespace OBJECTS for parity."""
import sys
from types import SimpleNamespace
sys.path.insert(0, ".")
from opensearch_pipeline.chunker import DocumentChunker

def objs(dicts):
    return [SimpleNamespace(block_type=b["block_type"], text=b["text"],
                            extra=b.get("extra", {}), page_num=b.get("page_num")) for b in dicts]

def run(layout, blocks):
    ck = DocumentChunker(split_mode="text", xlsx_layout_type=layout)
    chunks = ck.chunk_from_blocks(blocks, "DOC_TEST", 2, {"title": "T", "owner_dept": "PROD"})
    print(f"    {layout} [{type(blocks[0]).__name__}]: {len(chunks)} chunks; types={[c.chunk_type for c in chunks]}")
    return chunks

# 1) procedure_image_guide  (the path that crashed: blk.block_type on a dict)
proc = [
    {"block_type": "heading", "text": "Sheet1", "extra": {}},
    {"block_type": "paragraph", "text": "目的：规范打样作业流程", "extra": {}},
    {"block_type": "paragraph", "text": "步骤1：接收需求并确认", "extra": {"step_no": 1, "figure_refs": ["图1"]}, "page_num": 1},
    {"block_type": "paragraph", "text": "步骤2：打样并记录", "extra": {"step_no": 2}, "page_num": 1},
]
print("procedure_image_guide:")
pd = run("procedure_image_guide", proc)
po = run("procedure_image_guide", objs(proc))
assert len(pd) == len(po) == 3, f"expected 3 (1 header + 2 step_card), got dict={len(pd)} obj={len(po)}"
assert sum(1 for c in pd if c.chunk_type == "step_card") == 2, "expected 2 step_card chunks"

# 2) product_spec_instruction  (the other dict-unsafe method)
spec = [
    {"block_type": "heading", "text": "sheet", "extra": {}},
    {"block_type": "paragraph", "text": "物料基本信息：物料名称 富岭一次性杯", "extra": {"row_num": 1}},
    {"block_type": "paragraph", "text": "原材料信息：PP 树脂，食品级", "extra": {"row_num": 2}},
    {"block_type": "paragraph", "text": "包装规格：550ml x 25 条/箱", "extra": {"row_num": 3}},
]
print("product_spec_instruction:")
sd = run("product_spec_instruction", spec)
so = run("product_spec_instruction", objs(spec))
assert len(sd) == len(so), f"spec dict/object mismatch: {len(sd)} vs {len(so)}"
assert len(sd) >= 1, "expected >=1 spec section chunk"

print("\nALL DICT-BLOCK TESTS PASSED ✅  (procedure + product_spec, dict & object)")
