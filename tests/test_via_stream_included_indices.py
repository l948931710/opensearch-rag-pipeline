# -*- coding: utf-8 -*-
"""`generate_answer_via_stream` 必须透传 `included_doc_indices`（2026-07-27，codex 复核发现）。

缺陷：该包装器逐帧收集 SSE，但只留了 `sources`，把同帧下发的 `included_doc_indices`
丢掉 ⇒ 返回体缺该键 ⇒ `api.py` 的 `result.get("included_doc_indices")` 拿到 `None`
⇒ `build_mini_program_blocks` 退回**全量** image map，把被 `max_context_chars` 截掉、
**模型根本没看见**的图渲染给员工。

受影响的是 `thinking=True` 的 `/api/ask`（走本包装器）；非流式 `generate_answer`
（llm_generator.py:968）与 SSE 直连收集端（api.py:1700）都带了该字段，**只有这条
buffered 包装器漏了**——所以既有测试全绿也没拦住。

⚠️ 这是**生产用户可见**缺陷，不是评测口径问题：员工会看到与答案无关的图。
"""
from __future__ import annotations

import json
import os as _os

_os.environ.setdefault("RAG_SIMULATE", "true")

import pytest  # noqa: E402

import opensearch_pipeline.llm_generator as G  # noqa: E402


def _sse(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _fake_stream(frames):
    def _gen(*a, **kw):
        for f in frames:
            yield _sse(f)
    return _gen


def test_included_doc_indices_is_passed_through(monkeypatch):
    """核心用例：sources 帧带的可见文档号必须出现在返回体里。"""
    monkeypatch.setattr(G, "generate_answer_stream", _fake_stream([
        {"type": "sources", "sources": [{"title": "A"}], "included_doc_indices": [1, 3]},
        {"type": "chunk", "content": "正文 <<IMG:1>>"},
        {"type": "done", "model": "qwen-test", "usage": {"total_tokens": 9}},
    ]))
    out = G.generate_answer_via_stream("q", [{"chunk_text": "x"}])
    assert out["included_doc_indices"] == [1, 3]
    assert out["sources"] == [{"title": "A"}]


def test_missing_frame_yields_none_not_empty_list(monkeypatch):
    """**这条是护栏**：收不到 sources 帧时必须是 None，不能是 []。

    渲染器语义里 `[]` = "零文档进 context，一张图都不该出"，会把正常回答的图**全吃掉**；
    `None` = "此路不提供该信息，按旧行为走"，与 api.py 的 `.get()` 语义一致。
    """
    monkeypatch.setattr(G, "generate_answer_stream", _fake_stream([
        {"type": "chunk", "content": "正文"},
        {"type": "done", "model": "m", "usage": {}},
    ]))
    out = G.generate_answer_via_stream("q", [{"chunk_text": "x"}])
    assert out["included_doc_indices"] is None, "缺帧必须是 None——[] 会吃掉所有图"


def test_empty_list_from_frame_is_preserved(monkeypatch):
    """反向：帧里**明确**给了空列表（真的零文档进 context）时不得被当成缺失。"""
    monkeypatch.setattr(G, "generate_answer_stream", _fake_stream([
        {"type": "sources", "sources": [], "included_doc_indices": []},
        {"type": "done", "model": "m", "usage": {}},
    ]))
    out = G.generate_answer_via_stream("q", [{"chunk_text": "x"}])
    assert out["included_doc_indices"] == []


def test_both_generate_entrypoints_expose_the_same_key():
    """`generate_answer` 与 `generate_answer_via_stream` 的返回体必须暴露同名键。

    api.py 两条路径共用 `result.get("included_doc_indices")` 取值；键名不一致时
    只会静默拿到 None，而不是报错——正是本缺陷能潜伏至今的原因。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(G))
    for fn in ("generate_answer", "generate_answer_via_stream"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        rets = [r for r in ast.walk(node) if isinstance(r, ast.Return)
                and isinstance(r.value, ast.Dict)]
        assert rets, f"{fn} 未返回 dict 字面量"
        keys = {k.value for r in rets for k in r.value.keys
                if isinstance(k, ast.Constant)}
        assert "included_doc_indices" in keys, \
            f"{fn} 的返回体缺 included_doc_indices —— api.py 会静默拿到 None"


@pytest.mark.parametrize("indices,expect_imgs", [([1], 0), (None, 1), ([1, 2], 1)])
def test_renderer_honours_the_passed_indices(monkeypatch, indices, expect_imgs):
    """端到端语义：渲染器按 included_indices 收窄，被截断的图不得出现。

    答案只引 `<<IMG:2>>`：
      · indices=[1]（文档2 被截断）→ **0 张**，正是本缺陷该拦住的情形
      · indices=None（旧行为/信息缺失）→ 1 张
      · indices=[1,2]（都进了 context）→ 1 张
    """
    import opensearch_pipeline.content_blocks_builder as B
    # ⚠️ 必须打桩签名：测试环境无 OSS 凭据 → generate_signed_url 全失败 → 所有分支都渲染
    # 0 张图，`[1] → 0` 会**空断言通过**（0 不是因为索引过滤，是因为签名挂了）。
    # 第一版就是这么写的，被 None/[1,2] 两条红测试当场揭穿。
    monkeypatch.setattr(B, "generate_signed_url", lambda key, expires=None: f"https://x/{key}")
    build_content_blocks = B.build_content_blocks

    def ch(i):
        return {"chunk_id": f"c{i}", "chunk_type": "step_card", "chunk_text": f"文本{i}",
                "image_refs": [{"oss_key": f"k{i}", "source_image": f"s{i}.png",
                                "visual_summary": f"图{i}"}]}
    blocks = build_content_blocks("正文 <<IMG:2>>", [ch(1), ch(2)], included_indices=indices)
    n_img = sum(1 for b in blocks if str(b.get("type", "")).lower() in ("image", "img"))
    assert n_img == expect_imgs
