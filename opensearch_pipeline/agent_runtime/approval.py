# -*- coding: utf-8 -*-
"""
approval.py — ApprovalOutcome 判别联合（执行模型 §2 / 报告 §5 四处置）

审批决策回注 loop.resume 的值。四处置（fail-closed 全覆盖见报告 §5）：
- APPROVED           原参放行
- EDITED             人工改参（edited_args）→ 重校验/重过 Policy 后执行（重写历史 tool_call args，B4）
- REJECTED_FEEDBACK  理由回喂模型换方案续跑
- REJECTED_TERMINATE 硬终止 run（→ cancelled）

带 `kind` 判别键 → 序列化往返（checkpoint/审批卡片/console 审批队列）。frozen 不可变。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from typing_extensions import Annotated


class ApprovalOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: str


class Approved(ApprovalOutcome):
    kind: Literal["approved"] = "approved"


class Edited(ApprovalOutcome):
    kind: Literal["edited"] = "edited"
    edited_args: Dict[str, Any]           # 人工改后的参数 → 重过 jsonschema + Policy


class RejectedFeedback(ApprovalOutcome):
    kind: Literal["rejected_feedback"] = "rejected_feedback"
    reason: str                           # 回喂模型续跑


class RejectedTerminate(ApprovalOutcome):
    kind: Literal["rejected_terminate"] = "rejected_terminate"


AnyApprovalOutcome = Annotated[
    Union[Approved, Edited, RejectedFeedback, RejectedTerminate],
    Field(discriminator="kind"),
]

_OUTCOME_ADAPTER: TypeAdapter = TypeAdapter(AnyApprovalOutcome)


@dataclass(frozen=True)
class ApprovalGrant:
    """一次性审批放行凭据（executor.resume 写入、adjudicator **消费即销毁**）。

    绑定 (tool_name, args_digest)：凭据只放行「被批的那一次调用」——模型复用 call_id
    或改参重放时凭据不匹配 → 重新挂起，堵住 call_id 复用绕过审批的洞（深度审查 A 组 P1）。
    EDITED 的 digest 按人工改后参数计算（loop 重提时用改后参，两侧一致）。
    """

    outcome: ApprovalOutcome
    tool_name: str
    args_digest: str
    # PR-C（P0-06 回链）：审批事实随凭据走到执行点——adjudicator 消费时注入 ctx，
    # 工具把 request_id 落事实行、confirmed_by 记真实审批人。缺省 None（直驱测试/
    # 未接持久化路径零变化）。
    request_id: Optional[str] = None
    decided_by: Optional[str] = None
    approver_scope: Optional[str] = None
    # P0-B（重评审计）：凭据绑定**已落库的 approval_decision 行**——executor.resume 在建
    # 凭据前校验决定行存在且同向、final_args_digest 与将执行参数一致（见
    # _verify_persisted_decision）。未接 approval_store 的直驱路径为 None。
    decision_id: Optional[str] = None


def parse_outcome(data: Dict[str, Any]) -> ApprovalOutcome:
    return _OUTCOME_ADAPTER.validate_python(data)


def dump_outcome(outcome: ApprovalOutcome) -> Dict[str, Any]:
    return outcome.model_dump()
