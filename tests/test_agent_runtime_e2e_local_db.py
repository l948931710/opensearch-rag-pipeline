# -*- coding: utf-8 -*-
"""
test_agent_runtime_e2e_local_db.py — 全链 e2e 真库集成测试（系统测试产品化 · 守护 Bug1）

对**本地真 MySQL**（fuling_operation）跑完整 executor→loop→adjudicator→ToolExecutor→
knowledge_search→RDSRunStore，用脚本化 provider（2 轮：提工具→出答案）+ mock retrieve，
断言 agent_run/agent_step/tool_invocation/llm_call_log 全套真行落地，且 **llm_call_log.run_id
非空且等于本 run**——回归守护 executor「就地回填 run_id」的修复（此前 model_fn 闭包建 run 前的
旧 ctx → run_id 落 NULL，无法关联；staging 真跑第一刀抓到）。

无本地 MySQL / 无 agent 表 → 整体 skip（同 test_concurrency）；**host-pin 本地**，绝不写 staging/prod。
确定性：provider 脚本化、retrieve mock、无外部模型/HA3 → 可进 CI db-integration job。
"""
import uuid

import pytest

from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target


def _local_operation_conn():
    """本地 fuling_operation 连接；host-pin 杜绝把 run 行误写 staging/prod。"""
    import pymysql
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        raise RuntimeError(f"[PROD-GUARD] 仅本地 MySQL；解析到 host {cfg.rds.host!r}，拒绝连接。")
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.operation_database,
                           autocommit=True, connect_timeout=5)


def _db_ready():
    try:
        conn = _local_operation_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema=DATABASE() AND table_name IN "
                        "('agent_run','agent_step','tool_invocation','llm_call_log')")
            ok = cur.fetchone()[0] == 4
        conn.close()
        return ok
    except Exception:
        return False


skipif_no_agent_db = pytest.mark.skipif(
    not _db_ready(), reason="本地 MySQL 无 agent 表（先 apply schema 022/023/024）")


class _ScriptedProvider:
    """脚本化 DashScope：首轮提 knowledge_search，见到 tool 结果后出最终答案。"""

    name = "dashscope"

    def capabilities(self, model):
        return None

    def chat(self, model, req):
        from opensearch_pipeline.agent_runtime import ChatResponse, ToolCall, Usage
        if any(m.get("role") == "tool" for m in req.messages):
            return ChatResponse(text="根据检索：注塑车间需班班清洁、设备卫生常态化。",
                                tool_calls=[], usage=Usage(tokens_prompt=42, tokens_completion=16),
                                model=model)
        return ChatResponse(text="", tool_calls=[ToolCall(id="call_ks1", name="knowledge_search",
                                                          arguments={"query": "注塑车间清洁规范"})],
                            usage=Usage(tokens_prompt=31, tokens_completion=9), model=model)


@skipif_no_agent_db
def test_full_run_persists_rows_with_run_id(monkeypatch):
    """全链真库落地 + llm_call_log.run_id 回归守护（Bug1：executor 就地回填 run_id）。"""
    def fake_retrieve(query, *, top_k=None, user_dept=None, **k):
        return [{"doc_id": "D_TEST", "chunk_text": "注塑车间班班清洁；设备卫生常态化。",
                 "doc_title": "生产规范告知书"}]
    monkeypatch.setattr("opensearch_pipeline.retriever.retrieve_and_enrich", fake_retrieve)

    from opensearch_pipeline.agent_runtime import (
        DefaultAgentLoop, ExecutionContext, ModelGateway, ThreadedRunExecutor,
        default_policy_engine, make_adjudicator, make_model_fn)
    from opensearch_pipeline.agent_runtime.audit import RDSAuditLog
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore
    from opensearch_pipeline.agent_tools import build_default_registry

    registry = build_default_registry()
    run_store = RDSRunStore()
    # 注入真实 audit（镜像 routes/agent.py；④a 守护：漏注入则 agent_audit_log 静默不落）
    adjudicator = make_adjudicator(registry, default_policy_engine(), run_store, audit=RDSAuditLog())
    gateway = ModelGateway({"dashscope": _ScriptedProvider()},
                           routes={"default": [("dashscope", "qwen-test")]},
                           call_logger=run_store.record_llm_call)   # 每次模型调用记 llm_call_log
    executor = ThreadedRunExecutor(run_store, adjudicator, max_concurrent=2)
    ctx = ExecutionContext.create(request_id="e2e-" + uuid.uuid4().hex[:8], user_id="e2e-tester",
                                  acl_groups=["production"], roles=["employee"],
                                  channel="console", thread_id="e2e-thread")
    loop = DefaultAgentLoop(make_model_fn(gateway, ctx, "default"))
    tools = registry.list_specs(ctx)
    messages = [{"role": "system", "content": "先检索再据实作答。"},
                {"role": "user", "content": "注塑车间的清洁规范是什么？"}]

    handle = executor.submit(ctx, loop, messages, tools)
    run_id = handle.run_id
    events = [type(e).__name__ for e in handle.events()]
    executor.shutdown()

    assert "ToolCallProposed" in events and "RunCompleted" in events, f"事件异常: {events}"

    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            # 状态机 running→succeeded + 预算计数（turns/tool_calls）
            cur.execute("SELECT status, turns_used, tool_calls_used FROM agent_run WHERE run_id=%s", (run_id,))
            status, turns, tool_calls = cur.fetchone()
            assert status == "succeeded", f"run 状态应 succeeded，实际 {status}"
            assert turns == 2, f"应计 2 轮（tool 轮 + 最终答案轮），实际 {turns}（③ turns_used 回归守护）"
            assert tool_calls == 1

            # tool_invocation 落库（run_id 关联正确）
            cur.execute("SELECT tool_name, status FROM tool_invocation WHERE run_id=%s", (run_id,))
            assert ("knowledge_search", "succeeded") in cur.fetchall()

            # agent_step 落库 + ④b：模型轮记 model_call step（步骤序含模型轮，非只有 tool_call）
            cur.execute("SELECT kind, COUNT(*) FROM agent_step WHERE run_id=%s GROUP BY kind", (run_id,))
            step_kinds = dict(cur.fetchall())
            assert step_kinds.get("tool_call", 0) >= 1
            assert step_kinds.get("model_call", 0) == 2, f"应有 2 条 model_call step（④b），实得 {step_kinds}"

            # ④a：audit 已接线 → 读工具也落合规审计（tool_executor 对 ALLOW 全审计、读工具 fail-open）
            cur.execute("SELECT COUNT(*) FROM agent_audit_log WHERE run_id=%s AND event_type='tool_call'",
                        (run_id,))
            assert cur.fetchone()[0] >= 1, "knowledge_search 应有 agent_audit_log 行（④a：须注入 RDSAuditLog）"

            # 🔑 Bug1 回归守护：llm_call_log 必须带**本 run** 的 run_id（此前 model_fn 闭包旧 ctx→NULL）
            cur.execute("SELECT COUNT(*) FROM llm_call_log WHERE run_id=%s AND status='ok'", (run_id,))
            n_llm = cur.fetchone()[0]
            assert n_llm >= 1, "llm_call_log 应带正确 run_id（executor 就地回填 run_id 回归守护）"
    finally:
        # 清理本 run 所有测试行，保持本地库干净
        with conn.cursor() as cur:
            for t in ("llm_call_log", "tool_invocation", "agent_checkpoint",
                      "agent_step", "agent_audit_log", "agent_run"):
                try:
                    cur.execute(f"DELETE FROM `{t}` WHERE run_id=%s", (run_id,))
                except Exception:
                    pass
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# 场景矩阵：不同 run 结局的真库落地（deny / 工具失败 / 预算耗尽 / require_approval）
# 全部确定性（脚本 provider + mock retrieve）、真 RDSRunStore + RDSAuditLog、host-pin 本地。
# ─────────────────────────────────────────────────────────────────────────────
_TABLES = ("llm_call_log", "tool_invocation", "agent_checkpoint",
           "agent_step", "agent_audit_log", "agent_run")


def _cleanup_run(run_id):
    conn = _local_operation_conn()
    with conn.cursor() as cur:
        for t in _TABLES:
            try:
                cur.execute(f"DELETE FROM `{t}` WHERE run_id=%s", (run_id,))
            except Exception:
                pass
    conn.close()


def _fetch(sql, params):
    conn = _local_operation_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _write_tool(scope, risk):
    """测试写型工具（HIGH_WRITE/LOW_WRITE 均须 key_required）。"""
    from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec
    _risk = {"high": RiskLevel.HIGH_WRITE, "low": RiskLevel.LOW_WRITE}[risk]

    class _T:
        spec = ToolSpec(
            name="u8_writeback", version="1.0", description="测试写回工具",
            input_schema={"type": "object", "additionalProperties": False,
                          "properties": {"qty": {"type": "integer"}}},
            output_schema={"type": "object"}, risk_level=_risk,
            permission_scope=scope, data_classification="internal", idempotency="key_required")

        def run(self, ctx, args, idempotency_key=None):
            return ToolResult.text_ok("已写回")

    return _T()


class _SeqProvider:
    """按调用序返回预设 ChatResponse（耗尽后重复最后一个）。"""
    name = "dashscope"

    def __init__(self, responses):
        self._r = list(responses)
        self.i = 0

    def capabilities(self, model):
        return None

    def chat(self, model, req):
        r = self._r[min(self.i, len(self._r) - 1)]
        self.i += 1
        return r


def _tc(name, args, cid="c1"):
    from opensearch_pipeline.agent_runtime.events import Usage
    from opensearch_pipeline.agent_runtime.model_gateway import ChatResponse, ToolCall
    return ChatResponse(text="", tool_calls=[ToolCall(id=cid, name=name, arguments=args)],
                        usage=Usage(tokens_prompt=10, tokens_completion=3), model="qwen-test")


def _final(text="完成"):
    from opensearch_pipeline.agent_runtime.events import Usage
    from opensearch_pipeline.agent_runtime.model_gateway import ChatResponse
    return ChatResponse(text=text, tool_calls=[], usage=Usage(tokens_prompt=8, tokens_completion=5),
                        model="qwen-test")


def _run_scenario(registry, policy, responses, *, budget=None):
    """跑一次 run（真 RDSRunStore + RDSAuditLog + 脚本 provider），返回 (run_id, [event 名])。"""
    from opensearch_pipeline.agent_runtime import (
        DefaultAgentLoop, ExecutionContext, ModelGateway, ThreadedRunExecutor,
        make_adjudicator, make_model_fn)
    from opensearch_pipeline.agent_runtime.audit import RDSAuditLog
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore

    run_store = RDSRunStore()
    adjudicator = make_adjudicator(registry, policy, run_store, audit=RDSAuditLog())
    gateway = ModelGateway({"dashscope": _SeqProvider(responses)},
                           routes={"default": [("dashscope", "qwen-test")]},
                           call_logger=run_store.record_llm_call, max_retries=0)
    executor = ThreadedRunExecutor(run_store, adjudicator, max_concurrent=1)
    ctx = ExecutionContext.create(request_id="e2e-" + uuid.uuid4().hex[:8], user_id="e2e",
                                  acl_groups=["production"], roles=["employee"],
                                  channel="console", thread_id="t", budget=budget)
    loop = DefaultAgentLoop(make_model_fn(gateway, ctx, "default"))
    handle = executor.submit(ctx, loop, [{"role": "user", "content": "q"}],
                             registry.list_specs(ctx))
    run_id = handle.run_id
    events = [type(e).__name__ for e in handle.events()]
    executor.shutdown()
    return run_id, events


@skipif_no_agent_db
def test_scenario_policy_deny():
    """写型工具无授予 → default-deny：tool_invocation 记 denied、无审计行，run 仍完成（模型据拒答复）。"""
    from opensearch_pipeline.agent_runtime.policy import default_policy_engine
    from opensearch_pipeline.agent_runtime.registry import ToolRegistry
    reg = ToolRegistry()
    reg.register(_write_tool("u8.writeback", "low"))     # 无对应 allow 规则 → 拒
    run_id, events = _run_scenario(
        reg, default_policy_engine(), [_tc("u8_writeback", {"qty": 5}), _final("无权限，已停止")])
    try:
        assert "RunCompleted" in events
        rows = _fetch("SELECT status FROM tool_invocation WHERE run_id=%s", (run_id,))
        assert any(s == "denied" for (s,) in rows), f"应有 denied 调用，实得 {rows}"
        # deny 也入合规审计（深度审查 E 组：此前只记 ALLOW authorized，deny 裁决零审计）
        deny_rows = _fetch(
            "SELECT decision FROM agent_audit_log WHERE run_id=%s", (run_id,))
        assert any(d == "denied" for (d,) in deny_rows), f"deny 裁决应有审计行，实得 {deny_rows}"
    finally:
        _cleanup_run(run_id)


@skipif_no_agent_db
def test_scenario_tool_failure(monkeypatch):
    """工具执行抛错 → tool_invocation 记 failed，run 优雅完成；write-ahead 审计仍在。"""
    from opensearch_pipeline.agent_runtime.policy import default_policy_engine
    from opensearch_pipeline.agent_tools import build_default_registry

    def _boom(query, *, top_k=None, user_dept=None, **k):
        raise RuntimeError("HA3 连接失败")
    monkeypatch.setattr("opensearch_pipeline.retriever.retrieve_and_enrich", _boom)

    run_id, events = _run_scenario(
        build_default_registry(), default_policy_engine(),
        [_tc("knowledge_search", {"query": "x"}), _final("检索失败，无法作答")])
    try:
        assert "RunCompleted" in events
        rows = _fetch("SELECT status FROM tool_invocation WHERE run_id=%s", (run_id,))
        assert any(s == "failed" for (s,) in rows), f"应有 failed 调用，实得 {rows}"
        assert _fetch("SELECT COUNT(*) FROM agent_audit_log WHERE run_id=%s", (run_id,))[0][0] >= 1
    finally:
        _cleanup_run(run_id)


@skipif_no_agent_db
def test_scenario_budget_fail_closed(monkeypatch):
    """max_tool_calls=1、模型连提 2 工具 → 第 2 个执行前 fail-closed，run failed。"""
    from opensearch_pipeline.agent_runtime.context import RunBudget
    from opensearch_pipeline.agent_runtime.policy import default_policy_engine
    from opensearch_pipeline.agent_tools import build_default_registry

    def _ok(query, *, top_k=None, user_dept=None, **k):
        return [{"doc_id": "D", "chunk_text": "x", "doc_title": "t"}]
    monkeypatch.setattr("opensearch_pipeline.retriever.retrieve_and_enrich", _ok)

    run_id, events = _run_scenario(
        build_default_registry(), default_policy_engine(),
        [_tc("knowledge_search", {"query": "a"}, "c1"),
         _tc("knowledge_search", {"query": "b"}, "c2")],   # 连提、永不 final
        budget=RunBudget(max_turns=8, max_tool_calls=1))
    try:
        assert "RunFailed" in events
        st = _fetch("SELECT status, tool_calls_used FROM agent_run WHERE run_id=%s", (run_id,))[0]
        assert st[0] == "failed" and st[1] == 2       # 消费到 2（>1）才拦
    finally:
        _cleanup_run(run_id)


@skipif_no_agent_db
def test_scenario_require_approval_pending():
    """HIGH_WRITE 且被授予 → require_approval：tool_invocation 记 pending_approval（WS3 真挂起后续）。"""
    from opensearch_pipeline.agent_runtime.policy import PolicyEngine, PolicyRule
    from opensearch_pipeline.agent_runtime.registry import ToolRegistry
    reg = ToolRegistry()
    reg.register(_write_tool("u8.writeback", "high"))
    policy = PolicyEngine([PolicyRule(effect="allow", scopes=("u8.writeback",), policy_id="test.grant")])
    run_id, events = _run_scenario(reg, policy, [_tc("u8_writeback", {"qty": 5}), _final("已提交审批")])
    try:
        rows = _fetch("SELECT status FROM tool_invocation WHERE run_id=%s", (run_id,))
        assert any(s == "pending_approval" for (s,) in rows), f"应有 pending_approval，实得 {rows}"
    finally:
        _cleanup_run(run_id)


@skipif_no_agent_db
def test_scenario_idempotency_short_circuit():
    """key_required 工具同 idempotency_key 二次执行 → 短路复用回执、**不重复副作用**、不二次落 invocation
    （重放/重试/resume/崩溃恢复的写安全兜底；短路在 record_invocation 之前，故不撞 uk_tool_idem）。"""
    from opensearch_pipeline.agent_runtime.context import ExecutionContext
    from opensearch_pipeline.agent_runtime.run_store import AgentStep, RDSRunStore
    from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec
    from opensearch_pipeline.agent_runtime.tool_executor import ToolExecutor

    counter = {"n": 0}

    class _CountingTool:
        spec = ToolSpec(
            name="u8_writeback", version="1.0", description="计数写回工具",
            input_schema={"type": "object", "additionalProperties": False,
                          "properties": {"qty": {"type": "integer"}}},
            output_schema={"type": "object"}, risk_level=RiskLevel.LOW_WRITE,
            permission_scope="u8.writeback", data_classification="internal",
            idempotency="key_required")

        def run(self, ctx, args, idempotency_key=None):
            counter["n"] += 1                       # 真实副作用
            return ToolResult.ok(receipt={"executed_times": counter["n"], "qty": args.get("qty")})

    run_store = RDSRunStore()
    ctx = ExecutionContext.create(request_id="e2e-idem", user_id="e2e", acl_groups=["production"],
                                  roles=["employee"], channel="console", thread_id="t")
    run_id = run_store.create_run(ctx, "default")
    try:
        ex = ToolExecutor(run_store)
        idem = f"{run_id}:1"                         # 同一逻辑操作的幂等键（真实来自 run_id:step_no）
        s1 = run_store.append_step(run_id, AgentStep(kind="tool_call", payload={}))
        r1 = ex.execute(ctx, _CountingTool(), {"qty": 5}, run_id=run_id, step_no=s1,
                        policy_decision="allow", policy_id="test", idempotency_key=idem)
        # 二次执行同 key（模拟 resume/重试/崩溃恢复重放）
        s2 = run_store.append_step(run_id, AgentStep(kind="tool_call", payload={}))
        r2 = ex.execute(ctx, _CountingTool(), {"qty": 5}, run_id=run_id, step_no=s2,
                        policy_decision="allow", policy_id="test", idempotency_key=idem)

        assert r1.status == "succeeded" and r2.status == "succeeded"
        assert counter["n"] == 1, f"第二次应短路不再执行，副作用计数应为 1，实得 {counter['n']}"
        assert r2.receipt == {"executed_times": 1, "qty": 5}, "第二次应复用首次回执"
        # 短路在 record_invocation 之前 → 只落 1 条 invocation（不撞 uk_tool_idem）
        n_inv = _fetch("SELECT COUNT(*) FROM tool_invocation WHERE run_id=%s", (run_id,))[0][0]
        assert n_inv == 1, f"短路不应二次落 invocation，实得 {n_inv}"
    finally:
        _cleanup_run(run_id)


# ─────────────────────────────────────────────────────────────────────────────
# WS3 审批真挂：require_approval → suspend(checkpoint,run=suspended) → resume(两步 CAS)
# → approve(续跑执行被批工具) / reject-feedback(不执行、理由续跑) / terminate(cancelled)
# ─────────────────────────────────────────────────────────────────────────────
def _counting_write_tool(counter):
    from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec

    class _T:
        spec = ToolSpec(
            name="u8_writeback", version="1.0", description="计数写回工具",
            input_schema={"type": "object", "additionalProperties": False,
                          "properties": {"qty": {"type": "integer"}}},
            output_schema={"type": "object"}, risk_level=RiskLevel.HIGH_WRITE,
            permission_scope="u8.writeback", data_classification="internal",
            idempotency="key_required")

        def run(self, ctx, args, idempotency_key=None):
            counter["n"] += 1                       # 真实副作用（批准才发生）
            return ToolResult.text_ok(f"已写回 qty={args.get('qty')}")

    return _T()


def _ws3_registry(counter):
    from opensearch_pipeline.agent_runtime.registry import ToolRegistry
    reg = ToolRegistry()
    reg.register(_counting_write_tool(counter))
    return reg


def _grant_writeback_policy():
    from opensearch_pipeline.agent_runtime.policy import PolicyEngine, PolicyRule
    return PolicyEngine([PolicyRule(effect="allow", scopes=("u8.writeback",), policy_id="grant")])


def _ws3_ctx():
    from opensearch_pipeline.agent_runtime import ExecutionContext
    return ExecutionContext.create(request_id="e2e-" + uuid.uuid4().hex[:8], user_id="e2e",
                                   acl_groups=["production"], roles=["employee"],
                                   channel="console", thread_id="t")


def _ws3_loop(gateway, ctx):
    from opensearch_pipeline.agent_runtime import DefaultAgentLoop, make_model_fn
    return DefaultAgentLoop(make_model_fn(gateway, ctx, "default"))


def _ws3_runtime(registry, policy, responses):
    """WS3 共享 runtime：run_store + approvals(executor↔adjudicator 同引用) + gateway(脚本 provider)。"""
    from opensearch_pipeline.agent_runtime import ModelGateway, ThreadedRunExecutor, make_adjudicator
    from opensearch_pipeline.agent_runtime.audit import RDSAuditLog
    from opensearch_pipeline.agent_runtime.run_store import RDSRunStore
    run_store = RDSRunStore()
    approvals = {}
    adjudicator = make_adjudicator(registry, policy, run_store, audit=RDSAuditLog(), approvals=approvals)
    executor = ThreadedRunExecutor(run_store, adjudicator, max_concurrent=2, approvals=approvals)
    gateway = ModelGateway({"dashscope": _SeqProvider(responses)},
                           routes={"default": [("dashscope", "qwen-test")]},
                           call_logger=run_store.record_llm_call, max_retries=0)
    return executor, gateway


def _ws3_suspend(executor, gateway, registry):
    """submit 一个提 HIGH_WRITE 的 run，跑到挂起，返回 (run_id, [event 名])。"""
    ctx = _ws3_ctx()
    handle = executor.submit(ctx, _ws3_loop(gateway, ctx),
                             [{"role": "user", "content": "写回 7 件"}], registry.list_specs(ctx))
    return handle.run_id, [type(e).__name__ for e in handle.events()]


@skipif_no_agent_db
def test_ws3_suspend_then_approve_executes():
    """require_approval → 真挂起(checkpoint,run=suspended) → 批准 → 续跑执行被批工具 → 完成。"""
    from opensearch_pipeline.agent_runtime.approval import Approved
    counter = {"n": 0}
    reg = _ws3_registry(counter)
    executor, gateway = _ws3_runtime(reg, _grant_writeback_policy(),
                                     [_tc("u8_writeback", {"qty": 7}), _final("已完成写回")])
    run_id = None
    try:
        run_id, ev1 = _ws3_suspend(executor, gateway, reg)
        assert "RunSuspended" in ev1
        assert _fetch("SELECT status FROM agent_run WHERE run_id=%s", (run_id,))[0][0] == "suspended"
        assert _fetch("SELECT COUNT(*) FROM agent_checkpoint WHERE run_id=%s", (run_id,))[0][0] >= 1
        assert counter["n"] == 0                            # 挂起时工具尚未执行
        assert _fetch("SELECT COUNT(*) FROM agent_step WHERE run_id=%s AND kind='approval'",
                      (run_id,))[0][0] >= 1                  # 审批点入步骤序
        # 批准 → 续跑执行
        ctx2 = _ws3_ctx()
        h2 = executor.resume(run_id, ctx2, Approved(), _ws3_loop(gateway, ctx2), reg.list_specs(ctx2))
        ev2 = [type(e).__name__ for e in h2.events()]
        assert "RunCompleted" in ev2
        assert counter["n"] == 1                            # 批准后执行一次
        assert _fetch("SELECT status FROM agent_run WHERE run_id=%s", (run_id,))[0][0] == "succeeded"
        inv = {s for (s,) in _fetch("SELECT status FROM tool_invocation WHERE run_id=%s", (run_id,))}
        assert "pending_approval" in inv and "succeeded" in inv   # 挂起记 pending + 批准执行 succeeded
    finally:
        executor.shutdown()
        if run_id:
            _cleanup_run(run_id)


@skipif_no_agent_db
def test_ws3_suspend_then_reject_feedback_not_executed():
    """挂起 → 拒绝(反馈理由) → 工具从不执行、理由回喂模型续跑 → 完成。"""
    from opensearch_pipeline.agent_runtime.approval import RejectedFeedback
    counter = {"n": 0}
    reg = _ws3_registry(counter)
    executor, gateway = _ws3_runtime(reg, _grant_writeback_policy(),
                                     [_tc("u8_writeback", {"qty": 7}), _final("好的，改用别的方式")])
    run_id = None
    try:
        run_id, ev1 = _ws3_suspend(executor, gateway, reg)
        assert "RunSuspended" in ev1
        ctx2 = _ws3_ctx()
        h2 = executor.resume(run_id, ctx2, RejectedFeedback(reason="金额过大，暂不执行"),
                             _ws3_loop(gateway, ctx2), reg.list_specs(ctx2))
        ev2 = [type(e).__name__ for e in h2.events()]
        assert "RunCompleted" in ev2
        assert counter["n"] == 0                            # 拒绝 → 工具从不执行
        assert _fetch("SELECT status FROM agent_run WHERE run_id=%s", (run_id,))[0][0] == "succeeded"
    finally:
        executor.shutdown()
        if run_id:
            _cleanup_run(run_id)


@skipif_no_agent_db
def test_ws3_suspend_then_terminate_cancels():
    """挂起 → 硬终止 → run cancelled、工具不执行。"""
    from opensearch_pipeline.agent_runtime.approval import RejectedTerminate
    counter = {"n": 0}
    reg = _ws3_registry(counter)
    executor, gateway = _ws3_runtime(reg, _grant_writeback_policy(),
                                     [_tc("u8_writeback", {"qty": 7}), _final("x")])
    run_id = None
    try:
        run_id, ev1 = _ws3_suspend(executor, gateway, reg)
        assert "RunSuspended" in ev1
        ctx2 = _ws3_ctx()
        h2 = executor.resume(run_id, ctx2, RejectedTerminate(), _ws3_loop(gateway, ctx2),
                             reg.list_specs(ctx2))
        assert "RunFailed" in [type(e).__name__ for e in h2.events()]
        assert counter["n"] == 0
        assert _fetch("SELECT status FROM agent_run WHERE run_id=%s", (run_id,))[0][0] == "cancelled"
    finally:
        executor.shutdown()
        if run_id:
            _cleanup_run(run_id)
