# -*- coding: utf-8 -*-
"""B6（生产级外审 2026-07-17 P2-15/P2-12）：观测/告警骨架。

核查实测的失效形态回归：①launchd 监控以 PROD-RO 跑、心跳写被只读守卫挡死 → 读侧
恒 None → 旧 `is not None and >26` 永不触发（死人开关名存实亡）——期望心跳声明下
None 必须判死；②webhook 未配 → critical 全部 SUPPRESSED 只活在日志——压制计数
必须上浮到 governance/posture。（posture 自报测试在分支 readiness 层——main 无 readiness，
本文件只保留 kb_console/alerting 两段。）
"""
import pytest


class TestMonitorDead:
    """_monitor_dead 纯函数四象限（kb_console 死人开关判定锚点）。"""

    @pytest.mark.parametrize("age_h,expected,dead", [
        (30.0, False, True),    # 有心跳且超时 → 死（原语义，不需声明）
        (30.0, True, True),
        (2.0, True, False),     # 新鲜心跳 → 活
        (2.0, False, False),
        (None, True, True),     # 期望有而完全缺席 → 死（B6 新语义：核查实测形态）
        (None, False, False),   # 未声明期望 → 未知不红（本地/staging 零误伤）
    ])
    def test_quadrants(self, age_h, expected, dead):
        from opensearch_pipeline.routes.kb_console import _monitor_dead
        assert _monitor_dead(age_h, expected) is dead

    def test_expected_flag_env(self, monkeypatch):
        from opensearch_pipeline.routes.kb_console import _heartbeat_expected
        monkeypatch.delenv("RAG_OPS_HEARTBEAT_EXPECTED", raising=False)
        assert _heartbeat_expected() is False
        monkeypatch.setenv("RAG_OPS_HEARTBEAT_EXPECTED", "true")
        assert _heartbeat_expected() is True


class TestSuppressedStats:
    """alerting 压制计数：webhook 未配 / 域被拒 两条压制路径都要入账。"""

    def _reset(self):
        from opensearch_pipeline import alerting
        alerting._SUPPRESSED.update({"count": 0, "last_title": "", "last_ts": 0.0})
        alerting._LAST_SENT.clear()

    def test_unset_webhook_counts(self, monkeypatch):
        from opensearch_pipeline import alerting
        self._reset()
        monkeypatch.delenv("RAG_OPS_ALERT_WEBHOOK", raising=False)
        monkeypatch.delenv("RAG_OPS_ALERT_SECRET", raising=False)
        assert alerting.send_ops_alert("压制样本A", "x", severity="critical") is False
        s = alerting.suppressed_stats()
        assert s["count"] == 1 and s["last_title"] == "压制样本A" and s["last_ts"] > 0

    def test_disallowed_domain_counts(self, monkeypatch):
        from opensearch_pipeline import alerting
        self._reset()
        monkeypatch.setenv("RAG_OPS_ALERT_WEBHOOK", "https://evil.example.com/hook")
        monkeypatch.delenv("RAG_OPS_ALERT_WEBHOOK_ALLOW", raising=False)
        assert alerting.send_ops_alert("压制样本B", "x") is False
        assert alerting.suppressed_stats()["count"] == 1

    def test_stats_returns_snapshot_copy(self, monkeypatch):
        from opensearch_pipeline import alerting
        self._reset()
        snap = alerting.suppressed_stats()
        snap["count"] = 999
        assert alerting.suppressed_stats()["count"] == 0   # 外部改快照不污染内部
