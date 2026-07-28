# -*- coding: utf-8 -*-
"""#F-mm15：规则 13 —— 把本轮可用图片编号显式列进 prompt（`RAG_IMG_ID_WHITELIST`，**2026-07-27 起默认 ON**）。

为什么是"逐文档粒度"而不是"语义重绑定"
──────────────────────────────────────
`#F-mm14` 之后残余非法 marker 全部 |N − 最近合法图号| ≤ 2，看着像"数错号"，SOTA 调研
给出的唯一有量级支撑的路线也是 CiteFix 式重绑定（+15.5% MQLA / 0.015s）。但判别实验
（`docs/evidence/marker_slip_discriminator_20260727/`，判据先于数据写死、两种 marker
归属窗口互为对照且 **11/11 一致**）给出相反结论：

  · **H-noimg（来源判对了、那篇没图）9 条**，a−b = +0.06 ~ **+0.28**
    （B-clean_manual 三条 a≈0.30 vs b≈0.02，差一个数量级）
  · H-slip（数错号）仅 2 条，差 −0.038/−0.032 且 a、b 都在 0.03~0.10 的噪声区

被引文档类型吻合：9 条 H-noimg 全落在 `procedure_parent`(5) / `text_chunk`(4)——这两类
天然不绑图，图挂在兄弟 `step_card` 上。⇒ 重绑定会把图硬塞到别的文档上 = 给员工看错图。

A/B 实测（35 题图 case，mm14 已 ON 的姿态上加 mm15）：
    mm14 ON   79 marker /  8 非法 / validity 0.8987 / 被引用文档 65 / 放对≥1图 24
    +mm15     79 marker /  2 非法 / validity **0.9747** / 被引用文档 70 / 放对≥1图 24
覆盖率不降反升 ⇒ 不是靠少出图买来的合法率。

本组测试钉住：OFF 逐字节不变、清单取自 context 串（不是入参 chunks）、子下标归并、
去重排序、无编号不追加、显式 system_prompt 与 pure_text 不受影响、读 flag 失败 fail-open。
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("RAG_SIMULATE", "true")

import pytest  # noqa: E402

from opensearch_pipeline.llm_generator import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPT,
    TEXT_ONLY_SYSTEM_PROMPT,
    _img_id_whitelist_rule,
    _select_system_prompt,
)

CTX = "[文档3] a <<IMG:3>>\n[文档7] b <<IMG:7>>\n[文档9] c <<IMG:9>>\n"


@pytest.fixture
def _flag(monkeypatch):
    def _set(on: bool):
        from opensearch_pipeline.config import get_config
        monkeypatch.setattr(get_config().rag, "img_id_whitelist", on, raising=False)
        # 与 #F-mm14 解耦：本组只验规则 13，固定 mm14 为 OFF
        monkeypatch.setattr(get_config().rag, "img_rule_requires_images", False, raising=False)
    return _set


def test_off_is_byte_identical(_flag):
    """prompt-edit 纪律：OFF 时 prompt 必须与加本规则之前**逐字节**相同。"""
    _flag(False)
    assert _img_id_whitelist_rule(CTX) == ""
    assert _select_system_prompt(None, False, CTX) == DEFAULT_SYSTEM_PROMPT


def test_on_enumerates_sorted_deduped_ids(_flag):
    _flag(True)
    rule = _img_id_whitelist_rule("<<IMG:9>> x <<IMG:3>> y <<IMG:9>> z <<IMG:7>>")
    assert "只有 3、7、9 这3个" in rule, f"清单需去重+升序：{rule[:80]}"
    assert _select_system_prompt(None, False, CTX) == DEFAULT_SYSTEM_PROMPT + rule


def test_subindex_markers_collapse_to_the_document_number(_flag):
    """`<<IMG:7.1>>`/`<<IMG:7.2>>` 是同一篇文档的分图，清单里只能出现 7 一次。"""
    _flag(True)
    assert "只有 7 这1个" in _img_id_whitelist_rule("<<IMG:7.1>> <<IMG:7.2>>")


def test_rule_names_adjacent_ids_and_offers_abstention(_flag):
    """措辞是照判别实验定的，不是随手写：

    · 点名"相邻编号"—— 残差恰好全是 |Δ|≤2，泛泛说"清单以外"打不到点上；
    · 给出"宁可不插"的弃权许可 —— 9/11 是模型想配图而那篇没图，不给退路它就去够邻居。
    """
    _flag(True)
    rule = _img_id_whitelist_rule(CTX)
    assert "相邻" in rule
    assert "宁可不插" in rule


def test_no_ids_means_no_append(_flag):
    """无可用编号时不追加（该情形由 #F-mm14 负责，别在这里叠一条空清单规则）。"""
    _flag(True)
    assert _img_id_whitelist_rule("[文档1] 纯文本\n") == ""
    assert _img_id_whitelist_rule(None) == ""
    assert _select_system_prompt(None, False, "[文档1] 纯文本\n") == DEFAULT_SYSTEM_PROMPT


def test_ids_come_from_the_context_not_the_chunks(_flag):
    """清单必须取自**模型看到的 context 串**。

    被 max_context_chars 截掉的带图 chunk，其标记没进 context、模型引不了，
    也就不该出现在"可用编号"里——否则等于告诉模型去引一个它看不见的号。
    """
    _flag(True)
    truncated = "[文档1] a <<IMG:1>>\n[文档2] b...(截断)\n"     # 文档5 的图被截掉了
    assert "只有 1 这1个" in _img_id_whitelist_rule(truncated)


def test_explicit_system_prompt_and_pure_text_unaffected(_flag):
    _flag(True)
    assert _select_system_prompt("我自己的prompt", False, CTX) == "我自己的prompt"
    assert _select_system_prompt(None, True, CTX) == TEXT_ONLY_SYSTEM_PROMPT


def test_config_failure_falls_back_to_no_rule(monkeypatch):
    """读 flag 失败不得改变 prompt（fail-open 到历史行为）。"""
    import opensearch_pipeline.llm_generator as G
    monkeypatch.setattr(G, "get_config", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert _img_id_whitelist_rule(CTX) == ""


def test_composes_with_mm14(monkeypatch):
    """两个 flag 同时开：无图 → 走纯文本（mm14 优先，不追加清单）；有图 → 追加清单。"""
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config().rag, "img_id_whitelist", True, raising=False)
    monkeypatch.setattr(get_config().rag, "img_rule_requires_images", True, raising=False)
    assert _select_system_prompt(None, False, "[文档1] 纯文本\n") == TEXT_ONLY_SYSTEM_PROMPT
    got = _select_system_prompt(None, False, CTX)
    assert got.startswith(DEFAULT_SYSTEM_PROMPT) and "只有 3、7、9" in got


def test_both_prompt_flags_default_on():
    """**2026-07-27 起两个 flag 默认 ON**（Sam 拍板，各锁两轮 A/B 后）。

    钉住默认值本身：以前的测试全走 monkeypatch，谁把默认改回 False 都没人拦，而后果是
    幻觉 marker（mm14）与串邻号（mm15）静默重现 —— 恰恰是最难从指标上及时发现的那类回退。

    关闭走 `_env_bool` 的 off-list 语义（只认 false/0/no），未知值落回默认 ON：
    默认 ON 的开关若把配错的值也当关，一个错字就会静默退回旧行为。
    """
    from opensearch_pipeline.config import RAGConfig
    # 直接断言 **dataclass 默认值**，不经环境变量/单例 —— 单例可能已被别的测试污染，
    # 而"默认 ON"这件事本来就该由 dataclass 声明本身守住。
    assert RAGConfig().img_rule_requires_images is True, "#F-mm14 应默认 ON"
    assert RAGConfig().img_id_whitelist is True, "#F-mm15 应默认 ON"


@pytest.mark.parametrize("val,expected", [
    ("false", False), ("0", False), ("no", False), ("FALSE", False), (" false ", False),
    ("true", True), ("1", True), ("yes", True),
    ("", True), ("flase", True), ("off", True),      # 未知值 → 落回默认 ON
])
def test_off_list_semantics(monkeypatch, val, expected):
    """未知值必须落回 ON（含常见笔误 `flase`、以及**不被识别的** `off`）。

    ⚠️ `off` 不在 `_env_bool` 的关闭清单里 —— 想关必须写 false/0/no。这条测试把这个
    反直觉之处显式钉住，免得有人写 `RAG_IMG_ID_WHITELIST=off` 以为关了、实际没关。
    """
    # get_config 是模块级单例（`_config`，无 cache_clear），直接调 load_config() 重解析，
    # 不碰单例 —— 免得给同进程里后续测试留下被本用例改过的配置。
    import opensearch_pipeline.config as C
    monkeypatch.setenv("RAG_IMG_ID_WHITELIST", val)
    assert C.load_config().rag.img_id_whitelist is expected
