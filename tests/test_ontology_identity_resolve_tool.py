# -*- coding: utf-8 -*-
"""门 A：ontology_identity_resolve 工具 + approval_store approver_scope seam（PR8）。

锁死的不变量：
- approval_policy="always" → Policy 风险基线把任何授予上收 require_approval（提案必挂起）；
- 同 (ns,norm,target,revision) 重放幂等；指向其它目标 → 拒绝导流工作台（绝不静默覆盖）；
- 审批路由走 per-attr steward（seam）；scope 未登记/解析异常 → '' 仅 kb_admin（fail-closed）；
- 未注册解析器的工具走 derive_approver_scope（既有行为零变化）；
- 未接线守护：不在 build_default_registry。
"""
import pytest

import opensearch_pipeline.agent_runtime.approval_store as approval_store
from opensearch_pipeline.agent_runtime.approval_store import (
    RDSApprovalStore,
    _resolve_scope,
    register_approver_scope_resolver,
)
from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.policy import (
    PolicyEngine,
    PolicyRule,
    default_policy_engine,
)
from opensearch_pipeline.agent_runtime.tool import RiskLevel
from opensearch_pipeline.agent_tools.ontology_identity_resolve import (
    OntologyIdentityResolveTool,
)
from opensearch_pipeline.ontology import stewardship
from opensearch_pipeline.ontology.store import MemoryOntologyStore


def _ctx(acl=("pmc",), user="u1"):
    """P0-B 后发起人须对目标对象可读（_target 缺省 owner_dept='pmc' internal）——
    缺省 acl 给 pmc；专测审批路由/scope 推导的用例仍显式传 marketing。"""
    return ExecutionContext.create(request_id="", user_id=user, acl_groups=list(acl),
                                   roles=("employee",), channel="console", thread_id="t1")


@pytest.fixture(autouse=True)
def _fresh_resolver_registry(monkeypatch):
    """seam 注册表逐测隔离（工具 __init__ 全局注册，测试间不得串味）。"""
    monkeypatch.setattr(approval_store, "_SCOPE_RESOLVERS", {})


@pytest.fixture()
def store():
    mem = MemoryOntologyStore()
    stewardship.ensure_seeds(mem)
    return mem


@pytest.fixture()
def tool(store):
    return OntologyIdentityResolveTool(store=store)


def _target(store, title="6.2口径龙虾杯", otype="product", **kw):
    return store.mint_object(otype, title, owner_dept=kw.pop("owner_dept", "pmc"), **kw, _caller="test")


# ── spec / Policy 契约 ────────────────────────────────────────────────────────────


def test_spec_contract(tool):
    spec = tool.spec
    # PR-C（P0-06）：身份映射改变检索与计算依据——HIGH_WRITE（审计 fail-closed）
    assert spec.risk_level == RiskLevel.HIGH_WRITE
    assert spec.approval_policy == "always"
    assert spec.idempotency == "key_required" and spec.side_effects is True
    assert spec.permission_scope == "ontology.identity.resolve"
    assert spec.input_schema["additionalProperties"] is False
    for forged in ("confirmed_by", "confidence", "intent", "user_dept"):
        assert forged not in spec.input_schema["properties"]


def test_policy_always_escalates_grant_to_approval(tool):
    engine = PolicyEngine([PolicyRule(effect="allow",
                                      scopes=("ontology.identity.resolve",), policy_id="g1")])
    d = engine.authorize_tool_call(_ctx(), tool.spec, {})
    assert d.decision == "require_approval" and d.policy_id == "risk_baseline"


def test_default_policy_engine_denies_unwired(tool, monkeypatch):
    """默认（flag off）=未授予：default_policy_engine 对本 scope default-deny。"""
    monkeypatch.delenv("RAG_ONTOLOGY_TOOLS_ENABLE", raising=False)
    d = default_policy_engine().authorize_tool_call(_ctx(), tool.spec, {})
    assert d.decision == "deny"


def test_policy_flag_on_grants_but_still_requires_approval(tool, monkeypatch):
    """PR13：flag 开 → 授予（可提案）但 HIGH_WRITE + approval_policy=always 结构性
    require_approval——绝无免批执行路径（门 A 语义不因接线而松动）。"""
    monkeypatch.setenv("RAG_ONTOLOGY_TOOLS_ENABLE", "true")
    d = default_policy_engine().authorize_tool_call(_ctx(), tool.spec, {})
    assert d.decision == "require_approval"


def test_tool_not_wired_into_default_registry(monkeypatch):
    monkeypatch.delenv("RAG_ONTOLOGY_TOOLS_ENABLE", raising=False)
    from opensearch_pipeline.agent_tools import build_default_registry
    names = {spec.name for spec in build_default_registry().list_specs()}
    assert "ontology_identity_resolve" not in names


# ── run：写语义 ───────────────────────────────────────────────────────────────────


def test_confirm_happy_path_and_case_closure(tool, store):
    obj = _target(store)
    case_id = store.upsert_case("u8", "abc123", "ABC123", object_type_hint="product")
    res = tool.run(_ctx(user="steward_1"),
                   {"namespace": "u8", "value": "abc123",
                    "target_object_id": obj["object_id"], "note": "打样一致"})
    assert res.status == "succeeded"
    alias = store.get_active_identifier("u8", "ABC123")
    assert alias["target_object_id"] == obj["object_id"]
    assert alias["resolution_method"] == "manual" and alias["confirmed_by"] == "steward_1"
    assert res.receipt["idempotent"] is False
    assert res.receipt["closed_case_id"] == case_id                 # open case 顺带闭环
    assert store.get_case(case_id)["status"] == "resolved"
    assert "已确认" in res.content[0].text


def test_confirm_with_revision(tool, store):
    obj = _target(store)
    res = tool.run(_ctx(), {"namespace": "u8", "value": "abc123-m",
                            "target_object_id": obj["object_id"], "target_revision": "r2"})
    assert res.status == "succeeded"
    assert store.get_active_identifier("u8", "ABC123-M")["target_revision"] == "r2"


def test_idempotent_replay_same_target(tool, store):
    obj = _target(store)
    args = {"namespace": "u8", "value": "abc123", "target_object_id": obj["object_id"]}
    first = tool.run(_ctx(), args)
    second = tool.run(_ctx(), args)
    assert second.status == "succeeded" and second.receipt["idempotent"] is True
    assert second.receipt["identifier_id"] == first.receipt["identifier_id"]
    assert len(store.list_identifiers_for_target(obj["object_id"])) == 1   # 零重复副作用


def test_conflict_other_target_refused(tool, store):
    a, b = _target(store), _target(store, title="别的杯")
    tool.run(_ctx(), {"namespace": "u8", "value": "abc123", "target_object_id": a["object_id"]})
    res = tool.run(_ctx(), {"namespace": "u8", "value": "abc123",
                            "target_object_id": b["object_id"]})
    assert res.status == "failed" and "工作台" in (res.error or "")
    assert store.get_active_identifier("u8", "ABC123")["target_object_id"] == a["object_id"]


def test_conflict_same_target_different_revision(tool, store):
    obj = _target(store)
    tool.run(_ctx(), {"namespace": "u8", "value": "abc123",
                      "target_object_id": obj["object_id"], "target_revision": "r1"})
    res = tool.run(_ctx(), {"namespace": "u8", "value": "abc123",
                            "target_object_id": obj["object_id"], "target_revision": "r2"})
    assert res.status == "failed" and "工作台" in (res.error or "")


def test_target_missing_or_inactive(tool, store):
    assert tool.run(_ctx(), {"namespace": "u8", "value": "x",
                             "target_object_id": "ghost"}).status == "failed"
    retired = _target(store, title="退役品")
    store.retire_object(retired["object_id"])
    res = tool.run(_ctx(), {"namespace": "u8", "value": "x",
                            "target_object_id": retired["object_id"]})
    assert res.status == "failed" and "active" in (res.error or "")


def test_bad_value_and_forged_params(tool, store):
    obj = _target(store)
    res = tool.run(_ctx(), {"namespace": "u8", "value": "   ",
                            "target_object_id": obj["object_id"]})
    assert res.status == "failed" and "非法" in (res.error or "")
    with pytest.raises(Exception):
        tool.run(_ctx(), {"namespace": "u8", "value": "a",
                          "target_object_id": obj["object_id"], "confirmed_by": "hacker"})


def test_race_duplicate_resolves_to_idempotent(tool, store, monkeypatch):
    """插入撞 uk（并发赢家先落同目标）→ 重查归入幂等成功。"""
    from opensearch_pipeline.ontology.store import DuplicateActiveIdentifier
    obj = _target(store)
    real_get = store.get_active_identifier
    state = {"first": True}

    def _get(ns, norm):
        if state["first"]:
            state["first"] = False
            return None                     # 预检未见 → 走 insert
        return real_get(ns, norm)

    monkeypatch.setattr(store, "get_active_identifier", _get)

    def _insert(*a, **k):
        state["first"] = False
        # 并发赢家：直接用真方法落同目标，然后本次插入撞 uk
        real_insert(*a, **k)
        raise DuplicateActiveIdentifier("race")

    real_insert = MemoryOntologyStore.insert_identifier_closing_case.__get__(store)
    monkeypatch.setattr(store, "insert_identifier_closing_case", _insert)
    res = tool.run(_ctx(), {"namespace": "u8", "value": "abc123",
                            "target_object_id": obj["object_id"]})
    assert res.status == "succeeded" and res.receipt["idempotent"] is True


def test_case_closure_atomic_with_identifier(tool, store, monkeypatch):
    """PR-C（P0-05）：闭 case 与铸别名**同一事务**——存储层任何失败整体回滚，
    正式 identifier 与治理 case 不再可分叉（旧 fail-open 契约按外审废除）。"""
    obj = _target(store)
    store.upsert_case("u8", "abc123", "ABC123")
    # 正常路径：一次调用同时铸别名 + 闭 case
    res = tool.run(_ctx(), {"namespace": "u8", "value": "abc123",
                            "target_object_id": obj["object_id"]})
    assert res.status == "succeeded"
    assert res.receipt["closed_case_id"] is not None
    assert store.get_open_case("u8", "ABC123") is None
    # 故障路径：复合写失败 → 工具失败且零副作用（无半状态）
    store2 = MemoryOntologyStore()
    obj2 = store2.mint_object("product", "另一目标", owner_dept="pmc", _caller="test")
    store2.upsert_case("u8", "zz9", "ZZ9")
    monkeypatch.setattr(store2, "insert_identifier_closing_case",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    tool2 = OntologyIdentityResolveTool(store=store2)
    res2 = tool2.run(_ctx(), {"namespace": "u8", "value": "zz9",
                              "target_object_id": obj2["object_id"]})
    assert res2.status == "failed"
    assert store2.get_active_identifier("u8", "ZZ9") is None
    assert store2.get_open_case("u8", "ZZ9") is not None   # case 仍开着，零分叉


def test_confidential_unreadable_target_denied_as_not_found(store):
    """P0-B（外审动态探针场景）：requester 只有 pmc ACL、目标 supply/confidential →
    工具拒绝且与"目标不存在"**同答**（旧行为：掩码标题后照样铸别名成功=跨 ACL 写洞）。"""
    mat = store.mint_object("material", "PP-1100 特惠采购价目", owner_dept="supply",
                            data_classification="confidential", _caller="test")
    tool = OntologyIdentityResolveTool(store=store)
    res = tool.run(_ctx(acl=("pmc",)), {"namespace": "material_grade", "value": "pp-1100",
                                        "target_object_id": mat["object_id"]})
    ghost = tool.run(_ctx(acl=("pmc",)), {"namespace": "material_grade", "value": "pp-1100",
                                          "target_object_id": "0" * 26})
    assert res.status == "failed" and res.error == ghost.error   # 不可见 == 不存在
    assert "特惠采购价目" not in (res.error or "")
    assert mat["canonical_ref"] not in (res.error or "")
    assert store.get_active_identifier("material_grade", "PP-1100") is None   # 零副作用
    # 有权 requester（supply）正常成功，标题原样
    ok = tool.run(_ctx(acl=("supply",)), {"namespace": "material_grade", "value": "pp-1100",
                                          "target_object_id": mat["object_id"]})
    assert ok.status == "succeeded" and "特惠采购价目" in ok.content[0].text


# ── approver_scope seam ──────────────────────────────────────────────────────────


def test_resolve_scope_default_without_resolver():
    """未注册解析器：默认推导=发起人主属部门（既有行为零变化）。"""
    assert _resolve_scope("some_tool", _ctx(acl=("marketing",)), {}) == "marketing"
    assert _resolve_scope("some_tool", _ctx(acl=("public",)), {}) == ""


def test_resolve_scope_with_resolver_and_failures():
    register_approver_scope_resolver("t1", lambda ctx, args: "pmc")
    assert _resolve_scope("t1", _ctx(acl=("marketing",)), {}) == "pmc"   # 覆盖发起人部门
    register_approver_scope_resolver("t1", lambda ctx, args: None)
    assert _resolve_scope("t1", _ctx(acl=("marketing",)), {}) == ""      # 未登记→仅 kb_admin
    register_approver_scope_resolver(
        "t1", lambda ctx, args: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _resolve_scope("t1", _ctx(acl=("marketing",)), {}) == ""      # 异常 fail-closed


def test_tool_registers_steward_scope_resolver(tool, store):
    """工具构造即注册：product 目标 → steward CSV "pmc,rd"（种子含 backup，P1-11）；
    未登记域 → None→''。发起人 acl 须覆盖目标（P0-B propose 侧同闸），主属部门仍是
    marketing——路由到 steward 而非发起人部门。"""
    obj = _target(store)
    scope = _resolve_scope("ontology_identity_resolve", _ctx(acl=("marketing", "pmc")),
                           {"namespace": "u8", "target_object_id": obj["object_id"]})
    assert scope == "pmc,rd"                                 # steward+backup CSV，非发起人部门
    scope2 = _resolve_scope("ontology_identity_resolve", _ctx(acl=("marketing",)),
                            {"namespace": "lot_code", "target_object_id": "ghost"})
    assert scope2 == ""                                      # 对象不存在+域未登记 → 仅 kb_admin


class _CapturingCur:
    def __init__(self, sink):
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append((sql, params))


class _CapturingConn:
    def __init__(self, sink):
        self._sink = sink

    def cursor(self):
        return _CapturingCur(self._sink)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_create_request_routes_scope_and_keeps_requested_dept(monkeypatch, tool, store):
    """集成：create_request 的 approver_scope 走 seam（steward CSV），requested_dept
    仍是发起人主属部门（acl 第二组 pmc 只为过 P0-B propose 闸）。"""
    sink = []
    monkeypatch.setattr(RDSApprovalStore, "_conn", lambda self: _CapturingConn(sink))
    obj = _target(store)
    RDSApprovalStore().create_request(
        "run1", _ctx(acl=("marketing", "pmc"), user="u9"),
        {"call_id": "c1", "tool_name": "ontology_identity_resolve",
         "arguments": {"namespace": "u8", "value": "abc123",
                       "target_object_id": obj["object_id"]}})
    params = sink[-1][1]
    assert params[9] == "marketing"                          # requested_dept=发起人主属部门
    assert params[10] == "pmc,rd"                            # approver_scope=steward+backup CSV
    # 未注册工具：两者同源（默认推导），行为与 seam 前一致
    sink.clear()
    RDSApprovalStore().create_request(
        "run2", _ctx(acl=("marketing",)),
        {"call_id": "c1", "tool_name": "u8_writeback", "arguments": {"qty": 1}})
    params = sink[-1][1]
    assert params[9] == "marketing" and params[10] == "marketing"


# ── PR-C（P0-06）：approval 回链 + 落库前重验 ──────────────────────────────────


def test_approval_chain_lands_on_identifier(store):
    """ctx 携带审批事实（adjudicator 注入）→ identifier 行落 approval_request_id、
    confirmed_by=真实审批人；receipt 双向可回放。"""
    from dataclasses import replace
    obj = _target(store)
    ctx = replace(_ctx(), approval_request_id="req_" + "a" * 28,
                  approved_by="steward_9", approval_scope="pmc,rd")   # P1-11：CSV 含 backup
    tool = OntologyIdentityResolveTool(store=store)
    res = tool.run(ctx, {"namespace": "u8", "value": "abc123",
                         "target_object_id": obj["object_id"]})
    assert res.status == "succeeded"
    row = store.get_active_identifier("u8", "ABC123")
    assert row["approval_request_id"] == "req_" + "a" * 28
    assert row["confirmed_by"] == "steward_9"           # 审批人，非发起人
    assert res.receipt["requested_by"] == ctx.user_id
    assert res.receipt["approved_by"] == "steward_9"


def test_expected_version_pin_rejects_drift(store):
    """提案钉 expected_version → 审批期间对象变更（version 自增）→ 拒绝执行。"""
    obj = _target(store)
    tool = OntologyIdentityResolveTool(store=store)
    store.update_golden(obj["object_id"], {"改": "了"}, expected_version=1, allow_missing_provenance=True)   # version→2
    res = tool.run(_ctx(), {"namespace": "u8", "value": "abc123",
                            "target_object_id": obj["object_id"],
                            "expected_version": 1})
    assert res.status == "failed" and "重新发起提案" in (res.error or "")
    assert store.get_active_identifier("u8", "ABC123") is None


def test_scope_drift_rejects_execution(store):
    """审批时 scope 与执行时现算不一致（stewardship 变更）→ 拒绝执行。"""
    from dataclasses import replace

    from opensearch_pipeline.ontology import stewardship
    stewardship.ensure_seeds(store)                      # product→pmc（backup rd）
    obj = _target(store)
    ctx = replace(_ctx(), approval_request_id="req_" + "b" * 28,
                  approved_by="steward_9", approval_scope="pmc,rd")
    # 审批后 steward 换防：product scope 改归 supply
    store.upsert_stewardship([{"scope_type": "object_type", "scope_key": "product",
                               "steward_dept": "supply"}])
    tool = OntologyIdentityResolveTool(store=store)
    res = tool.run(ctx, {"namespace": "u8", "value": "abc123",
                         "target_object_id": obj["object_id"]})
    assert res.status == "failed" and "stewardship 已变更" in (res.error or "")
    assert store.get_active_identifier("u8", "ABC123") is None


def test_adjudicator_injects_approval_into_ctx(store):
    """adjudicator 消费 ApprovalGrant → ctx 注入审批事实 → 工具落回链（端到端最短链）。"""
    from opensearch_pipeline.agent_runtime.approval import Approved, ApprovalGrant
    from opensearch_pipeline.agent_runtime.events import ToolCallProposed, Usage
    from opensearch_pipeline.agent_runtime.policy import make_adjudicator, PolicyEngine, PolicyRule
    from opensearch_pipeline.agent_runtime.registry import ToolRegistry
    from opensearch_pipeline.agent_runtime.tool_executor import digest

    obj = _target(store)
    tool = OntologyIdentityResolveTool(store=store)
    registry = ToolRegistry()
    registry.register(tool)
    engine = PolicyEngine([PolicyRule(effect="allow",
                                      scopes=("ontology.identity.resolve",), policy_id="g1")])

    class _NullRunStore:
        def append_step(self, *a, **k):
            return 1

        def record_invocation(self, *a, **k):
            return "inv1"

        def find_succeeded_invocation(self, *a, **k):
            return None

        def finish_invocation(self, *a, **k):
            return None

    args = {"namespace": "u8", "value": "abc123", "target_object_id": obj["object_id"]}
    ctx = _ctx()
    approvals = {f"{ctx.run_id}:c1": ApprovalGrant(
        outcome=Approved(), tool_name="ontology_identity_resolve", args_digest=digest(args),
        request_id="req_" + "c" * 28, decided_by="steward_7")}
    adjudicate = make_adjudicator(registry, engine, _NullRunStore(), approvals=approvals)
    ev = ToolCallProposed(call_id="c1", tool_name="ontology_identity_resolve",
                          arguments=args, turn_index=0, usage=Usage())
    result = adjudicate(ctx, ev)
    assert result.status == "succeeded"
    row = store.get_active_identifier("u8", "ABC123")
    assert row["approval_request_id"] == "req_" + "c" * 28
    assert row["confirmed_by"] == "steward_7"
