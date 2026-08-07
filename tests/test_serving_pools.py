# -*- coding: utf-8 -*-
"""D4-②~⑥：serving 子线程池的拓扑、生命周期与饱和行为（2026-08-06）。

背景：`retriever.py` 此前四处**按请求新建** `ThreadPoolExecutor`，融合三臂那处每查询必起，
AnyIO 令牌 120 ⇒ 理论 360 个短生命周期子线程、无总量闸、无指标。
D3 第一步只做**旋钮 + 指标 + 上限**，默认值刻意维持现状（准入闸默认关）。

本文件钉住的是那些"改坏了不会有别的测试红"的性质。
"""
import threading

import pytest

from opensearch_pipeline import serving_pools as sp


@pytest.fixture(autouse=True)
def _clean():
    sp._reset_for_tests()
    yield
    sp._reset_for_tests()


# ── ② 不得再有 per-request 池 ───────────────────────────────────────────────

def test_retriever_has_no_per_request_thread_pool():
    """`ThreadPoolExecutor(` 只准出现在 `serving_pools.py`。

    per-request 池是本批要消灭的形态；谁再加一个，没有别的测试会红。
    """
    import ast
    import pathlib
    # 同下条：用 AST，注释里可以自由引用 `ThreadPoolExecutor(` 而不误伤。
    tree = ast.parse(pathlib.Path("opensearch_pipeline/retriever.py").read_text(encoding="utf-8"))
    hits = [n.lineno for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", getattr(n.func, "attr", None)) == "ThreadPoolExecutor"]
    assert not hits, (
        f"retriever.py:{hits} 又出现了 per-request 线程池 —— 请改用 serving_pools.submit_or_none()")


def test_retriever_never_shuts_down_a_shared_pool():
    """请求路径上 `.shutdown(` 一次都不许有。

    对**共享**池调用 shutdown 会把整个进程的池关掉。原先 `_px`/`_cx` 那两处
    `shutdown(wait=False)` 在私有池上是正确写法，换共享池后必须删（codex BLOCKER 3）。
    """
    import ast
    import pathlib
    # ⚠️ 用 AST 不用字符串搜：注释里**必须**能引用 `_px.shutdown(wait=False)` 解释为什么删掉它，
    # 而词法守卫会被自己的解释性注释绊倒（第一版就是这么红的）。注释天然不进 AST。
    tree = ast.parse(pathlib.Path("opensearch_pipeline/retriever.py").read_text(encoding="utf-8"))
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "shutdown"]
    assert not bad, (
        f"retriever.py:{bad} 调用了 shutdown —— 共享池的关闭点只有 api.py 的 lifespan 退出分支")


# ── ④ 拓扑必须无环：三个池，且互不相同 ──────────────────────────────────────

def test_pools_are_three_distinct_executors():
    """fusion / prefetch / fanout 必须是**三个不同**的 executor。

    合并成一个就死锁：fanout 任务会等 prefetch 的 primary future（`_one(0)` →
    `primary_supplier()`）和 fusion 的臂；同池时它们抢不到 worker
    （codex 2026-08-06 抓出的 P→P 自等待）。
    """
    pools = {k: sp.get_pool(k) for k in (sp.FUSION, sp.PREFETCH, sp.FANOUT)}
    assert len({id(p) for p in pools.values()}) == 3, f"池被合并了：{pools}"


def test_fanout_task_can_wait_on_prefetch_future_without_deadlock():
    """行为层反证：fanout 任务里等 prefetch 的 future 必须能完成。

    把两者放同一个 max_workers=1 的池就会挂死；分池才不会。
    """
    import os
    os.environ["RAG_POOL_PREFETCH_WORKERS"] = "1"
    os.environ["RAG_POOL_FANOUT_WORKERS"] = "1"
    try:
        sp._reset_for_tests()
        pre = sp.submit(sp.PREFETCH, lambda: "primary")
        out = sp.submit(sp.FANOUT, lambda: pre.result(timeout=5) + "+fanout")
        assert out.result(timeout=5) == "primary+fanout"
    finally:
        os.environ.pop("RAG_POOL_PREFETCH_WORKERS", None)
        os.environ.pop("RAG_POOL_FANOUT_WORKERS", None)


# ── ⑤ 饱和行为：默认不触发；开了闸也只降级不炸 ────────────────────────────

def test_admission_is_off_by_default_so_behaviour_is_unchanged():
    """🔴 默认值维持现状（Sam 2026-08-06 拍板）：准入闸默认关 ⇒ 永不拒绝。

    开它之前需要 staging 的配额/P95/降级率数据；拍脑袋定值等于把限流做成事故源。
    """
    import os
    os.environ.pop("RAG_POOL_ADMISSION", None)
    assert sp._admission_on() is False
    futs = [sp.submit(sp.FUSION, lambda: 1) for _ in range(50)]
    assert [f.result(timeout=5) for f in futs] == [1] * 50
    assert sp.pool_stats()[sp.FUSION]["rejected"] == 0


def test_saturation_returns_none_not_exception():
    """`submit_or_none` 饱和回 `None`，让调用方走自己既有的同步/降级路径。

    ⚠️ 不能靠抛异常：`_client_fusion_search` 的 fail-open 只包 `fut.result()`、
    **不包提交**，异常会直接崩掉整条查询（codex BLOCKER 2）。
    """
    import os
    os.environ["RAG_POOL_ADMISSION"] = "true"
    os.environ["RAG_POOL_FUSION_CAP"] = "1"
    os.environ["RAG_POOL_FUSION_WORKERS"] = "1"
    gate = threading.Event()
    try:
        sp._reset_for_tests()
        held = sp.submit(sp.FUSION, gate.wait)         # 占满
        assert sp.submit_or_none(sp.FUSION, lambda: 2) is None
        assert sp.pool_stats()[sp.FUSION]["rejected"] == 1
        gate.set()
        held.result(timeout=5)
    finally:
        gate.set()
        for k in ("RAG_POOL_ADMISSION", "RAG_POOL_FUSION_CAP", "RAG_POOL_FUSION_WORKERS"):
            os.environ.pop(k, None)


def test_permits_are_returned_on_success_failure_and_exception():
    """permit 生命周期：成功 / 任务抛异常 / 提交失败，都必须把 queued 退回 0。

    泄漏一次，"有界"就永久缩小一格；泄漏够多次就变成恒拒绝（codex BLOCKER 5）。
    """
    sp.submit(sp.FUSION, lambda: 1).result(timeout=5)
    f = sp.submit(sp.FUSION, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        f.result(timeout=5)
    st = sp.pool_stats()[sp.FUSION]
    assert st["queued"] == 0 and st["inflight"] == 0, f"permit 泄漏：{st}"


# ── ⑥ TLS 清理：worker 跨请求复用，残留 scope 会泄漏连接 ────────────────────

def test_task_wrapper_clears_and_closes_leaked_conn_scope():
    """任务结束必须**关掉**残留的 `_conn_scope.conn`，不只是置空引用。

    长活 worker 跨请求复用；残留的 conn 会被下一个任务当成"共享连接"复用，
    而它可能早已 close 或永不归还（codex 2026-08-06 点名）。
    """
    from opensearch_pipeline import retriever as R

    closed = []

    class _FakeConn:
        def close(self):
            closed.append(1)

    def _leaky():
        R._conn_scope.active = True
        R._conn_scope.conn = _FakeConn()
        return threading.current_thread().name

    tname = sp.submit(sp.FUSION, _leaky).result(timeout=5)
    assert closed == [1], "残留连接没有被关闭"

    def _observe():
        return (getattr(R._conn_scope, "active", False),
                getattr(R._conn_scope, "conn", None),
                threading.current_thread().name)
    # max_workers=1 的池里下一任务必然复用同一线程，才能观察到残留
    import os
    os.environ["RAG_POOL_FANOUT_WORKERS"] = "1"
    try:
        sp._reset_for_tests()
        sp.submit(sp.FANOUT, _leaky).result(timeout=5)
        active, conn, t2 = sp.submit(sp.FANOUT, _observe).result(timeout=5)
        assert active is False and conn is None, f"下一任务看到了残留 scope：active={active} conn={conn}"
        assert t2, f"（同线程复用校验用：{tname} / {t2}）"
    finally:
        os.environ.pop("RAG_POOL_FANOUT_WORKERS", None)


def test_context_vars_propagate_to_pool_workers():
    """长活 worker **不自动继承** ContextVar；必须每次 submit `copy_context()`。

    否则 request_id 在子线程里恒为 '-'，整条 trace 断掉（`request_context.py:41` 自陈该坑）。
    """
    from opensearch_pipeline.request_context import get_request_id, set_request_id
    set_request_id("cafebabe")
    got = sp.submit(sp.FUSION, get_request_id).result(timeout=5)
    assert got == "cafebabe", f"ContextVar 没传到 worker：{got!r}"


def test_pool_stats_exposes_what_monitoring_needs():
    """`rejected` 必须可观测 —— 否则饱和降级就是静默的（codex REMAINING）。"""
    st = sp.pool_stats()
    assert set(st) == {sp.FUSION, sp.PREFETCH, sp.FANOUT}
    for k, v in st.items():
        assert {"submitted", "inflight", "queued", "rejected", "completed",
                "max_workers", "admission", "alive"} <= set(v), f"{k} 指标不全：{v}"


def test_fusion_submit_side_uses_submit_or_none_not_submit():
    """🔴 codex BLOCKER 2 的锚：`_client_fusion_search` 的提交侧必须是 `submit_or_none`。

    换回 `submit` ⇒ 饱和时 `PoolSaturated` 会绕过那个**只包 `fut.result()`、不包提交**的
    except，直接崩掉整条查询而不是走单臂降级。

    ⚠️ **覆盖边界，如实说**：这是**静态**锚。饱和时 `submit_or_none` 回 None 的行为由
    `test_saturation_returns_none_not_exception` 覆盖；但「三臂全被拒 ⇒ 整条查询仍返回
    降级结果」的**端到端**行为测试**没写** —— 直驱 `_client_fusion_search` 需要构造
    HA3 client/cfg/SparseData 一整套，而真实饱和又要占满池、易 flaky。
    我先写过一版，写成了 `... if False else None` 的**空转假测试**，已删。
    这条缺口记在 `docs/main_code_review_verification_2026-08-06.md` 批 D 节。
    """
    import ast
    import pathlib
    tree = ast.parse(pathlib.Path("opensearch_pipeline/retriever.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_client_fusion_search")
    names = {n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "submit_or_none" in names, "融合三臂的提交侧必须用 submit_or_none（饱和回 None 走降级）"
    assert "submit" not in names, "不得用会抛 PoolSaturated 的 submit —— 那个 except 不包提交"


# ── ⑦ 饱和必须自己喊出来（告警）────────────────────────────────────────────

def test_saturation_fires_an_ops_alert(monkeypatch):
    """🔴 饱和降级**必须告警**，因为它的表征是"延迟变好"。

    离线扫描实测：60 并发 / cap=24 时 89% 的臂被拒、多数查询拿到零臂回落服务端混合，
    p50 从 0.642s "降到" 0.058s —— **快是因为没干活**。只盯 p50/p95 会把召回塌方
    读成性能改善，所以拒绝这件事必须主动喊，而不是等人去翻 `pool_stats()`。

    ⚠️ 落点不是 `ops_monitor`（那个 launchd 作业）：它每个检查都是对 RDS 发 SQL，
    而本计数在 serving 进程内存里，跨进程读不到。进程内直告是既有范式
    （`rate_limiter._dispatch_cap_alert`）。
    """
    import os
    sent = []
    monkeypatch.setattr(sp, "_DISPATCH_ASYNC", False)     # 同步直调，便于断言
    monkeypatch.setattr("opensearch_pipeline.alerting.send_ops_alert",
                        lambda title, text, **kw: sent.append((title, kw.get("severity"),
                                                               kw.get("dedup_key"))) or True)
    os.environ["RAG_POOL_ADMISSION"] = "true"
    os.environ["RAG_POOL_FUSION_CAP"] = "1"
    os.environ["RAG_POOL_FUSION_WORKERS"] = "1"
    gate = threading.Event()
    try:
        sp._reset_for_tests()
        held = sp.submit(sp.FUSION, gate.wait)
        assert sp.submit_or_none(sp.FUSION, lambda: 1) is None
        assert len(sent) == 1, f"饱和没有告警：{sent}"
        title, sev, key = sent[0]
        assert sev == "critical" and key == "pool-saturated:fusion", (title, sev, key)
        gate.set()
        held.result(timeout=5)
    finally:
        gate.set()
        for k in ("RAG_POOL_ADMISSION", "RAG_POOL_FUSION_CAP", "RAG_POOL_FUSION_WORKERS"):
            os.environ.pop(k, None)


def test_alert_failure_never_breaks_the_request_path():
    """fail-open：告警发不出去（webhook 未配 / 网络挂）绝不影响限流主路径。

    ⚠️ 现网 `RAG_OPS_ALERT_WEBHOOK` **未配**（B7），所以今天这条告警会被
    `alerting._note_suppressed` 吞掉——本测试保证"被吞"不会连带打断查询。
    """
    import os
    from unittest import mock
    os.environ["RAG_POOL_ADMISSION"] = "true"
    os.environ["RAG_POOL_FUSION_CAP"] = "1"
    os.environ["RAG_POOL_FUSION_WORKERS"] = "1"
    gate = threading.Event()
    try:
        sp._reset_for_tests()
        with mock.patch.object(sp, "_DISPATCH_ASYNC", False), \
             mock.patch("opensearch_pipeline.alerting.send_ops_alert",
                        side_effect=RuntimeError("webhook down")):
            held = sp.submit(sp.FUSION, gate.wait)
            assert sp.submit_or_none(sp.FUSION, lambda: 1) is None   # 仍正常降级
            assert sp.pool_stats()[sp.FUSION]["rejected"] == 1
        gate.set()
        held.result(timeout=5)
    finally:
        gate.set()
        for k in ("RAG_POOL_ADMISSION", "RAG_POOL_FUSION_CAP", "RAG_POOL_FUSION_WORKERS"):
            os.environ.pop(k, None)
