# -*- coding: utf-8 -*-
"""
routes/agent.py — 企业 Agent 入口（console-first；plan WS1-3）

`POST /api/agent/ask`：SSE 帧在现有 session/chunk/done/[DONE] 之上加 tool_call/approval。
`RAG_AGENT_ENABLE` 默认 off → 端点视同不存在（404），对现有链路零影响。

惯例（同 routes/console.py）：顶层 from-import api.py 共享件（依赖 api 上方名字已定义，注册块在
api 文件底部）。**agent_runtime 一律惰性 import**（flag-off 时永不加载 → 零启动成本、零回归面）。

⚠️ 本入口是 agent 代码首次接入 live 服务：加法（独立路由）+ flag-off。B9（agent↔qa_session_log
合流，console 历史）与真流式（ModelDelta，需 gateway chat_stream）留后续；首批 RunCompleted.final_text
以单 chunk 帧下发。ModelGateway/RDSRunStore/HA3 检索均为真实依赖——真正跑通需 schema/022 apply +
配置 DashScope，且 flag=on（均 user-gated）。
"""
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from opensearch_pipeline.api import (  # noqa: E402  顶层 from-import api 共享件（同 console.py 惯例）
    AskRequest,
    Identity,
    _SSE_HEADERS,
    _enforce_rate_limit,
    current_identity,
    generate_message_id,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_AGENT_SYSTEM_PROMPT = (
    "你是富岭企业知识库助手。回答涉及企业资料时，先调用 knowledge_search 检索，"
    "再严格依据检索到的片段作答；检索不到就如实说明没有，不要编造。"
)

# 运行时单例（每进程一套；executor 的有界线程池即 B1(b) 执行宿主）。惰性建，flag-off 时永不建。
_RUNTIME = None


def _agent_enabled() -> bool:
    return os.environ.get("RAG_AGENT_ENABLE", "").strip().lower() in ("1", "true", "yes", "on")


def _get_runtime():
    """惰性建运行时单例：(registry, gateway, executor, run_store)。"""
    global _RUNTIME
    if _RUNTIME is None:
        from opensearch_pipeline.agent_runtime import (
            ThreadedRunExecutor, default_gateway, default_policy_engine, make_adjudicator)
        from opensearch_pipeline.agent_runtime.audit import RDSAuditLog
        from opensearch_pipeline.agent_runtime.run_store import RDSRunStore
        from opensearch_pipeline.agent_tools import build_default_registry
        registry = build_default_registry()
        run_store = RDSRunStore()
        # WS3 审批：executor.resume 写已批准 call → adjudicator 放行执行；两者须共享**同一** approvals dict。
        approvals: dict = {}
        adjudicator = make_adjudicator(registry, default_policy_engine(), run_store,
                                       audit=RDSAuditLog(), approvals=approvals)   # 执行前合规审计
        gateway = default_gateway(call_logger=run_store.record_llm_call)   # 每次模型调用记 llm_call_log
        # WS2-3：装 rolling summary 真 summarizer（廉价模型压缩超窗历史）；是否生效由
        # RAG_SESSION_ROLLING_SUMMARY 控（默认 OFF→硬截断），装了也 dormant 直到开关打开。
        from opensearch_pipeline.agent_runtime import compaction
        compaction.install(gateway=gateway)
        max_runs = int(os.environ.get("RAG_AGENT_MAX_CONCURRENT_RUNS", "4"))
        executor = ThreadedRunExecutor(run_store, adjudicator, max_concurrent=max_runs,
                                       approvals=approvals)
        _RUNTIME = (registry, gateway, executor, run_store)
    return _RUNTIME


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _stream_events(handle, session_id: str, message_id: str, on_complete=None):
    """把一个 run 的事件流转成 SSE 帧（/ask 与 /approve 共用）。RunSuspended→approval 帧。
    on_complete(final_text)：run 正常完成时回调（/ask 用它把本轮 Q&A append 进会话记忆）。"""
    from opensearch_pipeline.agent_runtime.events import (
        ModelDelta, RunCompleted, RunFailed, RunSuspended, ToolCallProposed)
    yield _sse({"type": "session", "session_id": session_id, "message_id": message_id,
                "run_id": handle.run_id})
    try:
        for ev in handle.events():
            if isinstance(ev, ModelDelta):
                yield _sse({"type": "chunk", "content": ev.text})
            elif isinstance(ev, ToolCallProposed):
                yield _sse({"type": "tool_call", "call_id": ev.call_id,
                            "tool_name": ev.tool_name, "arguments": ev.arguments})
            elif isinstance(ev, RunSuspended):
                yield _sse({"type": "approval", "approval_request_id": ev.approval_request_id,
                            "checkpoint_id": ev.checkpoint_id, "pending_call": ev.pending_call})
            elif isinstance(ev, RunCompleted):
                if ev.final_text:
                    yield _sse({"type": "chunk", "content": ev.final_text})
                if on_complete is not None:
                    try:
                        on_complete(ev.final_text)
                    except Exception:   # noqa: BLE001 — 记忆 append 失败不影响回答
                        logger.warning("多轮记忆 append 失败", exc_info=True)
                yield _sse({"type": "done", "usage": ev.usage.model_dump()})
            elif isinstance(ev, RunFailed):
                yield _sse({"type": "error", "message": f"Agent 运行失败: {ev.error[:200]}"})
    except Exception:   # noqa: BLE001 — SSE 中断不外泄
        logger.error("agent SSE 中断", exc_info=True)
        yield _sse({"type": "error", "message": "Agent 运行异常"})
    finally:
        yield "data: [DONE]\n\n"


@router.post("/api/agent/ask")
def agent_ask(req: AskRequest, request: Request,
              identity: Optional[Identity] = Depends(current_identity)):
    if not _agent_enabled():
        raise HTTPException(status_code=404, detail="Not Found")   # 隐藏入口
    if identity is None:
        raise HTTPException(status_code=401, detail="需要登录")
    # 防刷准入：与 /api/ask 同层（在开销与 StreamingResponse 之前拒绝）
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=True)

    conv = getattr(req, "conversation_id", None)
    # 多轮记忆键：有 conversation_id → f"{conv}:{user}"（钉钉一致）；否则用 client session_id（缺则新建）。
    thread_key = f"{conv}:{identity.user_id}" if conv else getattr(req, "session_id", None)
    from opensearch_pipeline.agent_runtime.session_memory import default_session_memory
    memory = default_session_memory()
    snapshot = memory.get_snapshot(thread_key, owner=identity.user_id)   # 取最近 N 轮（含 rolling summary）
    thread_id = snapshot.thread_id                                       # 回填新建 sid（客户端下轮回传）
    session_id = thread_id
    message_id = generate_message_id()

    registry, gateway, executor, _run_store = _get_runtime()
    from opensearch_pipeline.agent_runtime import DefaultAgentLoop, ExecutionContext, make_model_fn
    from opensearch_pipeline.agent_runtime.executor import RunRejected

    try:
        from opensearch_pipeline.request_context import get_request_id
        rid = get_request_id() or ""
    except Exception:   # noqa: BLE001
        rid = ""

    ctx = ExecutionContext.create(
        request_id=rid, user_id=identity.user_id, acl_groups=identity.acl_groups,
        roles=(identity.role,) if identity.role else ("employee",),
        channel="console", thread_id=thread_id, conversation_id=conv)
    loop = DefaultAgentLoop(make_model_fn(gateway, ctx, "light"))   # 默认档=light(不思考/快/省)
    tools = registry.list_specs(ctx)
    # 多轮：system + 历史快照（前几轮 Q&A + summary）+ 本轮 user
    messages = ([{"role": "system", "content": _AGENT_SYSTEM_PROMPT}]
                + snapshot.messages
                + [{"role": "user", "content": req.question}])

    try:
        handle = executor.submit(ctx, loop, messages, tools)
    except RunRejected:
        raise HTTPException(status_code=429, detail="Agent 并发已满，请稍后再试")

    def _remember(final_text: str) -> None:
        memory.append(thread_id, req.question, final_text, owner=identity.user_id)   # 热态记忆
        # durable：落 qa_session_log（供重启回读重建 + console 会话历史；log_qa_session 自身 fail-safe）
        from opensearch_pipeline.qa_logger import log_qa_session
        log_qa_session(session_id=thread_id, message_id=message_id, user_id=identity.user_id,
                       user_dept=getattr(identity, "dept", None), query_text=req.question,
                       answer_text=final_text, conversation_id=conv, answer_status="SUCCESS",
                       model_name="agent")

    return StreamingResponse(_stream_events(handle, session_id, message_id, on_complete=_remember),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


class ApproveRequest(BaseModel):
    """审批决定回执。outcome ∈ {kind: approved|edited|rejected_feedback|rejected_terminate, ...}。"""

    run_id: str
    outcome: dict
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None


@router.post("/api/agent/approve")
def agent_approve(req: ApproveRequest, request: Request,
                  identity: Optional[Identity] = Depends(current_identity)):
    """WS3：对挂起 run 提交审批决定 → resume 续跑，SSE 复用 /ask 帧格式。归属=本人(自确认高危写)。"""
    if not _agent_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if identity is None:
        raise HTTPException(status_code=401, detail="需要登录")
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)

    registry, gateway, executor, run_store = _get_runtime()
    from opensearch_pipeline.agent_runtime import DefaultAgentLoop, ExecutionContext, make_model_fn
    from opensearch_pipeline.agent_runtime.approval import parse_outcome
    from opensearch_pipeline.agent_runtime.executor import RunRejected

    run = run_store.get_run(req.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run 不存在")
    if run.get("user_id") != identity.user_id:                 # 归属校验：只能批自己的 run
        raise HTTPException(status_code=403, detail="无权审批该 run")
    if run.get("status") != "suspended":
        raise HTTPException(status_code=409, detail=f"run 非挂起态（{run.get('status')}）")
    try:
        outcome = parse_outcome(req.outcome)
    except Exception:   # noqa: BLE001
        raise HTTPException(status_code=400, detail="审批 outcome 格式非法")

    try:
        from opensearch_pipeline.request_context import get_request_id
        rid = get_request_id() or ""
    except Exception:   # noqa: BLE001
        rid = ""
    # 铁律 5：resume 重建 ctx（重解析身份，不复用 checkpoint 里的旧 ACL 快照）
    ctx = ExecutionContext.create(
        request_id=rid, user_id=identity.user_id, acl_groups=identity.acl_groups,
        roles=(identity.role,) if identity.role else ("employee",),
        channel="console", thread_id=req.run_id, conversation_id=req.conversation_id)
    loop = DefaultAgentLoop(make_model_fn(gateway, ctx, "light"))   # 默认档=light(不思考/快/省)
    tools = registry.list_specs(ctx)
    try:
        handle = executor.resume(req.run_id, ctx, outcome, loop, tools)
    except RunRejected:
        raise HTTPException(status_code=409, detail="run 非挂起或已被认领")

    session_id = req.session_id or req.run_id
    return StreamingResponse(_stream_events(handle, session_id, generate_message_id()),
                             media_type="text/event-stream", headers=_SSE_HEADERS)
