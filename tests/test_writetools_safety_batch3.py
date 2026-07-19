# -*- coding: utf-8 -*-
"""
test_writetools_safety_batch3.py — 复核批次3（写工具安全线）A4 + D3 回归锁

A4 幂等复核：同键不同参拒绝复用回执（键碰撞不谎报）· 命中复核 args_digest ·
    幂等键编入轮次（跨轮同 call_id 不撞键）· gateway 兜底 call_id 消碰撞
D3 失败侧 fencing：失败侧回调经 checked CAS 门控——失去所有权（purge/收尸/取消
    抢先迁移）时不再落库（qa_session_log 不在主体擦除后长回新行）

（D3 purge quiesce 在 test_subject_purge.py；C1 目标锁在 test_ontology_store.py）
"""
from opensearch_pipeline.agent_runtime.events import RunFailed, ToolCallProposed
from opensearch_pipeline.agent_runtime.executor import ThreadedRunExecutor
from opensearch_pipeline.agent_runtime.policy import default_policy_engine, make_adjudicator
from opensearch_pipeline.agent_runtime.registry import ToolRegistry
from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec
from opensearch_pipeline.agent_runtime.tool_executor import ToolExecutor, digest
from tests.test_agent_cancel_and_relay_continuity import _StubLoop
from tests.test_agent_reaudit_fixes import _FakeStore, _ctx
from tests.test_agent_runtime_policy import _FakeStore as _PolicyStore
from tests.test_agent_runtime_policy import _FakeTool
from tests.test_agent_runtime_tool_executor import _Tool, _spec

ARGS = {"po": "PO-9", "qty": 3}


# ═══════════════════════════════════════════════════════════════
# A4：幂等命中复核 args_digest
# ═══════════════════════════════════════════════════════════════


class _KeyedStore:
    """捕获 record_invocation 的幂等键；可配置命中行（含 args_digest）。"""

    def __init__(self, hit=None):
        self.invocations = []
        self.keys = []
        self._hit = hit

    def record_invocation(self, run_id, step_no, **kw):
        self.keys.append(kw.get("idempotency_key"))
        iid = f"inv{len(self.invocations) + 1}"
        self.invocations.append({"id": iid, "status": kw["status"]})
        return iid

    def finish_invocation(self, invocation_id, **kw):
        for inv in self.invocations:
            if inv["id"] == invocation_id:
                inv.update(kw)

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return self._hit


def _hw_tool():
    return _Tool(_spec(risk=RiskLevel.HIGH_WRITE, idem="key_required", name="u8"),
                 lambda a: ToolResult.ok(receipt={"done": True}))


def _run(store, tool, key="k1"):
    return ToolExecutor(store).execute(None, tool, dict(ARGS), run_id="r", step_no=1,
                                       policy_decision="allow", policy_id="p",
                                       idempotency_key=key)


def test_a4_same_key_same_args_reuses_receipt():
    hit = {"invocation_id": "old", "result_digest": "d",
           "receipt_json": '{"order":"SO-1"}', "args_digest": digest(ARGS)}
    tool = _hw_tool()
    r = _run(_KeyedStore(hit), tool)
    assert r.status == "succeeded" and tool.calls == 0        # 真重放：复用不执行
    assert r.receipt == {"order": "SO-1"}


def test_a4_same_key_different_args_refuses_reuse_executes_new_key():
    hit = {"invocation_id": "old", "result_digest": "d",
           "receipt_json": '{"order":"SO-1"}', "args_digest": "someone-elses-digest"}
    store = _KeyedStore(hit)
    tool = _hw_tool()
    r = _run(store, tool)
    assert r.status == "succeeded" and tool.calls == 1        # 键碰撞：拒绝复用，真执行
    assert r.receipt == {"done": True}                        # 不是旧回执
    assert store.keys and store.keys[-1] == f"k1:a{digest(ARGS)[:16]}"   # 按内容派生新键


def test_a4_legacy_hit_without_digest_still_reuses():
    """历史行无 args_digest → 沿用复用行为（fail-open，不误伤旧数据）。"""
    hit = {"invocation_id": "old", "result_digest": "d", "receipt_json": '{"order":"SO-1"}'}
    tool = _hw_tool()
    r = _run(_KeyedStore(hit), tool)
    assert r.status == "succeeded" and tool.calls == 0
    assert r.receipt == {"order": "SO-1"}


# ═══════════════════════════════════════════════════════════════
# A4：幂等键编入轮次 + gateway 兜底 call_id 消碰撞
# ═══════════════════════════════════════════════════════════════


def test_a4_policy_idem_key_includes_turn():
    """同 call_id 跨轮不撞键：t{turn} 编入键；同轮重放键稳定。"""
    spec = ToolSpec(name="u8_write", version="1.0.0", description="d",
                    input_schema={"type": "object"}, output_schema={"type": "object"},
                    risk_level=RiskLevel.READ_ONLY, permission_scope="kb.search",
                    data_classification="internal", idempotency="key_required")
    reg = ToolRegistry()
    reg.register(_FakeTool(spec))
    store = _PolicyStore()
    adj = make_adjudicator(reg, default_policy_engine(), store)
    ctx = _ctx()
    ev_t0 = ToolCallProposed(call_id="call_0", tool_name="u8_write",
                             arguments={"q": "x"}, turn_index=0)
    ev_t2 = ToolCallProposed(call_id="call_0", tool_name="u8_write",
                             arguments={"q": "x"}, turn_index=2)
    adj(ctx, ev_t0)
    adj(ctx, ev_t2)
    assert adj(ctx, ev_t0) is not None
    from opensearch_pipeline.agent_runtime.tool_executor import drain_read_trace
    drain_read_trace()                                        # READ_ONLY trace 异步：断言前排水
    keys = [inv.get("idempotency_key") for inv in store.invocations]
    assert len(keys) == 3 and keys[0] != keys[1]              # 跨轮不碰撞
    assert ":t0:" in keys[0] and ":t2:" in keys[1]
    assert keys[2] == keys[0]                                  # 同轮重放键稳定


def test_a4_gateway_fallback_call_ids_do_not_collide():
    from opensearch_pipeline.agent_runtime.model_gateway import (
        ChatResponse, ToolCall, _resp_to_turn)
    from opensearch_pipeline.agent_runtime.events import Usage

    def _turn():
        return _resp_to_turn(ChatResponse(
            text="", tool_calls=[ToolCall(id="", name="t", arguments={"a": 1})],
            usage=Usage(), model="m"))

    id1 = _turn().tool_calls[0].call_id
    id2 = _turn().tool_calls[0].call_id                        # 模拟第二轮再兜底
    assert id1.startswith("call_0_") and id2.startswith("call_0_")
    assert id1 != id2                                          # 纯位置序号必撞，现不撞


# ═══════════════════════════════════════════════════════════════
# D3：失败侧回调 checked CAS 门控
# ═══════════════════════════════════════════════════════════════


class _DenyFailStore(_FakeStore):
    """running→failed CAS 恒失败：模拟行已被 purge 删除 / reaper 收尸 / 取消抢先。"""

    def transition(self, run_id, frm, to):
        ok = super().transition(run_id, frm, to)
        if (frm, to) == ("running", "failed"):
            return False
        return ok


def _fail_loop():
    return _StubLoop(run_events=[RunFailed(error="boom", retryable=False)])


def test_d3_failure_callback_fires_when_owned():
    failures = []
    ex = ThreadedRunExecutor(_FakeStore(), lambda ctx, ev: ToolResult.text_ok("r"),
                             max_concurrent=2)
    handle = ex.submit(_ctx(), _fail_loop(), [{"role": "user", "content": "q"}], [],
                       on_failure=failures.append)
    evs = list(handle.events())
    ex.shutdown()
    assert any(isinstance(e, RunFailed) for e in evs)
    assert failures == ["boom"]                               # 仍持有 run：失败侧照常落库


def test_d3_failure_callback_suppressed_when_ownership_lost():
    failures = []
    ex = ThreadedRunExecutor(_DenyFailStore(), lambda ctx, ev: ToolResult.text_ok("r"),
                             max_concurrent=2)
    handle = ex.submit(_ctx(), _fail_loop(), [{"role": "user", "content": "q"}], [],
                       on_failure=failures.append)
    evs = list(handle.events())
    ex.shutdown()
    # R1（RB-RT-02 改判）：失去所有权且真相未知（store 无 get_run）→ **不发终态帧**
    # ——旧断言「本地事件仍收流」正是评审探针抓的双真相（客户端见 failed、库仍 running）
    assert not any(isinstance(e, RunFailed) for e in evs)
    assert failures == []                                      # 失去所有权：不落失败侧
