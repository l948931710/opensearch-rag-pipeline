# -*- coding: utf-8 -*-
"""
routes/agent.py — 企业 Agent 入口（console-first；plan WS1-3）

`POST /api/agent/ask`：SSE 帧在现有 session/chunk/done/[DONE] 之上加 tool_call/approval。
`RAG_AGENT_ENABLE` 默认 off → 端点视同不存在（404），对现有链路零影响。

惯例（同 routes/console.py）：顶层 from-import api.py 共享件（依赖 api 上方名字已定义，注册块在
api 文件底部）。**agent_runtime 一律惰性 import**（flag-off 时永不加载 → 零启动成本、零回归面）。

⚠️ 本入口是 agent 代码首次接入 live 服务：加法（独立路由）+ flag-off。B9（agent↔qa_session_log
合流）已落地；**真流式已接**（gateway.complete_stream → loop ModelDelta → SSE chunk 打字机，
RAG_AGENT_STREAM 默认开、=false 回退整段单 chunk）。ModelGateway/RDSRunStore/HA3 检索均为
真实依赖——真正跑通需 schema/022+ apply + 配置 DashScope，且 flag=on（均 user-gated）。
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
_REGISTRY_STORE = None


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


def _get_registry_store():
    """tool_registry 治理单例（DB 驱动 kill switch；深度审查多实例运维组）。测试 patch 本函数。"""
    global _REGISTRY_STORE
    if _REGISTRY_STORE is None:
        from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore
        _REGISTRY_STORE = RDSToolRegistryStore(
            ttl_s=float(os.environ.get("RAG_AGENT_KILL_SWITCH_TTL_S", "30")))
    return _REGISTRY_STORE


def _start_reaper(run_store, approval_store=None, runtime=None) -> None:
    """后台收尸+对账线程（每进程一条，daemon），每轮三步：
    ①run_store.reap_stale_runs——stale running→failed、**stale resuming→回边 suspended**
      （保住已批决定的可重驱性，B6 前置）、超期 suspended→expired（纯 UPDATE 幂等，多实例安全）；
    ②approval_request 过期扫（过期=拒绝，「沉默不是同意」）；
    ③**B6 对账**：decided-but-not-resumed 死单按 approval_decision 重驱 resume
      （runtime=(registry, gateway, executor) 提供重驱件；未接则跳过）。
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
                if rep.get("failed") or rep.get("expired") or rep.get("resuming_reset"):
                    logger.warning(
                        "agent run 收尸：stale-running→failed %s · suspended→expired %s · "
                        "stale-resuming→suspended %s（B6 可重驱）",
                        rep.get("failed"), rep.get("expired"), rep.get("resuming_reset"))
            except Exception:   # noqa: BLE001
                logger.warning("agent run 收尸失败（下轮重试）", exc_info=True)
            if approval_store is not None:
                try:
                    n = approval_store.expire_stale()
                    if n:
                        logger.warning("agent 审批请求过期收尸：pending→expired %s", n)
                except Exception:   # noqa: BLE001
                    logger.warning("审批请求过期收尸失败（下轮重试）", exc_info=True)
                if runtime is not None:
                    try:
                        registry, gateway, executor = runtime
                        n = _reconcile_decided(registry, gateway, executor,
                                               run_store, approval_store)
                        if n:
                            logger.warning("B6 对账：本轮重驱 %s 个 decided-but-not-resumed run", n)
                    except Exception:   # noqa: BLE001
                        logger.warning("B6 对账失败（下轮重试）", exc_info=True)

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
        # 工具治理接线（深度审查多实例运维组）：①代码内声明 upsert 进 tool_registry
        # （status 不覆盖——重启后管理员停用的工具保持停用）②DB 驱动 kill switch 挂进
        # resolve（任一实例停用，全部实例一个 TTL 内生效）③drift 告警。全程 fail-open：
        # DB 抖动绝不阻断 runtime 建立（Policy/审批仍逐调用兜底）。
        registry_store = _get_registry_store()
        try:
            registry_store.sync_specs(registry)
            for w in registry_store.drift_warnings(registry):
                logger.warning("tool_registry 漂移：%s", w)
        except Exception:   # noqa: BLE001
            logger.warning("tool_registry 同步/漂移检查失败（fail-open，进程内注册照常）",
                           exc_info=True)
        registry.attach_disabled_source(registry_store.disabled_names)
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
        _start_reaper(run_store, approval_store, runtime=(registry, gateway, executor))
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
                # streamed=True：全文已按 ModelDelta 增量下发过（真流式），不重发整段——
                # 否则前端看到答案两遍；final_text 仍完整进 on_complete（durable/记忆侧要全文）。
                if ev.final_text and not getattr(ev, "streamed", False):
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
        rid = (get_request_id() or "")[:64]   # llm_call_log/agent_audit_log.request_id VARCHAR(64)（026 加宽；钳制防未迁移环境 1406）
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
                        outcome_kind: str) -> Optional[str]:
    """职责分离 + approver_scope 裁决（深度审查 A 组 P1「审批人=发起人」）。

    - 发起人本人：只允许撤回自己的申请（rejected_terminate）；批准/改参/反馈须他人。
    - 其他人：resolve_kb_identity **DB 权威现查**（绝不信任令牌 role 提示）——kb_admin 恒可审；
      dept_admin 需 approver_scope ∈ managed_owner_depts（dept_admin_grant 显式 seed）。
    - 请求行缺失（approval_store 故障/历史挂起）→ scope 未知 → 只有 kb_admin 可审（fail-closed）。
    """
    requester = run.get("user_id")
    scope = (areq or {}).get("approver_scope") or ""
    # PR-C（P0-06 #4）：注册了 per-tool 解析器的工具在**审批时现算** scope——
    # 提案后 stewardship 变更即时生效（旧 steward 部门不再能凭快照批准）。
    # 未注册工具返回 None → 沿用快照（默认推导语义零变化）；现算异常 '' fail-closed。
    if areq is not None:
        from opensearch_pipeline.agent_runtime.approval_store import resolve_scope_live
        live = resolve_scope_live(areq.get("tool_name"), areq.get("proposed_args"))
        if live is not None:
            scope = live
    if identity.user_id == requester:
        if outcome_kind == "rejected_terminate" or _self_approval_allowed():
            return scope                               # 撤回自己的申请恒允许
        raise HTTPException(status_code=403, detail="发起人不能审批自己的请求（职责分离）")
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline.kb_authz import ROLE_DEPT_ADMIN, ROLE_KB_ADMIN, managed_owner_depts
    kb = resolve_kb_identity(identity.user_id)
    if kb.role == ROLE_KB_ADMIN:
        return scope
    if kb.role == ROLE_DEPT_ADMIN and scope and scope in set(managed_owner_depts(kb)):
        return scope
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
    from opensearch_pipeline.agent_runtime import DefaultAgentLoop, make_model_fn
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
    effective_scope = _authorize_approver(identity, run, areq, outcome.kind)

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
        rid = (get_request_id() or "")[:64]   # llm_call_log/agent_audit_log.request_id VARCHAR(64)（026 加宽；钳制防未迁移环境 1406）
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
    # 铁律 5：resume 重建 ctx——以**发起人**（run.user_id）身份现解 ACL 续跑（共享助手，
    # /approve 与 B6 对账同一套语义：绝不套审批人/对账进程的权限组）。
    thread_id = run.get("thread_id") or req.session_id or req.run_id
    ctx, requester_id, req_groups = _requester_ctx(run, thread_id, rid=rid,
                                                   conversation_id=req.conversation_id)
    loop = DefaultAgentLoop(make_model_fn(gateway, ctx, "light"))   # 默认档=light(不思考/快/省)
    tools = registry.list_specs(ctx)
    message_id, _remember, _report_failure = _resume_callbacks(
        run_store, run, thread_id, requester_id, req_groups)

    # PR-C（P0-06 回链）：审批事实随凭据到执行点——工具落 approval_request_id、
    # confirmed_by 记真实审批人、执行前重验 scope 漂移
    approval_meta = None
    if areq is not None:
        approval_meta = {"request_id": areq.get("request_id"),
                         "decided_by": identity.user_id,
                         "approver_scope": effective_scope}
    try:
        handle = executor.resume(req.run_id, ctx, outcome, loop, tools,
                                 on_complete=_remember, on_failure=_report_failure,
                                 approval_meta=approval_meta)
    except RunRejected:
        raise HTTPException(status_code=409, detail="run 非挂起或已被认领")

    session_id = req.session_id or thread_id
    return StreamingResponse(_stream_events(handle, session_id, message_id),
                             media_type="text/event-stream", headers=_SSE_HEADERS)


def _requester_ctx(run: dict, thread_id: str, rid: str = "", conversation_id=None):
    """以**发起人**（run.user_id）身份现解 ACL 重建 resume ctx（铁律 5）——既不复用
    checkpoint 旧快照，也绝不套审批人/对账进程的身份（否则续跑段提权 + 归属错人）。
    解析失败 → 空组（收敛最小权限，绝不放大）。返回 (ctx, requester_id, req_groups)。"""
    from opensearch_pipeline.agent_runtime import ExecutionContext
    requester_id = run.get("user_id") or ""
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
        conversation_id=conversation_id or run.get("conversation_id"))
    return ctx, requester_id, req_groups


def _resume_callbacks(run_store, run: dict, thread_id: str, requester_id: str,
                      req_groups: list):
    """resume 的完成/失败回调（/approve 与 B6 对账共用）：被批 run 的最终答案进会话记忆 +
    qa_session_log（HIGH_WRITE 高危场景绝不从 durable 回读链静默消失）；失败落 AGENT_ERROR。
    本轮 user 问题从 checkpoint messages 兜底提取（fail-open）。归属恒为发起人。"""
    message_id = generate_message_id()
    cp_question = "[审批后续跑]"
    try:
        cp = run_store.load_latest_checkpoint(run.get("run_id"))
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

    return message_id, _remember, _report_failure


def _reconcile_decided(registry, gateway, executor, run_store, approval_store) -> int:
    """B6 对账：**决定已落库但 run 仍 suspended** 的死单重驱（resume 在 decide 后失败/进程
    崩溃，含 reaper 把 stale resuming 回边 suspended 的场景）。返回重驱数。

    - approved / rejected_feedback / rejected_terminate → 按 approval_decision 重建 outcome
      重发 executor.resume（CAS suspended→resuming 认领，与人工 /approve 重试赛跑安全——
      first-claim-wins）；
    - **edited 不自动重驱**：库里只有脱敏后的 edited_args（022 契约），掩码参数绝不能拿去
      执行——告警留人工（审批人在 console 重提同向决定，/approve 已决同向重放路径接住）；
    - 池满（RunRejected）→ 本轮跳过，下轮再来；单轮限量由 list_decided_unresumed LIMIT 兜。
    """
    import threading

    from opensearch_pipeline.agent_runtime import DefaultAgentLoop, make_model_fn
    from opensearch_pipeline.agent_runtime.approval import (
        Approved, RejectedFeedback, RejectedTerminate)
    from opensearch_pipeline.agent_runtime.executor import RunRejected

    grace = int(os.environ.get("RAG_AGENT_RECONCILE_GRACE_S", "120"))
    driven = 0
    for c in approval_store.list_decided_unresumed(grace_s=grace):
        run_id = c["run_id"]
        kind = c["decision"]
        if kind == "edited":
            logger.warning("B6 对账：run %s 的 EDITED 决定无法自动重驱（库存参数已脱敏），"
                           "请审批人在 console 重试", run_id)
            continue
        run = run_store.get_run(run_id)
        if not run or run.get("status") != "suspended":
            continue                                    # 已被人工重试/过期收尸，让位
        outcome = (Approved() if kind == "approved"
                   else RejectedFeedback(reason=c.get("reason") or "审批未通过")
                   if kind == "rejected_feedback" else RejectedTerminate())
        thread_id = run.get("thread_id") or run_id
        ctx, requester_id, req_groups = _requester_ctx(run, thread_id)
        loop = DefaultAgentLoop(make_model_fn(gateway, ctx, "light"))
        tools = registry.list_specs(ctx)
        _mid, _remember, _report_failure = _resume_callbacks(
            run_store, run, thread_id, requester_id, req_groups)
        try:
            handle = executor.resume(run_id, ctx, outcome, loop, tools,
                                     on_complete=_remember, on_failure=_report_failure,
                                     approval_meta={"request_id": c.get("request_id"),
                                                    "decided_by": c.get("decided_by")})
        except RunRejected:
            logger.warning("B6 对账：run %s 重驱被拒（池满/已被认领），下轮再试", run_id)
            continue
        except Exception:   # noqa: BLE001 — 单条失败不拖垮整轮对账
            logger.warning("B6 对账：run %s 重驱失败（下轮重试）", run_id, exc_info=True)
            continue
        # 无 SSE 消费者：起 daemon 线程排空事件队列（否则事件在队列里堆到 run 结束）
        threading.Thread(target=lambda h=handle: [None for _ in h.events()],
                         name=f"b6-drain-{run_id[:8]}", daemon=True).start()
        try:                                            # 对账重驱入合规审计（fail-open）
            from opensearch_pipeline.agent_runtime.audit import RDSAuditLog
            RDSAuditLog().record(
                None, event_type="approval_reconcile", action=f"run:{run_id}",
                decision=kind, run_id=run_id,
                detail={"request_id": c.get("request_id"), "decided_by": c.get("decided_by"),
                        "decided_at": c.get("decided_at")})
        except Exception:   # noqa: BLE001
            logger.warning("B6 对账审计写失败（fail-open）", exc_info=True)
        logger.warning("B6 对账：重驱 run %s（decision=%s，decided_by=%s）",
                       run_id, kind, c.get("decided_by"))
        driven += 1
    return driven


def _require_kb_admin(identity: Optional[Identity]) -> None:
    """工具治理端点的权限门：kb_admin（DB 权威现查）。报告 §N 设计的 agent_admin 角色
    尚未建（user_role 无此值）——平台治理先归 kb_admin，加 agent_admin 时在此放行。
    管理面与工具执行信道物理分离（OpenClaw 铁律）：本组端点绝不注册为工具、绝不经 LLM 触达。"""
    if identity is None:
        raise HTTPException(status_code=401, detail="需要登录")
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    if resolve_kb_identity(identity.user_id).role != ROLE_KB_ADMIN:
        raise HTTPException(status_code=403, detail="无权管理 Agent 工具（需 kb_admin）")


@router.get("/api/agent/tools")
def agent_tools(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """工具治理视图（kb_admin）：tool_registry 全行 + 当前停用集 + 代码↔DB 漂移告警。"""
    if not _agent_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    _require_kb_admin(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    registry, _gateway, _executor, _run_store = _get_runtime()
    store = _get_registry_store()
    rows = store.list_rows()
    for r in rows:
        r.pop("spec_json", None)                     # 治理视图不回灌全量 schema（噪声）
    return {"items": rows, "disabled": sorted(store.disabled_names()),
            "drift": store.drift_warnings(registry)}


class ToolToggleRequest(BaseModel):
    """kill switch 开关。disabled=true → 全局停用（所有实例一个 TTL 内生效）。"""

    tool_name: str
    disabled: bool
    reason: Optional[str] = None


@router.post("/api/agent/tools/toggle")
def agent_tool_toggle(req: ToolToggleRequest, request: Request,
                      identity: Optional[Identity] = Depends(current_identity)):
    """kill switch（kb_admin）：置 tool_registry.status=disabled/active——DB 是治理事实，
    多实例经 disabled_names TTL 缓存收敛（默认 30s，RAG_AGENT_KILL_SWITCH_TTL_S）。
    操作入合规审计（fail-open）。"""
    if not _agent_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    _require_kb_admin(identity)
    _enforce_rate_limit(request, identity, scope="ask", thinking=False, count_llm=False)
    _get_runtime()                                   # 确保 sync_specs 已跑（表内有行可置）
    store = _get_registry_store()
    status = "disabled" if req.disabled else "active"
    try:
        n = store.set_status(req.tool_name, status)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法 status")
    if n == 0:
        raise HTTPException(status_code=404, detail=f"tool_registry 无工具 {req.tool_name}")
    try:                                             # 拉闸是高风险治理动作：谁在何时停了什么
        from opensearch_pipeline.agent_runtime.audit import RDSAuditLog
        RDSAuditLog().record(
            None, event_type="tool_kill_switch", action=req.tool_name, decision=status,
            detail={"by": identity.user_id, "reason": (req.reason or "")[:500]})
    except Exception:   # noqa: BLE001
        logger.warning("kill switch 审计写失败（fail-open）", exc_info=True)
    logger.warning("kill switch：%s → %s（by %s，reason=%s）",
                   req.tool_name, status, identity.user_id, req.reason)
    return {"tool_name": req.tool_name, "status": status, "rows": n}


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
