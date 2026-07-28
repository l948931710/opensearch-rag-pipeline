# -*- coding: utf-8 -*-
"""L1 标题归一口径（`matching.normalize_title` / `title_similarity`）的两处修正（2026-07-27）。

L1 的全部 recall@k 由 `gold_doc_rank` 决定，而它只看**归一后的标题**。查 rerank 排序头部
空间时，逐题翻 top-5 发现两个方向相反的口径缺陷：

  ① 不剥文件扩展名 ⇒ **假阴性**（低报召回）
     语料标题就是文件名（HA3 的 `title` 字段带 `.docx`），金集写的是纯文档名。包含判定被
     尾巴挡死后只能退到 bigram Jaccard。实测 QA-109：检索把《A37-8化学品泄露.docx》放在
     rank 1/3/4，金集写《A37-8化学品泄露应急响应》，判不匹配 → 记成 gold_rank=13。

  ② 括号连内容一起删 ⇒ **假阳性**（高报召回）
     《通知（进出厂规定）》塌缩成 '通知'，而 '通知' ⊂ 任何含"通知"的标题 ⇒ 相似度 1.0。
     三份完全无关的通知类文档都能冒充金档。本轮实跑恰好没开火，是潜伏源。

两条同源（都在 `normalize_title`），且**必须一起修**：只做①会把②暴露出来
（'通知（进出厂规定）.pdf' 剥完扩展名恰好塌成 '通知'，假阳性反而更容易触发）。

21746 对（131 金集名 × 166 观测标题）全量交叉验证：翻上 2 组皆应匹配、翻下 3 组皆为该
假匹配家族、**0 组真匹配被破坏**。
"""
from __future__ import annotations

import pytest

from eval_harness.matching import (
    MATCHER_VERSION,
    chunk_matches_expected,
    gold_doc_rank,
    normalize_title,
    title_similarity,
)


# ── ① 扩展名：修假阴性 ───────────────────────────────────────────────────────
@pytest.mark.parametrize("title,stem", [
    ("A37-8化学品泄露.docx", "a378化学品泄露"),
    ("设备清扫基准书-拉片机.xlsx", "设备清扫基准书拉片机"),
    ("电脑安装作业指导书.pdf", "电脑安装作业指导书"),
    ("说明.PPTX", "说明"),
    ("readme.md", "readme"),
])
def test_file_extension_is_stripped(title, stem):
    assert normalize_title(title) == stem


def test_qa109_the_case_that_exposed_it():
    """检索把正确文档放在 rank 1，评测此前记成 13。逐字保留真实值作锚。"""
    gold = "A37-8化学品泄露应急响应"
    hit = "A37-8化学品泄露.docx"
    assert title_similarity(gold, hit) == 1.0
    retrieved = [{"title": hit, "doc_id": "DOC_HR_20260514123022_3D016F"},
                 {"title": "A29环境应急预案.docx", "doc_id": "X"}]
    assert gold_doc_rank(retrieved, [gold], []) == 1


def test_extension_only_ever_fixed_false_negatives():
    """方向性：剥掉尾巴只会让相似度**升**，不可能把真匹配判没。"""
    for gold, title in [("工作服管理规定", "工作服管理规定5.6.docx"),
                        ("A28宿舍管理制度", "A28宿舍管理制度.docx"),
                        ("A41车辆进出管理规定", "车辆进出管理规定.docx")]:
        assert title_similarity(gold, title) >= 0.6


# ── ② 括号：修假阳性 ─────────────────────────────────────────────────────────
def test_bracket_content_is_kept_not_dropped():
    """**这条是护栏**：括号内容被删会把标题塌缩成通用词根。"""
    assert normalize_title("通知（进出厂规定）") == "通知进出厂规定"
    assert normalize_title("关于饭卡充值的通知（有图）.pdf") == "关于饭卡充值的通知有图"


@pytest.mark.parametrize("impostor", [
    "关于饭卡充值的通知.docx",
    "关于2021年秋季上下班时间调整通知.pdf",
    "关于饭卡充值的通知（有图）.pdf",
])
def test_generic_stem_no_longer_impersonates_gold(impostor):
    """金集《通知（进出厂规定）》此前能匹配任何含"通知"的标题（相似度 1.0）。"""
    assert title_similarity("通知（进出厂规定）", impostor) < 0.6
    assert not chunk_matches_expected({"title": impostor}, ["通知（进出厂规定）"], [])


def test_the_real_document_still_matches_itself():
    """杀假阳性不能顺手杀掉真匹配 —— 这份才是《通知（进出厂规定）》本尊。"""
    assert chunk_matches_expected({"title": "通知（进出厂规定）.pdf"},
                                  ["通知（进出厂规定）"], [])


@pytest.mark.parametrize("gold,title", [
    # 括号内容保留后仍应命中的真实对（全部来自实跑 top-5）
    ("关于外来人员来访留宿相关规定（最终版）", "关于外来人员来访留宿相关规定(最终版）.docx"),
    ("设备清扫基准书-拉片机", "设备清扫基准书-拉片机（淳迪） - 2拉.xlsx"),
    ("新员工住宿安排作业指导书", "002《新员工住宿安排》作业指导书.pdf"),
    ("奶茶杯测水试验作业指导书", "FL-ZS-WI-002《奶茶杯测水试验》作业指导书.docx"),
    ("A10危险源辨识", "A10危险源辨识、风险评价管理程序.docx"),
    ("离职、转岗员工及终止合作客商管理程序", "A17离职、转岗员工及终止合作客商管理程序.docx"),
])
def test_no_legitimate_match_was_broken(gold, title):
    assert title_similarity(gold, title) >= 0.6, f"真匹配被破坏：{gold} ←→ {title}"


# ── 口径版本必须可追溯 ──────────────────────────────────────────────────────
def test_matcher_version_is_in_regime_keys():
    """改**尺子**必须与"检索变好了"可区分 —— 同 l4_ingestion / l6 两条先例。"""
    from eval_harness.baseline import _LENIENT_REGIME_KEYS, _REGIME_KEYS
    assert MATCHER_VERSION
    assert "l1_matcher_version" in _REGIME_KEYS
    assert "l1_matcher_version" not in _LENIENT_REGIME_KEYS, "跨口径比较必须硬失败"


def test_doc_id_fastpath_is_unaffected():
    """doc_id 命中走快路，与标题归一无关 —— 别把它一起改坏。"""
    assert chunk_matches_expected({"title": "毫不相干.docx", "doc_id": "DOC_X"}, [], ["DOC_X"])
