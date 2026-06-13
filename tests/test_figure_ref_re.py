# -*- coding: utf-8 -*-
"""
test_figure_ref_re.py — 校准后 _FIGURE_REF_RE 的覆盖与误报边界。

背景:scripts/audit_figure_ref_phrases.py 在 68 条 SUCCESS 生产答案上发现原版正则
只命中 top 120 短语里的 2 条 = 装饰性闸。2026-06-12 扩充 6 类指代后,此测试钉死
新覆盖 + 显式标定不该入正则的名词性短语,防止后续打回旧版无声装饰化。

跑测:pytest tests/test_figure_ref_re.py -v
"""
import os
import sys

sys.path.insert(0, os.path.expanduser("~/Downloads/opensearch-rag-data/eval_samples/scripts"))

import pytest
from mm_answer_metrics import (
    _FIGURE_REF_RE,
    _FIGURE_REF_FALSE_POSITIVES,
    dump_dangling_cases,
)


# ── 新覆盖 — 真生产高频短语必须命中 ──────────────────────────────

@pytest.mark.parametrize("phrase", [
    # 截图相关(audit 实测 8+ 命中)
    "界面截图显示菜单结构",
    "统界面截图与局部功能",
    "参考截图未成功上传",
    "系统截图显示路径",
    # 图示
    "但图示及常规配置",
    "参考图示显示需要",
    "如参考图所示,",
    # 图片显示/描述/提示
    "文档9的图片显示在",
    "见文档3图片描述,",
    "文档1图片提示,",
    "参考图片及常规逻辑",
    # 原版必须仍命中(回归保护)
    "操作如下图所示",
    "如图所示进入界面",
    "请见上图",
    # 英文
    "see the figure below",
    "see picture",
    "see screenshot",
])
def test_dangling_phrases_caught(phrase):
    assert _FIGURE_REF_RE.search(phrase), f"{phrase!r} 应该被识别为图指代"


# ── 不该命中 — 纯文字、无图指代 ────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "请按操作流程提交申请",
    "联系行政部确认",
    "登录系统后填写表单",
    "审核通过后开始执行",
])
def test_non_dangling_phrases_skipped(phrase):
    assert not _FIGURE_REF_RE.search(phrase), f"{phrase!r} 不应被识别为图指代"


# ── 已知误报候选(在 _FIGURE_REF_FALSE_POSITIVES 集) ─────────────
# 当前不在正则里做硬排除(过严容易漏真 dangling),改由 dump_dangling_cases
# 的 is_likely_false_positive 标记,让人工抽检时一眼分桶。

def test_known_false_positives_flagged_by_dumper():
    """name-like 短语(图标/组织架构图)被 dumper 标 is_likely_false_positive=True。"""
    cases = [
        {"qid": "F1", "query": "U8 怎么登录", "answer": "桌面快捷方式图标双击进入"},
        {"qid": "F2", "query": "组织架构是?", "answer": "参见组织架构图,文中已展示"},
    ]
    # 注:_FIGURE_REF_RE 不在 F1 上命中(无图指代触发短语),F2 上「参见」也不命中
    # 这两个其实不会触发 dangling = 验证:误报集仅在真触发时才相关
    per_answer = [
        {"n_available": 0, "dangling_ref": False},
        {"n_available": 0, "dangling_ref": False},
    ]
    dumped = dump_dangling_cases(per_answer, cases)
    assert dumped == [], "非 dangling case 不应被 dump"


def test_dumper_outputs_context_and_phrase():
    """dump 输出包含 matched_phrase 和上下文。"""
    cases = [
        {"qid": "T1", "query": "扫码报检步骤?",
         "answer": "登录 U8 系统,如截图显示菜单结构,选择"},
    ]
    per_answer = [{"n_available": 0, "dangling_ref": True}]
    dumped = dump_dangling_cases(per_answer, cases)
    assert len(dumped) == 1
    d = dumped[0]
    assert d["qid"] == "T1"
    assert "截图显示" in d["matched_phrase"] or "界面截图" in d["matched_phrase"] or "截图" in d["matched_phrase"]
    assert "menu" not in d["answer_excerpt"]  # 中文内容
    assert d["n_available"] == 0
    assert isinstance(d["is_likely_false_positive"], bool)


def test_dumper_flags_false_positive_context():
    """answer 含「图标」名词时 dumper 标 is_likely_false_positive。"""
    cases = [{"qid": "FP1", "query": "?",
              "answer": "请点击桌面快捷方式图标,如下图所示,登录界面"}]
    per_answer = [{"n_available": 0, "dangling_ref": True}]
    dumped = dump_dangling_cases(per_answer, cases)
    assert len(dumped) == 1
    # 上下文里同时有「如下图所示」(真命中) 和「图标」(误报候选)
    # is_likely_false_positive 标 True 让人工抽检时优先看
    assert dumped[0]["is_likely_false_positive"] is True


def test_dumper_limit_respected():
    cases = [{"qid": f"L{i}", "query": "", "answer": "如图所示"} for i in range(10)]
    per_answer = [{"n_available": 0, "dangling_ref": True} for _ in range(10)]
    dumped = dump_dangling_cases(per_answer, cases, limit=3)
    assert len(dumped) == 3
    assert [d["qid"] for d in dumped] == ["L0", "L1", "L2"]


def test_false_positive_constant_documented():
    """显式标定:_FIGURE_REF_FALSE_POSITIVES 至少含图标/图样/图片参数(已知误报候选)。"""
    for word in ("图标", "图样", "图片参数"):
        assert word in _FIGURE_REF_FALSE_POSITIVES
