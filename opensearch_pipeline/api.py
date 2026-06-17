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
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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
    dept: Optional[str] = None


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
    dept: Optional[str] = None
    name: str = ""


def current_identity(authorization: Optional[str] = Header(None)) -> Optional[Identity]:
    """从 Authorization: Bearer <token> 解析已验证身份；无/无效令牌返回 None。

    部门来自服务端签发的令牌，客户端不可篡改；端点据此解析部门，绝不信任请求体里的部门字段。
    """
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    payload = verify_session_token(parts[1].strip())
    if not payload:
        return None
    return Identity(
        user_id=payload.get("uid", ""),
        dept=(payload.get("dept") or None),
        name=payload.get("name", ""),
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
    token = issue_session_token(userid, dept=ident.get("dept"), name=ident.get("name"))
    return DingtalkAuthResponse(
        token=token,
        user_id=userid,
        display_name=ident.get("name") or "",
        dept=ident.get("dept"),
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
        user_dept = identity.dept if identity else None
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
    user_dept = identity.dept if identity else None

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
                yield f"data: {json.dumps({'type': 'done', 'model': 'N/A', 'usage': {}}, ensure_ascii=False)}\n\n"
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
                    yield event

                    # 收集完整回答 + 模型名（用于写历史 & 落库）
                    frame = parse_sse_data_frame(event)
                    if frame is None:
                        continue
                    if frame.get("type") == "chunk" and frame.get("content"):
                        collected_answer.append(frame["content"])
                    elif frame.get("type") == "done":
                        model_name = frame.get("model")

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
