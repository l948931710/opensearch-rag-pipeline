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
_APPROVAL_STORE = None


def _agent_enabled() -> bool:
    return os.environ.get("RAG_AGENT_ENABLE", "").strip().lower() in ("1", "true", "yes", "on")


def _get_approval_store():
    """审批持久化单例（schema/025）。独立于 _RUNTIME 四元组：既有测试 monkeypatch
    _get_runtime 返回 4-tuple 的契约不动，审批相关测试单独 patch 本函数。"""
    global _APPROVAL_STORE
    if _APPROVAL_STORE is None:
        from opensearch_pipeline.agent_runtime.approval_store import RDSApprovalStore
        _APPROVAL_STORE = RDSApprovalStore()
    return _APPROVAL_STORE


def _start_reaper(run_store, approval_store=None) -> None:
    """后台收尸线程（每进程一条，daemon）：周期调 run_store.reap_stale_runs——
    崩溃/滚动发布留下的 running 僵尸标 failed、超期 suspended 标 expired（纯 UPDATE 幂等，
    多实例并发安全）；approval_request 过期同扫（过期=拒绝，「沉默不是同意」）。
    失败只告警，绝不影响 serving。"""
    import threading

    interval = int(os.environ.get("RAG_AGENT_REAPER_INTERVAL_S", "300"))
    stale_s = int(os.environ.get("RAG_AGENT_STALE_RUNNING_S", "900"))
    ttl_s = int(os.environ.get("RAG_AGENT_SUSPENDED_TTL_S", "259200"))

    def _loop():
        while True:
            threading.Event().wait(interval)
            try:
                rep = run_store.reap_stale_runs(running_stale_s=stale_s, suspended_ttl_s=ttl_s)
                if rep.get("failed") or rep.get("expired"):
                    logger.warning("agent run 收尸：stale-running→failed %s · suspended→expired %s",
                                   rep.get("failed"), rep.get("expired"))
            except Exception:   # noqa: BLE001
                logger.warning("agent run 收尸失败（下轮重试）", exc_info=True)
            if approval_store is not None:
                try:
                    n = approval_store.expire_stale()
                    if n:
                        logger.warning("agent 审批请求过期收尸：pending→expired %s", n)
                except Exception:   # noqa: BLE001
                    logger.warning("审批请求过期收尸失败（下轮重试）", exc_info=True)

    threading.Thread(target=_loop, name="agent-run-reaper", daemon=True).start()


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
        # WS3 审批持久化：挂起侧 executor 写 approval_request（fail-closed），决策侧 /approve 写
        # approval_decision——request→decision→invocation→audit 四表回放链（深度审查 A 组 P1）。
        approval_store = _get_approval_store()
        executor = ThreadedRunExecutor(run_store, adjudicator, max_concurrent=max_runs,
                                       approvals=approvals, approval_store=approval_store)
        # SIGTERM/进程退出排水：非 daemon 线程池被直杀会留下 running 僵尸（reaper 兜底收尸，
        # 但正常退出应先排水）；收尸线程给崩溃/SIGKILL 场景兜底。
        import atexit
        atexit.register(executor.shutdown, wait=False)
        _start_reaper(run_store, approval_store)
        _RUNTIME = (registry, gateway, executor, run_store)
    return _RUNTIME


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _stream_events(handle, session_id: str, message_id: str):
    """把一个 run 的事件流转成 SSE 帧（/ask 与 /approve 共用）。RunSuspended→approval 帧。

    ⚠️ 落库/记忆 append **不在本函数**：挂在 SSE 消费侧会在客户端断连（GeneratorExit）时
    整段被跳过、答案静默丢失——已移到 run 完成侧（executor.submit/resume 的 on_complete）。
    GeneratorExit 时 finally 里不得再 yield（RuntimeError），用 closed 旗标跳过 [DONE]。"""
    from opensearch_pipeline.agent_runtime.events import (
        ModelDelta, RunCompleted, RunFailed, RunSuspended, ToolCallProposed)
    closed = False
    try:
        yield _sse({"type": "session", "session_id": session_id, "message_id": message_id,
                    "run_id": handle.run_id})
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
                yield _sse({"type": "done", "usage": ev.usage.model_dump()})
            elif isinstance(ev, RunFailed):
                yield _sse({"type": "error", "message": f"Agent 运行失败: {ev.error[:200]}"})
    except GeneratorExit:
        closed = True           # 客户端断连：run 仍在跑、落库在 run 完成侧，此处只安静退出
        raise
    except Exception:   # noqa: BLE001 — SSE 中断不外泄
        logger.error("agent SSE 中断", exc_info=True)
        yield _sse({"type": "error", "message": "Agent 运行异常"})
    finally:
        if not closed:
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
    # miniapp 前缀归属（对齐 api.py /api/ask）：'miniapp:<staffId>' 是可预测命名空间，
    # 不校验则认证用户可抢注他人 miniapp 会话键
    if thread_key and thread_key.startswith("miniapp:") and thread_key != f"miniapp:{identity.user_id}":
        raise HTTPException(status_code=403, detail="会话不属于当前用户")
    # 长度钳制：agent_run.thread_id VARCHAR(160) / qa_session_log.session_id VARCHAR(128)——
    # 超长直接拒（截断可能把两个不同会话合并到同一键），否则真库 INSERT 500
    if thread_key and len(thread_key) > 128:
        raise HTTPException(status_code=422, detail="session_id/conversation_id 过长")
    from opensearch_pipeline.agent_runtime.session_memory import default_session_memory
    from opensearch_pipeline.session_store import SessionOwnershipError
    memory = default_session_memory()
    try:
        snapshot = memory.get_snapshot(thread_key, owner=identity.user_id)   # 最近 N 轮（含 rolling summary）
    except SessionOwnershipError:
        # 越权探测回 403（api.py :626/:1125 同款映射）——此前未捕获传他人 session_id 得 500
        raise HTTPException(status_code=403, detail="会话不属于当前用户")
    thread_id = snapshot.thread_id                                       # 回填新建 sid（客户端下轮回传）
    session_id = thread_id
    message_id = generate_message_id()

    registry, gateway, executor, _run_store = _get_runtime()
    from opensearch_pipeline.agent_runtime import DefaultAgentLoop, ExecutionContext, make_model_fn
    from opensearch_pipeline.agent_runtime.executor import RunRejected

    try:
        from opensearch_pipeline.request_context import get_request_id
        rid = (get_request_id() or "")[:32]   # llm_call_log/agent_audit_log.request_id VARCHAR(32)
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

    def _remember(final_text: str) -> None:
        """run 完成侧回调（executor 调，非 SSE 消费侧——客户端断连也照常落库）。"""
        memory.append(thread_id, req.question, final_text, owner=identity.user_id)   # 热态记忆
        # durable：落 qa_session_log（供重启回读重建 + console 会话历史；log_qa_session 自身 fail-safe）
        from opensearch_pipeline.qa_logger import log_qa_session
        log_qa_session(session_id=thread_id, message_id=message_id, user_id=identity.user_id,
                       user_dept=getattr(identity, "dept", None), query_text=req.question,
                       answer_text=final_text, conversation_id=conv, answer_status="SUCCESS",
                       model_name="agent")

    def _report_failure(err: str) -> None:
        """run 失败侧回调：落 AGENT_ERROR 行（此前只记 SUCCESS——agent 失败对运维零可见、
        看板成功率虚高，深度审查治理组）。归 '%ERROR%' 错误族口径，model_name='agent' 可分段。
        失败不进会话记忆（不污染多轮历史）。"""
        from opensearch_pipeline.qa_logger import log_qa_session
        log_qa_session(session_id=thread_id, message_id=message_id, user_id=identity.user_id,
                       user_dept=getattr(identity, "dept", None), query_text=req.question,
                       answer_text=None, conversation_id=conv, answer_status="AGENT_ERROR",
                       model_name="agent", error_message=(err or "")[:500])

    try:
        handle = executor.submit(ctx, loop, messages, tools,
                                 on_complete=_remember, on_failure=_report_failure)
    except RunRejected:
        raise HTTPException(status_code=429, detail="Agent 并发已满，请稍后再试")

    return StreamingResponse(_stream_events(handle, session_id, message_id),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


class ApproveRequest(BaseModel):
    """审批决定回执。outcome ∈ {kind: approved|edited|rejected_feedback|rejected_terminate, ...}。
    idempotency_key：客户端重试/回调重放幂等（同键同处置幂等续跑，不重复决策）。"""

    run_id: str
    outcome: dict
    session_id: Optional[str] = None
    conversation_id: Optional[str] = None
    idempotency_key: Optional[str] = None


def _self_approval_allowed() -> bool:
    """dev/单人环境逃生门（默认关）。生产绝不开——开了职责分离即失效。"""
    return os.environ.get("RAG_AGENT_ALLOW_SELF_APPROVAL",
                          "").strip().lower() in ("1", "true", "yes", "on")


def _authorize_approver(identity: Identity, run: dict, areq: Optional[dict],
                        outcome_kind: str) -> None:
    """职责分离 + approver_scope 裁决（深度审查 A 组 P1「审批人=发起人」）。

    - 发起人本人：只允许撤回自己的申请（rejected_terminate）；批准/改参/反馈须他人。
    - 其他人：resolve_kb_identity **DB 权威现查**（绝不信任令牌 role 提示）——kb_admin 恒可审；
      dept_admin 需 approver_scope ∈ managed_owner_depts（dept_admin_grant 显式 seed）。
    - 请求行缺失（approval_store 故障/历史挂起）→ scope 未知 → 只有 kb_admin 可审（fail-closed）。
    """
    requester = run.get("user_id")
    if identity.user_id == requester:
        if outcome_kind == "rejected_terminate" or _self_approval_allowed():
            return                                     # 撤回自己的申请恒允许
        raise HTTPException(status_code=403, detail="发起人不能审批自己的请求（职责分离）")
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline.kb_authz import ROLE_DEPT_ADMIN, ROLE_KB_ADMIN, managed_owner_depts
    kb = resolve_kb_identity(identity.user_id)
    if kb.role == ROLE_KB_ADMIN:
        return
    scope = (areq or {}).get("approver_scope") or ""
    if kb.role == ROLE_DEPT_ADMIN and scope and scope in set(managed_owner_depts(kb)):
        return
    raise HTTPException(status_code=403,
                        detail="无权审批该请求（需 kb_admin 或 approver_scope 覆盖的 dept_admin）")


@router.post("/api/agent/approve")
def agent_approve(req: ApproveRequest, request: Request,
                  identity: Optional[Identity] = Depends(current_identity)):
    """WS3：对挂起 run 提交审批决定 → resume 续跑，SSE 复用 /ask 帧格式。

    审批闭环（schema/025）：职责分离（发起人只能撤回，批准须 kb_admin / approver_scope 覆盖的
    dept_admin，DB 现查）+ 决定持久化（approval_request CAS pending→处置 + approval_decision
    幂等键）+ resume 崩溃可重复（decision 已落库、run 回滚 suspended 后重试按「已决同向」续跑）。"""
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
    if run.get("status") != "suspended":
        raise HTTPException(status_code=409, detail=f"run 非挂起态（{run.get('status')}）")
    try:
        outcome = parse_outcome(req.outcome)
    except Exception:   # noqa: BLE001
        raise HTTPException(status_code=400, detail="审批 outcome 格式非法")

    approval_store = _get_approval_store()
    areq = None
    try:
        areq = approval_store.get_latest_by_run(req.run_id)
    except Exception:   # noqa: BLE001 — 读失败按「请求行缺失」处理（授权侧 fail-closed 到 kb_admin）
        logger.warning("approval_request 读取失败（授权收敛到 kb_admin）", exc_info=True)
    _authorize_approver(identity, run, areq, outcome.kind)

    # 决定持久化：pending → CAS 决出（first-valid-wins + uk_req_idem 幂等）；
    # 已决同向 → resume 重放（决策已落库、上次 resume 失败回滚 suspended 的重试路径）；
    # 已决不同向 / 已过期 → 409（迟到决策拒绝，「沉默不是同意」）。
    if areq is not None:
        from opensearch_pipeline.agent_runtime.approval_store import DECIDE_ALREADY_DECIDED
        if areq.get("status") == "pending":
            try:
                res = approval_store.decide(
                    areq["request_id"], decision=outcome.kind, decided_by=identity.user_id,
                    reason=getattr(outcome, "reason", None),
                    edited_args=getattr(outcome, "edited_args", None),
                    idempotency_key=req.idempotency_key)
            except Exception:   # noqa: BLE001 — 决策落库失败：宁拒不续（无 decision 行不放行执行）
                logger.error("approval_decision 写失败，拒绝续跑", exc_info=True)
                raise HTTPException(status_code=503, detail="审批决定落库失败，请重试")
            if res == DECIDE_ALREADY_DECIDED:
                raise HTTPException(status_code=409, detail="该审批请求已被处置或已过期")
            # ACCEPTED / DUPLICATE（同键同向重放）→ 继续 resume
        elif areq.get("status") != outcome.kind:
            raise HTTPException(status_code=409,
                                detail=f"该审批请求已被处置（{areq.get('status')}）")

    try:
        from opensearch_pipeline.request_context import get_request_id
        rid = (get_request_id() or "")[:32]   # llm_call_log/agent_audit_log.request_id VARCHAR(32)
    except Exception:   # noqa: BLE001
        rid = ""
    # 审批决定入合规审计（fail-open）：谁、对哪个 run/request、何种处置。只记 kind 不记原文。
    try:
        from opensearch_pipeline.agent_runtime.audit import RDSAuditLog
        RDSAuditLog().record(
            None, event_type="approval_decision", action=f"run:{req.run_id}",
            decision=outcome.kind, run_id=req.run_id,
            detail={"kind": outcome.kind, "approver": identity.user_id,
                    "request_id": (areq or {}).get("request_id")})
    except Exception:   # noqa: BLE001
        logger.warning("审批决定审计写失败（fail-open）", exc_info=True)
    # 铁律 5：resume 重建 ctx——以**发起人**（run.user_id）身份现解 ACL 续跑：既不复用
    # checkpoint 旧快照，也绝不套审批人的权限组（否则续跑段在审批人 ACL 下执行=提权，
    # 且记忆/落库会归属错人）。解析失败 → 空组（收敛为最小权限，绝不放大）。
    requester_id = run.get("user_id") or ""
    thread_id = run.get("thread_id") or req.session_id or req.run_id
    req_groups: list = []
    req_role = "employee"
    try:
        from opensearch_pipeline.dingtalk_identity import (
            _resolve_user_identity, resolve_kb_identity)
        req_groups = list((_resolve_user_identity(requester_id) or {}).get("dept") or [])
        req_role = resolve_kb_identity(requester_id).role or "employee"
    except Exception:   # noqa: BLE001
        logger.warning("resume 发起人身份现解失败（按最小权限续跑）", exc_info=True)
    ctx = ExecutionContext.create(
        request_id=rid, user_id=requester_id, acl_groups=req_groups,
        roles=(req_role,), channel="console", thread_id=thread_id,
        conversation_id=req.conversation_id)
    loop = DefaultAgentLoop(make_model_fn(gateway, ctx, "light"))   # 默认档=light(不思考/快/省)
    tools = registry.list_specs(ctx)

    # 被批 run 的最终答案也要进会话记忆 + qa_session_log（此前 approve 路径整轮丢失——
    # 恰是 HIGH_WRITE 高危场景，从多轮记忆与 durable 回读链里静默消失）。
    # 本轮 user 问题从 checkpoint messages 兜底提取（fail-open）。归属恒为发起人。
    message_id = generate_message_id()
    cp_question = "[审批后续跑]"
    try:
        cp = run_store.load_latest_checkpoint(req.run_id)
        if cp:
            from opensearch_pipeline.agent_runtime.loop import decode_checkpoint_state
            for m in reversed(decode_checkpoint_state(cp.state_blob).get("messages", [])):
                if m.get("role") == "user" and m.get("content"):
                    cp_question = m["content"]
                    break
    except Exception:   # noqa: BLE001
        pass

    def _remember(final_text: str) -> None:
        from opensearch_pipeline.agent_runtime.session_memory import default_session_memory
        default_session_memory().append(thread_id, cp_question, final_text, owner=requester_id)
        from opensearch_pipeline.qa_logger import log_qa_session
        log_qa_session(session_id=thread_id, message_id=message_id, user_id=requester_id,
                       user_dept=(req_groups[0] if req_groups else None), query_text=cp_question,
                       answer_text=final_text, conversation_id=run.get("conversation_id"),
                       answer_status="SUCCESS", model_name="agent")

    def _report_failure(err: str) -> None:
        from opensearch_pipeline.qa_logger import log_qa_session
        log_qa_session(session_id=thread_id, message_id=message_id, user_id=requester_id,
                       user_dept=(req_groups[0] if req_groups else None), query_text=cp_question,
                       answer_text=None, conversation_id=run.get("conversation_id"),
                       answer_status="AGENT_ERROR", model_name="agent",
                       error_message=(err or "")[:500])

    try:
        handle = executor.resume(req.run_id, ctx, outcome, loop, tools,
                                 on_complete=_remember, on_failure=_report_failure)
    except RunRejected:
        raise HTTPException(status_code=409, detail="run 非挂起或已被认领")

    session_id = req.session_id or thread_id
    return StreamingResponse(_stream_events(handle, session_id, message_id),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/api/agent/approvals")
def agent_approvals(request: Request, mine: bool = False, limit: int = 50,
                    identity: Optional[Identity] = Depends(current_identity)):
    """审批队列（console ManageView；报告 §N）。

    - 默认：审批人视角——kb_admin 见全部 pending；dept_admin 见 approver_scope ∈ managed
      的 pending（resolve_kb_identity DB 现查，与 kb_access 队列同纪律）；普通员工 403。
    - `?mine=1`：发起人视角——本人提交的 pending 请求（撤回入口用）。
    条目为脱敏后参数（proposed_args）+ render_summary，原文不出库。"""
    if not _agent_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    if identity is None:
        raise HTTPException(status_code=401, detail="需要登录")
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    limit = max(1, min(int(limit), 200))
    store = _get_approval_store()
    if mine:
        return {"items": store.list_pending(None, requested_by=identity.user_id, limit=limit)}
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline.kb_authz import ROLE_DEPT_ADMIN, ROLE_KB_ADMIN, managed_owner_depts
    kb = resolve_kb_identity(identity.user_id)
    if kb.role == ROLE_KB_ADMIN:
        return {"items": store.list_pending(None, limit=limit)}
    if kb.role == ROLE_DEPT_ADMIN:
        return {"items": store.list_pending(list(managed_owner_depts(kb)), limit=limit)}
    raise HTTPException(status_code=403, detail="无权查看审批队列（需 dept_admin / kb_admin）")
