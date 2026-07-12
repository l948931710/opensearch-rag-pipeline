# -*- coding: utf-8 -*-
"""
agent_runtime — 企业级 Agent Runtime 薄层（与业务模块隔离，禁止反向 import 业务细节）。

设计基调（v2 报告 §2 边界铁律）：工具协议 / 权限 / 记忆 / 审批 / 审计**不进 AgentLoop**；
ExecutionContext 由服务端构造、模型不可写；外部写一律在审批之后；resume 重解析权限。

本批（WS1-1 宿主无关子集）：
- tool.py        —— EnterpriseTool 契约（ToolSpec/ToolResult/RiskLevel/ContentBlock）
- events.py      —— AgentEvent 判别联合（Loop↔Runtime 唯一通信协议）
- context.py     —— ExecutionContext / RunBudget（frozen，服务端构造）
- run_store.py   —— RunStore（RDS durable run/step/checkpoint/invocation + 状态机）

⚠️ 执行宿主（B1：并发模型 / AgentLoop 返回类型）暂不冻结——待「从长计议」拍板后再落
loop.py / executor.py（见 docs/agent-platform-v2/WS0-Pre-rebaseline-2026-07-07.md §④）。

版本兼容：目标 Python ≥3.9（pyproject requires-python），故用 `(str, Enum)` 而非 StrEnum、
dataclass 不用 slots；`from __future__ import annotations` 兜住 `X | Y` 注解。
"""
from opensearch_pipeline.agent_runtime.approval import (  # noqa: F401
    Approved,
    ApprovalOutcome,
    Edited,
    RejectedFeedback,
    RejectedTerminate,
    dump_outcome,
    parse_outcome,
)
from opensearch_pipeline.agent_runtime.audit import (  # noqa: F401
    NULL_AUDIT,
    AuditLog,
    AuditWriteError,
    RDSAuditLog,
)
from opensearch_pipeline.agent_runtime.context import (  # noqa: F401
    DEFAULT_REAUTH_THRESHOLD_S,
    Channel,
    ExecutionContext,
    RunBudget,
)
from opensearch_pipeline.agent_runtime.executor import (  # noqa: F401
    RunHandle,
    RunRejected,
    ThreadedRunExecutor,
)
from opensearch_pipeline.agent_runtime.loop import (  # noqa: F401
    AgentLoop,
    DefaultAgentLoop,
    ModelTurn,
    ProposedCall,
    decode_checkpoint,
    encode_checkpoint,
)
from opensearch_pipeline.agent_runtime.policy import (  # noqa: F401
    PolicyDecision,
    PolicyEngine,
    PolicyRule,
    default_policy_engine,
    make_adjudicator,
)
from opensearch_pipeline.agent_runtime.registry import (  # noqa: F401
    ToolDisabled,
    ToolNotFound,
    ToolRegistry,
)
from opensearch_pipeline.agent_runtime.model_gateway import (  # noqa: F401
    BudgetExceeded,
    ChatRequest,
    ChatResponse,
    DashScopeProvider,
    ModelCapabilities,
    ModelError,
    ModelGateway,
    ModelProvider,
    ModelUnavailable,
    ToolCall,
    default_gateway,
    make_model_fn,
    tool_spec_to_openai,
)
from opensearch_pipeline.agent_runtime.events import (  # noqa: F401
    AgentEvent,
    ModelDelta,
    RunCompleted,
    RunFailed,
    RunSuspended,
    ToolCallProposed,
    Usage,
    dump_event,
    parse_event,
)
from opensearch_pipeline.agent_runtime.run_store import (  # noqa: F401
    RUN_STATUSES,
    AgentStep,
    InvalidTransition,
    RDSRunStore,
    RunCheckpoint,
    RunStore,
)
from opensearch_pipeline.agent_runtime.tool import (  # noqa: F401
    ContentBlock,
    EnterpriseTool,
    RiskLevel,
    ToolArgsError,
    ToolResult,
    ToolSpec,
    ToolSpecError,
)

__all__ = [
    # tool contract
    "RiskLevel",
    "ToolSpec",
    "ToolResult",
    "ContentBlock",
    "EnterpriseTool",
    "ToolSpecError",
    "ToolArgsError",
    # durable run store
    "RunStore",
    "RDSRunStore",
    "AgentStep",
    "RunCheckpoint",
    "InvalidTransition",
    "RUN_STATUSES",
    # events
    "AgentEvent",
    "ModelDelta",
    "ToolCallProposed",
    "RunSuspended",
    "RunCompleted",
    "RunFailed",
    "Usage",
    "parse_event",
    "dump_event",
    # execution context
    "ExecutionContext",
    "RunBudget",
    "Channel",
    "DEFAULT_REAUTH_THRESHOLD_S",
    # approval outcome
    "ApprovalOutcome",
    "Approved",
    "Edited",
    "RejectedFeedback",
    "RejectedTerminate",
    "parse_outcome",
    "dump_outcome",
    # loop
    "AgentLoop",
    "DefaultAgentLoop",
    "ModelTurn",
    "ProposedCall",
    "encode_checkpoint",
    "decode_checkpoint",
    # executor (B1(b))
    "ThreadedRunExecutor",
    "RunHandle",
    "RunRejected",
    # model gateway (境内多 provider)
    "ModelGateway",
    "ModelProvider",
    "DashScopeProvider",
    "ModelCapabilities",
    "ChatRequest",
    "ChatResponse",
    "ToolCall",
    "ModelError",
    "ModelUnavailable",
    "BudgetExceeded",
    "make_model_fn",
    "tool_spec_to_openai",
    "default_gateway",
    # tool registry
    "ToolRegistry",
    "ToolNotFound",
    "ToolDisabled",
    # policy (deny-first) + adjudicator
    "PolicyEngine",
    "PolicyDecision",
    "PolicyRule",
    "default_policy_engine",
    "make_adjudicator",
    # audit (合规审计：HIGH_WRITE fail-closed)
    "AuditLog",
    "RDSAuditLog",
    "AuditWriteError",
    "NULL_AUDIT",
]
