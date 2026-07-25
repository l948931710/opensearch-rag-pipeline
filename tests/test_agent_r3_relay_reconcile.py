# -*- coding: utf-8 -*-
"""R3-P1（ARR 外审 P1-RT-05/09/10）回归：SSE 游标/异步中继/对账退避。"""
import json
import time
from types import SimpleNamespace

from opensearch_pipeline.agent_runtime import event_relay as er


class _FakeRedis:
    """最小 stream 桩：xadd/xrange/exists + 可注入延迟。"""

    def __init__(self, delay=0.0):
        self.entries = []            # [(id, {"data": json})]
        self._seq = 0
        self.delay = delay

    def xadd(self, key, fields, maxlen=None, approximate=True):
        if self.delay:
            time.sleep(self.delay)
        self._seq += 1
        eid = f"{self._seq}-0"
        self.entries.append((eid, dict(fields)))
        return eid

    def expire(self, key, ttl):
        return True

    def exists(self, key):
        return 1 if self.entries else 0

    def xrange(self, key, min="-", max="+", count=None):
        return self.entries[:count] if self.entries else []


def _wire_redis(monkeypatch, fake):
    """双打：sys.modules + 包属性——全量套件里真实 redis_client 已入包属性时，
    `from opensearch_pipeline import redis_client` 直接取属性、不查 sys.modules。"""
    import sys
    import opensearch_pipeline as pkg
    mod = SimpleNamespace(get_client=lambda: fake,
                          key=lambda *parts: ":".join(parts),
                          is_configured=lambda: True)
    monkeypatch.setitem(sys.modules, "opensearch_pipeline.redis_client", mod)
    monkeypatch.setattr(pkg, "redis_client", mod, raising=False)


class TestCursorProtocol:
    def test_resolve_start_id_variants(self, monkeypatch):
        fake = _FakeRedis()
        _wire_redis(monkeypatch, fake)
        assert er.resolve_start_id("r", None) == ("0-0", False)      # 无游标=全量
        fake.entries = [("5-0", {"data": "{}"}), ("6-0", {"data": "{}"})]
        assert er.resolve_start_id("r", "5-0") == ("5-0", False)     # 窗口内续读
        assert er.resolve_start_id("r", "2-0") == ("0-0", True)      # 被 trim → reset
        fake.entries = []
        assert er.resolve_start_id("r", "9-0") == ("0-0", True)      # 流缺失 → reset

    def test_id_tuple_ordering(self):
        assert er._id_tuple("10-2") > er._id_tuple("10-1") > er._id_tuple("9-9")
        assert er._id_tuple("garbage") == (0, 0)


class TestAsyncRelay:
    def test_slow_redis_does_not_block_publisher(self, monkeypatch):
        """P1-RT-10 探针：Redis 每帧 200ms 时，100 帧 publish 必须远快于同步耗时。"""
        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "true")
        fake = _FakeRedis(delay=0.2)
        _wire_redis(monkeypatch, fake)
        r = er._RedisRelay("run-async")
        t0 = time.monotonic()
        for _ in range(100):
            r._xadd({"type": "model_delta", "text": "x"})
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"publish 被同步 XADD 拖住（{elapsed:.2f}s）"

    def test_queue_full_drops_delta_keeps_control(self, monkeypatch):
        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "true")
        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_QUEUE", "4")
        fake = _FakeRedis(delay=0.3)
        _wire_redis(monkeypatch, fake)
        r = er._RedisRelay("run-full")
        for _ in range(50):
            r._xadd({"type": "model_delta", "text": "x"})
        assert r._dropped > 0                                 # delta 队满即丢（计数留痕）
        r._xadd({"type": "run_failed", "error": "e"})         # 控制帧短等必达（不抛）

    def test_sync_kill_switch(self, monkeypatch):
        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "false")
        fake = _FakeRedis()
        _wire_redis(monkeypatch, fake)
        r = er._RedisRelay("run-sync")
        r._xadd({"type": "model_delta", "text": "x"})
        assert len(fake.entries) == 1                         # 同步旧路径：立即落

    def test_end_drains_queue(self, monkeypatch):
        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "true")
        fake = _FakeRedis()
        _wire_redis(monkeypatch, fake)
        r = er._RedisRelay("run-end")
        for i in range(10):
            r._xadd({"type": "model_delta", "text": str(i)})
        r.end()
        time.sleep(0.1)
        assert len(fake.entries) == 11                        # 10 delta + __end__


class TestStreamCursorReplay:
    def test_start_id_skips_consumed_and_carries_relay_id(self, monkeypatch):
        import json
        fake = _FakeRedis()
        _wire_redis(monkeypatch, fake)
        for i, t in enumerate(["model_delta", "model_delta", "run_completed"]):
            fake.xadd("k", {"data": json.dumps({"type": t, "n": i})})

        def _xread(streams, count=None, block=None):
            key, after = list(streams.items())[0]
            out = [(eid, f) for eid, f in fake.entries
                   if er._id_tuple(eid) > er._id_tuple(after)]
            return [("k", out)] if out else None

        fake.xread = _xread
        got = list(er.stream_run_events("r", start_id="1-0"))
        assert [d.get("n") for d in got] == [1, 2]             # 已读帧不重放
        assert all("_relay_id" in d for d in got)              # SSE id: 行的游标

    def test_announce_reset_prepends_marker(self, monkeypatch):
        fake = _FakeRedis()
        _wire_redis(monkeypatch, fake)
        fake.xread = lambda *a, **k: None
        gen = er.stream_run_events("r", announce_reset=True, overall_timeout_s=1,
                                   is_terminal_fn=lambda: True)
        first = next(gen)
        assert first["type"] == "__reset__"


class TestReconcileBackoff:
    def test_unknown_rows_deferred_and_quarantined(self, monkeypatch):
        from opensearch_pipeline.agent_runtime import operation_reconciler as orc
        deferred = []

        class _Store:
            def list_uncertain_invocations_for_reconcile(self, *, min_age_s, limit):
                return [{"invocation_id": "i1", "tool_name": "t", "run_id": "r",
                         "operation_id": "op1", "reconcile_attempts": 0},
                        {"invocation_id": "i2", "tool_name": "t", "run_id": "r",
                         "operation_id": "op2", "reconcile_attempts": 19}]

            def resolve_uncertain_invocation(self, *a, **k):
                return True

            def defer_reconcile(self, inv_id, *, attempts, delay_s):
                deferred.append((inv_id, attempts, delay_s))

        class _Tool:
            def check_operation(self, op_id):
                return {"outcome": "unknown"}

        reg = SimpleNamespace(get=lambda name: _Tool())
        counts = orc.reconcile_uncertain_invocations(_Store(), reg)
        assert counts["unknown"] == 2 and counts["deferred"] == 2
        assert counts["quarantined"] == 1                     # i2 达上限（20）隔离
        assert deferred[0][0] == "i1" and deferred[0][1] == 1
        assert 30 <= deferred[0][2] <= 3600                   # 指数退避封顶 1h
        assert deferred[1] == ("i2", 20, 3600)

    def test_missing_checker_also_backs_off(self):
        from opensearch_pipeline.agent_runtime import operation_reconciler as orc
        deferred = []

        class _Store:
            def list_uncertain_invocations_for_reconcile(self, *, min_age_s, limit):
                return [{"invocation_id": "i3", "tool_name": "gone", "run_id": "r",
                         "operation_id": "op3", "reconcile_attempts": 2}]

            def resolve_uncertain_invocation(self, *a, **k):
                return True

            def defer_reconcile(self, inv_id, *, attempts, delay_s):
                deferred.append((inv_id, attempts))

        reg = SimpleNamespace(get=lambda name: None)          # 工具已不在 registry
        counts = orc.reconcile_uncertain_invocations(_Store(), reg)
        assert counts["skipped"] == 1 and deferred == [("i3", 3)]


class TestEnsureWriterRace:
    """评审 2026-07-24：_ensure_writer 的 check-then-act 竞态。

    帧不止来自 executor 单驱动线程——`__end__` 由 RunHandle._finish 发，可能在
    完成/排水路径的另一线程。无锁时会建出两个队列两个 writer：先落的队列里的帧
    永不 XADD，且旧 writer 的 task_done 打到新队列上抛 ValueError 自杀 →
    回放端拿到一条没有 `__end__` 的截断流。
    """

    def test_concurrent_first_emit_creates_single_writer(self, monkeypatch):
        import threading

        fake = _FakeRedis()
        _wire_redis(monkeypatch, fake)
        monkeypatch.setenv("RAG_AGENT_EVENT_RELAY_ASYNC", "true")
        relay = er._RedisRelay("run-race")

        seen_queues, barrier = [], threading.Barrier(8)

        def _emit(i):
            barrier.wait()                       # 尽量把 8 线程挤进同一窗口
            relay._xadd({"type": "model_delta", "text": f"d{i}"})
            seen_queues.append(id(relay._q))

        threads = [threading.Thread(target=_emit, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)

        assert len(set(seen_queues)) == 1, "并发首发不得建出第二个队列"
        assert threading.active_count() >= 1
        relay.end()                              # 控制帧必达 + 关 writer
        assert relay._writer is not None
        relay._writer.join(5)
        assert not relay._writer.is_alive(), "writer 不得因 task_done 打错队列而中途死亡"
        types = [json.loads(f["data"])["type"] for _id, f in fake.entries]
        assert types.count(er._END_TYPE) == 1, "__end__ 必须恰好送达一次"
        assert types.count("model_delta") == 8, "并发首发的 delta 不得因换队列丢失"

    def test_drain_holds_its_own_queue(self):
        """_drain 必须按值收队列（而非回读 self._q）——这是不变量的另一半。"""
        import inspect

        sig = inspect.signature(er._RedisRelay._drain)
        assert len(sig.parameters) == 2, "_drain(self, q)：队列须按值传入"
        code = "\n".join(ln.split("#", 1)[0]           # 注释里提 self._q 是解释，不算读
                         for ln in inspect.getsource(er._RedisRelay._drain).splitlines())
        assert "self._q" not in code, "_drain 不得回读 self._q（换队列后会打错 task_done）"
