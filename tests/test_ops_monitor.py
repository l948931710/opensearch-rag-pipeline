# -*- coding: utf-8 -*-
"""tests/test_ops_monitor.py — Phase-3 single-entry health-job runner."""
from opensearch_pipeline import ops_monitor


def test_run_all_sequences_selected_jobs(monkeypatch):
    calls = []
    import opensearch_pipeline.reconcile as rec
    import opensearch_pipeline.qa_rollup as qr
    monkeypatch.setattr(rec, "run_parity_check", lambda **k: calls.append("ha3") or {"ok": True})
    monkeypatch.setattr(rec, "run_oss_parity_check", lambda **k: calls.append("oss") or {"ok": True})
    monkeypatch.setattr(rec, "run_raw_parity_check", lambda **k: calls.append("raw") or {"ok": True})
    monkeypatch.setattr(qr, "run_rollup", lambda **k: calls.append("rollup") or {"ok": True, "slo_ok": 1})
    out = ops_monitor.run_all(alert=False)
    # P2-34/P2-15 起作业集含 queue_aging/ingest_funnel（模拟态 no-op skipped）
    # B10+B11（2026-07-25）：作业集增 raw_inventory（raw/ 四桶只读盘点；未接调度，
    # 现网节点仍只跑 --only reconcile_ha3 reconcile_oss）
    # C3′ G10（2026-08-07）：作业集增 acl_projection（ACL 投影收敛度，纯 SQL 判据；
    # reconcile 的进程内计数器本机读不到 —— 见 queue_monitor.run_acl_projection_check）。
    # 同样未接调度。
    assert set(out) == {"reconcile_ha3", "reconcile_oss", "reconcile_raw", "qa_rollup",
                        "queue_aging", "ingest_funnel", "raw_inventory", "acl_projection"}
    assert calls == ["ha3", "oss", "raw", "rollup"]


def test_run_all_only_subset(monkeypatch):
    import opensearch_pipeline.reconcile as rec
    monkeypatch.setattr(rec, "run_parity_check", lambda **k: {"ok": True})
    out = ops_monitor.run_all(only=["reconcile_ha3"])
    assert set(out) == {"reconcile_ha3"}


def test_job_exit_codes():
    assert ops_monitor._job_exit("reconcile_ha3", {"skipped": "simulate"}) == 0
    assert ops_monitor._job_exit("reconcile_ha3", {"ok": True, "complete": True}) == 0
    assert ops_monitor._job_exit("reconcile_ha3", {"ok": False, "complete": True}) == 2
    assert ops_monitor._job_exit("reconcile_ha3", {"error": "x"}) == 3
    assert ops_monitor._job_exit("reconcile_ha3", {"complete": False}) == 3
    assert ops_monitor._job_exit("qa_rollup", {"ok": True, "slo_ok": 1}) == 0
    assert ops_monitor._job_exit("qa_rollup", {"ok": True, "slo_ok": 0}) == 2


def test_job_exit_fetch_verdict_contract():
    """终局裁决（2026-07-21）与退出码的契约：纯枚举盲区报告 ok=True → 绿灯 0；
    fetch 证实真丢失 / 无法定性 → ok=False → 2；扫描不完整恒 3（先于 ok 判定）。"""
    blind = {"ok": True, "complete": True, "verdict_basis": "fetch",
             "counts": {"rds_active_missing": 113, "query_invisible": 113,
                        "missing_confirmed": 0, "missing_unclassified": 0,
                        "vanished_at_risk": 0}}
    assert ops_monitor._job_exit("reconcile_ha3", blind) == 0
    confirmed = {"ok": False, "complete": True, "verdict_basis": "fetch",
                 "counts": {"missing_confirmed": 1}}
    assert ops_monitor._job_exit("reconcile_ha3", confirmed) == 2
    fetch_down = {"ok": False, "complete": True, "verdict_basis": "query_only",
                  "counts": {"rds_active_missing": 5}}
    assert ops_monitor._job_exit("reconcile_ha3", fetch_down) == 2
    truncated = {"ok": True, "complete": False, "verdict_basis": "fetch", "counts": {}}
    assert ops_monitor._job_exit("reconcile_ha3", truncated) == 3


def test_main_worst_exit_code(monkeypatch):
    monkeypatch.setattr(ops_monitor, "run_all", lambda **k: {
        "reconcile_ha3": {"ok": True, "complete": True},          # 0
        "reconcile_oss": {"ok": False, "complete": True},         # 2
        "qa_rollup": {"ok": True, "slo_ok": 1},                   # 0
    })
    assert ops_monitor.main(["--no-alert"]) == 2


def test_main_simulate_all_skipped(monkeypatch):
    # under simulate each real sub-job no-ops → exit 0
    monkeypatch.setattr(ops_monitor, "run_all", lambda **k: {
        "reconcile_ha3": {"skipped": "simulate"},
        "qa_rollup": {"skipped": "simulate"},
    })
    assert ops_monitor.main([]) == 0


# ── 2026-08-07：ops-monitor 的心跳必须由它自己盖章（agent 两段式）─────────────────

def _ops_monitor_plist_args():
    """读并解析 ops-monitor 的 LaunchAgent plist。

    ⚠️ 解析失败必须自解释（2026-08-07）：畸形 XML 原样抛出来只有一句
    `ExpatError: not well-formed (invalid token): line N, column M` —— 不说是哪个文件，
    更不说踩的是哪条规则。本文件实测踩过一次：带 `--check raw` 的命令写进了 XML 注释里，
    而 **XML 注释内不允许出现连续两个连字符**。`plutil -lint` 对此宽容、照样报 OK，
    plistlib / 任何严格解析器却拒收 —— 于是这份 plist 畸形了数周无人发现。
    把成因和处置写进失败信息里，下次照着改就行，不必再当一次考古。
    """
    import pathlib
    import plistlib
    import xml.parsers.expat

    import pytest

    p = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "com.fuling.ops-monitor.plist"
    raw = p.read_bytes()
    try:
        return plistlib.loads(raw)
    except xml.parsers.expat.ExpatError as e:
        lines = raw.decode("utf-8", "replace").splitlines()
        bad = lines[e.lineno - 1].strip() if 0 < e.lineno <= len(lines) else "（行号超出范围）"
        pytest.fail(
            f"{p} 不是合法 XML，plistlib 拒绝解析：{e}\n"
            f"  出错行 {e.lineno}: {bad}\n"
            "最常见成因：XML 注释里出现了连续两个连字符（`--`），典型是把带 `--flag` 的命令"
            "抄进了注释。⚠️ `plutil -lint` 不会报这个，launchd 侧同样会拒载。\n"
            "处置：把该命令挪进一个惰性 plist 键（本文件用的是 `_AdHocReconcileRaw`，"
            "launchd 忽略不认识的键），注释里只留指向该键的说明。",
            pytrace=False)


def test_ops_monitor_agent_stamps_its_own_heartbeat():
    """★ 死人开关必须证明 **ops-monitor 自己**活着，不是"某个调度器活着"。

    实测过的失效形态（2026-08-07）：该 agent 跑在 `RAG_ENV=prod_ro`
    （`.env.prod_ro` 设 `RAG_READONLY=true`）⇒ `run_all` 收尾的 `write_heartbeat()`
    **每次都被 ENV GUARD 挡掉、从未成功过**；看板读到的新鲜心跳其实来自
    `com.fuling.qa-rollup`（RAG_ENV=metrics）。于是"只有 ops-monitor 这一个 agent 坏了"
    这种情形**测不出来**——而 ops-monitor 正是漂移探测器本身。

    正解不是给 RAG_READONLY 开洞（那是刻意的安全属性），而是 agent 层两段式：
    对账器继续 prod_ro，心跳作为第二条命令用可写 env 单独盖章。本用例钉死这个形态。
    """
    pl = _ops_monitor_plist_args()
    args = pl["ProgramArguments"]
    assert args[:2] == ["/bin/sh", "-c"], (
        f"不再是两段式 shell 包装 ⇒ 心跳又回到 run_all 里、在 prod_ro 下必然写不进：{args}")
    cmd = args[2]
    assert "write_heartbeat" in cmd, "第二段没有独立的心跳盖章命令"
    assert "RAG_ENV=metrics" in cmd, (
        "心跳段没有覆写成可写 env —— 会继承 agent 的 prod_ro 而被 ENV GUARD 挡掉")
    # 对账器本身**必须**留在 prod_ro：只读保护不能为了写心跳而拆
    assert pl["EnvironmentVariables"]["RAG_ENV"] == "prod_ro", (
        "agent 的 RAG_ENV 被改离 prod_ro —— 三个对账器失去只读保护网，代价远大于收益")
    # 退出码必须仍来自对账器，否则 launchd 的红绿判据被心跳段顶替
    assert "exit $rc" in cmd, "退出码没有透传对账器结果"
    assert cmd.index("ops_monitor") < cmd.index("write_heartbeat"), (
        "心跳排在对账器之前 ⇒ 盖的章不代表'跑完了'")


def test_write_heartbeat_is_quiet_on_declared_readonly_session(monkeypatch, caplog):
    """★ 只读会话下不得再喊狼来了。

    改动前：prod_ro 会话每轮打一条 WARNING「心跳写入失败」，读起来像"监控链断了"，
    实际是"本作业按设计不可写"。**同一处文案两个月内把排查带偏两次**
    （先只写 schema/018、后加 1142，都不是真正命中的那个原因）。
    """
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline import queue_monitor
    cfg = get_config()
    monkeypatch.setattr(cfg, "simulate", False)
    monkeypatch.setattr(cfg, "simulate_db", False)
    monkeypatch.setattr(cfg, "readonly", True, raising=False)

    def _boom(*a, **k):
        raise AssertionError("只读会话下不应该去连库尝试写")

    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _boom)
    with caplog.at_level("INFO"):
        assert queue_monitor.write_heartbeat() is False
    # 🔴 必须按 **logger 名**收窄，不能只按等级过滤（2026-08-07）：`caplog` 装在 root 上，
    # 捕获的是当前进程**所有线程**的记录。本仓有真实的游离 ERROR 源 ——
    # `rate_limiter._dispatch_cap_alert` / `serving_pools` 默认 `_DISPATCH_ASYNC=True`，
    # 从**守护线程**调 `send_ops_alert`，而 webhook 未配 + severity=critical 会打
    # `logger.error`（alerting.py:105）。实测那条记录能落在**产生它的用例 PASSED 之后**
    # （pytest 把它归进 sessionfinish 阶段）⇒ 全量跑时会飘进当时正在跑的任意用例的 caplog
    # 窗口，本用例就间歇性红、而单模块跑必绿 —— 典型的"假红"，会把排查引向 xdist。
    # 本用例要证的是「`write_heartbeat` **自己**没喊狼来了」，别的 logger 与它无关。
    mine = [r for r in caplog.records if r.name == queue_monitor.__name__]
    msgs = [r.getMessage() for r in mine]
    assert any("只读" in m for m in msgs), f"没有解释为什么跳过：{msgs}"
    assert not [r for r in mine if r.levelname in ("WARNING", "ERROR")], (
        f"按设计不可写却打了 WARNING/ERROR（误导排查）：{msgs}")
