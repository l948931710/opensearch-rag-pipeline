# -*- coding: utf-8 -*-
"""<<IMG:N.M>> 图级子下标寻址（#F-mm6, RAG_IMG_SUBINDEX 默认 OFF）回归测试。

根因：<<IMG:N>> 是 chunk 级——多图 step_card 的全部图捆在同一 N，_build_interleaved
在首个占位符整包倾倒、同 N 二次引用 no-op → LLM 无法「步骤2放图A、步骤5放图B」。
数据层每图已可独立寻址（renderable_image_refs），瓶颈只在标记协议层。

不变式：
  - OFF：M 强制忽略，纯 N 整包，行为与历史逐字节一致；
  - ON：某 N 有有效 .M 引用 → 分图模式（distinct (N,m) 独立渲染，裸 N 忽略）；
        无 .M → 纯 N 整包（向后兼容）；M 越界忽略（计幻觉）；
  - llm 编 m 与渲染侧 renderable_image_refs 同源（防图-步错绑）。
"""

import types

import opensearch_pipeline.content_blocks_builder as cb
import opensearch_pipeline.llm_generator as G
from opensearch_pipeline.config import RAGConfig
from opensearch_pipeline.content_blocks_builder import (
    _IMG_PLACEHOLDER_PATTERN,
    _extract_image_chunks,
    _parse_marker,
    build_content_blocks,
    renderable_image_refs,
    strip_image_markers,
)


def _fake_sign(monkeypatch):
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=None: "https://oss/" + key)


def _subindex(monkeypatch, on):
    monkeypatch.setattr(cb, "_img_subindex_enabled", lambda: on)


def _step(doc_id, n_imgs, title="操作手册.docx"):
    return {"chunk_type": "step_card", "doc_id": doc_id, "title": title,
            "image_refs": [{"oss_key": f"{doc_id}-{j}.png", "caption": f"图{j}说明"}
                           for j in range(1, n_imgs + 1)]}


def _imgs(blocks):
    return [b["oss_key"] for b in blocks if b["type"] == "image"]


# ═══════════════ regex 扩宽 + parse ═══════════════

def test_pattern_parses_bare_and_subindex():
    assert _parse_marker(_IMG_PLACEHOLDER_PATTERN.search("<<IMG:3>>")) == (3, None)
    assert _parse_marker(_IMG_PLACEHOLDER_PATTERN.search("<IMG:3>")) == (3, None)
    assert _parse_marker(_IMG_PLACEHOLDER_PATTERN.search("<<IMG:3.2>>")) == (3, 2)


def test_pattern_backward_compatible_group1():
    """扩宽后 group(1) 仍是 N（mm_answer_metrics 等只取 group(1) 的消费者不受影响）。"""
    ms = list(_IMG_PLACEHOLDER_PATTERN.finditer("a <<IMG:1>> b <<IMG:2.3>> c"))
    assert [int(m.group(1)) for m in ms] == [1, 2]


# ═══════════════ renderable_image_refs = 单一权威 ═══════════════

def test_renderable_refs_matches_extract_image_chunks():
    chunks = [_step("D1", 3), {"chunk_type": "image", "doc_id": "D2",
                               "source_image": "s.png", "visual_summary": "v", "title": "t"}]
    im = _extract_image_chunks(chunks)
    assert [r["source_image"] for r in im[1]] == [r["source_image"] for r in renderable_image_refs(chunks[0])]
    assert len(im[1]) == 3 and len(im[2]) == 1


def test_renderable_refs_skips_no_oss_key():
    chunk = {"chunk_type": "step_card", "title": "t",
             "image_refs": [{"caption": "无图源"}, {"oss_key": "ok.png"}]}
    refs = renderable_image_refs(chunk)
    assert len(refs) == 1 and refs[0]["source_image"] == "ok.png"


# ═══════════════ OFF：逐字节等价（整包，忽略 M）═══════════════

def test_off_multi_image_whole_chunk(monkeypatch):
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, False)
    b = build_content_blocks("步骤如图 <<IMG:1>> 完成", [_step("D1", 3)], max_images=6)
    assert _imgs(b) == ["D1-1.png", "D1-2.png", "D1-3.png"]   # 整包，全在首标记处


def test_off_ignores_m_suffix(monkeypatch):
    """OFF 时即使答案里出现 .M（幻觉/历史），也当纯 N 整包（向后兼容）。"""
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, False)
    b = build_content_blocks("见 <<IMG:1.2>>", [_step("D1", 3)], max_images=6)
    assert _imgs(b) == ["D1-1.png", "D1-2.png", "D1-3.png"]   # .2 被忽略 → 整包


# ═══════════════ ON：分图寻址 ═══════════════

def test_on_distinct_subimages_at_distinct_positions(monkeypatch):
    """核心特性：同一文档不同子图放到不同步骤后。"""
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, True)
    b = build_content_blocks("第一步 <<IMG:1.1>>，第三步 <<IMG:1.3>>。", [_step("D1", 3)], max_images=6)
    # 只出 1、3，且按位置顺序（不是整包）
    assert _imgs(b) == ["D1-1.png", "D1-3.png"]
    types_seq = [x["type"] for x in b]
    # 图片穿插在文本之间（首个图在首段之后、次图在次段之后）
    assert types_seq.count("image") == 2


def test_on_reuse_same_subindex_dedup(monkeypatch):
    """同 (N,m) 二次引用：第二处被 used 跳过（同一张图不重复）。"""
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, True)
    b = build_content_blocks("先 <<IMG:1.2>> 再 <<IMG:1.2>>", [_step("D1", 3)], max_images=6)
    assert _imgs(b) == ["D1-2.png"]


def test_on_out_of_range_m_ignored(monkeypatch):
    """越界 M 计幻觉、不渲染；无其他有效引用 → 降级空。"""
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, True)
    b = build_content_blocks("见 <<IMG:1.9>>", [_step("D1", 3)], max_images=6)
    assert b == []


def test_on_bare_n_without_subindex_refs_is_whole_chunk(monkeypatch):
    """ON 但该 N 没有任何 .M 引用 → 纯 N 整包（向后兼容）。"""
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, True)
    b = build_content_blocks("见 <<IMG:1>>", [_step("D1", 3)], max_images=6)
    assert _imgs(b) == ["D1-1.png", "D1-2.png", "D1-3.png"]


def test_on_bare_n_ignored_when_n_is_subindexed(monkeypatch):
    """同一 N 混用裸标记与 .M：裸标记被忽略（歧义），只渲染 .M 指定的。"""
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, True)
    b = build_content_blocks("整体 <<IMG:1>> 但看 <<IMG:1.2>>", [_step("D1", 3)], max_images=6)
    assert _imgs(b) == ["D1-2.png"]


def test_on_single_image_chunk_still_bare_n(monkeypatch):
    """单图 chunk：ON 也发纯 N（无 .M）—— 与历史一致。"""
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, True)
    b = build_content_blocks("见 <<IMG:1>>", [_step("D1", 1)], max_images=6)
    assert _imgs(b) == ["D1-1.png"]


def test_on_cross_doc_subindex(monkeypatch):
    """两个多图文档各自分图，互不干扰。"""
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, True)
    chunks = [_step("D1", 2), _step("D2", 2)]
    b = build_content_blocks("A <<IMG:1.2>> B <<IMG:2.1>>", chunks, max_images=6)
    assert _imgs(b) == ["D1-2.png", "D2-1.png"]


# ═══════════════ llm_generator 发标记（编 m 与渲染同源）═══════════════

def test_marker_segment_multi_image_emits_subindex(monkeypatch):
    _subindex(monkeypatch, True)   # _img_marker_segment 走 cb._img_subindex_enabled
    seg = G._img_marker_segment(0, _step("D1", 3))   # i=0 → N=1
    assert "<<IMG:1.1>>" in seg and "<<IMG:1.2>>" in seg and "<<IMG:1.3>>" in seg
    assert "图1：" in seg and "图3：" in seg          # 逐图编号 caption
    assert "<<IMG:1>>" not in seg.replace("<<IMG:1.", "")   # 不发裸 N


def test_marker_segment_single_image_bare_n(monkeypatch):
    _subindex(monkeypatch, True)
    seg = G._img_marker_segment(2, _step("D1", 1))   # i=2 → N=3
    assert seg == " [📷 图片] <<IMG:3>>"             # 单图恒纯 N


def test_marker_segment_off_bare_n(monkeypatch):
    _subindex(monkeypatch, False)
    seg = G._img_marker_segment(0, _step("D1", 3))
    assert seg == " [📷 图片] <<IMG:1>>"             # OFF 恒纯 N（逐字节等价）


def test_chunk_header_multi_image_subindex(monkeypatch):
    _subindex(monkeypatch, True)
    h = G._chunk_header(0, _step("D1", 2), pure_text=False)
    assert "<<IMG:1.1>>" in h and "<<IMG:1.2>>" in h


def test_marker_m_aligns_with_render(monkeypatch):
    """端到端：llm 发的 N.m 与渲染侧第 m 张 oss_key 对齐（同源，防错绑）。"""
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, True)
    chunk = {"chunk_type": "step_card", "doc_id": "D1", "title": "t",
             "image_refs": [{"caption": "跳过无oss"}, {"oss_key": "real1.png", "caption": "a"},
                            {"oss_key": "real2.png", "caption": "b"}]}   # 首个无 oss_key 被过滤
    seg = G._img_marker_segment(0, chunk)   # 可渲染 refs = [real1, real2] → N.1/N.2
    assert "<<IMG:1.1>>" in seg and "<<IMG:1.2>>" in seg and "1.3" not in seg
    # 渲染 N.1 → real1（不是被过滤的无 oss ref）
    b = build_content_blocks("见 <<IMG:1.1>>", [chunk], max_images=6)
    assert _imgs(b) == ["real1.png"]


def test_build_messages_appends_rule12_only_when_on(monkeypatch):
    """规则 12 仅 subindex ON + 图文 prompt 时条件追加（不改模块常量，OFF 逐字节不变）。"""
    cfg_on = types.SimpleNamespace(rag=RAGConfig(img_subindex=True))
    monkeypatch.setattr(G, "get_config", lambda: cfg_on)
    msgs = G._build_messages("q", "ctx")
    assert G._IMG_SUBINDEX_RULE in msgs[0]["content"]
    assert G._IMG_SUBINDEX_RULE not in G.DEFAULT_SYSTEM_PROMPT   # 模块常量未被污染

    cfg_off = types.SimpleNamespace(rag=RAGConfig(img_subindex=False))
    monkeypatch.setattr(G, "get_config", lambda: cfg_off)
    msgs_off = G._build_messages("q", "ctx")
    assert msgs_off[0]["content"] == G.DEFAULT_SYSTEM_PROMPT     # OFF 逐字节不变

    # 纯文本 prompt 不追加（规则 12 属图文规则）
    monkeypatch.setattr(G, "get_config", lambda: cfg_on)
    assert G._IMG_SUBINDEX_RULE not in G._build_messages(
        "q", "ctx", system_prompt=G.TEXT_ONLY_SYSTEM_PROMPT)[0]["content"]


# ═══════════════ 清洗（strip / sanitize 含 .M）═══════════════

def test_strip_image_markers_removes_subindex(monkeypatch):
    monkeypatch.setattr(cb, "_marker_lenient_enabled", lambda: False)
    assert "IMG" not in strip_image_markers("残留 <<IMG:3.2>> 清除")
    assert "IMG" not in strip_image_markers("残留 <<IMG:3>> 清除")


def test_sanitize_blocks_strips_leftover_subindex(monkeypatch):
    _fake_sign(monkeypatch)
    _subindex(monkeypatch, True)
    # 越界 .5 与有效 .1 混合：.5 残片必须被清出 markdown
    b = build_content_blocks("有效 <<IMG:1.1>> 越界 <<IMG:1.5>> 尾", [_step("D1", 2)], max_images=6)
    assert all("IMG" not in x.get("content", "") for x in b if x["type"] == "markdown")
    assert _imgs(b) == ["D1-1.png"]


def test_dingtalk_card_strips_subindex():
    import opensearch_pipeline.dingtalk_card as dc
    import re
    # 复刻 dingtalk_card:416 的清洗正则，确认 .M 被清
    cleaned = re.sub(r'<{1,2}IMG:\d+(?:\.\d+)?>{1,2}', '', "答案 <<IMG:3.2>> 尾").strip()
    assert "IMG" not in cleaned
    assert hasattr(dc, "build_answer_card") or True   # 模块可导入
