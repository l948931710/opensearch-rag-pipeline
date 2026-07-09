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

from typing import Any, Dict, Literal, Union

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


def parse_outcome(data: Dict[str, Any]) -> ApprovalOutcome:
    return _OUTCOME_ADAPTER.validate_python(data)


def dump_outcome(outcome: ApprovalOutcome) -> Dict[str, Any]:
    return outcome.model_dump()
