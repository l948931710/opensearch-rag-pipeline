# -*- coding: utf-8 -*-
"""
policy.py — PolicyEngine（deny-first 统一裁决点）+ adjudicator（策略执行点）（v2 报告 §5 模块 D）

**控制面**：Policy 裁决"能不能调"（主体 × 操作 × 资源 × 风险 → ALLOW/DENY/REQUIRE_APPROVAL）。
**数据面**：裁决"能看多少"由工具内部复用现有 ACL（knowledge_search 已把 ctx.acl_groups 注入
retrieve_and_enrich→_build_permission_filter）——两层正交，不建第二套。

deny-first 语义：
- 任一匹配规则 DENY → 终裁 DENY；
- 无任何授予（allow/require_approval）→ **default-deny**（未匹配任何 ALLOW 也是 DENY）；
- 风险基线**只减不增**：HIGH_WRITE / approval_policy="always" / 授予本身要求审批 → REQUIRE_APPROVAL
  （可把 allow 上收为 require_approval，绝不把 deny/approval 放宽为 allow）。

make_adjudicator 把"schema 校验 → Policy 裁决 → registry.resolve → tool.run"串成 executor 需要的
`Adjudicator` 回调（策略执行点 PEP）。REQUIRE_APPROVAL 的真正挂起闭环属 WS3/P2；首批只读工具
（knowledge_search）不触发，本 stub 返回 pending 占位。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Tuple

from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolSpec

if TYPE_CHECKING:
    from opensearch_pipeline.agent_runtime.context import ExecutionContext
    from opensearch_pipeline.agent_runtime.events import ToolCallProposed
    from opensearch_pipeline.agent_runtime.registry import ToolRegistry
    from opensearch_pipeline.agent_runtime.tool import ToolResult


Decision = Literal["allow", "deny", "require_approval"]


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    policy_id: str
    reason: str
    obligations: Tuple[str, ...] = ()      # 如 "mask_output:phone"、"limit_rows:1000"（执行层强制，后续）


@dataclass(frozen=True)
class PolicyRule:
    """一条策略规则。scopes/roles/channels 空或含 '*' = 不限。"""

    effect: Decision
    scopes: Tuple[str, ...] = ("*",)       # permission_scope 匹配（精确 / 前缀 "kb.*" / "*"）
    roles: Tuple[str, ...] = ()            # 空=任意角色；否则需与 ctx.roles 有交集
    channels: Tuple[str, ...] = ()         # 空=任意渠道
    obligations: Tuple[str, ...] = ()
    policy_id: str = ""


def _scope_matches(pattern: str, scope: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return scope == pattern[:-2] or scope.startswith(pattern[:-1])   # "kb.*" 配 "kb" / "kb.search"
    return pattern == scope


def _rule_matches(rule: PolicyRule, ctx: "ExecutionContext", spec: ToolSpec) -> bool:
    if not any(_scope_matches(p, spec.permission_scope) for p in rule.scopes):
        return False
    if rule.roles and not (set(rule.roles) & set(ctx.roles)):
        return False
    if rule.channels and ctx.channel not in rule.channels:
        return False
    return True


class PolicyEngine:
    """deny-first 单点裁决。规则来源分层只减不增（平台基线 → 部门 → 角色 → 会话）。"""

    def __init__(self, rules: Optional[List[PolicyRule]] = None):
        self._rules = list(rules or [])

    def authorize_tool_call(self, ctx: "ExecutionContext", spec: ToolSpec,
                            args: Dict[str, Any]) -> PolicyDecision:
        matched = [r for r in self._rules if _rule_matches(r, ctx, spec)]

        # 1) deny-first：任一显式 DENY → 终裁
        for r in matched:
            if r.effect == "deny":
                return PolicyDecision("deny", r.policy_id or "rule.deny",
                                      f"显式拒绝 {spec.permission_scope}")

        # 2) default-deny：无任何授予（allow/require_approval）→ 拒绝
        grants = [r for r in matched if r.effect in ("allow", "require_approval")]
        if not grants:
            return PolicyDecision("deny", "default_deny",
                                  f"未授予调用 {spec.permission_scope} 的权限")

        obligations = tuple(o for r in grants for o in r.obligations)

        # 3) 风险基线（只减不增）：HIGH_WRITE / always / 授予要求审批 → REQUIRE_APPROVAL
        if spec.risk_level == RiskLevel.HIGH_WRITE or spec.approval_policy == "always":
            return PolicyDecision("require_approval", "risk_baseline",
                                  f"高风险操作 {spec.permission_scope} 需人工审批", obligations)
        if any(r.effect == "require_approval" for r in grants):
            gid = next(r.policy_id for r in grants if r.effect == "require_approval")
            return PolicyDecision("require_approval", gid or "rule.require_approval",
                                  f"策略要求审批 {spec.permission_scope}", obligations)

        # 4) ALLOW
        gid = next((r.policy_id for r in grants if r.effect == "allow"), "rule.allow")
        return PolicyDecision("allow", gid, f"允许 {spec.permission_scope}", obligations)


def default_policy_engine() -> PolicyEngine:
    """首批基线：只读工具授予任意已认证用户（数据面 ACL 另行过滤）；写型无默认授予→default-deny，
    且 HIGH_WRITE 即便被授予也走 REQUIRE_APPROVAL。写型授予由部门/角色策略显式追加。"""
    return PolicyEngine([
        PolicyRule(effect="allow", scopes=("kb.search", "sql.readonly.*"),
                   policy_id="baseline.readonly"),
    ])


# ── 策略执行点（adjudicator）：executor 的 Adjudicator 回调 ──────────
def make_adjudicator(registry: "ToolRegistry", policy: PolicyEngine, run_store,
                     audit=None, approvals=None) -> Callable:
    """串起 append_step → 解析 → schema 校验 → Policy 裁决 → 执行（经 ToolExecutor 中间件）。
    全程记 tool_invocation（proposed/denied/pending/executing→result）。返回 executor 的
    Adjudicator: (ctx, ToolCallProposed) -> ToolResult。

    adjudicator 内部自取 step_no（append 一条 tool_call agent_step），故 executor 契约不变。
    audit（可选 AuditLog）透传给 ToolExecutor——执行前写合规审计（HIGH_WRITE fail-closed）。
    """
    from opensearch_pipeline.agent_runtime.registry import ToolDisabled, ToolNotFound
    from opensearch_pipeline.agent_runtime.run_store import AgentStep
    from opensearch_pipeline.agent_runtime.tool import ToolArgsError, ToolResult
    from opensearch_pipeline.agent_runtime.tool_executor import ToolExecutor, digest

    tool_executor = ToolExecutor(run_store, audit=audit)

    def _rec(ctx, step_no, ev, version, status, decision, policy_id):
        run_store.record_invocation(
            ctx.run_id, step_no, tool_name=ev.tool_name, tool_version=version,
            args_json=json.dumps(ev.arguments, ensure_ascii=False), args_digest=digest(ev.arguments),
            idempotency_key=None, status=status, policy_decision=decision, policy_id=policy_id)

    def _adjudicate(ctx: "ExecutionContext", ev: "ToolCallProposed") -> "ToolResult":
        step_no = run_store.append_step(ctx.run_id, AgentStep(
            kind="tool_call", payload={"call_id": ev.call_id, "tool_name": ev.tool_name,
                                       "args_digest": digest(ev.arguments)}))
        try:
            tool = registry.resolve(ev.tool_name)
        except ToolNotFound:
            _rec(ctx, step_no, ev, "?", "denied", "not_found", "registry.not_found")
            return ToolResult.fail(f"未知工具: {ev.tool_name}")
        except ToolDisabled:
            _rec(ctx, step_no, ev, "?", "denied", "disabled", "registry.disabled")
            return ToolResult.denied(f"工具已停用: {ev.tool_name}")
        try:
            tool.spec.validate_args(ev.arguments)     # 伪造越权参数在此被拒
        except ToolArgsError as e:
            _rec(ctx, step_no, ev, tool.spec.version, "denied", "schema", "schema.invalid")
            return ToolResult.denied(f"参数校验失败: {e}")
        decision = policy.authorize_tool_call(ctx, tool.spec, ev.arguments)
        pdecision = decision.decision
        if pdecision == "deny":
            _rec(ctx, step_no, ev, tool.spec.version, "denied", "deny", decision.policy_id)
            return ToolResult.denied(decision.reason)
        if pdecision == "require_approval":
            # WS3：命中审批。approvals 里有本 (run_id, call_id) → 人工已批准 → 绕过挂起直接执行；
            # 否则真正挂起——loop 侧 yield RunSuspended（本函数只落 pending_approval trace + 回 pending）。
            approved = approvals.get(f"{ctx.run_id}:{ev.call_id}") if approvals else None
            if not approved:
                _rec(ctx, step_no, ev, tool.spec.version, "pending_approval", "require_approval",
                     decision.policy_id)
                return ToolResult.pending()
            pdecision = "approved"
        # ALLOW / 已批准 → 经中间件栈执行（超时/重试/幂等/熔断 + 记 executing→result）
        idem = f"{ctx.run_id}:{step_no}" if tool.spec.idempotency == "key_required" else None
        return tool_executor.execute(ctx, tool, ev.arguments, run_id=ctx.run_id, step_no=step_no,
                                     policy_decision=pdecision, policy_id=decision.policy_id,
                                     idempotency_key=idem)

    return _adjudicate
