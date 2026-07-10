# -*- coding: utf-8 -*-
"""
test_agent_runtime_policy.py — PolicyEngine（deny-first）+ adjudicator（Task 14 / 报告 §5）

覆盖：default-deny · 基线放行只读 · 显式 deny 终裁 · HIGH_WRITE/always→require_approval 只减不增 ·
scope/role/channel 匹配 · obligations 汇集 · adjudicator 各路径（allow 执行 / deny / 未知 / 停用 /
schema 违规 / require_approval 挂起不执行）。
"""

from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.events import ToolCallProposed
from opensearch_pipeline.agent_runtime.policy import (
    PolicyEngine,
    PolicyRule,
    _scope_matches,
    default_policy_engine,
    make_adjudicator,
)
from opensearch_pipeline.agent_runtime.registry import ToolRegistry
from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec


def _ctx(roles=("employee",), channel="console"):
    return ExecutionContext.create(request_id="r", user_id="u", acl_groups=["g"],
                                   roles=list(roles), channel=channel, thread_id="t")


def _spec(scope="kb.search", risk=RiskLevel.READ_ONLY, approval_policy=None, name="t",
          input_schema=None):
    return ToolSpec(name=name, version="1.0.0", description="d",
                    input_schema=input_schema or {"type": "object"},
                    output_schema={"type": "object"}, risk_level=risk, permission_scope=scope,
                    data_classification="internal", approval_policy=approval_policy,
                    idempotency="key_required" if risk is not RiskLevel.READ_ONLY else "none")


# ── PolicyEngine ────────────────────────────────────────────────
def test_default_deny_empty_engine():
    d = PolicyEngine().authorize_tool_call(_ctx(), _spec(), {})
    assert d.decision == "deny" and d.policy_id == "default_deny"


def test_default_engine_allows_kb_search():
    d = default_policy_engine().authorize_tool_call(_ctx(), _spec("kb.search"), {})
    assert d.decision == "allow"


def test_default_engine_denies_write_by_default():
    d = default_policy_engine().authorize_tool_call(
        _ctx(), _spec("u8.writeback", RiskLevel.HIGH_WRITE), {})
    assert d.decision == "deny"      # 写型无默认授予 → default-deny


def test_explicit_deny_wins_over_allow():
    eng = PolicyEngine([PolicyRule(effect="allow", scopes=("kb.search",)),
                        PolicyRule(effect="deny", scopes=("kb.search",), policy_id="blocklist")])
    d = eng.authorize_tool_call(_ctx(), _spec("kb.search"), {})
    assert d.decision == "deny" and d.policy_id == "blocklist"


def test_high_write_requires_approval_even_if_allowed():
    eng = PolicyEngine([PolicyRule(effect="allow", scopes=("u8.writeback",))])
    d = eng.authorize_tool_call(_ctx(), _spec("u8.writeback", RiskLevel.HIGH_WRITE), {})
    assert d.decision == "require_approval" and d.policy_id == "risk_baseline"


def test_approval_policy_always():
    eng = PolicyEngine([PolicyRule(effect="allow", scopes=("kb.search",))])
    d = eng.authorize_tool_call(_ctx(), _spec("kb.search", approval_policy="always"), {})
    assert d.decision == "require_approval"


def test_require_approval_grant():
    eng = PolicyEngine([PolicyRule(effect="require_approval", scopes=("kb.search",), policy_id="dept.review")])
    d = eng.authorize_tool_call(_ctx(), _spec("kb.search"), {})
    assert d.decision == "require_approval" and d.policy_id == "dept.review"


def test_scope_matching():
    assert _scope_matches("*", "kb.search")
    assert _scope_matches("kb.*", "kb.search") and _scope_matches("kb.*", "kb")
    assert _scope_matches("kb.search", "kb.search")
    assert not _scope_matches("kb.search", "kb.delete")
    assert not _scope_matches("sql.*", "kb.search")


def test_role_gated_rule():
    eng = PolicyEngine([PolicyRule(effect="allow", scopes=("admin.op",), roles=("agent_admin",))])
    assert eng.authorize_tool_call(_ctx(roles=("employee",)), _spec("admin.op"), {}).decision == "deny"
    assert eng.authorize_tool_call(_ctx(roles=("agent_admin",)), _spec("admin.op"), {}).decision == "allow"


def test_channel_gated_rule():
    eng = PolicyEngine([PolicyRule(effect="allow", scopes=("kb.search",), channels=("console",))])
    assert eng.authorize_tool_call(_ctx(channel="console"), _spec("kb.search"), {}).decision == "allow"
    assert eng.authorize_tool_call(_ctx(channel="dingtalk"), _spec("kb.search"), {}).decision == "deny"


def test_obligations_aggregated():
    eng = PolicyEngine([PolicyRule(effect="allow", scopes=("kb.search",),
                                   obligations=("mask:phone", "limit:100"))])
    d = eng.authorize_tool_call(_ctx(), _spec("kb.search"), {})
    assert d.decision == "allow" and set(d.obligations) == {"mask:phone", "limit:100"}


# ── adjudicator（策略执行点 + tool_invocation 落库）──────────────
class _FakeTool:
    def __init__(self, spec, result=None):
        self.spec = spec
        self._result = result or ToolResult.text_ok("执行结果")
        self.ran = False

    def run(self, ctx, args, idempotency_key=None):
        self.ran = True
        return self._result


class _FakeStore:
    """记录 append_step / tool_invocation 调用，供断言 trace 落库。"""

    def __init__(self):
        self._step = 0
        self.invocations = []

    def append_step(self, run_id, step):
        self._step += 1
        return self._step

    def record_invocation(self, run_id, step_no, **kw):
        iid = f"inv{len(self.invocations) + 1}"
        self.invocations.append({"id": iid, "step_no": step_no, **kw})
        return iid

    def finish_invocation(self, invocation_id, **kw):
        for inv in self.invocations:
            if inv["id"] == invocation_id:
                inv.update(kw)

    def find_succeeded_invocation(self, tool_name, idempotency_key):
        return None


_KS_SCHEMA = {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"], "additionalProperties": False}


def _tcp(name="knowledge_search", args=None):
    return ToolCallProposed(call_id="c1", tool_name=name, arguments=args or {"query": "x"})


def _adj(reg, policy=None):
    store = _FakeStore()
    return make_adjudicator(reg, policy or default_policy_engine(), store), store


def test_adjudicator_allow_executes_and_records():
    reg = ToolRegistry()
    tool = _FakeTool(_spec("kb.search", name="knowledge_search", input_schema=_KS_SCHEMA))
    reg.register(tool)
    adj, store = _adj(reg)
    res = adj(_ctx(), _tcp())
    assert res.status == "succeeded" and tool.ran
    assert store.invocations and store.invocations[0]["status"] == "succeeded"   # executing→succeeded


def test_adjudicator_unknown_tool_fails():
    adj, _ = _adj(ToolRegistry())
    assert adj(_ctx(), _tcp("nope")).status == "failed"


def test_adjudicator_disabled_denied_and_recorded():
    reg = ToolRegistry()
    reg.register(_FakeTool(_spec("kb.search", name="knowledge_search", input_schema=_KS_SCHEMA)))
    reg.disable("knowledge_search")
    adj, store = _adj(reg)
    assert adj(_ctx(), _tcp()).status == "denied"
    assert store.invocations[0]["status"] == "denied"


def test_adjudicator_schema_violation_denied():
    reg = ToolRegistry()
    reg.register(_FakeTool(_spec("kb.search", name="knowledge_search", input_schema=_KS_SCHEMA)))
    adj, store = _adj(reg)
    res = adj(_ctx(), _tcp(args={"query": "x", "forged": "y"}))
    assert res.status == "denied" and store.invocations[0]["policy_decision"] == "schema"


def test_adjudicator_policy_deny_recorded():
    reg = ToolRegistry()
    reg.register(_FakeTool(_spec("kb.search", name="knowledge_search", input_schema=_KS_SCHEMA)))
    adj, store = _adj(reg, PolicyEngine())          # 空引擎→default-deny
    assert adj(_ctx(), _tcp()).status == "denied"
    assert store.invocations[0]["status"] == "denied"


def test_adjudicator_high_write_pending_not_executed():
    spec = _spec("u8.writeback", RiskLevel.HIGH_WRITE, approval_policy="always", name="u8_writeback")
    reg = ToolRegistry()
    tool = _FakeTool(spec)
    reg.register(tool)
    adj, store = _adj(reg, PolicyEngine([PolicyRule(effect="allow", scopes=("u8.writeback",))]))
    res = adj(_ctx(), ToolCallProposed(call_id="c", tool_name="u8_writeback", arguments={}))
    assert res.status == "pending_approval" and not tool.ran
    assert store.invocations[0]["status"] == "pending_approval"
