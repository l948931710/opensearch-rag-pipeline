# -*- coding: utf-8 -*-
"""
events.py — AgentEvent 判别联合（v2 报告 §3）

AgentLoop 只**产出事件**（模型增量 / tool_call 提案 / 挂起 / 完成 / 失败），执行与裁决
全在 Runtime（铁律 1）。事件是 Loop→Runtime 单向通信的载荷；带 `type` 判别键 → 可序列化
往返（供 checkpoint 存档 / agent_step trace / SSE 帧）。

⚠️ 本模块**只定义事件类型**，不定义 `AgentLoop.run` 的返回类型（Iterator vs
Generator[.., ToolResult]）——那与执行宿主（B1）和 ToolResultInjector（B2）绑定，随
「从长计议」拍板后再落 loop.py（见 WS0-Pre-rebaseline §④）。工具**结果**经回注通道
（B2）送回 Loop，不在此单向事件流里。

不可变：事件是"已发生的事实"，frozen 防误改。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from typing_extensions import Annotated


class Usage(BaseModel):
    """一次 run/调用的 token 用量（RunCompleted 携带；成本记账/预算扣减用）。"""

    model_config = ConfigDict(frozen=True)

    tokens_prompt: int = 0
    tokens_completion: int = 0

    @property
    def total(self) -> int:
        return self.tokens_prompt + self.tokens_completion


class AgentEvent(BaseModel):
    """事件基类。子类以 `type` Literal 判别。"""

    model_config = ConfigDict(frozen=True)

    type: str


class ModelDelta(AgentEvent):
    """模型输出增量（流式 token；reasoning 为思维链增量，可空）。"""

    type: Literal["model_delta"] = "model_delta"
    text: str
    reasoning: Optional[str] = None


class ToolCallProposed(AgentEvent):
    """Loop 提案一次工具调用 —— **只提案不执行**，由 Runtime 裁决（Policy）后执行。"""

    type: Literal["tool_call_proposed"] = "tool_call_proposed"
    call_id: str
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    turn_index: int = 0        # 该提案属第几个模型轮（driver 据此每轮计一次 turn 预算；同批多 call 同值）
    # 产生本提案的模型轮 usage（loop 只挂在同批**第一个** call 上，其余为零值）——
    # driver 据此逐轮累加 token 预算；没有它，中间轮 usage 被丢弃、token_budget 全链无比较点。
    usage: Usage = Field(default_factory=Usage)


class ToolResultEmitted(AgentEvent):
    """一次工具调用的裁决/执行**结局**（driver 在 adjudicate 返回后发射）。

    P0-F 运行状态 UX：没有这帧，前端只见「提案了工具」无法区分「执行完了/被拒/挂起」，
    「正在调用工具」阶段成为无反馈长等待。**只带状态与耗时，不带内容/参数**——工具输出
    可能含敏感数据，回执详情走 run center（invocations 已脱敏落库）。
    status ∈ succeeded/failed/denied/pending_approval（与 ToolResult.status 同域）。"""

    type: Literal["tool_result"] = "tool_result"
    call_id: str
    tool_name: str
    status: str
    elapsed_ms: int = 0
    turn_index: int = 0
    # 进程内旁路（ToolResult.artifacts 直通；exclude=True → model_dump/序列化不带）：
    # serving 层据此构建 sources/content_blocks 帧。线协议 tool_result 帧仍只有状态+耗时。
    artifacts: Optional[Dict[str, Any]] = Field(default=None, exclude=True)


class RunCheckpointReady(AgentEvent):
    """Loop→driver **内部**事件（R4 运行中 checkpoint，RAG_AGENT_MIDRUN_CHECKPOINT 门控）：
    模型轮边界携带当前对话状态，driver 持久化后即消费——绝不外发（state_messages 是
    内部载荷；刻意不进 AnyAgentEvent 判别联合：它不该出现在任何序列化往返里）。
    turn_index = 该状态覆盖到的**最后一个已完成轮**（resume 语义 start_turn=turn+1 对齐）。"""

    type: Literal["run_checkpoint_ready"] = "run_checkpoint_ready"
    turn_index: int = 0
    state_messages: Optional[List[Dict[str, Any]]] = None


class RunSuspended(AgentEvent):
    """命中 REQUIRE_APPROVAL → Runtime 写 checkpoint 并挂起 run，等审批。

    loop 侧 yield 时只带 pending_call + state_messages（供 driver 存 checkpoint）；ids 留空。
    driver 持久化后回填 approval_request_id/checkpoint_id 并**剥离 state_messages** 再对外
    （SSE 只取两个 id + pending_call；state_messages 是内部载荷不外泄）。
    """

    type: Literal["run_suspended"] = "run_suspended"
    approval_request_id: str = ""
    checkpoint_id: str = ""
    pending_call: Optional[Dict[str, Any]] = None       # {call_id, tool_name, arguments}——审批 UI + resume
    turn_index: int = 0
    state_messages: Optional[List[Dict[str, Any]]] = None   # loop→driver 内部载荷，driver 存后置 None
    # P1「一轮多 tool call 挂起丢调用」：同批中排在待批 call 之后、尚未处理的 calls——
    # 随 checkpoint 持久化，resume 处置完 pending 后逐个续处理（保证 assistant 消息里的
    # 每个 tool_call 最终都有 tool response，OpenAI 消息序合法）。同为内部载荷不外泄。
    remaining_calls: Optional[List[Dict[str, Any]]] = None


class RunCompleted(AgentEvent):
    """run 正常结束，携带最终答案与用量。

    streamed=True：最终轮的文本已经以 ModelDelta 增量下发过（真流式）——SSE 层据此
    **不再重发** final_text 整段（否则前端看到答案两遍）；final_text 仍然完整携带，
    供 on_complete 落库/会话记忆（durable 侧永远要全文）。"""

    type: Literal["run_completed"] = "run_completed"
    final_text: str
    usage: Usage = Field(default_factory=Usage)
    streamed: bool = False


class RunFailed(AgentEvent):
    """run 失败；retryable 指示是否可重试（可重试错误分类，借 vlm_retry 白名单思路）。"""

    type: Literal["run_failed"] = "run_failed"
    error: str
    retryable: bool = False


# 判别联合 —— 供序列化往返（parse/dump）。子类仍是 AgentEvent 实例（isinstance 可用）。
AnyAgentEvent = Annotated[
    Union[ModelDelta, ToolCallProposed, ToolResultEmitted, RunSuspended, RunCompleted, RunFailed],
    Field(discriminator="type"),
]

_EVENT_ADAPTER: TypeAdapter = TypeAdapter(AnyAgentEvent)


def parse_event(data: Dict[str, Any]) -> AgentEvent:
    """从 dict（checkpoint/SSE 反序列化）还原为具体事件类型（按 type 判别）。"""
    return _EVENT_ADAPTER.validate_python(data)


def dump_event(event: AgentEvent) -> Dict[str, Any]:
    """序列化为 dict（含 type 判别键）——SSE 帧 / checkpoint / trace 用。"""
    return event.model_dump()
