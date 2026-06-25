# -*- coding: utf-8 -*-
"""
api.py — RAG 问答 FastAPI 应用

端点：
  POST /api/ask           非流式问答
  POST /api/ask/stream    SSE 流式问答
  POST /api/search        纯检索（不调用 LLM）
  GET  /api/health        健康检查
"""

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse
from pydantic import BaseModel, Field

from opensearch_pipeline.llm_generator import (
    generate_answer,
    generate_answer_stream,
    generate_answer_via_stream,
    is_low_confidence_band,
    parse_sse_data_frame,
    strip_doc_citations,
)
from opensearch_pipeline.retriever import search_chunks, retrieve_and_enrich
from opensearch_pipeline.dingtalk_bot import router as dingtalk_router
from opensearch_pipeline.dingtalk_identity import (
    _exchange_authcode_for_userid,
    _resolve_user_identity,
)
from opensearch_pipeline.qa_logger import generate_message_id, log_qa_session
from opensearch_pipeline.feedback_handler import handle_feedback
from opensearch_pipeline.content_blocks_builder import (
    build_content_blocks,
    content_blocks_to_json,
    build_mini_program_blocks,
    refresh_image_block_urls,
    strip_image_markers,
)
from opensearch_pipeline.auth_token import issue_session_token, verify_session_token
from opensearch_pipeline.rate_limiter import LIMITER, resolve_client_ip
from opensearch_pipeline.answer_flow import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    NO_RESULT_MESSAGE,
    build_qa_log_kwargs,
    is_refusal_answer,
    should_append_history,
)
from opensearch_pipeline.config import get_config
from opensearch_pipeline.session_store import (
    MAX_HISTORY_TURNS,
    append_to_history,
    clear_session,
    get_or_create_session,
)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """应用生命周期：启动时注册钉钉互动卡片 HTTP 回调地址（若已配置 DINGTALK_CARD_CALLBACK_URL），
    并按 DINGTALK_STREAM_MODE 启动钉钉 Stream 客户端（出站 WSS 接收机器人消息+卡片回调）。

    把本服务的 /dingtalk/card/callback 注册到 callbackRouteKey，使反馈按钮点击可回调。
    Stream 模式开启后 HTTP 注册仍保留：存量卡片（创建时 callbackType=HTTP）的按钮
    点击仍走 HTTP 路由，双模并存直到旧卡片自然老化。
    均非致命：失败只记日志、不阻断启动。多 worker 下每个进程各注册一次（幂等）。
    """
    try:
        from opensearch_pipeline.dingtalk_card import register_card_callback
        register_card_callback()
    except Exception:
        logger.warning("启动时注册卡片回调失败（忽略，不影响服务）", exc_info=True)
    try:
        from opensearch_pipeline.dingtalk_stream_runner import start_stream_client
        start_stream_client()
    except Exception:
        logger.warning("启动钉钉 Stream 客户端失败（忽略，HTTP 回调模式继续可用）", exc_info=True)
    try:
        logger.info("Serving 限流配置：%s", LIMITER.describe())
    except Exception:
        logger.warning("读取限流配置失败（忽略）", exc_info=True)
    yield


app = FastAPI(
    title="RAG 知识库问答 API",
    description="基于 OpenSearch HA3 向量检索 + Qwen LLM 的 RAG 问答服务",
    version="0.1.0",
    lifespan=_lifespan,
)

# CORS — 通过环境变量 CORS_ALLOWED_ORIGINS 配置允许的来源（逗号分隔）
# 生产环境示例: CORS_ALLOWED_ORIGINS=https://kb.fuling.com,https://admin.fuling.com
# 未配置时默认允许所有来源但禁用 credentials（安全的开发默认值）
_cors_origins_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
if _cors_origins_raw:
    _cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # 本地开发：允许所有来源但不允许 credentials，避免 Starlette Origin 反射漏洞
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# 钉钉机器人路由
app.include_router(dingtalk_router)




# ═══════════════════════════════════════════════════════════════
# Pydantic Models
# ═══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    # 只允许 user/assistant：客户端注入 role:"system"/"tool" 会被 _build_messages 原样拼进
    # 真实 system prompt 之后 —— 未鉴权的提示注入。非法 role 直接 422（线上无客户端传 history）。
    role: Literal["user", "assistant"] = Field(..., description="消息角色: user / assistant")
    content: str = Field(..., min_length=1, max_length=8000, description="消息内容")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    top_k: int = Field(
        default_factory=lambda: get_config().rag.default_top_k,
        ge=1, le=20, description="检索返回的文档数量（默认取 RAG_TOP_K，评测锁定 7）",
    )
    history: Optional[List[ChatMessage]] = Field(
        None, max_length=40, description="对话历史（最多 40 条 = 20 轮）",
    )
    session_id: Optional[str] = Field(None, description="会话 ID，用于追踪对话")
    temperature: Optional[float] = Field(DEFAULT_TEMPERATURE, ge=0.0, le=2.0, description="生成温度")
    max_tokens: Optional[int] = Field(DEFAULT_MAX_TOKENS, ge=100, le=8192, description="最大生成 token 数")
    user_id: Optional[str] = Field(None, description="用户 ID（钉钉 staffId）；仅用于日志归因，权限部门只从 Bearer 令牌解析")
    user_dept: Optional[str] = Field(
        None,
        description="[已废弃·服务端忽略] 部门一律由服务端按 Bearer 令牌/user_id 解析（防越权）",
        pattern=r'^[\w\-\u4e00-\u9fff]{0,64}$',
    )
    pure_text: Optional[bool] = Field(
        None,
        description="纯文本开关：None 取全局 RAG_PURE_TEXT；True 仅文字回答（不穿插图片）",
    )
    thinking: Optional[bool] = Field(
        None,
        description="深度思考开关（默认关闭）：True 时启用 Qwen3 思考模式，改走流式通道"
                    "服务端攒流（DashScope qwen3 思考仅流式可用）并放宽 max_tokens"
                    "（思考挤占 token 预算会截断答案）。显著更慢（约 3-5 倍），逐问生效。",
    )


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="搜索查询")
    top_k: int = Field(5, ge=1, le=50, description="返回结果数")
    user_id: Optional[str] = Field(None, description="用户 ID（钉钉 staffId）；仅用于日志归因，权限部门只从 Bearer 令牌解析")
    user_dept: Optional[str] = Field(
        None,
        description="[已废弃·服务端忽略] 部门一律由服务端按 Bearer 令牌/user_id 解析（防越权）",
        pattern=r'^[\w\-\u4e00-\u9fff]{0,64}$',
    )


class SourceInfo(BaseModel):
    doc_id: str
    title: str
    section: str = ""
    score: float = 0.0
    # 相关度档位 'high'|'mid'|'low'（与 prompt 高/中/低 同源，见 llm_generator.score_level）。
    # rerank 开启后 score 为 0-1 量纲，客户端必须用本字段、不可按融合阈值重算。
    level: str = ""


class AskResponse(BaseModel):
    answer: str
    sources: List[SourceInfo]
    blocks: List[Dict[str, Any]] = []   # 小程序原生图文渲染块（图文穿插）；纯文字/无引用图片时为 []
    session_id: str
    message_id: str = ""
    model: str
    usage: Dict[str, Any] = {}
    latency_ms: int = 0
    # 知识库未命中：检索为空 或 LLM 拒答（answer_flow.is_refusal_answer，可伴随弱相关来源）。
    # 客户端据此渲染"未找到"空结果卡（隐藏来源/赞踩，保留转人工出口）。
    no_result: bool = False
    # 低置信带：top 检索分 < medium 阈值（llm_generator.is_low_confidence_band，
    # 与 RAG_LOW_CONFIDENCE_GUARD 开关无关）。客户端据此渲染低匹配提示条。
    guard: bool = False
    # 「换个说法」建议（仅 no_result=True 时非空）：优先近 30 天 SUCCESS 过的相似
    # 真实问题（可答性有保证），不足回退清洗版原问题。空结果卡的逃生出口。
    rephrase: List[str] = []


class SearchResult(BaseModel):
    chunk_text: str
    title: str
    section_title: str = ""
    doc_id: str
    category_l1: str = ""
    score: float = 0.0


class SearchResponse(BaseModel):
    results: List[SearchResult]
    total: int
    latency_ms: int = 0


class DingtalkAuthRequest(BaseModel):
    auth_code: str = Field(
        ..., min_length=1, max_length=512,
        description="dd.getAuthCode 返回的免登码（5 分钟、单次有效）",
    )


class DingtalkAuthResponse(BaseModel):
    token: str = Field(..., description="服务端签发的会话令牌，后续请求放入 Authorization: Bearer")
    user_id: str
    display_name: str = ""
    acl_groups: List[str] = Field(default_factory=list, description="用户所属 ACL 权限组（权威）")
    dept: Optional[str] = None  # 旧·兼容：acl_groups 的 CSV
    role: str = Field(default="employee", description="知识库写授权角色：employee/dept_admin/kb_admin")
    can_manage_kb: bool = Field(default=False, description="是否显示「知识库管理」入口（角色为管理员）")


# ═══════════════════════════════════════════════════════════════
# 会话存储（与 dingtalk_bot 共用；import 见文件顶部）
# ═══════════════════════════════════════════════════════════════

# 保持向后兼容（内部调用仍使用下划线命名）
_get_or_create_session = get_or_create_session
_append_to_history = append_to_history


# ═══════════════════════════════════════════════════════════════
# 鉴权依赖（钉钉小程序 Bearer 令牌 → 身份）
# ═══════════════════════════════════════════════════════════════

@dataclass
class Identity:
    user_id: str
    acl_groups: List[str] = field(default_factory=list)  # 权威：ACL 权限组列表
    dept: Optional[str] = None  # 旧·兼容：CSV（acl_groups 的逗号拼接）
    name: str = ""
    role: str = "employee"  # 知识库写授权角色【UI 提示】——非边界；写接口须 DB 现查 resolve_kb_identity


def current_identity(authorization: Optional[str] = Header(None)) -> Optional[Identity]:
    """从 Authorization: Bearer <token> 解析已验证身份；无/无效令牌返回 None。

    ACL 权限组来自服务端签发的令牌，客户端不可篡改；端点据此解析权限，绝不信任请求体。
    优先读新令牌的 acl_groups（数组）；旧令牌只有 dept（CSV/标量）则拆分兼容。
    """
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    payload = verify_session_token(parts[1].strip())
    if not payload:
        return None
    raw_groups = payload.get("acl_groups")
    legacy_dept = payload.get("dept") or None
    if isinstance(raw_groups, list):
        groups = [str(g).strip() for g in raw_groups if str(g).strip()]
    elif legacy_dept:  # 旧令牌：dept 为 CSV 或标量
        groups = [s.strip() for s in str(legacy_dept).split(",") if s.strip()]
    else:
        groups = []
    return Identity(
        user_id=payload.get("uid", ""),
        acl_groups=groups,
        dept=legacy_dept,
        name=payload.get("name", ""),
        role=(payload.get("role") or "employee"),
    )


# ═══════════════════════════════════════════════════════════════
# 公网防刷准入（rate_limiter 四层：限频/匿名IP限额/深思日配额/全局日熔断）
# ═══════════════════════════════════════════════════════════════

def _client_ip(request: Optional[Request]) -> str:
    """从请求解析真实客户端 IP（EIP 直连 / SLB 后两形态自适应，见 resolve_client_ip）。"""
    if request is None or request.client is None:
        return "unknown"
    return resolve_client_ip(request.client.host, request.headers.get("x-forwarded-for"))


def _enforce_rate_limit(request: Optional[Request], identity: Optional[Identity], *,
                        scope: str, thinking: bool = False, count_llm: bool = False) -> None:
    """限流准入：拒绝抛 429/403/503（中文 detail 小程序错误卡直接可见）+ Retry-After。

    计数主体：已验证令牌按 user_id（令牌即身份，限额更宽）；匿名按客户端 IP（严格档）。
    请求体里的 user_id 是未鉴权字段，绝不能用作限流 key（攻击者可随意轮换）。
    限流器自身异常 fail-open 放行——防护组件绝不拖垮回答主链路（项目降级约定）。
    /api/health 与 /dingtalk/*（验签 + Stream 出站）不经过本函数。
    """
    denial = None
    try:
        if identity and identity.user_id:
            actor, is_user = f"u:{identity.user_id}", True
        else:
            actor, is_user = f"ip:{_client_ip(request)}", False
        if scope == "ask":
            denial = LIMITER.admit_ask(actor, is_user=is_user,
                                       thinking=thinking, count_llm=count_llm)
        else:
            denial = LIMITER.admit_aux(actor)
    except Exception:
        logger.warning("限流器内部异常（fail-open 放行）", exc_info=True)
        return
    if denial is not None:
        headers = {"Retry-After": str(denial.retry_after)} if denial.retry_after > 0 else None
        raise HTTPException(status_code=denial.status_code, detail=denial.message,
                            headers=headers)


# ═══════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health_check():
    """Liveness stub — process is up. Use /api/ready for dependency health."""
    return {"status": "ok", "service": "rag-qa-api"}


@app.get("/api/ready")
async def readiness_check():
    """OBS-1 deep readiness probe: RDS + HA3 (critical) + DashScope (config-only, no live cost).

    200 when the critical deps (RDS + HA3) are reachable, else 503 with the failing component — so a
    load balancer / SAE / k8s readiness probe can stop routing to an instance that can't actually
    answer (the old /api/health never probed anything). Simulate mode returns 200/skipped (no live
    calls). DashScope is reported by config presence only (a live ping would cost quota).
    """
    from fastapi.responses import JSONResponse
    cfg = get_config()
    if getattr(cfg, "simulate", False):
        return {"status": "ok", "mode": "simulate", "rds": "skipped", "ha3": "skipped", "dashscope": "skipped"}

    checks: Dict[str, str] = {}
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            checks["rds"] = "ok"
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - readiness must report, not raise
        checks["rds"] = f"error: {str(e)[:80]}"

    try:
        from opensearch_pipeline.retriever import _get_ha3_client
        from alibabacloud_ha3engine_vector.models import QueryRequest
        client = _get_ha3_client()
        if client == "MOCK_HA3_CLIENT":
            checks["ha3"] = "skipped"
        else:
            client.query(QueryRequest(
                table_name=cfg.alibaba_vector.table_name, vector=[0.0] * 1024, top_k=1,
                include_vector=False, output_fields=["id"], filter="id>=0 AND id<1"))
            checks["ha3"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["ha3"] = f"error: {str(e)[:80]}"

    checks["dashscope"] = "configured" if getattr(cfg.embedding, "api_key", None) else "unconfigured"

    critical_ok = checks.get("rds") == "ok" and checks.get("ha3") in ("ok", "skipped")
    body = {"status": "ok" if critical_ok else "degraded", **checks}
    return body if critical_ok else JSONResponse(status_code=503, content=body)


@app.post("/api/auth/dingtalk", response_model=DingtalkAuthResponse)
def auth_dingtalk(req: DingtalkAuthRequest, request: Request):
    """钉钉小程序免登：authCode → userid → 部门/姓名 → 签发会话令牌。

    部门在服务端解析后写入令牌；客户端只持有短期 authCode 与签发的令牌，
    AppSecret / 签名密钥永不下发到客户端。模拟模式下返回测试用户（见 _exchange_authcode_for_userid）。
    """
    # 按 IP 限频（身份尚未建立）：滥打 authCode 烧钉钉 OpenAPI 配额，可能拖垮发卡链路
    _enforce_rate_limit(request, None, scope="aux")
    userid = _exchange_authcode_for_userid(req.auth_code)
    if not userid:
        raise HTTPException(status_code=401, detail="免登失败：authCode 无效或已过期")
    ident = _resolve_user_identity(userid)
    groups = ident.get("dept") or []  # _resolve_user_identity 现返回 ACL 组列表
    # 知识库写授权角色（DB 现查；写入令牌仅作入口可见性 UI 提示，特权接口仍会再现查裁决）
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline.kb_authz import can_access_console
    kb_ident = resolve_kb_identity(userid)
    token = issue_session_token(userid, dept=groups, name=ident.get("name"), role=kb_ident.role)
    return DingtalkAuthResponse(
        token=token,
        user_id=userid,
        display_name=ident.get("name") or "",
        acl_groups=groups,
        dept=",".join(groups) if isinstance(groups, list) else (groups or None),
        role=kb_ident.role,
        can_manage_kb=can_access_console(kb_ident),
    )


@app.post("/api/search", response_model=SearchResponse)
def search(req: SearchRequest, request: Request,
           identity: Optional[Identity] = Depends(current_identity)):
    """纯检索接口 — 只返回相关文档片段，不调用 LLM。"""
    # 与问答共享限频/日配额（embedding+HA3 也是真金白银），但不计入全局 LLM 熔断
    _enforce_rate_limit(request, identity, scope="ask", count_llm=False)
    t0 = time.time()
    try:
        # 权限部门仅来自已验证的 Bearer 令牌；无令牌一律按匿名处理（仅 public 文档）。
        # 请求体里的身份字段绝不能反查部门授予 dept_internal 权限。
        user_dept = identity.acl_groups if identity else None
        results = search_chunks(req.query, top_k=req.top_k, user_dept=user_dept)
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("Search failed [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败，请联系管理员 (trace: {trace_id})")

    latency = int((time.time() - t0) * 1000)
    return SearchResponse(
        results=[SearchResult(**r) for r in results],
        total=len(results),
        latency_ms=latency,
    )


def _prepare_ask(req: AskRequest, identity: Optional["Identity"], *, cosurface_images: bool = False):
    """/api/ask 与 /api/ask/stream 共用的前置段：会话管理、客户端历史合并、
    身份/部门解析（仅信 Bearer 令牌）、检索 + 计时。

    检索失败统一抛 HTTPException(500)（流式端点也要求在返回 StreamingResponse 之前抛出）。
    retrieve_and_enrich 经本模块全局名调用，保持测试 monkeypatch(api.retrieve_and_enrich) 接缝。
    """
    t0 = time.time()

    # 1. 会话管理
    session_id, session_history = _get_or_create_session(req.session_id)

    # 合并客户端传入的 history 和服务端 session history（客户端显式传了则优先）
    merged_history = list(session_history)
    if req.history:
        merged_history = [{"role": m.role, "content": m.content} for m in req.history]
    # 与服务端会话同一裁剪策略（保留最近 N 轮），防止客户端 history 撑爆 LLM 上下文
    max_messages = MAX_HISTORY_TURNS * 2
    if len(merged_history) > max_messages:
        merged_history = merged_history[-max_messages:]

    uid = identity.user_id if identity else (req.user_id or "")
    # 权限部门仅来自已验证的 Bearer 令牌；无令牌一律按匿名处理（仅 public 文档）。
    # 请求体里的 user_id 只用于日志归因，绝不能反查部门授予 dept_internal 权限。
    user_dept = identity.acl_groups if identity else None

    # 2. 检索
    try:
        chunks = retrieve_and_enrich(
            req.question, top_k=req.top_k, user_dept=user_dept,
            cosurface_images=cosurface_images,
        )
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("Search failed [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败，请联系管理员 (trace: {trace_id})")

    t_retrieval = time.time()
    retrieval_latency_ms = int((t_retrieval - t0) * 1000)
    return t0, session_id, merged_history, uid, user_dept, chunks, t_retrieval, retrieval_latency_ms


@app.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest, request: Request,
        identity: Optional[Identity] = Depends(current_identity)):
    """非流式问答接口 — 检索 + LLM 一次性返回。

    用 def（非 async）声明：内部全是同步阻塞 I/O（embedding HTTP、HA3、pymysql、LLM
    requests.post），FastAPI 会把 def 处理器放进线程池执行，避免阻塞事件循环、拖垮并发请求。
    """
    # 防刷准入：须在 _prepare_ask（embedding/HA3/rerank 开销）之前拒绝
    _enforce_rate_limit(request, identity, scope="ask",
                        thinking=bool(req.thinking), count_llm=True)
    (t0, session_id, merged_history, uid, user_dept,
     chunks, t_retrieval, retrieval_latency_ms) = _prepare_ask(req, identity)

    if not chunks:
        latency = int((time.time() - t0) * 1000)
        msg_id = generate_message_id()
        log_qa_session(**build_qa_log_kwargs(
            session_id=session_id,
            message_id=msg_id,
            question=req.question,
            user_id=uid,
            user_dept=user_dept,
            chunks=[],
            latency_ms=latency,
            retrieval_latency_ms=retrieval_latency_ms,
            answer_status="NO_RESULT",
        ))
        return AskResponse(
            answer=NO_RESULT_MESSAGE,
            sources=[],
            session_id=session_id,
            message_id=msg_id,
            model="N/A",
            usage={},
            latency_ms=latency,
            no_result=True,
            rephrase=_suggest_rephrase(req.question),
        )

    # 3. LLM 生成。深度思考（req.thinking）走流式通道服务端攒流：DashScope qwen3
    #    思考仅流式可用；同时放宽 max_tokens —— 思考挤占 token 预算会截断答案（实测）。
    try:
        if req.thinking:
            result = generate_answer_via_stream(
                req.question,
                chunks,
                history=merged_history if merged_history else None,
                max_tokens=max(req.max_tokens or DEFAULT_MAX_TOKENS, 4096),
                temperature=req.temperature or DEFAULT_TEMPERATURE,
                pure_text=req.pure_text,
                thinking=True,
            )
            # 深思标记进 model 名（响应与落库同值）：qa_session_log 无独立列，
            # "+thinking" 后缀让后续成本/质量分析能按模式分组（LIKE '%+thinking'）。
            result["model"] = f"{result.get('model') or ''}+thinking"
        else:
            result = generate_answer(
                req.question,
                chunks,
                history=merged_history if merged_history else None,
                max_tokens=req.max_tokens or DEFAULT_MAX_TOKENS,
                temperature=req.temperature or DEFAULT_TEMPERATURE,
                pure_text=req.pure_text,
            )
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("LLM generation failed [trace=%s]: %s", trace_id, e, exc_info=True)
        # 失败也落库：此前这里只回 500、不留任何记录，是四条链路中唯一的"无痕失败"
        log_qa_session(**build_qa_log_kwargs(
            session_id=session_id,
            message_id=generate_message_id(),
            question=req.question,
            user_id=uid,
            user_dept=user_dept,
            chunks=chunks,
            latency_ms=int((time.time() - t0) * 1000),
            retrieval_latency_ms=retrieval_latency_ms,
            answer_status="LLM_ERROR",
            error_message=f"[trace={trace_id}] {str(e)[:500]}",
        ))
        raise HTTPException(status_code=500, detail=f"回答生成失败，请联系管理员 (trace: {trace_id})")

    # [文档N] 编号引用清洗：generate_answer 已在源头清过，这里再过一遍是服务层契约
    # （/api/ask 出口绝不带内部编号）。与 <<IMG:N>> 不同：编号引用对 blocks/历史/落库
    # 都没有下游用途，且入史会诱导后续轮模仿 → 在一切消费之前清理。
    result["answer"] = strip_doc_citations(result["answer"])

    # 4. 更新会话历史（统一策略：仅非空 SUCCESS 回答入史）
    if should_append_history(result["answer"], "SUCCESS"):
        _append_to_history(session_id, req.question, result["answer"])

    t_llm = time.time()
    llm_latency_ms = int((t_llm - t_retrieval) * 1000)
    latency = int((t_llm - t0) * 1000)
    msg_id = generate_message_id()

    # 小程序图文渲染块（复用 build_content_blocks 核心；纯文字/未引用图片时为 []）。
    # 先于落库构建：实际下发给客户端的块也要进 qa_session_log（latency 已在 t_llm 定格，不受影响）。
    try:
        blocks = build_mini_program_blocks(result["answer"], chunks)
    except Exception:
        logger.warning("mini-program blocks 构建失败 (non-fatal)", exc_info=True)
        blocks = []

    # 响应契约：blocks 必须先用【原始 answer】构建（穿插位置依赖占位符），
    # 之后才清理客户端可见文本 —— blocks 为空时小程序把 answer 当纯文本渲染，
    # 残留 <<IMG:N>> 会原样打给用户。qa_session_log / 会话历史仍存原始 answer（日志保真）。
    answer_out = strip_image_markers(result["answer"])
    resp_no_result = is_refusal_answer(answer_out)   # 拒答形态（可伴随弱相关来源）
    resp_guard = is_low_confidence_band(chunks)      # 不依赖 RAG_LOW_CONFIDENCE_GUARD 开关

    # 5. 落库。拒答型回答（检索有候选但 LLM 按护栏拒答）标 REFUSAL，与 NO_RESULT
    #    （检索为空，前面已 return）分桶 —— 语料排查一句 SQL 区分「缺语料」vs
    #    「语料弱/未召回」。入史策略不变（拒答照旧入史，只改落库状态）。
    log_qa_session(**build_qa_log_kwargs(
        session_id=session_id,
        message_id=msg_id,
        question=req.question,
        user_id=uid,
        user_dept=user_dept,
        answer_text=result["answer"],
        chunks=chunks,
        cited_docs=result.get("sources"),
        latency_ms=latency,
        retrieval_latency_ms=retrieval_latency_ms,
        llm_latency_ms=llm_latency_ms,
        answer_status="REFUSAL" if resp_no_result else "SUCCESS",
        model_name=result.get("model"),
        content_blocks_json=content_blocks_to_json(blocks) if blocks else None,
    ))

    return AskResponse(
        answer=answer_out,
        sources=[SourceInfo(**s) for s in result["sources"]],
        blocks=blocks,
        session_id=session_id,
        message_id=msg_id,
        model=result["model"],
        usage=result["usage"],
        latency_ms=latency,
        no_result=resp_no_result,
        guard=resp_guard,
        rephrase=_suggest_rephrase(req.question) if resp_no_result else [],
    )


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@app.post("/api/ask/stream")
def ask_stream(req: AskRequest, request: Request,
               identity: Optional[Identity] = Depends(current_identity)):
    """SSE 流式问答接口 — 检索 + LLM 逐 token 输出。

    SSE 事件格式：
        data: {"type": "session", "session_id": "...", "message_id": "..."}
        data: {"type": "sources", "sources": [...]}
        data: {"type": "chunk", "content": "..."}
        data: {"type": "done", "model": "...", "usage": {...}}
        data: {"type": "content_blocks", "content_blocks": [...]}   # 图文模式且有被引用图片时
        data: [DONE]

    说明：
      - `session` 帧携带 `message_id` —— 流式回答现已落库（qa_session_log）且写入会话历史，
        客户端可凭此 `message_id` 调用 /api/feedback 提交反馈（与 /api/ask 一致）。
      - 单个 <<IMG:N>> 标记可能被拆分到多个 chunk 帧中；需要渲染图片的客户端应以结束前的
        `content_blocks` 帧为准（权威定稿），而非从流式文本里解析标记。图片只能在全文生成
        完成后定稿，故 `content_blocks` 帧总是在 `done` 之后、`[DONE]` 之前发出。
    """
    import json

    # 防刷准入：与 /api/ask 同层（在检索开销与 StreamingResponse 之前拒绝，普通 JSON 4xx/5xx）
    _enforce_rate_limit(request, identity, scope="ask",
                        thinking=bool(req.thinking), count_llm=True)

    # 图文模式才补充图片（纯文本模式不展示图片，跳过 co-surfacing 的额外 HA3 查询）
    _pure = req.pure_text if req.pure_text is not None else get_config().rag.pure_text

    # 前置段与 /api/ask 共用；检索失败在返回 StreamingResponse 之前即抛 500
    (t0, session_id, merged_history, uid, user_dept,
     chunks, _t_retrieval, retrieval_latency_ms) = _prepare_ask(
        req, identity, cosurface_images=not _pure)
    message_id = generate_message_id()

    # 无结果：仍发出 message_id 并落库（NO_RESULT），与 /api/ask 空结果分支保持一致
    if not chunks:

        def empty_gen():
            # 同步生成器：StreamingResponse 会在线程池迭代它，finally 里的阻塞 log_qa_session
            # 不会阻塞事件循环。落库放 finally：客户端中途断开（GeneratorExit）时仍保证 NO_RESULT
            # 落库，与主流式路径的 finally 收尾保持一致。
            try:
                yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'message_id': message_id}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'chunk', 'content': NO_RESULT_MESSAGE}, ensure_ascii=False)}\n\n"
                # done 帧带 no_result + rephrase：让流式前端也能渲染结构化空结果卡（换个说法 + 转人工）
                yield f"data: {json.dumps({'type': 'done', 'model': 'N/A', 'usage': {}, 'no_result': True, 'rephrase': _suggest_rephrase(req.question)}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                log_qa_session(**build_qa_log_kwargs(
                    session_id=session_id,
                    message_id=message_id,
                    question=req.question,
                    user_id=uid,
                    user_dept=user_dept,
                    chunks=[],
                    latency_ms=int((time.time() - t0) * 1000),
                    retrieval_latency_ms=retrieval_latency_ms,
                    answer_status="NO_RESULT",
                ))

        return StreamingResponse(empty_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # 3. SSE 流式生成
    def event_generator():
        # 先发 session_id + message_id（反馈关联键）
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'message_id': message_id}, ensure_ascii=False)}\n\n"

        collected_answer: List[str] = []
        model_name: Optional[str] = None
        answer_status = "SUCCESS"
        error_message: Optional[str] = None
        content_blocks_json_str: Optional[str] = None

        try:
            try:
                _stream_guard = bool(is_low_confidence_band(chunks))   # 低置信带：补进 done 帧供前端渲染提示条
                for event in generate_answer_stream(
                    req.question,
                    chunks,
                    history=merged_history if merged_history else None,
                    # 思考挤占 token 预算会截断答案：thinking 时放宽（与 /api/ask 同策略）
                    max_tokens=(max(req.max_tokens or DEFAULT_MAX_TOKENS, 4096)
                                if req.thinking else (req.max_tokens or DEFAULT_MAX_TOKENS)),
                    temperature=req.temperature or DEFAULT_TEMPERATURE,
                    pure_text=req.pure_text,
                    thinking=req.thinking,
                ):
                    # 截获生成器自带的 [DONE]，改由本函数在 content_blocks 之后统一收尾
                    if event.strip() == "data: [DONE]":
                        continue
                    frame = parse_sse_data_frame(event)
                    # done 帧补 guard 后再下发（流式也能显示低置信提示条）；其余帧原样透传
                    if frame is not None and frame.get("type") == "done":
                        model_name = frame.get("model")
                        frame["guard"] = _stream_guard
                        yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
                        continue
                    yield event
                    # 收集完整回答（用于写历史 & 落库）
                    if frame is not None and frame.get("type") == "chunk" and frame.get("content"):
                        collected_answer.append(frame["content"])

                # 正常完成：图文模式下补发 content_blocks 帧（图片须在全文完成后定稿）。
                # [文档N] 引用清洗只能作用在定稿（chunk 帧已流出，标记可能跨帧切断）：
                # 流中残留靠 prompt 规则 8 压制，blocks/历史/落库由这里兜底。
                full_answer = strip_doc_citations("".join(collected_answer))
                if full_answer and not _pure:
                    try:
                        blocks = build_content_blocks(full_answer, chunks)
                        if blocks:
                            yield f"data: {json.dumps({'type': 'content_blocks', 'content_blocks': blocks}, ensure_ascii=False)}\n\n"
                            content_blocks_json_str = content_blocks_to_json(blocks)
                    except Exception:
                        logger.warning("content_blocks 构建失败 (non-fatal)", exc_info=True)

                yield "data: [DONE]\n\n"

            except Exception as e:
                answer_status = "LLM_ERROR"
                trace_id = uuid.uuid4().hex[:8]
                error_message = f"[trace={trace_id}] {str(e)[:500]}"
                logger.error("Stream generation failed [trace=%s]: %s", trace_id, e, exc_info=True)
                error_msg = json.dumps({"type": "error", "message": f"回答生成失败，请联系管理员 (trace: {trace_id})"}, ensure_ascii=False)
                yield f"data: {error_msg}\n\n"
                yield "data: [DONE]\n\n"
        finally:
            # 无论正常结束还是客户端中途断开（GeneratorExit），都写历史 + 落库，
            # 保证流式回答可被反馈/分析（落库函数自身吞掉异常，绝不影响回复）
            full_answer = strip_doc_citations("".join(collected_answer))
            # 统一策略：仅非空 SUCCESS 回答入史 —— 出错前的半截回答不再污染后续轮次上下文
            if should_append_history(full_answer, answer_status):
                _append_to_history(session_id, req.question, full_answer)
            # 拒答型标 REFUSAL（入史判定在上面、用原状态 —— 历史策略不变）；深思加 model 后缀
            if answer_status == "SUCCESS" and is_refusal_answer(full_answer):
                answer_status = "REFUSAL"
            if req.thinking and model_name:
                model_name = f"{model_name}+thinking"
            log_qa_session(**build_qa_log_kwargs(
                session_id=session_id,
                message_id=message_id,
                question=req.question,
                user_id=uid,
                user_dept=user_dept,
                answer_text=full_answer,
                chunks=chunks,
                latency_ms=int((time.time() - t0) * 1000),
                retrieval_latency_ms=retrieval_latency_ms,
                answer_status=answer_status,
                model_name=model_name,
                error_message=error_message,
                content_blocks_json=content_blocks_json_str,
            ))

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ═══════════════════════════════════════════════════════════════
# 反馈接口
# ═══════════════════════════════════════════════════════════════

class FeedbackRequest(BaseModel):
    message_id: str = Field(..., description="关联的 qa_session_log.message_id")
    user_id: str = Field("", description="反馈用户 ID")
    feedback_type: str = Field(..., description="upvote / downvote / handoff")
    feedback_reason: Optional[str] = Field(None, description="反馈原因代码")
    feedback_comment: Optional[str] = Field(None, description="反馈备注")


@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest, request: Request,
                    identity: Optional[Identity] = Depends(current_identity)):
    """反馈接口 — 供前端/管理后台使用。"""
    _enforce_rate_limit(request, identity, scope="aux")
    # 有令牌时以令牌身份为准，避免客户端伪造 user_id（uk_message_user 去重才可信）
    uid = identity.user_id if identity else req.user_id
    success = handle_feedback(
        message_id=req.message_id,
        user_id=uid,
        action=req.feedback_type,
        reason=req.feedback_reason,
        comment=req.feedback_comment,
    )
    if not success:
        raise HTTPException(status_code=500, detail="反馈处理失败")
    return {"status": "ok", "message_id": req.message_id}


class SessionClearRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=128, description="要清除的会话 ID")


@app.post("/api/session/clear")
def session_clear(req: SessionClearRequest, request: Request,
                  identity: Optional[Identity] = Depends(current_identity)):
    """清除服务端会话历史（小程序「清除会话」/ 钉钉「新会话」）。

    鉴权与 /api/ask 同为可选 Bearer；但 'miniapp:<staffId>' 是可预测的命名空间
    （chat.js 用 'miniapp:'+userId 构造），必须校验令牌归属，防止匿名清掉他人会话上下文。
    其余 session_id（服务端 UUID / 钉钉会话 key，不可枚举）按持有即所有处理。
    幂等：会话不存在/已过期返回 200 + cleared=false（客户端结果一致：下一轮是全新会话）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    if req.session_id.startswith("miniapp:"):
        if not identity or req.session_id != f"miniapp:{identity.user_id}":
            raise HTTPException(status_code=403, detail="无权清除该会话")
    cleared = clear_session(req.session_id)
    return {"status": "ok", "cleared": cleared, "session_id": req.session_id}


# ═══════════════════════════════════════════════════════════════
# 小程序辅助接口：过期图重签 / 历史问答 / 猜你想问
# ═══════════════════════════════════════════════════════════════

# 重签白名单：仅 ingestion 上传的抽取图片（pipeline_nodes._upload_clean_assets 的
# key 规则 processing/assets/{dept}/{doc_id}/v{n}/{file}）+ 图片扩展名。
# 本接口绝不能变成任意 OSS 对象读取器 —— raw/ 下是未脱敏原始文档。
_RESIGN_ALLOWED_PREFIX = "processing/assets/"
_RESIGN_ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


class ResignImagesRequest(BaseModel):
    oss_keys: List[str] = Field(
        ..., min_length=1, max_length=10,
        description="待重签的图片 oss_key 列表（来自 blocks[].oss_key）",
    )


@app.post("/api/resign-images")
def resign_images(req: ResignImagesRequest, request: Request,
                  identity: Optional[Identity] = Depends(current_identity)):
    """过期图片重签：OSS 签名 URL 默认 1 小时过期，客户端凭 blocks 里的
    oss_key 换取新签名 URL（「图片已过期 · 点按重新加载」的真实后半段）。

    单 key 失败/非法不影响其它 key（返回空串，客户端保留过期占位态）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    from opensearch_pipeline.oss_url import generate_signed_url

    urls: Dict[str, str] = {}
    for raw_key in req.oss_keys:
        key = (raw_key or "").strip()
        if not key:
            continue
        allowed = (
            key.startswith(_RESIGN_ALLOWED_PREFIX)
            and key.lower().endswith(_RESIGN_ALLOWED_EXT)
            and ".." not in key
        )
        if not allowed:
            logger.warning("resign-images 拒绝白名单外 key: %r", key[:128])
            urls[key] = ""
            continue
        try:
            urls[key] = generate_signed_url(key)
        except Exception:
            logger.warning("resign-images 签名失败: %s", key, exc_info=True)
            urls[key] = ""
    return {"urls": urls}


class HistoryItem(BaseModel):
    message_id: str
    question: str
    answer: str = ""
    blocks: List[Dict[str, Any]] = []
    created_at: str = ""
    status: str = ""  # SUCCESS / NO_RESULT / LLM_ERROR


class HistoryResponse(BaseModel):
    items: List[HistoryItem]
    has_more: bool = False


@app.get("/api/history", response_model=HistoryResponse)
def history(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    identity: Optional[Identity] = Depends(current_identity),
):
    """历史问答（仅本人）：按 Bearer 令牌身份查 qa_session_log。

    强制鉴权 —— user_id 是查询主键，匿名无从归属（也防扫他人记录）。
    blocks 经 refresh_image_block_urls 重签，历史里的图文答案可直接渲染；
    answer 字段已剥离 <<IMG:N>> 占位符（落库保留原始 answer 的约定不变）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="历史记录需要登录后查看")
    limit = max(1, min(limit, 50))
    offset = max(0, offset)

    from opensearch_pipeline.qa_logger import _op_db

    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT message_id, query_text, answer_text, content_blocks_json,
                           created_at, answer_status
                    FROM {_op_db()}.qa_session_log
                    WHERE user_id = %s AND query_text IS NOT NULL AND query_text != ''
                    ORDER BY id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (identity.user_id, limit + 1, offset),
                )
                rows = cursor.fetchall()
        finally:
            conn.close()
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("history 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"历史记录查询失败 (trace: {trace_id})")

    has_more = len(rows) > limit
    items: List[HistoryItem] = []
    for row in rows[:limit]:
        message_id, question, answer_text, blocks_json, created_at, status = row
        blocks: List[Dict[str, Any]] = []
        if blocks_json:
            try:
                blocks = json.loads(refresh_image_block_urls(
                    blocks_json if isinstance(blocks_json, str)
                    else json.dumps(blocks_json, ensure_ascii=False)
                ))
            except Exception:
                blocks = []  # 重签/解析失败退回纯文字（fail open）
        items.append(HistoryItem(
            message_id=message_id or "",
            question=question or "",
            answer=strip_image_markers(answer_text or ""),
            blocks=blocks,
            created_at=str(created_at) if created_at else "",
            status=status or "",
        ))
    return HistoryResponse(items=items, has_more=has_more)


# 猜你想问：近 30 天高频 SUCCESS 问题（进程内缓存 1 小时；DB 不可用回退静态默认）。
# 与 chat 页静态 chips 保持同一兜底集，客户端取接口失败时显示一致。
_HOT_QUESTIONS_FALLBACK = ["U8+ 如何登录？", "请假流程是什么？", "访客 WiFi 密码是多少？"]
_HOT_QUESTIONS_TTL_S = 3600
_hot_questions_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
# 不计入热门的控制指令与联调账号
_HOT_Q_EXCLUDE_TEXT = ("新会话", "重新开始")
_HOT_Q_EXCLUDE_USER_PREFIX = ("miniapp-proto", "eval-", "test-")


@app.get("/api/hot-questions")
def hot_questions(request: Request,
                  identity: Optional[Identity] = Depends(current_identity)):
    """真实高频问题 top-N，驱动 chat 页「示例问题」快捷栏。"""
    _enforce_rate_limit(request, identity, scope="aux")
    now = time.time()
    cached = _hot_questions_cache["data"]
    if cached is not None and now - _hot_questions_cache["ts"] < _HOT_QUESTIONS_TTL_S:
        return {"questions": cached}

    from opensearch_pipeline.qa_logger import _op_db

    questions: List[str] = []
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cursor:
                user_excludes = " AND ".join(
                    "user_id NOT LIKE %s" for _ in _HOT_Q_EXCLUDE_USER_PREFIX
                )
                cursor.execute(
                    f"""
                    SELECT query_text, COUNT(*) AS cnt
                    FROM {_op_db()}.qa_session_log
                    WHERE answer_status = 'SUCCESS'
                      AND created_at >= NOW() - INTERVAL 30 DAY
                      AND CHAR_LENGTH(query_text) BETWEEN 4 AND 30
                      AND {user_excludes}
                    GROUP BY query_text
                    HAVING cnt >= 2
                    ORDER BY cnt DESC, MAX(id) DESC
                    LIMIT 20
                    """,
                    tuple(p + "%" for p in _HOT_Q_EXCLUDE_USER_PREFIX),
                )
                for text, _cnt in cursor.fetchall():
                    t = (text or "").strip()
                    if not t or t in _HOT_Q_EXCLUDE_TEXT:
                        continue
                    questions.append(t)
                    if len(questions) >= 6:
                        break
        finally:
            conn.close()
    except Exception:
        logger.warning("hot-questions 查询失败，使用静态兜底", exc_info=True)

    # 不足 3 条时用静态默认补齐（去重保序）
    for q in _HOT_QUESTIONS_FALLBACK:
        if len(questions) >= 3:
            break
        if q not in questions:
            questions.append(q)

    _hot_questions_cache["ts"] = now
    _hot_questions_cache["data"] = questions
    return {"questions": questions}


# ── NO_RESULT「换个说法」建议 ─────────────────────────────────────
# 设计依据：模板/LLM 改写都不保证改写后可答（LLM 改写在本链路已 A/B 为 dark），
# 而近 30 天 SUCCESS 过的真实问题可答性有保证 —— 按字符 bigram 重叠推荐相似者，
# 不足时回退「清洗版原问题」。纯启发式、零 LLM 成本、fail open。

_SUCCESS_POOL_TTL_S = 3600
_success_pool_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_REPHRASE_NOISE_PREFIX = (
    "请问一下", "请问", "你好", "您好", "帮我查一下", "帮我查", "帮我",
    "我想知道", "我想问一下", "我想问", "想问一下", "问一下", "请教一下", "请教",
)


def _success_question_pool() -> List[str]:
    """近 30 天 SUCCESS 问题池（去重、4-40 字、排除控制指令/联调账号；缓存 1h）。"""
    now = time.time()
    cached = _success_pool_cache["data"]
    if cached is not None and now - _success_pool_cache["ts"] < _SUCCESS_POOL_TTL_S:
        return cached

    from opensearch_pipeline.qa_logger import _op_db

    pool: List[str] = []
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cursor:
                user_excludes = " AND ".join(
                    "user_id NOT LIKE %s" for _ in _HOT_Q_EXCLUDE_USER_PREFIX
                )
                cursor.execute(
                    f"""
                    SELECT query_text, COUNT(*) AS cnt
                    FROM {_op_db()}.qa_session_log
                    WHERE answer_status = 'SUCCESS'
                      AND created_at >= NOW() - INTERVAL 30 DAY
                      AND CHAR_LENGTH(query_text) BETWEEN 4 AND 40
                      AND {user_excludes}
                    GROUP BY query_text
                    ORDER BY cnt DESC
                    LIMIT 200
                    """,
                    tuple(p + "%" for p in _HOT_Q_EXCLUDE_USER_PREFIX),
                )
                for text, _cnt in cursor.fetchall():
                    t = (text or "").strip()
                    if t and t not in _HOT_Q_EXCLUDE_TEXT:
                        pool.append(t)
        finally:
            conn.close()
    except Exception:
        logger.warning("success 问题池查询失败（rephrase 退化为仅清洗原问题）", exc_info=True)

    _success_pool_cache["ts"] = now
    _success_pool_cache["data"] = pool
    return pool


def _char_bigrams(s: str) -> set:
    """字符 bigram 集（仅保留中日韩/字母数字 —— 中文无需分词的相似度量纲）。"""
    chars = [c for c in s if c.isalnum() or "一" <= c <= "鿿"]
    return {"".join(chars[i:i + 2]) for i in range(len(chars) - 1)}


def _suggest_rephrase(question: str, limit: int = 2) -> List[str]:
    """NO_RESULT 出口的「换个说法」建议（fail open 返回 []）。"""
    try:
        q = (question or "").strip()
        if not q:
            return []
        q_grams = _char_bigrams(q)
        out: List[str] = []
        if q_grams:
            scored = []
            for cand in _success_question_pool():
                if cand == q:
                    continue
                c_grams = _char_bigrams(cand)
                if not c_grams:
                    continue
                inter = len(q_grams & c_grams)
                if not inter:
                    continue
                jac = inter / len(q_grams | c_grams)
                # 太像（>0.85）≈ 同一问法换标点，再问大概率还是 NO_RESULT；太弱（<0.12）不相关
                if 0.12 <= jac < 0.85:
                    scored.append((jac, cand))
            scored.sort(key=lambda x: -x[0])
            out = [c for _, c in scored[:limit]]

        # 不足时回退：剥掉客套前缀/尾标点的清洗版原问题（确有变化才建议）
        if len(out) < limit:
            cleaned = q
            for p in _REPHRASE_NOISE_PREFIX:
                if cleaned.startswith(p):
                    cleaned = cleaned[len(p):].strip("，, ")
                    break
            cleaned = cleaned.rstrip("？?。！!")
            if cleaned and cleaned != q.rstrip("？?。！!") and len(cleaned) >= 4 and cleaned not in out:
                out.append(cleaned)
        return out[:limit]
    except Exception:
        logger.warning("rephrase 建议生成失败（忽略）", exc_info=True)
        return []


# ═══════════════════════════════════════════════════════════════
# 知识库管理（部门管理员）— Phase 0 只读接口
# 全部【只读】(SELECT)，PROD-RO 安全；授权一律【先现查】resolve_kb_identity（DB 权威，
# 撤销管理员即时生效），非管理员在任何 DB 查询【之前】403。owner_dept 作用域：kb_admin 全量，
# dept_admin 仅其 dept_admin_grant 授予的 owner_dept（绝不用读组推导）。
# ═══════════════════════════════════════════════════════════════

# 10 个 ACL 组的中文标签（权限选择器展示用；单一来源 retriever._VALID_ACL_GROUPS）
_KB_ACL_GROUP_LABELS = {
    "finance": "财务", "it": "信息技术", "marketing": "营销", "production": "生产",
    "pmc": "计划 PMC", "admin": "行政", "hr": "人力资源", "rd": "研发",
    "quality": "品质技术", "supply": "资材供应",
}


class KbOrgTreeResponse(BaseModel):
    acl_groups: List[Dict[str, str]] = Field(default_factory=list, description="[{code,label}] 10 个权限组")
    dept_name_to_groups: Dict[str, List[str]] = Field(default_factory=dict, description="钉钉部门名→组")
    my_role: str = "employee"
    my_managed_owner_depts: List[str] = Field(default_factory=list)
    my_grantable_owner_depts: List[str] = Field(default_factory=list)
    org_tree: Optional[Dict[str, Any]] = Field(default=None, description="org 快照（缺失则 null）")


class KbDocItem(BaseModel):
    doc_id: str
    title: str = ""
    original_filename: str = ""
    owner_dept: str = ""
    permission_level: str = "public"
    current_version_no: int = 1
    status: str = "active"
    status_badge: str = ""
    updated_at: str = ""


class KbMyDocsResponse(BaseModel):
    items: List[KbDocItem] = Field(default_factory=list)
    has_more: bool = False


class KbVersionItem(BaseModel):
    version_no: int
    content_process_status: str = ""
    chunk_status: str = ""
    index_status: str = ""
    publish_status: str = ""
    status_badge: str = ""
    error_message: str = ""
    created_at: str = ""


class KbVersionHistoryResponse(BaseModel):
    doc_id: str
    owner_dept: str = ""
    versions: List[KbVersionItem] = Field(default_factory=list)


class KbDocStatusResponse(BaseModel):
    doc_id: str
    version_no: int
    owner_dept: str = ""
    content_process_status: str = ""
    chunk_status: str = ""
    index_status: str = ""
    chunk_total: int = 0
    chunk_active: int = 0
    chunk_indexed: int = 0
    status_badge: str = ""
    error_message: str = ""


def _kb_status_badge(content_status, index_status, doc_status, chunk_active=None,
                     publish_status=None) -> str:
    """把管线多字段折叠为用户可读态：排队中/处理中/已上线/处理失败/内容未变/已隔离/已退役。"""
    cs = (content_status or "").upper()
    ix = (index_status or "").upper()
    if doc_status and str(doc_status).lower() not in ("active", ""):
        return "已退役"
    # PII 隔离优先于"已上线"：隔离件的 index_status 可能残留 'SUCCESS'，但 chunk 已停用、不在检索中，
    # 绝不能显示"已上线"（会被误读为可搜/已脱敏）。统一显示"已隔离"，等脱敏重灌。
    if str(publish_status or "").upper() == "QUARANTINED":
        return "已隔离"
    # 管线把 document_version.index_status 置 'SUCCESS'（非 'INDEXED'）作为上线成功值。
    if ix in ("INDEXED", "SUCCESS") and (chunk_active is None or chunk_active > 0):
        return "已上线"
    if cs == "FAILED" or ix == "FAILED":
        return "处理失败"
    # 升版被 kb_admin 驳回：content_process_status='REJECTED'（见 kb_reject）。my-docs JOIN 在
    # current_version_no 上，驳回不回退该指针 → 不显式区分会落到默认"处理中"，让上传者误以为还在跑。
    if cs == "REJECTED":
        return "已驳回"
    if cs == "SKIPPED_DUPLICATE":
        return "内容未变"
    if cs == "PENDING_APPROVAL":
        return "待审核"
    if cs in ("", "NOT_STARTED"):
        return "排队中"
    return "处理中"


def _require_kb_console(identity: Optional[Identity]):
    """强制：调用者必须是知识库管理员（dept_admin/kb_admin）。返回 DB 现查的 KbIdentity。

    授权【现查】DB（resolve_kb_identity），不信令牌里的 role 提示——撤销管理员/收回授权即时生效。
    """
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline.kb_authz import can_access_console
    kb = resolve_kb_identity(identity.user_id)
    if not can_access_console(kb):
        raise HTTPException(status_code=403, detail="无知识库管理权限")
    return kb


def _kb_owner_scope_sql(kb, col: str = "owner_dept"):
    """owner_dept 作用域 SQL：kb_admin 不限；dept_admin 限其 managed；无授权 → 匹配空集。"""
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    if kb.role == ROLE_KB_ADMIN:
        return "", []
    owners = managed_owner_depts(kb)
    if not owners:
        return "AND 1=0", []
    placeholders = ",".join(["%s"] * len(owners))
    return f"AND {col} IN ({placeholders})", list(owners)


def _kb_can_manage(kb, owner_dept: str) -> bool:
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    if kb.role == ROLE_KB_ADMIN:
        return True
    return (owner_dept or "") in set(managed_owner_depts(kb))


def _kb_content_dups(etag: str, exclude_doc_id: str, kb):
    """按 OSS ETag（字节级内容指纹）找其它 active 文档（跨部门内容查重）。

    隐私分级：调用者【可管理】的命中给详情（doc_id/标题/部门）；管理范围外的只计数（仅提示"存在"，
    不泄露部门/标题）。只读、**fail-open**——任何异常都返回空，绝不影响 register（advisory，不拦上传）。
    覆盖面 = 已存 etag 的文档（今后自助上传从零累积）；docx↔pdf 跨格式孪生由管线 canonical_sha256 去重处理。
    """
    if not etag:
        return [], 0
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.doc_id, m.title, m.owner_dept
                    FROM fuling_knowledge.document_meta m
                    JOIN fuling_knowledge.document_version v
                      ON v.doc_id = m.doc_id AND v.version_no = m.current_version_no
                    WHERE v.etag = %s AND v.etag <> '' AND m.status = 'active' AND m.doc_id <> %s
                    LIMIT 20
                    """,
                    (etag, exclude_doc_id),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.info("content-dup 查询失败（fail-open，不报警）: %s", e)
        return [], 0
    visible, other = [], 0
    for r in rows:
        doc_id, title, owner = (r[0] or ""), (r[1] or ""), (r[2] or "")
        if _kb_can_manage(kb, owner):
            visible.append(KbDupDoc(doc_id=doc_id, title=title, owner_dept=owner))
        else:
            other += 1
    return visible[:10], other


def _load_org_tree_snapshot() -> Optional[Dict[str, Any]]:
    """读取 org 树快照（scratch/dingtalk_org_tree.json）；缺失/异常 → None（fail open）。"""
    try:
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "scratch" / "dingtalk_org_tree.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


class KbWhoamiResponse(BaseModel):
    user_id: str
    display_name: str = ""
    role: str = "employee"
    can_manage_kb: bool = False
    managed_owner_depts: List[str] = Field(default_factory=list)


@app.get("/api/kb/whoami", response_model=KbWhoamiResponse)
def kb_whoami(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """当前 Bearer 身份的角色/可管理范围（DB 现查）。供 web-view 上传页用传入 token 拿身份，
    无需在 H5 里再走 requestAuthCode 免登（token 由小程序传入）。仅要求登录，不要求管理员。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline.kb_authz import can_access_console, managed_owner_depts
    kb = resolve_kb_identity(identity.user_id)
    return KbWhoamiResponse(
        user_id=kb.user_id, display_name=kb.name or "", role=kb.role,
        can_manage_kb=can_access_console(kb), managed_owner_depts=managed_owner_depts(kb),
    )


@app.get("/api/kb/org-tree", response_model=KbOrgTreeResponse)
def kb_org_tree(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """权限选择器数据：10 个 ACL 组 + 钉钉部门→组映射 + 调用者自身可管理/可授权范围 + org 快照。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.dingtalk_identity import _DEPT_NAME_TO_GROUPS
    from opensearch_pipeline.kb_authz import managed_owner_depts, grantable_owner_depts
    return KbOrgTreeResponse(
        acl_groups=[{"code": c, "label": _KB_ACL_GROUP_LABELS.get(c, c)}
                    for c in sorted(_KB_ACL_GROUP_LABELS)],
        dept_name_to_groups={k: list(v) for k, v in _DEPT_NAME_TO_GROUPS.items()},
        my_role=kb.role,
        my_managed_owner_depts=managed_owner_depts(kb),
        my_grantable_owner_depts=grantable_owner_depts(kb),
        org_tree=_load_org_tree_snapshot(),
    )


@app.get("/api/kb/my-docs", response_model=KbMyDocsResponse)
def kb_my_docs(request: Request, limit: int = 20, offset: int = 0, q: str = "",
               identity: Optional[Identity] = Depends(current_identity)):
    """管理员可管理的文档列表（kb_admin 全量；dept_admin 限其 managed owner_dept）。只读。

    q：文档名搜索（标题 / 原始文件名子串匹配），用于"是否已有现存版本"自查。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    clause, params = _kb_owner_scope_sql(kb, "m.owner_dept")
    # 文档名搜索：转义 LIKE 通配符（% _ \）防"输入 % 即匹配全部"，作用域过滤仍在前 → 不越权。
    q = (q or "").strip()[:80]
    search_clause, search_params = "", []
    if q:
        # 用非反斜杠转义符 '!'：不依赖 DB 的 sql_mode（NO_BACKSLASH_ESCAPES 开启时反斜杠转义会失效）。
        esc = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        like = "%" + esc + "%"
        search_clause = "AND (m.title LIKE %s ESCAPE '!' OR m.original_filename LIKE %s ESCAPE '!')"
        search_params = [like, like]
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT m.doc_id, m.title, m.original_filename, m.owner_dept,
                           m.permission_level, m.current_version_no, m.status, m.updated_at,
                           v.content_process_status, v.index_status, v.publish_status
                    FROM fuling_knowledge.document_meta m
                    LEFT JOIN fuling_knowledge.document_version v
                      ON v.doc_id = m.doc_id AND v.version_no = m.current_version_no
                    WHERE 1=1 {clause} {search_clause}
                    ORDER BY (m.status='active') DESC, m.updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (*params, *search_params, limit + 1, offset),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_my_docs 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档列表查询失败 (trace: {trace_id})")

    has_more = len(rows) > limit
    items = []
    for r in rows[:limit]:
        (doc_id, title, fname, owner, perm, cur_ver, status, updated, cps, ixs, pubs) = r
        items.append(KbDocItem(
            doc_id=doc_id or "", title=title or "", original_filename=fname or "",
            owner_dept=owner or "", permission_level=perm or "public",
            current_version_no=int(cur_ver or 1), status=status or "active",
            status_badge=_kb_status_badge(cps, ixs, status, publish_status=pubs),
            updated_at=str(updated) if updated else "",
        ))
    return KbMyDocsResponse(items=items, has_more=has_more)


@app.get("/api/kb/version-history", response_model=KbVersionHistoryResponse)
def kb_version_history(request: Request, doc_id: str,
                       identity: Optional[Identity] = Depends(current_identity)):
    """某文档的版本历史（含每版管线状态）。授权：kb_admin 或文档 owner_dept 在调用者 managed 内。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    if not doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT owner_dept, status FROM fuling_knowledge.document_meta "
                            "WHERE doc_id=%s LIMIT 1", (doc_id,))
                meta = cur.fetchone()
                if not meta:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, _doc_status = meta[0] or "", meta[1]
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权查看该文档")
                cur.execute(
                    """
                    SELECT version_no, content_process_status, chunk_status, index_status,
                           publish_status, error_message, created_at
                    FROM fuling_knowledge.document_version
                    WHERE doc_id=%s ORDER BY version_no DESC
                    """,
                    (doc_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_version_history 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"版本历史查询失败 (trace: {trace_id})")

    versions = []
    for r in rows:
        (vno, cps, chs, ixs, pubs, err, created) = r
        versions.append(KbVersionItem(
            version_no=int(vno or 0), content_process_status=cps or "",
            chunk_status=chs or "", index_status=ixs or "", publish_status=pubs or "",
            status_badge=_kb_status_badge(cps, ixs, None),
            error_message=err or "", created_at=str(created) if created else "",
        ))
    return KbVersionHistoryResponse(doc_id=doc_id, owner_dept=owner_dept, versions=versions)


@app.get("/api/kb/doc-status", response_model=KbDocStatusResponse)
def kb_doc_status(request: Request, doc_id: str, version: Optional[int] = None,
                  identity: Optional[Identity] = Depends(current_identity)):
    """某文档某版本的详细管线状态 + chunk 计数（不传 version → 取 current_version_no）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    if not doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT owner_dept, status, current_version_no "
                            "FROM fuling_knowledge.document_meta WHERE doc_id=%s LIMIT 1", (doc_id,))
                meta = cur.fetchone()
                if not meta:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, doc_status, cur_ver = meta[0] or "", meta[1], int(meta[2] or 1)
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权查看该文档")
                vno = int(version) if version else cur_ver
                cur.execute(
                    "SELECT content_process_status, chunk_status, index_status, error_message "
                    "FROM fuling_knowledge.document_version WHERE doc_id=%s AND version_no=%s LIMIT 1",
                    (doc_id, vno),
                )
                dv = cur.fetchone()
                cur.execute(
                    "SELECT COUNT(*), SUM(is_active=1), SUM(index_status='INDEXED') "
                    "FROM fuling_knowledge.chunk_meta WHERE doc_id=%s AND version_no=%s",
                    (doc_id, vno),
                )
                total, active, indexed = cur.fetchone() or (0, 0, 0)
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_doc_status 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档状态查询失败 (trace: {trace_id})")

    cps, chs, ixs, err = (dv or ("", "", "", ""))
    active = int(active or 0)
    return KbDocStatusResponse(
        doc_id=doc_id, version_no=vno, owner_dept=owner_dept,
        content_process_status=cps or "", chunk_status=chs or "", index_status=ixs or "",
        chunk_total=int(total or 0), chunk_active=active, chunk_indexed=int(indexed or 0),
        status_badge=_kb_status_badge(cps, ixs, doc_status, active),
        error_message=err or "",
    )


# ═══════════════════════════════════════════════════════════════
# 知识库管理 — Phase 1 上传/升版/审批（写）
# 两段式：upload-url 颁发后端钦定 raw_key + 签名 PUT + upload token；客户端直传 OSS；
# register 校验 token（HMAC）+ OSS-HEAD 实物 + 现查授权 + 事务内分配 version_no（行锁）+ 幂等。
# 公开 / 跨组共享 → content_process_status='PENDING_APPROVAL'（scanner 不认领，等 kb_admin 审批）。
# 写守卫用【轻量】assert_metadata_write_allowed（≠ HA3 删除级开关）。
# ═══════════════════════════════════════════════════════════════

class KbUploadUrlRequest(BaseModel):
    action: Literal["new", "version"] = "new"
    filename: str
    owner_dept: str
    permission_level: str = "dept_internal"
    title: Optional[str] = None
    category_l1: Optional[str] = None
    category_l2: Optional[str] = None
    doc_id: Optional[str] = None                       # action=version 必填
    share_owner_depts: Optional[List[str]] = None      # 多部门共享意图（Phase 2 才在检索侧生效）


class KbUploadUrlResponse(BaseModel):
    upload_token: str
    put_url: str
    raw_key: str
    doc_id: str
    expires_in: int
    requires_kb_admin_approval: bool = False


class KbRegisterRequest(BaseModel):
    upload_token: str


class KbDupDoc(BaseModel):
    doc_id: str
    title: str = ""
    owner_dept: str = ""


class KbRegisterResponse(BaseModel):
    doc_id: str
    version_no: int
    content_process_status: str
    requires_kb_admin_approval: bool = False
    status_badge: str = ""
    idempotent: bool = False
    title: str = ""
    # 内容查重（按 OSS ETag = 字节级指纹，跨部门）。advisory，不拦上传。
    content_dups: List[KbDupDoc] = Field(default_factory=list)   # 调用者可见范围内的同内容文档
    content_dups_other: int = 0                                   # 可见范围外的同内容文档计数（仅提示存在，不泄露部门/标题）


class KbApprovalRequest(BaseModel):
    doc_id: str
    version_no: Optional[int] = None
    reason: Optional[str] = None


class KbRetireRequest(BaseModel):
    doc_id: str
    reason: Optional[str] = None


class KbRetireResponse(BaseModel):
    status: str = "ok"
    doc_id: str
    retired: bool = False
    already: bool = False
    status_badge: str = "已退役"
    note: str = ""


@app.post("/api/kb/upload-url", response_model=KbUploadUrlResponse)
def kb_upload_url(req: KbUploadUrlRequest, request: Request,
                  identity: Optional[Identity] = Depends(current_identity)):
    """颁发签名 PUT URL + upload token。后端钦定 raw_key/doc_id（客户端不可改）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline import kb_upload, kb_authz
    from opensearch_pipeline.oss_url import generate_signed_url
    from opensearch_pipeline.config import get_config

    ok, ext, reason = kb_upload.validate_upload_filename(req.filename)
    if not ok:
        msg = {"legacy_format": "旧版 Office 格式（.doc/.xls/.ppt）暂不支持，请另存为 .docx/.xlsx/.pptx 后重传",
               "unsupported_format": "不支持的文件类型",
               "no_extension": "文件缺少扩展名"}.get(reason, "文件名非法")
        raise HTTPException(status_code=400, detail=msg)

    owner = (req.owner_dept or "").strip()
    perm = req.permission_level

    if req.action == "version":
        if not req.doc_id:
            raise HTTPException(status_code=400, detail="升版需提供 doc_id")
        try:
            from opensearch_pipeline.pipeline_nodes import _get_db_conn
            conn = _get_db_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT owner_dept, permission_level FROM fuling_knowledge.document_meta "
                                "WHERE doc_id=%s LIMIT 1", (req.doc_id,))
                    row = cur.fetchone()
            finally:
                conn.close()
        except HTTPException:
            raise
        except Exception as e:
            trace_id = uuid.uuid4().hex[:8]
            logger.error("upload-url 查 doc 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"查询文档失败 (trace: {trace_id})")
        if not row:
            raise HTTPException(status_code=404, detail="升版目标文档不存在")
        if (row[0] or "") != owner or not _kb_can_manage(kb, owner):
            raise HTTPException(status_code=403, detail="无权升版该文档（owner_dept 不在管理范围）")
        # 升版强制继承原文档 permission_level —— 忽略客户端传值（升版不得改可见范围，防越权）。
        perm = row[1] or perm
        doc_id = req.doc_id
    else:
        doc_id = kb_upload.new_doc_id()

    # 授权裁决用最终生效的 perm（新建=客户端选；升版=原文档继承）。
    decision = kb_authz.authorize_upload(kb, owner, perm, req.share_owner_depts)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=f"无权上传：{decision.reason}")

    upload_id = kb_upload.new_ulid()
    raw_key = kb_upload.build_raw_key(owner, doc_id, upload_id, req.filename)
    token = kb_upload.sign_upload_token({
        "uid": kb.user_id, "action": req.action, "doc_id": doc_id, "owner_dept": owner,
        "raw_key": raw_key, "filename": kb_upload.safe_filename(req.filename), "ext": ext,
        "title": req.title or kb_upload.safe_filename(req.filename),
        "category_l1": req.category_l1 or "", "category_l2": req.category_l2 or "",
        "permission_level": perm,
        "share_owner_depts": kb_authz.sanitize_owner_depts(req.share_owner_depts),
        "max_size": kb_upload.MAX_UPLOAD_BYTES,
        "requires_approval": bool(decision.requires_kb_admin_approval),
        "owner_name": kb.name,
    })
    bucket = get_config().oss.bucket_name
    put_url = generate_signed_url(raw_key, expires=kb_upload.UPLOAD_TOKEN_TTL, method="PUT")
    logger.info("kb upload-url: uid=%s action=%s doc_id=%s owner=%s bucket=%s",
                kb.user_id, req.action, doc_id, owner, bucket)
    return KbUploadUrlResponse(
        upload_token=token, put_url=put_url, raw_key=raw_key, doc_id=doc_id,
        expires_in=kb_upload.UPLOAD_TOKEN_TTL,
        requires_kb_admin_approval=bool(decision.requires_kb_admin_approval),
    )


@app.post("/api/kb/register", response_model=KbRegisterResponse)
def kb_register(req: KbRegisterRequest, request: Request,
                identity: Optional[Identity] = Depends(current_identity)):
    """登记上传：校验 token + OSS-HEAD + 现查授权 → 事务内分配 version_no（行锁）写 RDS（幂等）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    import hashlib
    from opensearch_pipeline import kb_upload, kb_authz
    from opensearch_pipeline.oss_url import head_object
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config

    payload = kb_upload.verify_upload_token(req.upload_token)
    if not payload:
        raise HTTPException(status_code=400, detail="upload_token 无效或已过期")
    if (payload.get("uid") or "") != kb.user_id:
        raise HTTPException(status_code=403, detail="upload_token 与当前用户不符")

    owner = payload["owner_dept"]
    raw_key = payload["raw_key"]
    perm = payload["permission_level"]
    # 现查授权（撤销/收回授权后即时生效，绝不信旧 token 的判断）
    decision = kb_authz.authorize_upload(kb, owner, perm, payload.get("share_owner_depts"))
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=f"无权登记：{decision.reason}")
    requires_approval = bool(decision.requires_kb_admin_approval)

    # OSS-HEAD 实物校验：存在 + 大小
    meta = head_object(raw_key)
    if not meta:
        raise HTTPException(status_code=400, detail="未检测到已上传的文件（请先完成直传，或 PUT 已过期）")
    size = int(meta.get("size") or 0)
    if size <= 0:
        raise HTTPException(status_code=400, detail="上传的文件为空")
    if size > int(payload.get("max_size") or kb_upload.MAX_UPLOAD_BYTES):
        raise HTTPException(status_code=413, detail="文件超过大小上限")
    # OSS ETag = 内容指纹（自助上传单次 PUT ⇒ 内容 MD5，与路径/部门无关）→ 用于跨部门内容查重。
    etag_val = (meta.get("etag") or "")[:128]

    cfg = get_config()
    assert_metadata_write_allowed("kb_register_upload", cfg.rds.host, kind="rds")

    cps = "PENDING_APPROVAL" if requires_approval else "NOT_STARTED"
    appr = "PENDING" if requires_approval else "APPROVED"
    action = payload.get("action", "new")
    bucket = cfg.oss.bucket_name
    trace_id = uuid.uuid4().hex[:8]

    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 幂等：同一 raw_key 已登记 → 直接返回既有行
                cur.execute("SELECT doc_id, version_no, content_process_status "
                            "FROM fuling_knowledge.document_version WHERE raw_key=%s LIMIT 1", (raw_key,))
                exist = cur.fetchone()
                if exist:
                    conn.commit()
                    return KbRegisterResponse(
                        doc_id=exist[0], version_no=int(exist[1]),
                        content_process_status=exist[2] or cps,
                        requires_kb_admin_approval=requires_approval,
                        status_badge=_kb_status_badge(exist[2], None, "active"),
                        idempotent=True,
                        title=payload.get("title") or "",
                    )
                doc_id = payload["doc_id"]
                if action == "version":
                    # 行锁串行化版本号分配，避免并发升版撞号
                    cur.execute("SELECT current_version_no, permission_level FROM fuling_knowledge.document_meta "
                                "WHERE doc_id=%s FOR UPDATE", (doc_id,))
                    mrow = cur.fetchone()
                    if not mrow:
                        raise HTTPException(status_code=404, detail="升版目标文档不存在")
                    # 纵深防御：升版绝不改可见范围（token 由 upload-url 钦定继承，此处再核一次）
                    if perm != (mrow[1] or perm):
                        raise HTTPException(status_code=403, detail="升版不可改变可见范围")
                    version_no = int(mrow[0] or 1) + 1
                    cur.execute("UPDATE fuling_knowledge.document_meta "
                                "SET current_version_no=%s, updated_at=NOW() WHERE doc_id=%s",
                                (version_no, doc_id))
                else:
                    version_no = 1
                    cur.execute(
                        """
                        INSERT INTO fuling_knowledge.document_meta
                          (doc_id, title, original_filename, owner_dept, owner_user_id, owner_name,
                           category_l1, category_l2, permission_level, kb_type, status, current_version_no)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',1)
                        ON DUPLICATE KEY UPDATE current_version_no=GREATEST(current_version_no,1),
                                                updated_at=NOW()
                        """,
                        (doc_id, payload.get("title"), payload.get("filename"), owner,
                         kb.user_id, payload.get("owner_name") or kb.name,
                         payload.get("category_l1") or None, payload.get("category_l2") or None,
                         perm, ("public" if perm == "public" else "private")),
                    )
                # raw_key_hash 与生产管线/批量注册一致写入（自助路径此前置 NULL）——供 reconcile/dedup
                # 工具按内容键去重，并为未来的 UNIQUE(raw_key_hash) 加固预填数据。
                raw_key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
                try:
                    cur.execute(
                        """
                        INSERT INTO fuling_knowledge.document_version
                          (doc_id, version_no, bucket_name, raw_key, raw_key_hash, etag, file_ext, mime_type,
                           file_size_bytes, content_process_status, approval_status, status, received_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',NOW())
                        """,
                        (doc_id, version_no, bucket, raw_key, raw_key_hash, etag_val, payload.get("ext"),
                         kb_upload.expected_mime(payload.get("ext")), size, cps, appr),
                    )
                except Exception as ins_err:
                    # uk_doc_version(doc_id,version_no) 唯一键 1062：并发双提交（同一 upload_token 双击/
                    # 重试，共用 upload-url 钦定的 doc_id+version_no）。赢家事务已提交该版本（InnoDB 唯一键
                    # 把输家的 INSERT 阻塞到赢家 commit 才抛 1062），故回滚本事务（连带撤销 meta 的
                    # current_version_no 副作用，避免输家留下半截写入），按 raw_key 重查赢家行返回幂等成功——
                    # 而非把可预期的竞态当 500 抛给用户。非 1062 的完整性错误照常上抛走 500 分支。
                    if (getattr(ins_err, "args", None) or (None,))[0] != 1062:
                        raise
                    conn.rollback()
                    with conn.cursor() as c2:
                        c2.execute("SELECT doc_id, version_no, content_process_status "
                                   "FROM fuling_knowledge.document_version WHERE raw_key=%s LIMIT 1", (raw_key,))
                        won = c2.fetchone()
                    if not won:
                        raise   # 1062 但查不到赢家行 → 非预期，按 500 处理
                    logger.info("kb_register 并发幂等命中：raw_key=%s 赢家 doc=%s v=%s", raw_key, won[0], won[1])
                    return KbRegisterResponse(
                        doc_id=won[0], version_no=int(won[1]),
                        content_process_status=won[2] or cps,
                        requires_kb_admin_approval=requires_approval,
                        status_badge=_kb_status_badge(won[2], None, "active"),
                        idempotent=True, title=payload.get("title") or "",
                    )
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_register 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"登记失败 (trace: {trace_id})")

    write_audit(doc_id=doc_id, version_no=version_no,
                action_type=("VERSION_UP" if action == "version" else "UPLOAD_REGISTER"),
                operator_type="user", operator_id=kb.user_id, oss_key=raw_key, trace_id=trace_id,
                message=f"owner={owner} perm={perm} approval={appr} share={payload.get('share_owner_depts')}")
    # 跨部门内容查重（按 ETag 字节指纹）：advisory，命中也不拦上传——仅在响应里提示，让上传者决定是否退役其一。
    # 升版（同 doc_id 换文件）天然不算重复，故仅新建查；fail-open。
    dups, dups_other = ([], 0)
    if action != "version":
        dups, dups_other = _kb_content_dups(etag_val, doc_id, kb)
    return KbRegisterResponse(
        doc_id=doc_id, version_no=version_no, content_process_status=cps,
        requires_kb_admin_approval=requires_approval,
        status_badge=_kb_status_badge(cps, None, "active"),
        title=payload.get("title") or "",
        content_dups=dups, content_dups_other=dups_other,
    )


@app.post("/api/kb/approve")
def kb_approve(req: KbApprovalRequest, request: Request,
               identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 审批放行：PENDING_APPROVAL → NOT_STARTED（下一批入库）。仅 kb_admin。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if kb.role != ROLE_KB_ADMIN:
        raise HTTPException(status_code=403, detail="仅知识库管理员可审批")
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    assert_metadata_write_allowed("kb_approve", get_config().rds.host, kind="rds")
    trace_id = uuid.uuid4().hex[:8]
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                vfilter = "AND version_no=%s" if req.version_no else ""
                vargs = (req.version_no,) if req.version_no else ()
                n = cur.execute(
                    f"UPDATE fuling_knowledge.document_version "
                    f"SET content_process_status='NOT_STARTED', approval_status='APPROVED', updated_at=NOW() "
                    f"WHERE doc_id=%s {vfilter} AND content_process_status='PENDING_APPROVAL'",
                    (req.doc_id, *vargs),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("kb_approve 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"审批失败 (trace: {trace_id})")
    write_audit(doc_id=req.doc_id, version_no=req.version_no, action_type="APPROVE",
                operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                message=f"approved {n} version(s)")
    return {"status": "ok", "approved": n}


@app.post("/api/kb/reject")
def kb_reject(req: KbApprovalRequest, request: Request,
              identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 驳回：PENDING_APPROVAL → REJECTED（永不入库）。仅 kb_admin。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if kb.role != ROLE_KB_ADMIN:
        raise HTTPException(status_code=403, detail="仅知识库管理员可驳回")
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    assert_metadata_write_allowed("kb_reject", get_config().rds.host, kind="rds")
    trace_id = uuid.uuid4().hex[:8]
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                vfilter = "AND version_no=%s" if req.version_no else ""
                vargs = (req.version_no,) if req.version_no else ()
                n = cur.execute(
                    f"UPDATE fuling_knowledge.document_version "
                    f"SET content_process_status='REJECTED', approval_status='REJECTED', "
                    f"    content_process_error=%s, updated_at=NOW() "
                    f"WHERE doc_id=%s {vfilter} AND content_process_status='PENDING_APPROVAL'",
                    ((req.reason or "rejected")[:500], req.doc_id, *vargs),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("kb_reject 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"驳回失败 (trace: {trace_id})")
    write_audit(doc_id=req.doc_id, version_no=req.version_no, action_type="REJECT",
                operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                message=(req.reason or "")[:200])
    return {"status": "ok", "rejected": n}


@app.post("/api/kb/retire", response_model=KbRetireResponse)
def kb_retire(req: KbRetireRequest, request: Request,
              identity: Optional[Identity] = Depends(current_identity)):
    """软退役（可逆，不删 HA3）：把文档标记下线 + 停用本版本 RDS chunk，交现有 gated 运维完成 HA3 移除。

    授权：kb_admin 任意；dept_admin 限其 managed owner_dept，且【公开文档需 kb_admin】（影响全公司）。
    仅改 RDS（document_meta/version.status='retired' + chunk_meta.is_active=0），**不触碰 HA3**——
    真实检索下线由 gated 运维（带 prod token 的 HA3 删除/reconcile）完成；本接口仅"申请退役 + 即时标记"，
    文案如实告知。可逆：运维侧把 status 改回 active 即恢复（HA3 未删）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    assert_metadata_write_allowed("kb_retire", get_config().rds.host, kind="rds")
    trace_id = uuid.uuid4().hex[:8]
    owner_dept = perm = ""
    cur_ver = 1
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 行锁文档元数据，串行化并发退役 / 退役-vs-升版
                cur.execute("SELECT owner_dept, permission_level, status, current_version_no "
                            "FROM fuling_knowledge.document_meta WHERE doc_id=%s FOR UPDATE", (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm = (row[0] or ""), (row[1] or "")
                status, cur_ver = (row[2] or "active"), int(row[3] or 1)
                # 授权：先作用域，再"公开需 kb_admin"（与上传同款不对称——公开影响全公司）
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权退役该文档（owner_dept 不在管理范围）")
                if perm == "public" and kb.role != ROLE_KB_ADMIN:
                    raise HTTPException(status_code=403, detail="公开文档需知识库管理员退役")
                if str(status).lower() != "active":
                    conn.commit()       # 幂等：已退役/非活跃 → 直接回既有态
                    return KbRetireResponse(doc_id=req.doc_id, retired=False, already=True,
                                            note="该文档已是退役/非活跃状态")
                cur.execute("UPDATE fuling_knowledge.document_meta SET status='retired', updated_at=NOW() "
                            "WHERE doc_id=%s", (req.doc_id,))
                cur.execute("UPDATE fuling_knowledge.document_version SET status='retired', updated_at=NOW() "
                            "WHERE doc_id=%s AND version_no=%s", (req.doc_id, cur_ver))
                # RDS 侧停用本版本 chunk（停止邻居拼接复用 + 给 reconcile/HA3 删除一个明确信号）；HA3 不动。
                cur.execute("UPDATE fuling_knowledge.chunk_meta SET is_active=0 "
                            "WHERE doc_id=%s AND version_no=%s AND is_active=1", (req.doc_id, cur_ver))
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_retire 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"退役失败 (trace: {trace_id})")
    write_audit(doc_id=req.doc_id, version_no=cur_ver, action_type="RETIRE_REQUEST",
                operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                message=f"owner={owner_dept} perm={perm} reason={(req.reason or '')[:200]}")
    return KbRetireResponse(
        doc_id=req.doc_id, retired=True,
        note="已申请退役：已标记下线、停止作为升版目标；从检索彻底移除将在下次维护完成（本操作可逆）")


class KbPendingItem(BaseModel):
    doc_id: str
    version_no: int = 1
    title: str = ""
    original_filename: str = ""
    owner_dept: str = ""
    permission_level: str = "public"
    owner_name: str = ""
    created_at: str = ""


class KbPendingResponse(BaseModel):
    items: List[KbPendingItem] = Field(default_factory=list)


@app.get("/api/kb/pending-approvals", response_model=KbPendingResponse)
def kb_pending_approvals(request: Request,
                         identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 待审批队列：列出 content_process_status='PENDING_APPROVAL' 的版本。仅 kb_admin。只读。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    if kb.role != ROLE_KB_ADMIN:
        raise HTTPException(status_code=403, detail="仅知识库管理员可查看审批队列")
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.doc_id, v.version_no, m.title, m.original_filename, m.owner_dept,
                           m.permission_level, m.owner_name, v.received_at
                    FROM fuling_knowledge.document_version v
                    JOIN fuling_knowledge.document_meta m ON m.doc_id = v.doc_id
                    WHERE v.content_process_status = 'PENDING_APPROVAL'
                    ORDER BY v.received_at DESC
                    LIMIT 100
                    """
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_pending_approvals 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"待审批队列查询失败 (trace: {trace_id})")
    items = [
        KbPendingItem(
            doc_id=r[0] or "", version_no=int(r[1] or 1), title=r[2] or "",
            original_filename=r[3] or "", owner_dept=r[4] or "",
            permission_level=r[5] or "public", owner_name=r[6] or "",
            created_at=str(r[7]) if r[7] else "",
        )
        for r in rows
    ]
    return KbPendingResponse(items=items)


# ═══════════════════════════════════════════════════════════════
# 电脑端 H5 控制台（PC 免登 + 文档上传/管理）— 同源单页，钉钉小程序"PC 端访问地址"指向 /console
# ═══════════════════════════════════════════════════════════════

_KB_CONSOLE_HTML_CACHE: Dict[str, Any] = {"html": None}


@app.get("/console", response_class=HTMLResponse)
def kb_console_page():
    """自包含 H5 控制台单页：jsapi 免登 → /api/auth/dingtalk → /api/kb/*（同源调用）。"""
    if _KB_CONSOLE_HTML_CACHE["html"] is None:
        from pathlib import Path
        p = Path(__file__).resolve().parent / "webconsole" / "console.html"
        try:
            _KB_CONSOLE_HTML_CACHE["html"] = p.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("console.html 读取失败: %s", e)
            _KB_CONSOLE_HTML_CACHE["html"] = "<h1>知识库控制台页面缺失</h1>"
    return HTMLResponse(_KB_CONSOLE_HTML_CACHE["html"])


# ═══════════════════════════════════════════════════════════════
# 启动入口
# ═══════════════════════════════════════════════════════════════

def main():
    """命令行启动。"""
    import uvicorn
    uvicorn.run(
        "opensearch_pipeline.api:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )


if __name__ == "__main__":
    main()
