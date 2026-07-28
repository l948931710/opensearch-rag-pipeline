# -*- coding: utf-8 -*-
"""chunker 静默降级检测（L6 Family D2）——B17 路由留痕的第一个真实消费点。

## 这条闸门要防的事故

2026-05 那批 **120 篇制度类文档产出 `text_chunk`**：路由**判对了**（cat=policy 或标题带
「制度/规定/规范」⇒ clause 模式），但 `_CLAUSE_RE` 旧版漏掉真实制度编号，clause 模式切不出
边界就**默默回退 text**（见 `chunker.py::_CLAUSE_RE` 注释、
`docs/audits/clause_routing_inconsistency_scope_2026-06-15.md`，当时估 ~47% 制度文档受影响）。

该缺陷**潜伏约一个月**，靠人写审计才发现 —— 因为当时没有任何指标能区分：
  (1) 路由选错模式（router 的问题）        → `routing_match_rate` 测的是这个
  (2) 路由对了但 chunker 空命中静默回退    → **此前无人测**

## 为什么本测试组存在

B17（2026-07-25）已在摄取侧记下 `route_final_mode` 等三个量，但实测**唯一的消费方是它
自己的单元测试**：生产写、测试验、闸门不看 ⇒ 对质量保障贡献为零。而且全库 27484 条活跃
chunk 里带该留痕的是 **0 条**（07-25 之后现网没再摄取过）——观测点从落地起就是 0 覆盖，
无人发现。本组测试连同 `_mode_downgrade_stats` 是把它接进闸门的那一步。
"""
from __future__ import annotations

import os as _os

_os.environ.setdefault("RAG_SIMULATE", "true")

import pytest  # noqa: E402

# ⚠️ import `eval_harness.layers.l6_chunk_quality` 会在 **import 期**触发
# `envboot.boot()`，它把全局 os.environ 改成 LIVE 姿态（RAG_SIMULATE=false + 线上端点
# + 认证开）。该副作用会泄漏给**同一 xdist worker 里后续收集的所有测试** —— 初版没做
# 快照还原，单跑本文件全绿，`make test` 却炸出 40 failed / 1415 errors（大量 401
# "需要登录"）。既有的 tests/test_l6_chunk_quality.py 早有同款注释，我没照抄。
_SAVED_ENV = dict(_os.environ)
from eval_harness.layers.l6_chunk_quality import (  # noqa: E402
    _MODE_EXPECTED_FAMILY,
    _mode_downgrade_stats,
)
_os.environ.clear()
_os.environ.update(_SAVED_ENV)


def _c(doc, ctype, mode=None):
    d = {"doc_id": doc, "chunk_type": ctype}
    if mode is not None:
        d["extra_json"] = '{"route_final_mode": "%s"}' % mode
    return d


def test_no_trace_abstains_instead_of_reporting_zero():
    """**最要紧的一条**：没有留痕时必须是 None，不能是 0.0。

    把"没有留痕所以看不见降级"报成"零降级"，正是这条指标要防的那类静默 —— 也是今天
    反复踩到的坑（把"看不见"当"通过"）。
    """
    out = _mode_downgrade_stats([_c("d1", "text_chunk"), _c("d2", "clause_chunk")])
    assert out["mode_downgrade_rate"] is None
    assert out["mode_downgrade_count"] is None
    assert out["route_trace_coverage"] == 0.0


def test_detects_the_2026_05_clause_downgrade_signature():
    """路由说 clause、实际产出 text_chunk = 降级签名（那 120 篇的形态）。"""
    out = _mode_downgrade_stats([
        _c("d1", "text_chunk", mode="clause"),      # 降级
        _c("d2", "clause_chunk", mode="clause"),    # 正常
    ])
    assert out["mode_downgrade_rate"] == 0.5
    assert out["mode_downgrade_sample"][0]["doc_id"] == "d1"
    assert out["mode_downgrade_sample"][0]["route_final_mode"] == "clause"


def test_step_mode_downgrade_is_also_caught():
    """不止 clause —— step 模式回退成 text/clause 同样是降级（B1-3 那条升级路径的反面）。"""
    out = _mode_downgrade_stats([_c("d1", "text_chunk", mode="step")])
    assert out["mode_downgrade_rate"] == 1.0


@pytest.mark.parametrize("mode,ctype", [
    ("clause", "clause_chunk"), ("faq", "faq_chunk"),
    ("step", "step_card"), ("step", "procedure_parent"),
    ("text", "text_chunk"), ("text", "table_chunk"), ("slide", "section_chunk"),
])
def test_legitimate_mode_family_pairs_are_not_flagged(mode, ctype):
    """正常配对一律不得误报——误报会让这条闸门很快被当噪声忽略。"""
    out = _mode_downgrade_stats([_c("d1", ctype, mode=mode)])
    assert out["mode_downgrade_rate"] == 0.0, f"{mode} → {ctype} 被误判为降级"


def test_doc_with_any_expected_type_counts_as_ok():
    """同一文档产出多种类型时，只要**命中**期望家族之一就算正常（与 family_routing 同语义）。"""
    out = _mode_downgrade_stats([
        _c("d1", "text_chunk", mode="step"), _c("d1", "step_card", mode="step"),
    ])
    assert out["mode_downgrade_rate"] == 0.0


def test_unknown_mode_abstains():
    """未知模式不猜——记进覆盖率但不判降级（避免新增模式时闸门自动变红）。"""
    out = _mode_downgrade_stats([_c("d1", "text_chunk", mode="brand_new_mode")])
    assert out["n_docs_with_route_trace"] == 1
    assert out["mode_downgrade_rate"] == 0.0


def test_malformed_extra_json_is_treated_as_no_trace():
    """坏 JSON 不得炸，也不得被当成有留痕（fail-open 到弃权）。"""
    out = _mode_downgrade_stats([{"doc_id": "d1", "chunk_type": "text_chunk",
                                  "extra_json": "{不是合法JSON"}])
    assert out["mode_downgrade_rate"] is None


def test_b17_telemetry_has_a_real_consumer_now():
    """B17 留痕必须被**闸门**消费，而不只是被它自己的单测验证。

    此前 route_final_mode 的唯一引用方是 tests/test_ingest_b_b4_b5_b17.py —— 生产写、
    测试验、闸门不看。这条断言钉住"已接入 L6"。
    """
    import ast
    import inspect

    from eval_harness.layers import l6_chunk_quality as L
    src = inspect.getsource(L)
    assert "route_final_mode" in src, "L6 未消费 B17 留痕"
    # ⚠️ 初版写成 `inspect.getsource(L.build_gates_l6 if hasattr(...) else L)` —— 函数名
    # 猜错就退化成扫整个模块，等于空断言。改成**定位真正写 gates 的那个函数**再断言。
    tree = ast.parse(src)
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
           and any(isinstance(x, ast.Subscript)
                   and getattr(getattr(x, "value", None), "id", None) == "gates"
                   for x in ast.walk(n))]
    assert fns, "找不到写 gates 的函数"
    assert any("mode_downgrade_rate" in ast.get_source_segment(src, f) or ""
               for f in fns), "降级率未进闸门"


def test_expected_family_map_covers_every_router_mode():
    """路由模式与期望家族的映射必须完整——漏一个模式会让该模式的文档永远弃权。"""
    for mode in ("faq", "clause", "step", "text", "slide"):
        assert mode in _MODE_EXPECTED_FAMILY and _MODE_EXPECTED_FAMILY[mode]
