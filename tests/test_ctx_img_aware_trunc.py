# -*- coding: utf-8 -*-
"""context 截断图感知（#F-mm11）回归测试。

(a) 半截标记防漏（无 flag 常开）：截断点落在 header 首行内（超长标题+章节）时
    整条放弃 —— 半截 <<IMG: 会被 LLM 照抄成渲染/清洗正则不认识的残片漏给用户；
    切点在正文内保持原截断行为（chunk_text 不含标记）。
(b) RAG_CTX_IMG_AWARE_TRUNC（默认 OFF）：截断丢弃的尾部带图 chunk 以
    「header + 正文前 200 字」压缩条目补回最多 3 条，保住 <<IMG:N>> 提示；
    显式 10% 溢出预算。
"""

import types

import opensearch_pipeline.llm_generator as G
from opensearch_pipeline.config import RAGConfig
from opensearch_pipeline.llm_generator import _chunk_header, _format_context


def _cfg(monkeypatch, on: bool):
    cfg = types.SimpleNamespace(rag=RAGConfig(ctx_img_aware_trunc=on))
    monkeypatch.setattr(G, "get_config", lambda: cfg)


def _text_chunk(i, n_chars=400, title="设备操作规程.docx"):
    return {"chunk_type": "text_chunk", "title": title, "chunk_text": "字" * n_chars, "score": 8.0}


def _img_step(i, title="U8操作手册.docx", n_chars=400):
    return {"chunk_type": "step_card", "title": title, "step_no": i,
            "chunk_text": "步" * n_chars, "score": 7.9,
            "image_refs": [{"oss_key": f"s{i}.png", "caption": "截图"}]}


# ═══════════════ (a) 半截标记防漏（常开） ═══════════════

def test_mid_text_cut_behavior_preserved(monkeypatch):
    """切点在正文内：header 完整、保持「前缀+...(截断)」原行为。"""
    _cfg(monkeypatch, False)
    chunks = [_text_chunk(1, n_chars=500)]
    ctx = _format_context(chunks, max_chars=300)
    assert "...(截断)" in ctx
    assert ctx.startswith("[文档1] 设备操作规程.docx")


def test_header_line_cut_drops_entry_no_partial_marker(monkeypatch):
    """超长标题使切点落在 header 首行内 → 整条放弃，绝不漏半截 <<IMG:。
    旧行为：entry[:remaining] 会输出 '...[📷 图片] <<IMG' 这类残片。"""
    _cfg(monkeypatch, False)
    long_title = "超长标题" * 60   # header 首行 >240 chars
    filler = _text_chunk(1, n_chars=140)
    victim = _img_step(2, title=long_title)
    # max_chars 使 filler 全进、victim 的 remaining 落在 header 行内（>100 但 < header 长）
    ctx = _format_context([filler, victim], max_chars=len(f"[文档1] {filler['title']} (相关度: 高 8.00)\n{'字'*140}\n") + 150)
    assert "<<IMG" not in ctx or "<<IMG:2>>" in ctx   # 要么没有 victim 痕迹要么是完整标记
    # 更严格：不允许任何以半截形态出现的标记片段
    import re
    assert not re.search(r"<<?IMG(?::\d*)?$", ctx.replace("\n", " ").strip())
    assert not re.search(r"<<?IMG(?!:\d+>)", ctx)     # 出现的 IMG 标记必须是完整形态


def test_no_truncation_no_change(monkeypatch):
    _cfg(monkeypatch, False)
    chunks = [_text_chunk(1, n_chars=100), _img_step(2, n_chars=100)]
    ctx = _format_context(chunks, max_chars=6000)
    assert "...(截断)" not in ctx and "<<IMG:2>>" in ctx


# ═══════════════ (b) 压缩条目补救（flag） ═══════════════

def _truncating_setup():
    """3 个大文本 chunk 顶满预算（remaining<100，第 4 条起整体丢弃）+ 尾部 5 个带图 step_card。"""
    big = [_text_chunk(i, n_chars=1950) for i in range(3)]
    tail = [_img_step(i) for i in range(4, 9)]
    return big + tail


def test_flag_off_dropped_images_stay_dropped(monkeypatch):
    _cfg(monkeypatch, False)
    ctx = _format_context(_truncating_setup(), max_chars=6000)
    assert "<<IMG:" not in ctx     # 尾部带图卡整体出局（现状）


def test_flag_on_salvages_up_to_three_with_markers(monkeypatch):
    _cfg(monkeypatch, True)
    chunks = _truncating_setup()
    ctx = _format_context(chunks, max_chars=6000)
    # 最多 3 条压缩条目，带完整 <<IMG:N>> 标记（N=1-based 原位置）
    salvaged = [n for n in range(4, 9) if f"<<IMG:{n}>>" in ctx]
    assert 1 <= len(salvaged) <= 3
    assert "...(截断)" in ctx
    # 压缩条目正文 ≤200 字
    assert "步" * 201 not in ctx


def test_flag_on_respects_overflow_budget(monkeypatch):
    """压缩条目总量受 10% 溢出预算约束：总 context ≤ max_chars*1.1 + 分隔符。"""
    _cfg(monkeypatch, True)
    max_chars = 6000
    ctx = _format_context(_truncating_setup(), max_chars=max_chars)
    assert len(ctx) <= int(max_chars * 1.1) + 200   # 分隔符/尾串余量


def test_flag_on_pure_text_never_salvages(monkeypatch):
    """纯文本模式不展示图片 → 压缩补救无意义，必须不触发。"""
    _cfg(monkeypatch, True)
    ctx = _format_context(_truncating_setup(), max_chars=6000, pure_text=True)
    assert "<<IMG:" not in ctx


def test_flag_on_skips_imageless_dropped_chunks(monkeypatch):
    """被丢弃的无图 chunk 不参与补救（只救带图的）。"""
    _cfg(monkeypatch, True)
    chunks = ([_text_chunk(i, n_chars=1900) for i in range(3)]
              + [_text_chunk(4), _img_step(5)])
    ctx = _format_context(chunks, max_chars=6000)
    assert "<<IMG:5>>" in ctx
    assert ctx.count("...(截断)") >= 1


# ═══════════════ header 抽取等价性（防重构漂移） ═══════════════

def test_chunk_header_matches_context_output(monkeypatch):
    """_chunk_header 与 _format_context 输出的 header 逐字节一致（抽取重构回归）。"""
    _cfg(monkeypatch, False)
    for chunk in (_img_step(1), _text_chunk(1),
                  {"chunk_type": "image", "title": "图doc", "source_image": "a.png",
                   "visual_summary": "描述", "chunk_text": "", "score": 7.8},
                  {"chunk_type": "procedure_parent", "title": "流程doc", "chunk_text": "概览", "score": 7.0}):
        h = _chunk_header(0, chunk, pure_text=False)
        ctx = _format_context([chunk], max_chars=6000)
        assert ctx.startswith(h), f"header 漂移: {h!r} vs {ctx[:120]!r}"
