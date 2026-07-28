# -*- coding: utf-8 -*-
"""#F-mm14：无可插图时不发图规则（`RAG_IMG_RULE_REQUIRES_IMAGES`，**2026-07-27 起默认 ON**）。

起因（2026-07-27，35 题图 case 实测，`docs/evidence/marker_validity_20260727/`）：
`marker_validity` 0.6458 对硬闸 ≥0.95。逐条裁定"越界 / 截断 / 渲染侧不可渲染"三种解释
**各 0 例**；21 个去重非法 N 全部是"文档在 context 里但没有 `<<IMG:N>>` 标记"，其中
**62%（21/34 次）来自 4 道 context 里一张图都没有的题**（BIND-06 把 `<<IMG:1>>` 发了 7 次）。

根因：规则 10 只按调用点的 `pure_text` 挂，不看本轮 context 有没有图。

本组测试钉住：
  1. OFF 时**逐字节**回到原行为（prompt-edit 纪律的硬要求）；
  2. ON 且 context 无 `<<IMG:` 时改用纯文本 prompt；
  3. ON 但 context **有**图标记时不得误伤；
  4. 显式 `system_prompt` 入参永远优先，flag 不介入；
  5. 判据用 **context 串**而非入参 chunks —— 带图 chunk 被 6000 字预算截掉时，
     标记同样没进 context，规则同样该撤。
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("RAG_SIMULATE", "true")

import pytest  # noqa: E402

from opensearch_pipeline.llm_generator import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPT,
    TEXT_ONLY_SYSTEM_PROMPT,
    _IMG_INTERLEAVE_RULE,
    _select_system_prompt,
)

CTX_WITH_IMG = "[文档1] 标题 [📷 图片] <<IMG:1>>\n正文\n"
CTX_NO_IMG = "[文档1] 标题\n正文\n[文档2] 另一篇\n正文\n"


@pytest.fixture
def _flag(monkeypatch):
    def _set(on: bool):
        from opensearch_pipeline.config import get_config
        monkeypatch.setattr(get_config().rag, "img_rule_requires_images", on, raising=False)
        # 与 #F-mm15 解耦：本组只验规则 10 的挂载条件。mm15 于 2026-07-27 翻默认 ON 后
        # 会往 DEFAULT 后面追加规则 13，逐字节等值断言随之失效 —— 对称于 mm15 测试里
        # 固定 mm14 为 OFF 的做法。
        monkeypatch.setattr(get_config().rag, "img_id_whitelist", False, raising=False)
    return _set


def test_off_is_byte_identical_to_legacy(_flag):
    """prompt-edit 纪律：flag OFF 时选出的 prompt 必须与旧表达式**逐字节**相同。"""
    _flag(False)
    for ctx in (CTX_WITH_IMG, CTX_NO_IMG, ""):
        assert _select_system_prompt(None, False, ctx) == DEFAULT_SYSTEM_PROMPT
        assert _select_system_prompt(None, True, ctx) == TEXT_ONLY_SYSTEM_PROMPT


def test_on_drops_image_rule_when_context_has_no_marker(_flag):
    _flag(True)
    got = _select_system_prompt(None, False, CTX_NO_IMG)
    assert got == TEXT_ONLY_SYSTEM_PROMPT
    assert _IMG_INTERLEAVE_RULE not in got, "规则 10 必须不在场——它就是幻觉的来源"


def test_on_keeps_image_rule_when_context_has_marker(_flag):
    """不能误伤：有图时规则 10 必须还在，否则图文能力直接没了。"""
    _flag(True)
    got = _select_system_prompt(None, False, CTX_WITH_IMG)
    assert got == DEFAULT_SYSTEM_PROMPT
    assert _IMG_INTERLEAVE_RULE in got


def test_explicit_system_prompt_always_wins(_flag):
    """调用方显式传 prompt 时 flag 不得介入（api/bot 的自定义 prompt 路径）。"""
    for on in (False, True):
        _flag(on)
        assert _select_system_prompt("我自己的prompt", False, CTX_NO_IMG) == "我自己的prompt"
        assert _select_system_prompt("我自己的prompt", True, CTX_WITH_IMG) == "我自己的prompt"


def test_pure_text_unaffected_by_flag(_flag):
    for on in (False, True):
        _flag(on)
        assert _select_system_prompt(None, True, CTX_WITH_IMG) == TEXT_ONLY_SYSTEM_PROMPT


def test_criterion_is_the_context_string_not_the_chunks(_flag):
    """判据必须是 context 串。

    带图 chunk 被 `max_context_chars` 截掉时，`<<IMG:N>>` 同样没进 context —— 若改用
    "入参 chunks 里有没有 image_refs"作判据，这种情形会保留规则 10，幻觉照旧。
    """
    _flag(True)
    truncated_ctx = "[文档1] 标题\n正文...(截断)\n"      # 带图的文档 2 被截掉了
    assert _select_system_prompt(None, False, truncated_ctx) == TEXT_ONLY_SYSTEM_PROMPT


def test_none_context_is_tolerated(_flag):
    """fail-open：context 为 None/空不得抛异常（生成链路不因辅助判据断掉）。"""
    _flag(True)
    assert _select_system_prompt(None, False, None) == TEXT_ONLY_SYSTEM_PROMPT


def test_config_failure_falls_back_to_legacy(monkeypatch):
    """配置读不到时维持历史行为，不得因为读 flag 失败就改变 prompt。"""
    import opensearch_pipeline.llm_generator as G
    monkeypatch.setattr(G, "get_config", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert _select_system_prompt(None, False, CTX_NO_IMG) == DEFAULT_SYSTEM_PROMPT


def _selector_calls(node, name="_select_system_prompt"):
    import ast
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)
            and (getattr(c.func, "id", None) == name
                 or getattr(c.func, "attr", None) == name)]


def test_all_generation_paths_use_the_single_selector():
    """**每一条**产答案的路径都必须走 `_select_system_prompt` —— 含评测侧。

    ⚠️ 这条测试第一版只查了 llm_generator 的两个生产函数，于是漏掉了
    `eval_harness/gen_nothink.py` 里那份逐字复制的 `TEXT_ONLY if _pure else DEFAULT`。
    后果：#F-mm14 落地当天的 A/B 是**假 ON** —— flag 只改了生产路径，评测走复制品，
    读出的 0.6458→0.6774 全是温度噪声。评测与生产的 prompt 选择不同源 = 量的不是被改
    的那个东西。新增产答案路径时，请一并加进下面的清单。
    """
    import ast
    import inspect

    import opensearch_pipeline.llm_generator as G
    from eval_harness import gen_nothink as N

    targets = [(G, "generate_answer", "_format_context_ex"),
               (G, "generate_answer_stream", "_format_context_ex"),
               (N, "generate_answer_nothink", "_format_context")]
    for mod, fn, fmt_name in targets:
        tree = ast.parse(inspect.getsource(mod))
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        calls = _selector_calls(node)
        assert calls, f"{mod.__name__}.{fn} 未走 _select_system_prompt（又抄了一份？）"
        # 且 context 必须在选 prompt **之前**算出来（否则判据拿不到 context）
        fmt = [c.lineno for c in ast.walk(node) if isinstance(c, ast.Call)
               and (getattr(c.func, "id", None) == fmt_name
                    or getattr(c.func, "attr", None) == fmt_name)]
        assert fmt and min(fmt) < min(c.lineno for c in calls), \
            f"{fn} 里 {fmt_name} 必须先于 _select_system_prompt"


def test_no_generation_module_hardcodes_the_legacy_selection():
    """反向护栏：产答案的模块里不得再出现 `... if ... else DEFAULT_SYSTEM_PROMPT` 的裸表达式。

    只允许 `_select_system_prompt` 自己持有该三元式。
    """
    import inspect
    import re

    import opensearch_pipeline.llm_generator as G
    from eval_harness import gen_nothink as N

    # `[\w.]*` 而非 `\w*`：跨模块引用写作 `L.DEFAULT_SYSTEM_PROMPT`，点号不属于 \w。
    # 第一版漏了这一点 ⇒ 正则永远匹配不到，测试恒绿 = 空断言。已做变异验证。
    pat = re.compile(r"if\s+_?pure\w*\s+else\s+[\w.]*DEFAULT_SYSTEM_PROMPT")
    for mod in (G, N):
        hits = [ln.strip() for ln in inspect.getsource(mod).splitlines() if pat.search(ln)]
        assert not hits, f"{mod.__name__} 里仍有裸的 prompt 选择表达式（应改调 " \
                         f"_select_system_prompt）: {hits}"
