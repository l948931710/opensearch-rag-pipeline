

# ── 2026-08-06：比率型 SLO 的最小样本量门槛 ─────────────────────────────────

def _t(**over):
    from opensearch_pipeline.qa_rollup import _slo_thresholds
    t = _slo_thresholds()
    t.update(over)
    return t


def test_low_sample_ratio_breach_is_downgraded_not_paged():
    """n=5 的 0.6 应答率不是服务劣化，是样本噪声 —— 不得单独 page。

    现网实测日均 ~7 条查询，07-27(n=5, ar=0.600) / 08-04(n=3) 这类历史 breach
    把 `com.fuling.qa-rollup` 的 exit=2 变成了日常。
    """
    from opensearch_pipeline.qa_rollup import evaluate_slos
    v = evaluate_slos({"total_queries": 5, "answer_rate": 0.6}, _t())
    assert v["ok"] is True, f"低样本边缘比率不该 page：{v['breaches']}"
    assert [b["slo"] for b in v["low_confidence"]] == ["answer_rate_min"]
    assert v["low_confidence"][0]["sample"] == 5


def test_low_confidence_is_never_silently_dropped():
    """★ 反证锚：降级 ≠ 丢弃。低置信项必须仍在结构里（看板/周报/告警正文都消费它）。"""
    from opensearch_pipeline.qa_rollup import evaluate_slos
    v = evaluate_slos({"total_queries": 4, "answer_rate": 0.1, "no_result_rate": 0.9,
                       "error_rate": 0.5}, _t())
    got = sorted(b["slo"] for b in v["low_confidence"])
    # 三条都在：ar=0.1 虽不算"全灭"（≠0.0），但同样击穿了阈值 ⇒ 必须**降级保留**而不是丢弃。
    assert got == ["answer_rate_min", "error_rate_max", "no_result_rate_max"], got
    assert v["ok"] is True, "低置信项不得让 slo_ok 翻红"
    assert all("样本量" in b["note"] for b in v["low_confidence"])


def test_total_failure_still_pages_even_at_low_sample():
    """🔴 **全军覆没不是噪声**：无结果率恰好 1.0 是定性信号（一条都没成），
    低样本下仍必须 page —— 08-04 那天 3 条查询全 no-result 就是这一档。"""
    from opensearch_pipeline.qa_rollup import evaluate_slos
    v = evaluate_slos({"total_queries": 3, "no_result_rate": 1.0, "answer_rate": 0.0}, _t())
    assert v["ok"] is False
    slos = sorted(b["slo"] for b in v["breaches"])
    assert slos == ["answer_rate_min", "no_result_rate_max"], slos
    assert all("全军覆没" in b.get("note", "") for b in v["breaches"])


def test_total_failure_below_min_n_is_still_downgraded():
    """n=1 连"全军覆没"都算不上（一条查询没结果太常见）——门槛 total_failure_min_n=3。"""
    from opensearch_pipeline.qa_rollup import evaluate_slos
    v = evaluate_slos({"total_queries": 1, "no_result_rate": 1.0}, _t())
    assert v["ok"] is True and len(v["low_confidence"]) == 1


def test_high_sample_ratio_breach_still_pages():
    """★ 反向锚：样本量够时行为**逐字不变**（别把门槛做成全局静音）。"""
    from opensearch_pipeline.qa_rollup import evaluate_slos
    v = evaluate_slos({"total_queries": 500, "answer_rate": 0.6}, _t())
    assert v["ok"] is False
    assert [b["slo"] for b in v["breaches"]] == ["answer_rate_min"]
    assert not v["low_confidence"]


def test_count_and_traffic_slos_are_not_sample_gated():
    """计数型/流量型不受样本量门槛约束：熔断拒绝与零流量本身就是低样本事件。"""
    from opensearch_pipeline.qa_rollup import evaluate_slos
    v = evaluate_slos({"total_queries": 0, "rejected_global_cap": 1,
                       "traffic_median_ref": 50}, _t())
    slos = sorted(b["slo"] for b in v["breaches"])
    assert slos == ["global_cap_rejected", "zero_traffic"], slos


def test_missing_total_queries_is_unknown_not_zero():
    """★ 反证锚（既有单测 test_evaluate_slos_breach_and_clean 抓出的设计缺陷）：
    `total_queries` **缺失 = unknown**，不得当 0 —— 否则"没给这个字段"就成了全局静音开关。
    """
    from opensearch_pipeline.qa_rollup import evaluate_slos
    v = evaluate_slos({"answer_rate": 0.5}, _t())          # 无 total_queries
    assert v["ok"] is False and [b["slo"] for b in v["breaches"]] == ["answer_rate_min"]
    assert not v["low_confidence"]
