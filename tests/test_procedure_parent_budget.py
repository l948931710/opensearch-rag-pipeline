# -*- coding: utf-8 -*-
"""procedure_parent token 预算 + validate 引用完整性安全网回归测试 (2026-06-15)。

bug: 多步 SOP（如 A37突发事件 0959E5, 116 步）把全部 step 标题拼进 procedure_parent →
2370 tokens > node_validate_chunks 2000 上限 → parent 静默丢弃 → 全部 step_card 成孤儿。

fix: chunker 按 token 预算截断 parent 标题列表（保留总步数提示），稳低于 2000；
     validate 增加引用完整性安全网（parent 被丢时切断 step 的悬挂 parent_chunk_id + 告警）。
"""
import os
os.environ.setdefault("RAG_SIMULATE", "true")

from opensearch_pipeline.chunker import DocumentChunker, _estimate_tokens


def _numbered_blocks(n: int):
    """构造 n 个编号步骤 block + 标题/前导，触发 step 模式。"""
    blocks = [
        {"block_type": "heading", "text": "XX突发事件处理工作规程", "page_num": 1},
        {"block_type": "paragraph", "text": "1目的：规范本公司各类突发事件的应急处理流程与职责分工。", "page_num": 1},
    ]
    for i in range(1, n + 1):
        blocks.append({
            "block_type": "paragraph",
            "text": f"4.{i} 第{i}步：办公室负责执行第{i}项应急处理措施并记录归档结果与跟进。",
            "page_num": 1 + i // 10,
        })
    return blocks


def _chunk(n):
    ch = DocumentChunker(split_mode="step")
    chunks = ch.chunk_from_blocks(_numbered_blocks(n), doc_id="DOC_TEST_SOP", version_no=1,
                                  metadata={"title": "XX突发事件处理工作规程.docx"})
    parents = [c for c in chunks if c.chunk_type == "procedure_parent"]
    steps = [c for c in chunks if c.chunk_type == "step_card"]
    return chunks, parents, steps


def test_many_steps_parent_under_cap_and_children_linked():
    """120 步：parent token<2000、带截断提示、所有 step_card parent_chunk_id 解析到该 parent。"""
    chunks, parents, steps = _chunk(120)
    assert len(parents) == 1, f"应恰好 1 个 procedure_parent，实际 {len(parents)}"
    p = parents[0]
    assert p.token_count <= 2000, f"parent token={p.token_count} 必须 <=2000 否则被 validate 丢"
    assert _estimate_tokens(p.chunk_text) <= 2000
    assert "完整流程共" in p.chunk_text and "个步骤" in p.chunk_text, "应有截断提示+总步数"
    assert len(steps) >= 100, f"应产出大量 step_card，实际 {len(steps)}"
    # 引用完整性：每个 step 的 parent_chunk_id 必须 == parent.chunk_id（在有效集内）
    valid_ids = {c.chunk_id for c in chunks}
    for s in steps:
        pid = s.extra.get("parent_chunk_id")
        assert pid == p.chunk_id, f"step parent_chunk_id={pid} != parent {p.chunk_id}"
        assert pid in valid_ids, "parent_chunk_id 必须可解析（不孤儿）"


def test_few_steps_no_truncation():
    """少量步骤：parent 不截断，无总步数提示，全部标题在内。"""
    chunks, parents, steps = _chunk(5)
    assert len(parents) == 1
    p = parents[0]
    assert p.token_count <= 2000
    assert "仅展示前" not in p.chunk_text, "5 步不该触发截断"


def test_step_preamble_emitted_once_not_duplicated():
    """F-4：step 模式的前导文本只发一次 text_chunk（删了重复的 Phase 4.9）。

    此前 Phase 2 逐条发块 + Phase 4.9 又把同一份 preamble 扁平化再发一遍 → 每个带前言的 SOP
    产出逐字节相同的重复 text_chunk（白付 embedding + 索引位、挤占 top_k、污染来源面板）。"""
    long_pre = ("目的：规范本公司各类突发事件的应急处理流程与职责分工，明确各岗位在应急响应中的"
                "具体职责、上报路径与记录归档要求，确保处置及时、可追溯。")
    blocks = [
        {"block_type": "heading", "text": "XX突发事件处理工作规程", "page_num": 1},
        {"block_type": "paragraph", "text": long_pre, "page_num": 1},
        {"block_type": "paragraph", "text": "4.1 第1步：办公室负责执行第1项应急处理措施并记录归档结果与跟进。", "page_num": 1},
        {"block_type": "paragraph", "text": "4.2 第2步：办公室负责执行第2项应急处理措施并记录归档结果与跟进。", "page_num": 1},
    ]
    ch = DocumentChunker(split_mode="step")
    chunks = ch.chunk_from_blocks(blocks, doc_id="DOC_PRE", version_no=1,
                                  metadata={"title": "XX突发事件处理工作规程.docx"})
    text_chunks = [c for c in chunks if c.chunk_type == "text_chunk"]
    # 前导必须至少产出一个 text_chunk（长度已远超 min_chunk_chars）
    assert text_chunks, "前导文本应产出 text_chunk"
    # 关键：绝不出现逐字节相同的重复 text_chunk
    texts = [c.chunk_text for c in text_chunks]
    assert len(texts) == len(set(texts)), f"step 前导文本被重复发块：{texts}"
    # parent 仍从前导抽摘要，不受影响
    assert any(c.chunk_type == "procedure_parent" for c in chunks)


def test_validate_severs_orphan_parent_link():
    """validate 安全网：parent 被丢(too_many_tokens)时，step 的悬挂 parent_chunk_id 被置空。"""
    from opensearch_pipeline.pipeline_nodes import node_validate_chunks
    ch = DocumentChunker(split_mode="step")
    # 造一个超长 parent（>2000 token）+ 2 个引用它的 step_card
    long_text = "应急处理措施" * 700  # ~4200 cn chars → ~2800 tokens
    parent = ch._create_chunk(doc_id="D", version_no=1, chunk_index=99,
                              chunk_type="procedure_parent", chunk_text=long_text)
    s1 = ch._create_chunk(doc_id="D", version_no=1, chunk_index=0,
                          chunk_type="step_card", chunk_text="4.1 第一步操作说明内容足够长以通过校验阈值")
    s2 = ch._create_chunk(doc_id="D", version_no=1, chunk_index=1,
                          chunk_type="step_card", chunk_text="4.2 第二步操作说明内容足够长以通过校验阈值")
    s1.extra["parent_chunk_id"] = parent.chunk_id
    s2.extra["parent_chunk_id"] = parent.chunk_id
    ctx = {"chunks": [parent, s1, s2]}
    node_validate_chunks(ctx)
    valid = ctx["valid_chunks"]
    assert parent.token_count > 2000, "测试前提：parent 超 2000"
    assert parent not in valid, "超长 parent 应被判 invalid 丢弃"
    assert s1 in valid and s2 in valid, "step 应保留（优雅降级，不阻断）"
    for s in (s1, s2):
        assert s.extra.get("parent_chunk_id") is None, "悬挂 parent_chunk_id 应被切断置空"
        assert s.extra.get("orphaned_parent") == parent.chunk_id, "应留痕原 parent 便于定位"
    assert ctx.get("validation_warnings"), "应记录 severed 告警"
