# -*- coding: utf-8 -*-
"""#F-clause-img 回归：clause 模式按文档位置就近绑图，而非全部挂到最后一个 clause_chunk。

缺陷（修复前）：_chunk_by_clause 把整份 pending_image_refs 全挂到 result_chunks[-1]，
多条款文档里分散在不同条款的图全被绑到最后一块 → 检索命中末条款渲染无关图、相关条款丢图。
修复：按图在 full_text 中的偏移就近绑到覆盖它的 clause_chunk（对齐 _chunk_by_step）。
"""

from opensearch_pipeline.chunker import DocumentChunker


def _para(text):
    return {"block_type": "paragraph", "text": text, "page_num": 1,
            "section_path": None, "source": "native", "extra": {}}


def _img(tag):
    return {"block_type": "image_ref", "text": "", "page_num": 1,
            "section_path": None, "source": "native",
            "extra": {"visual_summary": tag, "oss_key": f"processing/assets/{tag}.png",
                      "filename": f"{tag}.png"}}


# 三条独立条款，每条 >= min_chunk_chars(50) 以形成独立 clause_chunk（避免短条款被合并）；
# 图 A 紧跟第一条正文之后、图 C 在第三条正文之后。
_C1 = ("第一条 为规范本公司各类信息系统的日常使用与运行维护管理，切实维护企业核心数据资产的"
       "安全性与保密性，保障各项关键业务的连续稳定运行，特制定本管理制度并要求全体员工遵照执行。")
_C2 = ("第二条 全体员工在登录并使用U8业务系统及其相关子系统时，必须使用经审批开通的个人实名账号，"
       "严禁以任何形式共享账号、外借口令或将登录凭据告知他人代为操作使用。")
_C3 = ("第三条 系统各主要业务操作界面的标准示例详见本制度随附的图示说明，员工在实际操作过程中"
       "一旦遇到系统异常或数据错误，应在第一时间联系信息技术部门介入排查处理。")


def _run_clause():
    ch = DocumentChunker(split_mode="clause")
    blocks = [_para(_C1), _img("IMG_A"), _para(_C2), _para(_C3), _img("IMG_C")]
    return ch._chunk_by_clause(blocks, doc_id="CLAUSEIMG01", version_no=1)


def _refs(chunk):
    return [r.get("visual_summary") for r in (chunk.extra or {}).get("image_refs", [])]


def test_scattered_clause_images_bind_near_not_all_to_last():
    chunks = [c for c in _run_clause() if c.chunk_type != "table_chunk"]
    assert len(chunks) == 3, f"应得 3 个 clause_chunk，实得 {len(chunks)}：{[c.chunk_text[:12] for c in chunks]}"

    # 每张图绑到覆盖其文档位置的条款：A→第一条(chunk0)，C→第三条(chunk2)
    assert "IMG_A" in _refs(chunks[0]), f"IMG_A 应绑到第一条，实际 chunk0={_refs(chunks[0])}"
    assert "IMG_C" in _refs(chunks[2]), f"IMG_C 应绑到第三条，实际 chunk2={_refs(chunks[2])}"
    # 关键：第二条不应无端得到别条款的图
    assert _refs(chunks[1]) == [], f"第二条不应携带图，实际={_refs(chunks[1])}"
    # 修复前的错误形态：两图都在最后一块
    assert not ("IMG_A" in _refs(chunks[2]) and "IMG_C" in _refs(chunks[2])), \
        "回归：两张分散图不应双双绑到最后一个 clause_chunk"


def test_no_image_lost_in_clause_binding():
    all_refs = []
    for c in _run_clause():
        all_refs += _refs(c)
    assert sorted(all_refs) == ["IMG_A", "IMG_C"], f"绝不丢图，实得 {sorted(all_refs)}"
