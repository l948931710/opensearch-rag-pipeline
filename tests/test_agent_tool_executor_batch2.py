# -*- coding: utf-8 -*-
"""
test_agent_tool_executor_batch2.py — 外审核查修复批次2（2026-07-16）副作用语义回归

台账 docs/audits/enterprise_agent_review_verification_2026-07-16.md 批次2：
- P1-03 副作用工具的**任何**耗尽异常收 uncertain（不只 timeout/schema 违约），
  且禁 in-loop 自动重试（connection reset 类恰是「下游已提交」形态）；
  ToolResult.fail 维持 failed（工具作者的预边界声明，有意不变）。
- P1-04 幂等命中走 apply-on-hit：当前 output_schema 复验（版本漂移拒绝复用）+
  当前决策 obligations 后处理（回执不再未掩码入模型可见文本）；
  派生键重放复用回执（不再误报「同幂等键并发执行冲突」）。
- P1-02 过渡：per-tool 并发舱壁（满则 fail-fast=确定无副作用，绝不进对账）+
  超时池大小可配；配额随 future 真正完成才释放（挂死线程持续占坑不外溢）。
"""
import threading
import time

from opensearch_pipeline.agent_runtime.tool import (
    ContentBlock, RiskLevel, ToolResult, ToolSpec)
from opensearch_pipeline.agent_runtime.tool_executor import (
    ToolExecutor,
    digest,
    drain_read_trace,
    register_obligation_handler,
)


class _Store:
    def __init__(self, succeeded_hit=None):
        self.invocations = []
        self._hit = succeeded_hit

    def record_invocation(self, run_id, step_no, **kw):
        iid = f"inv{len(self.invocations) + 1}"
        self.invocations.append({"id": iid, "status": kw["status"]})
        return iid

    def finish_invocation(self, invocation_id, **kw):
        for inv in self.invocations:
            if inv["id"] == invocation_id:
                inv.update(kw)

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return self._hit


class _Tool:
    def __init__(self, spec, fn):
        self.spec = spec
        self._fn = fn
        self.calls = 0

    def run(self, ctx, args, idempotency_key=None):
        self.calls += 1
        return self._fn(args)


def _spec(risk=RiskLevel.READ_ONLY, idem="none", timeout_s=30.0, max_retries=0, name="t",
          side_effects=False, output_schema=None):
    return ToolSpec(name=name, version="1.0.0", description="d", input_schema={"type": "object"},
                    output_schema=output_schema or {"type": "object"}, risk_level=risk,
                    permission_scope="kb.search", data_classification="internal",
                    idempotency=idem, timeout_s=timeout_s, max_retries=max_retries,
                    side_effects=side_effects)


def _exec(store=None):
    return ToolExecutor(store or _Store())


def _call(ex, tool, args=None, **kw):
    base = dict(run_id="r", step_no=1, policy_decision="allow", policy_id="p")
    base.update(kw)
    return ex.execute(None, tool, args or {}, **base)


def _raiser(exc):
    def fn(a):
        raise exc
    return fn


# ── P1-03：副作用工具的异常语义 ─────────────────────────────────────────────


def test_p1_03_sideeffect_ordinary_exception_lands_uncertain():
    """普通异常（非 FuturesTimeout）≠ 确定未生效：副作用工具收 uncertain 进对账，
    此前落 failed → 同键重试回收原行再执行 = 潜在重复副作用。"""
    store = _Store()
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="w"),
                 _raiser(ValueError("response parse boom")))
    r = _call(_exec(store), tool, idempotency_key="k1")
    assert r.status == "failed" and "不确定" in r.error and "对账" in r.error
    assert store.invocations[0]["status"] == "uncertain"


def test_p1_03_readonly_exception_still_failed():
    """无副作用工具的异常语义不变：failed（可安全重试）。"""
    store = _Store()
    tool = _Tool(_spec(name="ro"), _raiser(ValueError("boom")))
    r = _call(_exec(store), tool)
    assert r.status == "failed" and "不确定" not in (r.error or "")
    assert drain_read_trace()
    assert store.invocations[0]["status"] == "failed"


def test_p1_03_sideeffect_retryable_exception_no_inloop_retry():
    """副作用工具禁 in-loop 自动重试：connection reset 恰是「下游可能已提交」形态，
    立刻重放与同键回收重试同罪。此前 max_retries>0 时会原地重跑。"""
    store = _Store()
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="w",
                       max_retries=2),
                 _raiser(ConnectionError("connection reset by peer")))
    r = _call(_exec(store), tool, idempotency_key="k1")
    assert tool.calls == 1                              # 绝不 in-loop 重放
    assert r.status == "failed" and "不确定" in r.error
    assert store.invocations[0]["status"] == "uncertain"


def test_p1_03_readonly_retryable_still_retries():
    """读工具的 in-loop 重试语义不变。"""
    calls = {"n": 0}

    def fn(a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("connection reset")
        return ToolResult.text_ok("ok")

    r = _call(_exec(), _Tool(_spec(max_retries=1, name="ro2"), fn))
    assert r.status == "succeeded" and calls["n"] == 2


def test_p1_03_toolresult_fail_stays_failed():
    """ToolResult.fail 是工具作者的预边界声明（写工具全部预提交路径如此）——
    有意维持 failed，不进对账（核查台账明确拒绝改动此语义）。"""
    store = _Store()
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="w"),
                 lambda a: ToolResult.fail("业务预检不过（未执行副作用）"))
    r = _call(_exec(store), tool, idempotency_key="k1")
    assert r.status == "failed"
    assert store.invocations[0]["status"] == "failed"


# ── P1-04：幂等命中 apply-on-hit ────────────────────────────────────────────


def test_p1_04_hit_applies_obligations():
    """命中回执被渲染进模型可见文本前必须过当前决策的 obligations——
    此前零后处理，义务一旦挂上命中即成掩码旁路。"""
    register_obligation_handler(
        "test_scrub_b2",
        lambda param, result: ToolResult(
            status=result.status,
            content=[ContentBlock.of_text(b.text.replace("SECRET", "###"))
                     if b.type == "text" and b.text else b for b in result.content],
            receipt=result.receipt, error=result.error))
    store = _Store(succeeded_hit={"invocation_id": "old",
                                  "receipt_json": '{"code":"SECRET-1"}'})
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="w"),
                 lambda a: ToolResult.ok(receipt={"code": "SECRET-2"}))
    r = _call(_exec(store), tool, idempotency_key="k1",
              obligations=("test_scrub_b2",))
    assert tool.calls == 0                              # 命中不执行
    assert r.status == "succeeded"
    text = " ".join(b.text for b in r.content if b.type == "text" and b.text)
    assert "###" in text and "SECRET" not in text       # 义务在命中路径生效


def test_p1_04_hit_schema_drift_refuses_reuse():
    """旧回执不符**当前** output_schema（工具版本漂移）→ 拒绝复用且不执行（宁停不错）。"""
    store = _Store(succeeded_hit={"invocation_id": "old",
                                  "receipt_json": '{"legacy_field":1}'})
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="w",
                       output_schema={"type": "object", "required": ["order"]}),
                 lambda a: ToolResult.ok(receipt={"order": "SO-1"}))
    r = _call(_exec(store), tool, idempotency_key="k1")
    assert r.status == "failed" and "契约" in r.error
    assert tool.calls == 0                              # 绝不因拒绝复用而重放副作用
    assert store.invocations == []                      # 未新增执行行


def test_p1_04_hit_without_receipt_not_blocked_by_strict_schema():
    """无回执的历史成功行：无泄漏面也无漂移面——严格 schema 下仍照常复用（不误伤）。"""
    store = _Store(succeeded_hit={"invocation_id": "old", "receipt_json": None})
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="w",
                       output_schema={"type": "object", "required": ["order"]}),
                 lambda a: ToolResult.ok(receipt={"order": "SO-1"}))
    r = _call(_exec(store), tool, idempotency_key="k1")
    assert r.status == "succeeded" and tool.calls == 0


def test_p1_04_derived_key_replay_reuses_receipt():
    """键碰撞后派生新键的调用被**真重放**：派生键上的 succeeded 行应复用回执——
    此前直落 INSERT 撞 uk_tool_idem 误报「同幂等键并发执行冲突」（回执其实在库里）。"""
    args = {"x": 1}

    class _DerivedStore(_Store):
        def find_succeeded_invocation(self, tool_name, key):
            if ":a" in key:                             # 派生键：同参重放的成功行
                return {"invocation_id": "old2", "args_digest": digest(args),
                        "receipt_json": '{"order":"SO-9"}'}
            return {"invocation_id": "old1", "args_digest": "mismatch-digest",
                    "receipt_json": '{"order":"SO-OTHER"}'}

    store = _DerivedStore()
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="w"),
                 lambda a: ToolResult.ok(receipt={"order": "SO-NEW"}))
    r = _call(_exec(store), tool, args, idempotency_key="k1")
    assert r.status == "succeeded" and tool.calls == 0
    assert r.receipt == {"order": "SO-9"}               # 复用派生键回执
    assert "冲突" not in (r.error or "")


# ── P1-02 过渡：per-tool 并发舱壁 + 池大小可配 ──────────────────────────────


def test_p1_02_bulkhead_fail_fast_definite_failed(monkeypatch):
    """配额满 → fail-fast，且拒绝发生在提交执行之前=确定无副作用：
    即便是副作用工具也收 failed（可立即重试），绝不进 uncertain 对账。"""
    monkeypatch.setenv("RAG_AGENT_TOOL_MAX_CONCURRENCY", "1")
    started, release = threading.Event(), threading.Event()

    def blocking(a):
        started.set()
        release.wait(5)
        return ToolResult.text_ok("ok")

    store = _Store()
    ex = _exec(store)
    tool = _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="wb",
                       timeout_s=5), blocking)
    box = {}
    t = threading.Thread(
        target=lambda: box.setdefault("r1", _call(ex, tool, idempotency_key="k1")))
    t.start()
    assert started.wait(5)                              # 首个调用已占住唯一配额
    r2 = _call(ex, tool, idempotency_key="k2")
    assert r2.status == "failed" and "并发额度" in r2.error
    release.set()
    t.join(5)
    assert box["r1"].status == "succeeded"
    assert tool.calls == 1                              # 第二个调用从未执行
    statuses = [i["status"] for i in store.invocations]
    assert "uncertain" not in statuses                  # 舱壁拒绝绝不进对账
    assert "failed" in statuses


def test_p1_02_bulkhead_permit_released_on_completion(monkeypatch):
    """配额随 future 完成释放：quota=1 时顺序两次调用都成功（不泄漏配额）。"""
    monkeypatch.setenv("RAG_AGENT_TOOL_MAX_CONCURRENCY", "1")
    ex = _exec()
    tool = _Tool(_spec(name="seq"), lambda a: ToolResult.text_ok("ok"))
    assert _call(ex, tool).status == "succeeded"
    assert _call(ex, tool).status == "succeeded"


def test_p1_02_bulkhead_hung_thread_keeps_permit(monkeypatch):
    """超时返回调用方后，挂死线程仍占本工具配额（舱壁语义：占用不外溢也不提前释放）；
    线程真正结束后配额回收。"""
    monkeypatch.setenv("RAG_AGENT_TOOL_MAX_CONCURRENCY", "1")

    def slow(a):
        time.sleep(0.5)
        return ToolResult.text_ok("ok")

    ex = _exec()
    tool = _Tool(_spec(name="hung", timeout_s=0.05), slow)
    r1 = _call(ex, tool)
    assert r1.status == "failed"                        # 超时（READ_ONLY → failed）
    r2 = _call(ex, tool)
    assert "并发额度" in (r2.error or "")                 # 线程仍挂 → 配额被占
    r3 = r2
    deadline = time.monotonic() + 3                     # 线程自然结束 → done 回调释放
    while time.monotonic() < deadline and "并发额度" in (r3.error or ""):
        time.sleep(0.1)
        r3 = _call(ex, tool)
    assert r3.status == "failed" and "并发额度" not in (r3.error or "")   # 配额已回收


def test_p1_02_timeout_pool_size_env(monkeypatch):
    """RAG_AGENT_TOOL_TIMEOUT_POOL_SIZE 可配（此前硬编码 8）。"""
    import opensearch_pipeline.agent_runtime.tool_executor as te
    monkeypatch.setenv("RAG_AGENT_TOOL_TIMEOUT_POOL_SIZE", "3")
    monkeypatch.setattr(te, "_TIMEOUT_POOL", None)      # 绕过进程级缓存
    pool = te._get_timeout_pool()
    try:
        assert pool._max_workers == 3
    finally:
        pool.shutdown(wait=False)                       # monkeypatch 会还原全局
