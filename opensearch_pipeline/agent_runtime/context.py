# -*- coding: utf-8 -*-
"""
context.py — ExecutionContext / RunBudget（v2 报告 §3 模块 A）

铁律 2：ExecutionContext 由**服务端构造**，模型与请求体**不可写**身份/ACL/审批策略。
- frozen dataclass → 构造后不可变；
- acl_groups / roles 是**服务端生成**（current_identity 解析、白名单归一后）的 tuple →
  请求体伪造 acl_groups 无效（构造入口只收服务端解析结果）。
铁律 5：resume ≠ 恢复旧权限 —— auth_resolved_at 超阈值必须重解析（needs_reauth）。

⚠️ 评审 B8：预算的**消耗跟踪**（已用 turns/tokens）不放 frozen ctx——装不下可变扣减，
且消耗需跨 suspend/resume 持久化。本模块 RunBudget 只是**上限（caps）**；消耗跟踪归
run_store（loop/executor 阶段随 B1 落地）。现按报告原样 frozen，不提前改架构。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional, Sequence, Tuple

Channel = Literal["dingtalk", "console", "miniapp", "api"]

# 预算与阈值默认值（B8 要求"定义重解析阈值默认值"）
DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_TOOL_CALLS = 24
DEFAULT_TOKEN_BUDGET = 200_000
DEFAULT_RUN_TTL_S = 600              # run deadline 默认 10 分钟
DEFAULT_REAUTH_THRESHOLD_S = 300     # resume 距上次身份解析 >5min 必须重解析（LIVE reread 延伸）


@dataclass(frozen=True)
class RunBudget:
    """run 的预算**上限**（不可变 caps）。消耗跟踪见模块 docstring（B8）。"""

    max_turns: int = DEFAULT_MAX_TURNS
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    token_budget: int = DEFAULT_TOKEN_BUDGET
    deadline: Optional[datetime] = None

    @classmethod
    def with_ttl(cls, now: datetime, ttl_s: int = DEFAULT_RUN_TTL_S,
                 max_turns: int = DEFAULT_MAX_TURNS,
                 max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
                 token_budget: int = DEFAULT_TOKEN_BUDGET) -> "RunBudget":
        return cls(max_turns=max_turns, max_tool_calls=max_tool_calls,
                   token_budget=token_budget, deadline=now + timedelta(seconds=ttl_s))

    def is_past_deadline(self, now: datetime) -> bool:
        return self.deadline is not None and now >= self.deadline


@dataclass(frozen=True)
class ExecutionContext:
    """服务端构造、不可变。模型/请求体不可写任何字段（铁律 2）。

    字段（报告 §3）；dataclass 要求带默认的字段置后，故 run_id/conversation_id 在末尾，
    构造走 create() 用关键字参数（顺序无关）。
    """

    request_id: str                       # 复用 request_context 的 X-Request-Id
    user_id: str                          # current_identity 解析
    acl_groups: Tuple[str, ...]           # 服务端生成、白名单归一后（不可变 tuple）
    roles: Tuple[str, ...]                # employee/dept_admin/kb_admin/agent_admin...
    channel: Channel
    thread_id: str                        # 会话键；钉钉沿用 f"{conversation_id}:{staff_id}"
    auth_resolved_at: datetime            # 超阈值 resume 必须重解析
    budget: RunBudget
    run_id: Optional[str] = None          # agent_run 主键；同步问答可为 None
    conversation_id: Optional[str] = None # agent_run 分列存
    model_profile: Optional[str] = None   # 模型档（light/high/…）：入口按「深度思考」定档，
                                          # create_run 落 agent_run.model_profile，resume 沿用
    # 投机检索句柄（serving 层瞬态执行辅助，非身份/审计数据；模型/请求体无写入通道，
    # 铁律 2 不破）：submit 时用原问题并行预取，knowledge_search 命中即复用。
    # 不落库不进 checkpoint；resume 重建的 ctx 恒为 None（预取只在 submit 段有意义）。
    speculative_search: Optional[Any] = None
    # 检索会话（同上瞬态三律）：跨检索去重 seen 集 + 已提交次数；executor 在成功结果
    # 送达点 commit_keys（见 knowledge_search.SearchSession）。resume 恒 None=fail-open。
    # TODO(清理)：与 speculative_search 合并为单一 run_scratch 槽位（评审 R②-6 建议）。
    search_session: Optional[Any] = None
    # PR-C（P0-06 回链）：审批放行的调用由 adjudicator 在消费 ApprovalGrant 时注入——
    # 工具据此把 approval_request_id 落到事实行（如 ontology_identifier）、confirmed_by
    # 记真实审批人；approval_scope=审批时用的裁决键，供工具落库前重验 stewardship 漂移。
    # 服务端注入，模型/请求体无通道（铁律 2 不破）。
    approval_request_id: Optional[str] = None
    approved_by: Optional[str] = None
    approval_scope: Optional[str] = None
    # PR-3 Stage C（操作台账）：executor 对副作用工具注入（= tool_invocation.invocation_id；
    # 同键 reclaim 重试复用同一 invocation 行 ⇒ 同 id=fencing 键）。工具把台账行与副作用
    # 同一事务落库（operation_ledger.insert_operation_tx），「行存在⇔副作用已提交」从此
    # 可查证（check_operation→自动对账）。服务端注入，模型/请求体无通道（铁律 2 不破）。
    operation_id: Optional[str] = None

    @classmethod
    def create(cls, *, request_id: str, user_id: str,
               acl_groups: Sequence[str], roles: Sequence[str],
               channel: Channel, thread_id: str,
               budget: Optional[RunBudget] = None,
               run_id: Optional[str] = None,
               conversation_id: Optional[str] = None,
               model_profile: Optional[str] = None,
               speculative_search: Optional[Any] = None,
               search_session: Optional[Any] = None,
               auth_resolved_at: Optional[datetime] = None) -> "ExecutionContext":
        """服务端构造入口。acl_groups/roles 归一为不可变 tuple；时间/预算给默认。

        auth_resolved_at 建议传 tz-aware（UTC）；不传则取 now(UTC)。
        """
        now = auth_resolved_at or datetime.now(timezone.utc)
        return cls(
            request_id=request_id, user_id=user_id,
            acl_groups=tuple(acl_groups), roles=tuple(roles),
            channel=channel, thread_id=thread_id,
            auth_resolved_at=now, budget=budget or RunBudget.with_ttl(now),
            run_id=run_id, conversation_id=conversation_id,
            model_profile=model_profile, speculative_search=speculative_search,
            search_session=search_session,
        )

    def needs_reauth(self, now: datetime, threshold_s: int = DEFAULT_REAUTH_THRESHOLD_S) -> bool:
        """resume 时：距上次身份解析是否超阈值 → 必须重建 ctx 并重过 Policy（铁律 5）。"""
        return (now - self.auth_resolved_at).total_seconds() > threshold_s

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def with_run_id(self, run_id: str) -> "ExecutionContext":
        """建 run 后回填 run_id —— frozen 不原地改，返回新实例。"""
        return replace(self, run_id=run_id)
