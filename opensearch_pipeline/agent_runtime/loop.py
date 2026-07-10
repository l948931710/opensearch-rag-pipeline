# -*- coding: utf-8 -*-
"""
loop.py — AgentLoop 契约 + 自研 DefaultAgentLoop（执行模型 §2）

铁律 1：Loop **只产出事件**（tool_call 提案 / 完成 / 失败）；执行与裁决全在 Runtime 驱动器。
B2 解法：不用 ToolResultInjector，改 `Generator[AgentEvent, ToolResult|None, None]` + `.send()`
单线程回注结果（驱动器：next → 若 ToolCallProposed 则 adjudicate+execute → gen.send(result)）。

⚠️ 本 stub：`model_fn` 是 ModelGateway 的接缝（WS1 注入真实实现）；checkpoint 用 json 编解码
（msgpack + 加密是后续优化）。完整 resume/EDITED 重写 args 的裁决在驱动器+P2 审批闭环，
本文件只落 Loop 侧结构。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, List, Optional, Protocol, Tuple

from opensearch_pipeline.agent_runtime.approval import (
    ApprovalOutcome,
    Edited,
    RejectedFeedback,
    RejectedTerminate,
)
from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.events import (
    AgentEvent,
    RunCompleted,
    RunFailed,
    RunSuspended,
    ToolCallProposed,
    Usage,
)
from opensearch_pipeline.agent_runtime.tool import ToolResult, ToolSpec

Msg = Dict[str, Any]                       # 标准 chat 消息 {role, content, ...}
CHECKPOINT_VERSION = 1                      # 序列化版本号（跨 CD 版本 resume 兼容，F7）


@dataclass
class ProposedCall:
    call_id: str
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelTurn:
    """model_fn 的一次返回：要么 tool_calls，要么 final text。"""

    text: str = ""
    tool_calls: List[ProposedCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


# model_fn 由 ModelGateway 提供（WS1）；stub 阶段注入
ModelFn = Callable[[List[Msg], List[ToolSpec]], ModelTurn]


def encode_checkpoint(messages: List[Msg], *, pending_call: Optional[Dict[str, Any]] = None,
                      turn: int = 0, version: int = CHECKPOINT_VERSION) -> Tuple[bytes, str]:
    """(messages[, pending_call, turn]) → (state_blob, digest)。Loop 层拥有 checkpoint 编解码（B4）。
    pending_call = 挂起时待审批的工具调用（resume APPROVED 据此重执行）。"""
    blob = json.dumps({"version": version, "messages": messages,
                       "pending_call": pending_call, "turn": turn},
                      ensure_ascii=False).encode("utf-8")
    return blob, hashlib.sha256(blob).hexdigest()


def decode_checkpoint(state_blob: bytes) -> List[Msg]:
    """向后兼容：只取 messages。"""
    if isinstance(state_blob, str):
        state_blob = state_blob.encode("utf-8")
    return json.loads(state_blob.decode("utf-8")).get("messages", [])


def decode_checkpoint_state(state_blob: bytes) -> Dict[str, Any]:
    """完整解码 → {messages, pending_call, turn}（resume 用）。

    F7：校验序列化版本——写入 version 但解码忽略等于没有版本机制，跨 CD 发布后
    resume 到不兼容格式会以更隐蔽的方式坏掉。不兼容 → 抛错，executor 回滚认领
    （run 留在 suspended，可由新版本代码或对账处置）。
    """
    if isinstance(state_blob, str):
        state_blob = state_blob.encode("utf-8")
    d = json.loads(state_blob.decode("utf-8"))
    ver = int(d.get("version", CHECKPOINT_VERSION))
    if ver != CHECKPOINT_VERSION:
        raise ValueError(f"checkpoint 版本不兼容: {ver}（当前 {CHECKPOINT_VERSION}）")
    return {"messages": d.get("messages", []), "pending_call": d.get("pending_call"),
            "turn": int(d.get("turn", 0))}


def _result_text(result: Optional[ToolResult]) -> str:
    if result is None:
        return ""
    parts = [b.text for b in result.content if b.type == "text" and b.text]
    if parts:
        return "\n".join(parts)
    return result.error or ""


class AgentLoop(Protocol):
    def run(self, ctx: ExecutionContext, messages: List[Msg],
            tools: List[ToolSpec]) -> Generator[AgentEvent, Optional[ToolResult], None]: ...

    def resume(self, ctx: ExecutionContext, checkpoint: Any,
               outcome: ApprovalOutcome) -> Generator[AgentEvent, Optional[ToolResult], None]: ...


class DefaultAgentLoop:
    """自研事件流可挂起循环。model_fn 注入（ModelGateway 接缝）。"""

    def __init__(self, model_fn: ModelFn):
        self._model = model_fn

    def run(self, ctx: ExecutionContext, messages: List[Msg],
            tools: List[ToolSpec], start_turn: int = 0
            ) -> Generator[AgentEvent, Optional[ToolResult], None]:
        """start_turn：resume 续跑时从 checkpoint turn+1 起算——turn_index 必须跨
        suspend/resume 单调递增，否则续跑段模型轮 turn_index 回绕到 0，驱动器的
        「turn_index >= 已计数」去重会让这些轮**整段逃逸预算与 trace**（深度审查 C 组 P1）。"""
        msgs: List[Msg] = list(messages)
        max_turns = ctx.budget.max_turns
        for _turn in range(start_turn, max_turns):
            mt = self._model(msgs, tools)
            if not mt.tool_calls:
                yield RunCompleted(final_text=mt.text, usage=mt.usage)
                return
            msgs.append({"role": "assistant",
                         "tool_calls": [{"call_id": c.call_id, "name": c.tool_name,
                                         "arguments": c.arguments} for c in mt.tool_calls]})
            for i, call in enumerate(mt.tool_calls):
                # Loop 只提案；驱动器裁决+执行后经 .send() 把 ToolResult 回注（= yield 表达式的值）。
                # 本轮 usage 只挂同批第一个 call（驱动器按 turn 去重累加 token 预算）。
                result: Optional[ToolResult] = yield ToolCallProposed(
                    call_id=call.call_id, tool_name=call.tool_name, arguments=call.arguments,
                    turn_index=_turn, usage=(mt.usage if i == 0 else Usage()))
                if result is not None and result.status == "pending_approval":
                    # 命中 REQUIRE_APPROVAL → 挂起：状态（含本轮 assistant tool_calls msg）+ 待批 call
                    # 交驱动器写 checkpoint；生成器就此结束，resume 新建一个从 checkpoint 续跑。
                    yield RunSuspended(
                        pending_call={"call_id": call.call_id, "tool_name": call.tool_name,
                                      "arguments": call.arguments},
                        turn_index=_turn, state_messages=msgs)
                    return
                msgs.append({"role": "tool", "call_id": call.call_id,
                             "content": _result_text(result)})
        yield RunFailed(error=f"max_turns={max_turns} 超限", retryable=False)

    def resume(self, ctx: ExecutionContext, checkpoint_state: Any, outcome: ApprovalOutcome,
               tools: Optional[List[ToolSpec]] = None
               ) -> Generator[AgentEvent, Optional[ToolResult], None]:
        """从 checkpoint 状态 + 审批结局续跑。checkpoint_state = {messages, pending_call, turn}
        （驱动器 decode_checkpoint_state 后传入 dict；也容 RunCheckpoint 对象）。tools=续跑时模型可见工具集。"""
        state = checkpoint_state if isinstance(checkpoint_state, dict) else \
            decode_checkpoint_state(getattr(checkpoint_state, "state_blob", b"{}"))
        msgs: List[Msg] = list(state.get("messages", []))
        pending = state.get("pending_call")
        tools = tools or []
        turn = int(state.get("turn", 0))

        # 硬终止：驱动器随后 transition(resuming→cancelled)
        if isinstance(outcome, RejectedTerminate):
            yield RunFailed(error="审批拒绝并终止", retryable=False)
            return
        if isinstance(outcome, RejectedFeedback):
            # 拒绝反馈：给 pending call 一个拒绝 tool 结果（保持 assistant tool_call 有对应结果）+ 理由续跑
            if pending:
                msgs.append({"role": "tool", "call_id": pending["call_id"],
                             "content": f"[审批未通过] {outcome.reason}"})
            else:
                msgs.append({"role": "user", "content": f"[审批未通过，请换方案] {outcome.reason}"})
            yield from self.run(ctx, msgs, tools, start_turn=turn + 1)
            return
        # APPROVED / EDITED：重提待批 call（驱动器因已授权而执行；EDITED 用人工改后参）。
        # 重提 turn_index=checkpoint turn（挂起前已计过数，驱动器不重复计费）。
        if pending:
            args = outcome.edited_args if isinstance(outcome, Edited) else pending.get("arguments", {})
            result: Optional[ToolResult] = yield ToolCallProposed(
                call_id=pending["call_id"], tool_name=pending["tool_name"],
                arguments=args, turn_index=turn)
            msgs.append({"role": "tool", "call_id": pending["call_id"],
                         "content": _result_text(result)})
        yield from self.run(ctx, msgs, tools, start_turn=turn + 1)
