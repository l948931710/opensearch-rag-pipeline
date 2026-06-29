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
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse, RedirectResponse, Response
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
    conversation_id: Optional[str] = Field(None, max_length=128, description="客户端会话 ID（控制台会话历史归属；仅 RAG_CONVERSATION_HISTORY 开启时落库）")
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
    # 0-1 归一相关度（按 high 阈值归一，rerank/RRF 自洽）：供前端按比例画相关度条。
    relevance: float = 0.0
    # 正文省略版（折叠空白 + 截断的 chunk_text）：供前端「点击来源看正文」。已脱敏、已权限过滤。
    preview: str = ""


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

    读组撤销窗口（S1）收敛：默认（RAG_LIVE_ACL_REREAD=true）对令牌内嵌 acl_groups 做【读时实时
    重查】——以 DB user_role 现查为准（_resolve_user_dept_cached，SELECT-only 无副作用），部门收紧/
    放宽即时生效，不等令牌过期。复查无在册行 / DB 失败 → 保留令牌内嵌组（绝不因瞬时抖动把用户降到
    仅 public），由短 TTL（RAG_SESSION_TOKEN_TTL_HOURS，默认 2h，原 8h）兜底。令牌不可伪造；跨部门
    读另有 retriever 查询侧实时拒绝（_deny_revoked_cross_dept）。flag 关 → 退回纯令牌内嵌（历史行为）。
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
    uid = payload.get("uid", "")
    # 读时实时重查 acl（默认 ON）：令牌内嵌组仅作兜底，实时以 DB user_role 为准（部门变更即时生效）。
    # 无在册行 / DB 失败 → live 为 None → 保留令牌组（绝不因瞬时抖动降级）。
    if uid and os.environ.get("RAG_LIVE_ACL_REREAD", "true").lower() in ("1", "true", "yes"):
        from opensearch_pipeline.dingtalk_identity import _resolve_user_dept_cached
        live = _resolve_user_dept_cached(uid)
        if live is not None:
            groups = live
    return Identity(
        user_id=uid,
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
            conversation_id=req.conversation_id,
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
            conversation_id=req.conversation_id,
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
        conversation_id=req.conversation_id,
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
                    conversation_id=req.conversation_id,
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
                conversation_id=req.conversation_id,
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


# ── 服务端会话历史（控制台 Phase 2/3；全部 gate 在 RAG_CONVERSATION_HISTORY，关时返回空/404）──
# 设计：qa_session_log 仅承载 conversation_id（审计事实、append-only，删除会话绝不动它）；会话级状态
# （标题/最近活动/隐藏）在独立的 qa_conversation 表。列表读 qa_conversation；消息读 qa_session_log；
# 隐藏只 UPDATE qa_conversation.hidden_at。

class ConversationSummary(BaseModel):
    conversation_id: str
    title: str = ""
    updated_at: str = ""   # = qa_conversation.last_message_at


class ConversationListResponse(BaseModel):
    items: List[ConversationSummary] = Field(default_factory=list)
    has_more: bool = False


@app.get("/api/conversations", response_model=ConversationListResponse)
def list_conversations(request: Request, limit: int = 30, offset: int = 0,
                       identity: Optional[Identity] = Depends(current_identity)):
    """本人未隐藏会话列表（直接读 qa_conversation，按最近活动倒序）。仅本人。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    if not get_config().rag.conversation_history:
        return ConversationListResponse(items=[], has_more=False)
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    from opensearch_pipeline.qa_logger import _op_db
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT conversation_id, title, last_message_at
                    FROM {_op_db()}.qa_conversation
                    WHERE user_id=%s AND hidden_at IS NULL
                    ORDER BY last_message_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (identity.user_id, limit + 1, offset),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("list_conversations 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"会话列表查询失败 (trace: {trace_id})")
    has_more = len(rows) > limit
    items = [ConversationSummary(
        conversation_id=r[0] or "", title=(r[1] or "")[:255],
        updated_at=str(r[2]) if r[2] else "",
    ) for r in rows[:limit]]
    return ConversationListResponse(items=items, has_more=has_more)


@app.get("/api/conversations/{conversation_id}", response_model=HistoryResponse)
def get_conversation(conversation_id: str, request: Request,
                     identity: Optional[Identity] = Depends(current_identity)):
    """某会话的全部消息（时间正序，走 idx_user_conversation_time）；复用 history 图文重签。仅本人。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    if not get_config().rag.conversation_history:
        return HistoryResponse(items=[], has_more=False)
    from opensearch_pipeline.qa_logger import _op_db
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT message_id, query_text, answer_text, content_blocks_json,
                           created_at, answer_status
                    FROM {_op_db()}.qa_session_log
                    WHERE user_id=%s AND conversation_id=%s
                    ORDER BY created_at ASC, id ASC
                    LIMIT 200
                    """,
                    (identity.user_id, conversation_id),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("get_conversation 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"会话读取失败 (trace: {trace_id})")
    items: List[HistoryItem] = []
    for row in rows:
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
            message_id=message_id or "", question=question or "",
            answer=strip_image_markers(answer_text or ""), blocks=blocks,
            created_at=str(created_at) if created_at else "", status=status or "",
        ))
    return HistoryResponse(items=items, has_more=False)


class DeleteConversationResponse(BaseModel):
    deleted: bool = False


@app.delete("/api/conversations/{conversation_id}", response_model=DeleteConversationResponse)
def delete_conversation(conversation_id: str, request: Request,
                        identity: Optional[Identity] = Depends(current_identity)):
    """从会话列表移除（软删除：qa_conversation.hidden_at=NOW）。仅本人；qa_session_log 审计行【不动】。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    if not get_config().rag.conversation_history:
        raise HTTPException(status_code=404, detail="会话历史未启用")
    if not conversation_id:
        raise HTTPException(status_code=400, detail="缺少 conversation_id")
    from opensearch_pipeline.qa_logger import _op_db
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {_op_db()}.qa_conversation SET hidden_at=NOW(3) "
                    f"WHERE user_id=%s AND conversation_id=%s AND hidden_at IS NULL",
                    (identity.user_id, conversation_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("delete_conversation 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"会话删除失败 (trace: {trace_id})")
    return DeleteConversationResponse(deleted=True)


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
    # 可操作性（= 写作用域 managed；my-docs 恒 True，browse 全部门时外部门为 False）。
    # 与"可见"解耦：浏览看得见 ≠ 能管。前端据此决定 升版/退役 还是 申请授权。
    can_manage: bool = True


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


_KB_MAX_OFFSET = 10000   # 文档列表分页 offset 上界（全库 ~1600 篇，1 万足够；防巨大 offset 深分页扫表，G7）


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


def _require_kb_admin(identity: Optional[Identity]):
    """强制：调用者必须是【知识库管理员 kb_admin】（成员/角色管理 = kb_admin 专属，dept_admin 不可）。"""
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    if kb.role != ROLE_KB_ADMIN:
        raise HTTPException(status_code=403, detail="仅知识库管理员可管理成员/角色")
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
    # 用户所属 ACL 读权限组（仅展示/审计，写授权不据此推导）。与 /api/auth/dingtalk 的 acl_groups 同源，
    # 补齐后 web-view ?token= 直登路径也能拿到部门信息（员工概览「我的部门」依赖它）。
    acl_groups: List[str] = Field(default_factory=list)


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
        acl_groups=list(kb.acl_groups),
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
    offset = max(0, min(offset, _KB_MAX_OFFSET))   # 上界防深分页扫表（全库 ~1600，1万 offset 绰绰有余，G7）
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


@app.get("/api/kb/browse", response_model=KbMyDocsResponse)
def kb_browse(request: Request, scope: str = "all", q: str = "", owner_dept: str = "",
              limit: int = 20, offset: int = 0,
              identity: Optional[Identity] = Depends(current_identity)):
    """全部门只读浏览：部门管理员看【其他部门】文档（可见、不可操作）。只读。

    与 my-docs 的根本区别——**绝不复用 _kb_owner_scope_sql（写作用域）**：
      · 可见范围 = 全部门（不按 managed 过滤）；可操作(can_manage) 仍 = 写作用域 managed。
      · 只列 permission_level ∈ {public, dept_internal}（**允许清单**，restricted 及任何未知值
        一律排除）——审计/法务/总经办等 restricted 敏感件连标题都不外露（锁定决策 2026-06-26）。
      · 只列 status='active'（退役件无需被申请检索）。
      · 每行带 can_manage（kb_admin 全 True；dept_admin 仅其 managed owner_dept）。
    申请其他部门文档检索 → 授权申请（Phase C）；真正放行检索 → allowed_depts 接入检索（Phase D）。
    employee/匿名在任何 DB 查询【之前】被 401/403（_require_kb_console 先行）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    if scope != "all":
        # 目前仅 all 语义（本部门用 my-docs）；非法 scope fail-closed 空，避免静默当全量。
        return KbMyDocsResponse(items=[], has_more=False)
    limit = max(1, min(limit, 50))
    offset = max(0, min(offset, _KB_MAX_OFFSET))   # 上界防深分页扫表（G7）

    # owner_dept facet（可选）：参数化 = %s 本身防注入，这里再剥离注入字符 + 限长做纵深防御。
    from opensearch_pipeline.kb_authz import _SANITIZE_RE
    owner_facet = _SANITIZE_RE.sub("", (owner_dept or "").strip())[:64]
    owner_clause, owner_params = "", []
    if owner_dept and not owner_facet:
        return KbMyDocsResponse(items=[], has_more=False)   # 非法 facet → fail-closed 空
    if owner_facet:
        owner_clause = "AND m.owner_dept = %s"
        owner_params = [owner_facet]

    # 文档名搜索：与 my-docs 同款显式 '!' 转义（不依赖 sql_mode 的 NO_BACKSLASH_ESCAPES）。
    q = (q or "").strip()[:80]
    search_clause, search_params = "", []
    if q:
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
                    WHERE m.status='active'
                      AND m.permission_level IN ('public','dept_internal')
                      {owner_clause} {search_clause}
                    ORDER BY m.owner_dept ASC, m.updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (*owner_params, *search_params, limit + 1, offset),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_browse 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"全部门浏览查询失败 (trace: {trace_id})")

    has_more = len(rows) > limit
    items = []
    for r in rows[:limit]:
        (doc_id, title, fname, owner, perm, cur_ver, status, updated, cps, ixs, pubs) = r
        items.append(KbDocItem(
            doc_id=doc_id or "", title=title or "", original_filename=fname or "",
            owner_dept=owner or "", permission_level=perm or "dept_internal",
            current_version_no=int(cur_ver or 1), status=status or "active",
            status_badge=_kb_status_badge(cps, ixs, status, publish_status=pubs),
            updated_at=str(updated) if updated else "",
            can_manage=_kb_can_manage(kb, owner or ""),
        ))
    return KbMyDocsResponse(items=items, has_more=has_more)


class KbStatsResponse(BaseModel):
    total: int = 0
    active: int = 0
    retired: int = 0
    chunks: int = 0                      # 作用域内当前已索引分块数（is_active=1 AND index_status='INDEXED'）
    new_this_month: int = 0              # 本月新增文档数（document_meta.created_at 落在当月，active）
    by_badge: Dict[str, int] = Field(default_factory=dict)


@app.get("/api/kb/stats", response_model=KbStatsResponse)
def kb_stats(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """管理范围内文档聚合（真实总数 + 状态分布 + 已索引分块数），不受 my-docs 的 50 上限影响。

    只读、按 owner 作用域过滤（与 my-docs 同一 _kb_owner_scope_sql，不会越权统计他部门）；
    徽章在 Python 端按与 my-docs 相同的 _kb_status_badge 复算，故口径一致。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    clause, params = _kb_owner_scope_sql(kb, "m.owner_dept")
    ck_clause, ck_params = _kb_owner_scope_sql(kb, "owner_dept")   # chunk_meta.owner_dept 同口径作用域
    dm_clause, dm_params = _kb_owner_scope_sql(kb, "owner_dept")   # document_meta.owner_dept（本月新增计数）
    from datetime import date
    month_start = date.today().replace(day=1).isoformat()         # 当月首日；以参数传入避免 % 转义坑
    chunks = new_this_month = 0
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT m.status, v.content_process_status, v.index_status, v.publish_status
                    FROM fuling_knowledge.document_meta m
                    LEFT JOIN fuling_knowledge.document_version v
                      ON v.doc_id = m.doc_id AND v.version_no = m.current_version_no
                    WHERE 1=1 {clause}
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
                # 当前已索引分块总数（设计「全库已索引 chunk」口径）；取数失败仅置 0，不拖垮主统计。
                try:
                    cur.execute(
                        f"SELECT COUNT(*) FROM fuling_knowledge.chunk_meta "
                        f"WHERE is_active=1 AND index_status='INDEXED' {ck_clause}",
                        tuple(ck_params),
                    )
                    chunks = int((cur.fetchone() or (0,))[0] or 0)
                except Exception as e:
                    logger.warning("kb_stats 分块计数失败: %s", e)
                # 本月新增文档数（设计「+N 本月新增」徽标）；月首日以参数传入；取数失败仅置 0。
                try:
                    cur.execute(
                        f"SELECT COUNT(*) FROM fuling_knowledge.document_meta "
                        f"WHERE created_at >= %s AND status='active' {dm_clause}",
                        tuple([month_start] + dm_params),
                    )
                    new_this_month = int((cur.fetchone() or (0,))[0] or 0)
                except Exception as e:
                    logger.warning("kb_stats 本月新增计数失败: %s", e)
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_stats 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"统计查询失败 (trace: {trace_id})")
    active = retired = 0
    by_badge: Dict[str, int] = {}
    for row in rows:
        status, cps, ixs, pubs = row[0], row[1], row[2], row[3]
        if (status or "active") == "active":
            active += 1
        else:
            retired += 1
        badge = _kb_status_badge(cps, ixs, status, publish_status=pubs)
        by_badge[badge] = by_badge.get(badge, 0) + 1
    return KbStatsResponse(total=len(rows), active=active, retired=retired, chunks=chunks,
                           new_this_month=new_this_month, by_badge=by_badge)


# ─────────────────────────────────────────────────────────────────────────────
# Phase E — 概览看板的真实数据（不造数）。两个只读聚合端点，口径全部来自真实 RDS 表：
#   GET /api/kb/insights    —— owner 作用域的「知识使用成效 + 知识缺口」（dept_admin 看本部门、
#                              kb_admin 看全库；经 retrieved_docs_json→doc_id→owner_dept 归属）
#   GET /api/kb/governance  —— 全库运行健康 / 治理风险 / 部门覆盖（仅 kb_admin）
#
# 关键事实（scratch/phase_e_data_probe.py 实测 + qa-log-analytics-gotchas）：
#  · qa_session_log / user_feedback / escalation_ticket 在 fuling_operation；document_meta /
#    chunk_meta / pipeline_run / document_sensitive_finding 在 fuling_knowledge —— 同实例可跨库 JOIN。
#  · retrieved_docs_json 元素只留 doc_id 等 7 键、**不含 owner_dept** → 必须 JOIN document_meta 取归属。
#    JSON_TABLE 抽出的串默认 utf8mb4_0900_ai_ci，与 document_meta.doc_id(unicode_ci) 直接 JOIN 报
#    1267（kb_access_request 同坑），必须 CONVERT(... USING utf8mb4) COLLATE utf8mb4_unicode_ci。
#  · answer_status ∈ {SUCCESS, NO_RESULT, REFUSAL, LLM_ERROR}（无裸 'ERROR'，错误用 LIKE '%ERROR%'）。
#  · created_at 是 SAE 容器太平洋时间（北京 = +15h）：日历分桶用 DATE_ADD(created_at, INTERVAL 15 HOUR)。
#  · 每个子查询独立 try/except：单指标取数失败只让该指标诚实空，不拖垮整块看板（auxiliary fail-open）。
# ─────────────────────────────────────────────────────────────────────────────
_KB_INSIGHTS_WINDOW_DAYS = 30

# retrieved_docs_json → doc_id → document_meta.owner_dept 的归属 JOIN（collation-cast 必需）。
# 末尾 WHERE 已含窗口占位符 %s；调用处再拼 _kb_owner_scope_sql 的作用域子句（kb_admin 为空 = 全库）。
_KB_QA_OWNER_JOIN = (
    " FROM fuling_operation.qa_session_log q"
    " JOIN JSON_TABLE(q.retrieved_docs_json, '$[*]' COLUMNS(doc_id VARCHAR(100) PATH '$.doc_id')) jt"
    " JOIN fuling_knowledge.document_meta m"
    "   ON m.doc_id = CONVERT(jt.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci"
    " WHERE q.retrieved_docs_json IS NOT NULL"
    "   AND q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
)


class KbTopDocItem(BaseModel):
    title: str = ""
    owner_dept: str = ""
    hits: int = 0


class KbGapQueryItem(BaseModel):
    query: str = ""
    count: int = 0
    avg_top: float = 0.0


class KbInsightsResponse(BaseModel):
    scope: str = "dept"                  # 'global'（kb_admin 全库）| 'dept'（dept_admin 本部门）
    window_days: int = _KB_INSIGHTS_WINDOW_DAYS
    questions: int = 0                   # 命中所辖文档的提问数（DISTINCT message_id，去 JSON 扇出重复）
    askers: int = 0
    success: int = 0
    refusal: int = 0
    cited: int = 0                       # 所辖文档被「实际引用」的提问数
    effective_rate: float = 0.0          # success / questions
    top_docs: List[KbTopDocItem] = Field(default_factory=list)
    gap_queries: List[KbGapQueryItem] = Field(default_factory=list)


@app.get("/api/kb/insights", response_model=KbInsightsResponse)
def kb_insights(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """知识使用成效 + 知识缺口（owner 作用域；真实口径，无造数）。

    归属链 retrieved_docs_json→doc_id→document_meta.owner_dept，按 _kb_owner_scope_sql 作用域：
    dept_admin 只见本部门文档被使用情况，kb_admin 见全库。各子查询独立降级，缺数据诚实空。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    scope_clause, scope_params = _kb_owner_scope_sql(kb, "m.owner_dept")
    win = _KB_INSIGHTS_WINDOW_DAYS
    base = _KB_QA_OWNER_JOIN + (" " + scope_clause if scope_clause else "")
    args = tuple([win] + scope_params)
    out = KbInsightsResponse(scope=("global" if kb.role == ROLE_KB_ADMIN else "dept"), window_days=win)
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_insights 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"洞察查询失败 (trace: {trace_id})")
    fails = 0   # 子查询失败计数；全失败 = 连接级故障 → 诚实 500（而非 all-zeros 伪装无数据）
    try:
        # 共享一个游标跑多条子查询：依赖 pymysql 默认 buffered Cursor（_init_db_pool 未设 SSCursor），
        # 某子查询异常后结果已全量缓冲，下一句 execute 不会 "Commands out of sync (2014)"。
        with conn.cursor() as cur:
            # 1) 使用聚合：提问数 / 提问人 / 成功 / 拒答（DISTINCT message_id 去 JSON 扇出）
            try:
                cur.execute(
                    "SELECT COUNT(DISTINCT q.message_id), COUNT(DISTINCT q.user_id),"
                    " COUNT(DISTINCT CASE WHEN q.answer_status='SUCCESS' THEN q.message_id END),"
                    " COUNT(DISTINCT CASE WHEN q.answer_status='REFUSAL' THEN q.message_id END)" + base,
                    args)
                r = cur.fetchone() or (0, 0, 0, 0)
                out.questions, out.askers = int(r[0] or 0), int(r[1] or 0)
                out.success, out.refusal = int(r[2] or 0), int(r[3] or 0)
                out.effective_rate = round(out.success / out.questions, 4) if out.questions else 0.0
            except Exception as e:
                fails += 1; logger.warning("kb_insights 使用聚合失败: %s", e)
            # 2) 被引用问题数（cited_docs_json JOIN；NO_RESULT/REFUSAL 行该列为空，故弱于 retrieved）
            try:
                cur.execute(
                    "SELECT COUNT(DISTINCT q.message_id)"
                    " FROM fuling_operation.qa_session_log q"
                    " JOIN JSON_TABLE(q.cited_docs_json, '$[*]' COLUMNS(doc_id VARCHAR(100) PATH '$.doc_id')) jt"
                    " JOIN fuling_knowledge.document_meta m"
                    "   ON m.doc_id = CONVERT(jt.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci"
                    " WHERE q.cited_docs_json IS NOT NULL"
                    "   AND q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    + (" " + scope_clause if scope_clause else ""), args)
                out.cited = int((cur.fetchone() or (0,))[0] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_insights cited 失败: %s", e)
            # 3) 最常被检索的文档（COUNT(DISTINCT message_id) 去扇出，与其它计数同一纪律）
            try:
                cur.execute(
                    "SELECT m.title, m.owner_dept, COUNT(DISTINCT q.message_id)" + base
                    + " GROUP BY m.doc_id, m.title, m.owner_dept"
                    " ORDER BY COUNT(DISTINCT q.message_id) DESC LIMIT 8", args)
                out.top_docs = [KbTopDocItem(title=row[0] or "", owner_dept=row[1] or "", hits=int(row[2] or 0))
                                for row in cur.fetchall()]
            except Exception as e:
                fails += 1; logger.warning("kb_insights top_docs 失败: %s", e)
            # 4) 知识缺口：所辖文档上「未答好」的提问（REFUSAL = 召回了我的文档但没答好，最可行动）。
            #    avg_top 必须在「去扇出后的每问一行」上求均值——直接 AVG(q.top_score) 会被检索文档数
            #    （最多 top_k=7）加权失真，故先 DISTINCT message_id 折叠扇出再外层 AVG。
            try:
                cur.execute(
                    "SELECT d.query_text, COUNT(*), ROUND(AVG(d.top_score), 3) FROM ("
                    "SELECT DISTINCT q.message_id, q.query_text, q.top_score" + base
                    + " AND q.answer_status='REFUSAL') d"
                    " GROUP BY d.query_text ORDER BY COUNT(*) DESC LIMIT 10", args)
                out.gap_queries = [
                    KbGapQueryItem(query=row[0] or "", count=int(row[1] or 0),
                                   avg_top=float(row[2]) if row[2] is not None else 0.0)
                    for row in cur.fetchall()]
            except Exception as e:
                fails += 1; logger.warning("kb_insights gap_queries 失败: %s", e)
    finally:
        conn.close()
    if fails >= 4:   # 4 条子查询全失败 = 连接级故障：诚实 500，前端据此显「加载中」而非 0
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_insights 全部子查询失败 [trace=%s]", trace_id)
        raise HTTPException(status_code=500, detail=f"洞察查询失败 (trace: {trace_id})")
    return out


class KbEmbedRunItem(BaseModel):
    bizdate: str = ""
    embedded: int = 0
    failed: int = 0
    fail_rate: float = 0.0


class KbDeptCoverageItem(BaseModel):
    owner_dept: str = ""
    docs: int = 0                        # 已上线（active）文档数
    new_month: int = 0                   # 本月新增
    qa_hits: int = 0                     # 使用量（命中本部门文档的提问数）
    no_answer_rate: float = 0.0          # 无答案率（命中本部门文档的提问中 REFUSAL 占比）
    pii_docs: int = 0                    # 风险（含 PII 脱敏/隔离的文档数）
    # 文档总量周环比：本周净变化 = active 新增 − 本周退役（退役仅计上周末前已存在者）。
    #   wow_net  = 净变化「篇数」（前端徽标主显，对大部门比百分比更可读）。
    #   wow_total = 净变化 / 上周末总量（比率）；无上周基数(全新部门)→ null。
    # 近似口径：退役时点用 updated_at（retire 即 status='retired'+updated_at=NOW()，无独立 retired_at）；
    #   superseded（版本/去重转移）不计入本指标；故为估算非账面精确值。
    wow_net: Optional[int] = None
    wow_total: Optional[float] = None
    # 使用量周环比：近7天 vs 前7天 命中提问数（COUNT(DISTINCT message_id)）。
    #   qa_wow_net = 净变化「次」（徽标主显）；qa_wow = 净变化 / 上周使用量（无上周使用→ null）。
    qa_wow_net: Optional[int] = None
    qa_wow: Optional[float] = None


class KbFeedbackDay(BaseModel):
    day: str = ""
    up: int = 0
    down: int = 0


class KbDownvoteReason(BaseModel):
    reason: str = ""                     # 中文原因标签
    count: int = 0


class KbFileType(BaseModel):
    ftype: str = ""                      # PDF / DOCX / XLSX / PPTX / 图片 / 其他
    count: int = 0


class KbGovernanceResponse(BaseModel):
    window_days: int = _KB_INSIGHTS_WINDOW_DAYS
    # 资产构成
    file_types: List[KbFileType] = Field(default_factory=list)   # 文件类型分布（按扩展名归类）
    # 运行健康
    docs_active: int = 0
    docs_in_index: int = 0
    dual_version_docs: int = 0
    avg_latency_ms: int = 0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    avg_retrieval_ms: int = 0
    avg_llm_ms: int = 0
    embed_runs: List[KbEmbedRunItem] = Field(default_factory=list)
    # 服务可用性（近 30 天 + 近 24h）
    qa_api_success_rate: float = 0.0     # (总 - LLM_ERROR)/总
    retrieval_api_success_rate: float = 0.0   # (总 - 检索未完成 hit_count IS NULL)/总
    errors_24h: int = 0                  # 近 24 小时错误请求数
    qa_total_30d: int = 0                # 近 30 天问答总数（成功率分母）
    # 治理风险 / 知识效果
    pii_redacted_docs: int = 0
    pii_quarantined_docs: int = 0
    answer_total: int = 0
    answer_success: int = 0
    answer_refusal: int = 0
    answer_no_result: int = 0
    answer_error: int = 0
    effective_rate: float = 0.0
    feedback_up: int = 0
    feedback_down: int = 0
    feedback_total: int = 0
    helpful_rate: float = 0.0
    feedback_last7: int = 0              # 近 7 天反馈数
    feedback_daily: List[KbFeedbackDay] = Field(default_factory=list)   # 近 30 北京日 up/down 趋势
    downvote_reasons: List[KbDownvoteReason] = Field(default_factory=list)  # 点踩原因分布
    escalations: int = 0
    # 部门覆盖 / 使用失衡
    dept_coverage: List[KbDeptCoverageItem] = Field(default_factory=list)


@app.get("/api/kb/governance", response_model=KbGovernanceResponse)
def kb_governance(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """全库运行健康 / 治理风险 / 部门覆盖（仅 kb_admin；真实口径，无造数）。

    延迟为端到端（含钉钉打字机流式渲染，非纯推理）；嵌入失败率仅取 OBS-3 列非空的 stage-3 跑批，
    NULL 视为「未知」绝不当 0；PII/隔离按 document_sensitive_finding 的 COUNT(DISTINCT doc_id)。
    各子查询独立降级，缺数据诚实空。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    _require_kb_admin(identity)
    win = _KB_INSIGHTS_WINDOW_DAYS
    out = KbGovernanceResponse(window_days=win)
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_governance 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"治理查询失败 (trace: {trace_id})")
    fails = 0   # 子查询失败计数；全失败 = 连接级故障 → 诚实 500（而非 all-zeros 伪装健康）
    try:
        # 共享一个游标跑多条子查询：依赖 pymysql 默认 buffered Cursor（_init_db_pool 未设 SSCursor），
        # 某子查询异常后结果已全量缓冲，下一句 execute 不会 "Commands out of sync (2014)"。
        with conn.cursor() as cur:
            # 1) 资产 / 索引可见性
            try:
                cur.execute(
                    "SELECT (SELECT COUNT(*) FROM fuling_knowledge.document_meta WHERE status='active'),"
                    " (SELECT COUNT(DISTINCT doc_id) FROM fuling_knowledge.chunk_meta"
                    "   WHERE is_active=1 AND index_status='INDEXED')")
                r = cur.fetchone() or (0, 0)
                out.docs_active, out.docs_in_index = int(r[0] or 0), int(r[1] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance 资产 失败: %s", e)
            # 2) 双版本残留（stage-3 不变量被破坏的信号；健康应为 0）
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM (SELECT doc_id FROM fuling_knowledge.chunk_meta"
                    " WHERE is_active=1 AND index_status='INDEXED'"
                    " GROUP BY doc_id HAVING COUNT(DISTINCT version_no) > 1) t")
                out.dual_version_docs = int((cur.fetchone() or (0,))[0] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance dual_version 失败: %s", e)
            # 3) 端到端延迟（avg + p50/p95 + 检索/生成分段；窗口内 latency_ms>0）
            try:
                cur.execute(
                    "SELECT ROUND(AVG(latency_ms)), ROUND(AVG(retrieval_latency_ms)), ROUND(AVG(llm_latency_ms)),"
                    " MAX(CASE WHEN pr<=0.5 THEN latency_ms END), MAX(CASE WHEN pr<=0.95 THEN latency_ms END)"
                    " FROM (SELECT latency_ms, retrieval_latency_ms, llm_latency_ms,"
                    "   PERCENT_RANK() OVER (ORDER BY latency_ms) pr"
                    "   FROM fuling_operation.qa_session_log"
                    "   WHERE latency_ms > 0 AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)) t",
                    (win,))
                r = cur.fetchone() or (0, 0, 0, 0, 0)
                out.avg_latency_ms = int(r[0] or 0); out.avg_retrieval_ms = int(r[1] or 0)
                out.avg_llm_ms = int(r[2] or 0)
                out.p50_latency_ms = int(r[3] or 0); out.p95_latency_ms = int(r[4] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance latency 失败: %s", e)
            # 4) 嵌入失败率（OBS-3）：两列都必须非空，否则失败数未知。embedding_failed_chunks 是
            #    独立可空列（embedded_chunks=100、failed=NULL 是合法「未知」），若只判 embedded_chunks
            #    非空会把 NULL 当 0 → 伪造 0% 完美率。故 WHERE 同时要求 failed 非空，未知批次整条不计入。
            try:
                cur.execute(
                    "SELECT bizdate, embedded_chunks, embedding_failed_chunks"
                    " FROM fuling_knowledge.pipeline_run"
                    " WHERE stage=3 AND embedded_chunks IS NOT NULL AND embedding_failed_chunks IS NOT NULL"
                    " ORDER BY started_at DESC LIMIT 8")
                runs = []
                for row in cur.fetchall():
                    emb, fail = int(row[1] or 0), int(row[2] or 0)
                    denom = emb + fail
                    runs.append(KbEmbedRunItem(bizdate=str(row[0] or ""), embedded=emb, failed=fail,
                                               fail_rate=round(fail / denom, 4) if denom else 0.0))
                out.embed_runs = runs
            except Exception as e:
                fails += 1; logger.warning("kb_governance embed_runs 失败: %s", e)
            # 5) 全库回答结果分布（原始 qa_session_log，含 NO_RESULT）
            try:
                cur.execute(
                    "SELECT answer_status, COUNT(*) FROM fuling_operation.qa_session_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY answer_status", (win,))
                for status, n in cur.fetchall():
                    n = int(n or 0); st = (status or "").upper()
                    out.answer_total += n
                    if st == "SUCCESS":
                        out.answer_success += n
                    elif st == "REFUSAL":
                        out.answer_refusal += n
                    elif st == "NO_RESULT":
                        out.answer_no_result += n
                    elif "ERROR" in st:
                        out.answer_error += n
                out.effective_rate = round(out.answer_success / out.answer_total, 4) if out.answer_total else 0.0
            except Exception as e:
                fails += 1; logger.warning("kb_governance answer_mix 失败: %s", e)
            # 6) PII：已脱敏 / 已隔离文档数（COUNT DISTINCT doc_id，按动作）
            try:
                cur.execute(
                    "SELECT (SELECT COUNT(DISTINCT doc_id) FROM fuling_knowledge.document_sensitive_finding"
                    "   WHERE action='REDACTED'),"
                    " (SELECT COUNT(DISTINCT doc_id) FROM fuling_knowledge.document_sensitive_finding"
                    "   WHERE action='QUARANTINED')")
                r = cur.fetchone() or (0, 0)
                out.pii_redacted_docs, out.pii_quarantined_docs = int(r[0] or 0), int(r[1] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance pii 失败: %s", e)
            # 7) 用户反馈（二元好评率 + 近7天量，累计；反馈稀疏故不按窗口切薄）
            try:
                cur.execute(
                    "SELECT SUM(feedback_type='upvote'), SUM(feedback_type='downvote'), COUNT(*),"
                    " SUM(created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY))"
                    " FROM fuling_operation.user_feedback WHERE feedback_type IN ('upvote','downvote')")
                r = cur.fetchone() or (0, 0, 0, 0)
                out.feedback_up, out.feedback_down = int(r[0] or 0), int(r[1] or 0)
                out.feedback_total = int(r[2] or 0); out.feedback_last7 = int(r[3] or 0)
                out.helpful_rate = round(out.feedback_up / out.feedback_total, 4) if out.feedback_total else 0.0
            except Exception as e:
                fails += 1; logger.warning("kb_governance feedback 失败: %s", e)
            # 7b) 反馈趋势：近 30 北京日 up/down（+15h 分桶）
            try:
                cur.execute(
                    "SELECT DATE(DATE_ADD(created_at, INTERVAL 15 HOUR)),"
                    " SUM(feedback_type='upvote'), SUM(feedback_type='downvote')"
                    " FROM fuling_operation.user_feedback"
                    " WHERE feedback_type IN ('upvote','downvote')"
                    "   AND created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)"
                    " GROUP BY 1 ORDER BY 1")
                out.feedback_daily = [KbFeedbackDay(day=str(row[0]), up=int(row[1] or 0), down=int(row[2] or 0))
                                      for row in cur.fetchall()]
            except Exception as e:
                fails += 1; logger.warning("kb_governance feedback_daily 失败: %s", e)
            # 7c) 点踩原因分布（feedback_reason 多选逗号拼接 → Python 拆分计数 + 中文标签；null=未注明）
            try:
                cur.execute(
                    "SELECT feedback_reason, COUNT(*) FROM fuling_operation.user_feedback"
                    " WHERE feedback_type='downvote' GROUP BY feedback_reason")
                _RLABEL = {"inaccurate": "不准确", "irrelevant": "不相关", "incomplete": "不完整",
                           "outdated": "已过时", "not_found": "未找到", "other": "其他"}
                rcount: Dict[str, int] = {}
                for reason, n in cur.fetchall():
                    n = int(n or 0)
                    codes = [x.strip() for x in (reason or "").split(",") if x.strip()] or ["__none__"]
                    for code in codes:
                        label = "未注明" if code == "__none__" else _RLABEL.get(code, code)
                        rcount[label] = rcount.get(label, 0) + n
                out.downvote_reasons = sorted(
                    [KbDownvoteReason(reason=k, count=v) for k, v in rcount.items()],
                    key=lambda x: x.count, reverse=True)
            except Exception as e:
                fails += 1; logger.warning("kb_governance downvote_reasons 失败: %s", e)
            # 8) 转人工工单数
            try:
                cur.execute("SELECT COUNT(*) FROM fuling_operation.escalation_ticket")
                out.escalations = int((cur.fetchone() or (0,))[0] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance escalations 失败: %s", e)
            # 9) 部门覆盖与失衡：已上线 / 本月新增 / 使用量(命中提问数) / 无答案率(refusal占比) / 风险(PII文档)。
            #    qa_hits + refusal 用 COUNT(DISTINCT message_id) 去 chunk 扇出；PII JOIN 同样需 collation-cast。
            try:
                from datetime import date as _date
                ms = _date.today().replace(day=1).isoformat()
                cov: Dict[str, Dict[str, int]] = {}

                def _cell(d):
                    return cov.setdefault(d or "unknown", {"docs": 0, "new_month": 0, "qa_hits": 0, "refusal": 0, "pii": 0, "new7": 0, "ret7": 0, "qa7": 0, "qa_prev7": 0})

                cur.execute("SELECT owner_dept, COUNT(*) FROM fuling_knowledge.document_meta"
                            " WHERE status='active' GROUP BY owner_dept")
                for dept, docs in cur.fetchall():
                    _cell(dept)["docs"] = int(docs or 0)
                cur.execute("SELECT owner_dept, COUNT(*) FROM fuling_knowledge.document_meta"
                            " WHERE status='active' AND created_at >= %s GROUP BY owner_dept", (ms,))
                for dept, n in cur.fetchall():
                    _cell(dept)["new_month"] = int(n or 0)
                # 文档总量周环比：本周 active 新增；本周退役只计【上周末前已存在】者（created_at < 7d），
                # 否则「同周内先建后退役」会被算成 −1 幻影下跌（该文档上/本周末都不在 active 集，净贡献应为 0）。
                # updated_at 近似退役时点（无独立 retired_at）。
                wow_ok = True
                try:
                    cur.execute("SELECT owner_dept, COUNT(*) FROM fuling_knowledge.document_meta"
                                " WHERE status='active' AND created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY owner_dept")
                    for dept, n in cur.fetchall():
                        _cell(dept)["new7"] = int(n or 0)
                    cur.execute("SELECT owner_dept, COUNT(*) FROM fuling_knowledge.document_meta"
                                " WHERE status='retired' AND updated_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
                                " AND created_at < DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY owner_dept")
                    for dept, n in cur.fetchall():
                        _cell(dept)["ret7"] = int(n or 0)
                except Exception as e:
                    wow_ok = False; logger.warning("kb_governance dept wow 失败: %s", e)
                cur.execute(
                    "SELECT m.owner_dept, COUNT(DISTINCT q.message_id),"
                    " COUNT(DISTINCT CASE WHEN q.answer_status='REFUSAL' THEN q.message_id END)"
                    " FROM fuling_operation.qa_session_log q"
                    " JOIN JSON_TABLE(q.retrieved_docs_json, '$[*]' COLUMNS(doc_id VARCHAR(100) PATH '$.doc_id')) jt"
                    " JOIN fuling_knowledge.document_meta m"
                    "   ON m.doc_id = CONVERT(jt.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci"
                    " WHERE q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY m.owner_dept", (win,))
                for dept, hits, refu in cur.fetchall():
                    cell = _cell(dept); cell["qa_hits"] = int(hits or 0); cell["refusal"] = int(refu or 0)
                # 各部门使用量周环比：近7天 vs 前7天 命中提问数（与 qa_hits 同 DISTINCT message_id 去 chunk 扇出口径）。
                qa_wow_ok = True
                try:
                    cur.execute(
                        "SELECT m.owner_dept,"
                        " COUNT(DISTINCT CASE WHEN q.created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) THEN q.message_id END),"
                        " COUNT(DISTINCT CASE WHEN q.created_at >= DATE_SUB(NOW(), INTERVAL 14 DAY)"
                        "   AND q.created_at < DATE_SUB(NOW(), INTERVAL 7 DAY) THEN q.message_id END)"
                        " FROM fuling_operation.qa_session_log q"
                        " JOIN JSON_TABLE(q.retrieved_docs_json, '$[*]' COLUMNS(doc_id VARCHAR(100) PATH '$.doc_id')) jt"
                        " JOIN fuling_knowledge.document_meta m"
                        "   ON m.doc_id = CONVERT(jt.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci"
                        " WHERE q.created_at >= DATE_SUB(NOW(), INTERVAL 14 DAY) GROUP BY m.owner_dept")
                    for dept, q7, qp7 in cur.fetchall():
                        cell = _cell(dept); cell["qa7"] = int(q7 or 0); cell["qa_prev7"] = int(qp7 or 0)
                except Exception as e:
                    qa_wow_ok = False; logger.warning("kb_governance dept usage wow 失败: %s", e)
                cur.execute(
                    "SELECT m.owner_dept, COUNT(DISTINCT f.doc_id)"
                    " FROM fuling_knowledge.document_sensitive_finding f"
                    " JOIN fuling_knowledge.document_meta m"
                    "   ON m.doc_id = CONVERT(f.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci"
                    " WHERE f.action IN ('QUARANTINED','REDACTED') GROUP BY m.owner_dept")
                for dept, n in cur.fetchall():
                    _cell(dept)["pii"] = int(n or 0)
                def _wow_net(v):                          # 本周净变化「篇数」
                    return (v["new7"] - v["ret7"]) if wow_ok else None
                def _wow_pct(v):                          # 净变化 / 上周末总量（无上周基数→null）
                    if not wow_ok:
                        return None
                    delta = v["new7"] - v["ret7"]
                    base = v["docs"] - delta               # 上周末总量 = 今总量 − 净变化
                    return round(delta / base, 4) if base > 0 else None
                def _qa_wow_net(v):                        # 使用量本周净变化「次」
                    return (v["qa7"] - v["qa_prev7"]) if qa_wow_ok else None
                def _qa_wow(v):                            # 使用量周环比（无上周使用→null）
                    if not qa_wow_ok:
                        return None
                    return round((v["qa7"] - v["qa_prev7"]) / v["qa_prev7"], 4) if v["qa_prev7"] > 0 else None
                out.dept_coverage = sorted(
                    [KbDeptCoverageItem(
                        owner_dept=k, docs=v["docs"], new_month=v["new_month"], qa_hits=v["qa_hits"],
                        no_answer_rate=round(v["refusal"] / v["qa_hits"], 4) if v["qa_hits"] else 0.0,
                        pii_docs=v["pii"], wow_net=_wow_net(v), wow_total=_wow_pct(v),
                        qa_wow_net=_qa_wow_net(v), qa_wow=_qa_wow(v)) for k, v in cov.items()],
                    key=lambda x: x.docs, reverse=True)
            except Exception as e:
                fails += 1; logger.warning("kb_governance dept_coverage 失败: %s", e)
            # 10) 文件类型分布（按 original_filename 扩展名归类；Python 端合并到 PDF/DOCX/XLSX/PPTX/图片/其他）
            try:
                cur.execute(
                    "SELECT LOWER(SUBSTRING_INDEX(original_filename, '.', -1)) ext, COUNT(*)"
                    " FROM fuling_knowledge.document_meta"
                    " WHERE status='active' AND original_filename LIKE '%.%' GROUP BY ext")
                _EXT2T = {"pdf": "PDF", "docx": "DOCX", "doc": "DOCX", "xlsx": "XLSX", "xls": "XLSX",
                          "pptx": "PPTX", "ppt": "PPTX",
                          "png": "图片", "jpg": "图片", "jpeg": "图片", "gif": "图片", "webp": "图片", "bmp": "图片"}
                _ORDER = ["PDF", "DOCX", "XLSX", "PPTX", "图片", "其他"]
                ftc: Dict[str, int] = {}
                for ext, n in cur.fetchall():
                    ftc[_EXT2T.get((ext or "").strip(), "其他")] = ftc.get(_EXT2T.get((ext or "").strip(), "其他"), 0) + int(n or 0)
                out.file_types = [KbFileType(ftype=t, count=ftc[t]) for t in _ORDER if ftc.get(t)]
            except Exception as e:
                fails += 1; logger.warning("kb_governance file_types 失败: %s", e)
            # 11) 服务可用性：问答API成功率(非 LLM_ERROR) / 检索API成功率(hit_count 非空) / 近30天总数 / 近24h错误数。
            #     检索错误（HA3 connection refused）在 serving 里落到 LLM_ERROR + hit_count=NULL，故用 NULL 判检索未完成。
            try:
                cur.execute(
                    "SELECT COUNT(*), SUM(answer_status='LLM_ERROR'), SUM(opensearch_hit_count IS NULL)"
                    " FROM fuling_operation.qa_session_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)", (win,))
                r = cur.fetchone() or (0, 0, 0)
                tot = int(r[0] or 0); llm_err = int(r[1] or 0); hit_null = int(r[2] or 0)
                out.qa_total_30d = tot
                out.qa_api_success_rate = round((tot - llm_err) / tot, 4) if tot else 0.0
                out.retrieval_api_success_rate = round((tot - hit_null) / tot, 4) if tot else 0.0
                cur.execute(
                    "SELECT SUM(answer_status LIKE '%ERROR%') FROM fuling_operation.qa_session_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)")
                out.errors_24h = int((cur.fetchone() or (0,))[0] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance availability 失败: %s", e)
    finally:
        conn.close()
    if fails >= 13:   # 13 条子查询全失败 = 连接级故障：诚实 500，前端据此显「加载中」而非伪造健康
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_governance 全部子查询失败 [trace=%s]", trace_id)
        raise HTTPException(status_code=500, detail=f"治理查询失败 (trace: {trace_id})")
    return out


class KbConfigResponse(BaseModel):
    max_upload_bytes: int = 0
    accepted_exts: List[str] = Field(default_factory=list)


@app.get("/api/kb/config", response_model=KbConfigResponse)
def kb_config(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """前端能力配置（上传上限/受理类型）—— 后端权威，省得客户端硬编码 50MB/类型导致"传完才 413"漂移。

    **有意公开**（不加 _require_kb_console）：仅暴露静态能力常量（上传字节上限 + 扩展名白名单），
    非敏感、无部门/文档数据；客户端在上传前自检需要它，限流即足以防滥用（G6）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    from opensearch_pipeline.kb_upload import MAX_UPLOAD_BYTES, _PHASE1_EXTS
    return KbConfigResponse(
        max_upload_bytes=int(MAX_UPLOAD_BYTES),
        accepted_exts=sorted(_PHASE1_EXTS),
    )


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
            status_badge=_kb_status_badge(cps, ixs, _doc_status),   # 传 doc 级状态 → 退役文档各版本如实显「已退役」(B4)
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
    content_type: str = ""   # 客户端 PUT 必须发此 Content-Type（已签入 put_url，不一致 OSS 403）；G4


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


class KbRestoreResponse(BaseModel):
    status: str = "ok"
    doc_id: str
    restored: bool = False
    already: bool = False
    status_badge: str = "在线"
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
    # G4：把 Content-Type 按申报扩展名钉死并签入 PUT URL —— 客户端须发完全一致的 Content-Type，
    # 否则 OSS 拒签（403），杜绝持 URL 者上传任意类型/与扩展名不符的字节。content_type 回传客户端。
    from opensearch_pipeline.oss_url import mime_for_ext
    content_type = mime_for_ext(ext)
    put_url = generate_signed_url(raw_key, expires=kb_upload.UPLOAD_TOKEN_TTL, method="PUT",
                                  content_type=content_type)
    logger.info("kb upload-url: uid=%s action=%s doc_id=%s owner=%s bucket=%s ctype=%s",
                kb.user_id, req.action, doc_id, owner, bucket, content_type)
    return KbUploadUrlResponse(
        upload_token=token, put_url=put_url, raw_key=raw_key, doc_id=doc_id,
        expires_in=kb_upload.UPLOAD_TOKEN_TTL,
        requires_kb_admin_approval=bool(decision.requires_kb_admin_approval),
        content_type=content_type,
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
                # RDS 侧停用该文档【全部活跃版本】chunk（不限当前版本）——若此前部分入库/搬迁残留了旧版本
                # is_active=1（双版本 gap），只停当前版本会让它们退役后仍存活、被邻居拼接复用、且 HA3 清除
                # 漏删而无限期滞留。退役语义是「整篇下线」，故停全部活跃 chunk（stage-3 reconcile 再兜底 HA3）。
                cur.execute("UPDATE fuling_knowledge.chunk_meta SET is_active=0 "
                            "WHERE doc_id=%s AND is_active=1", (req.doc_id,))
                # 审计行入【同事务】（commit 前、同 cursor）：与退役变更原子提交，杜绝 commit 与审计之间
                # 崩溃丢记录的窗口（B1）。失败 → 整笔回滚 → 500 可重试。
                write_audit(doc_id=req.doc_id, version_no=cur_ver, action_type="RETIRE_REQUEST",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"owner={owner_dept} perm={perm} reason={(req.reason or '')[:200]}",
                            cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_retire 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"退役失败 (trace: {trace_id})")
    return KbRetireResponse(
        doc_id=req.doc_id, retired=True,
        note="已申请退役：已标记下线、停止作为升版目标；从检索彻底移除将在下次维护完成（本操作可逆）")


@app.post("/api/kb/restore", response_model=KbRestoreResponse)
def kb_restore(req: KbRetireRequest, request: Request,
               identity: Optional[Identity] = Depends(current_identity)):
    """恢复上线（退役的逆操作）：把退役文档重新激活 + 标脏待重索引。授权与退役同款。

    仅改 RDS（document_meta/version.status='active' + chunk_meta.is_active=1 + index_status='NOT_INDEXED'）。
    软退役不删 HA3（is_active=0 仅 RDS 标记）：若退役后【尚未】跑 HA3 清除维护，chunk 仍在 HA3 →
    本操作即时恢复检索；若已被 gated 维护从 HA3 删除，则标脏 NOT_INDEXED，下次 stage-3 drain 重嵌+重推
    后恢复（与退役"可逆"承诺对齐，且覆盖已清除的边界情形）。不触碰 HA3（重推交 stage-3）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    assert_metadata_write_allowed("kb_restore", get_config().rds.host, kind="rds")
    trace_id = uuid.uuid4().hex[:8]
    owner_dept = perm = ""
    cur_ver = 1
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT owner_dept, permission_level, status, current_version_no "
                            "FROM fuling_knowledge.document_meta WHERE doc_id=%s FOR UPDATE", (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm = (row[0] or ""), (row[1] or "")
                status, cur_ver = (row[2] or "active"), int(row[3] or 1)
                # 授权：与退役同款不对称——作用域 + 公开文档需 kb_admin
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权恢复该文档（owner_dept 不在管理范围）")
                if perm == "public" and kb.role != ROLE_KB_ADMIN:
                    raise HTTPException(status_code=403, detail="公开文档需知识库管理员恢复")
                if str(status).lower() == "active":
                    conn.commit()       # 幂等：已在线 → 直接回既有态
                    return KbRestoreResponse(doc_id=req.doc_id, restored=False, already=True,
                                             note="该文档已是在线状态")
                cur.execute("UPDATE fuling_knowledge.document_meta SET status='active', updated_at=NOW() "
                            "WHERE doc_id=%s", (req.doc_id,))
                cur.execute("UPDATE fuling_knowledge.document_version SET status='active', updated_at=NOW() "
                            "WHERE doc_id=%s AND version_no=%s", (req.doc_id, cur_ver))
                # 重新激活本版本 chunk + 标脏 NOT_INDEXED（下次 stage-3 重推 HA3；若 HA3 未删则为幂等重推）。
                cur.execute("UPDATE fuling_knowledge.chunk_meta SET is_active=1, index_status='NOT_INDEXED' "
                            "WHERE doc_id=%s AND version_no=%s AND is_active=0", (req.doc_id, cur_ver))
                write_audit(doc_id=req.doc_id, version_no=cur_ver, action_type="RESTORE_REQUEST",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"owner={owner_dept} perm={perm} reason={(req.reason or '')[:200]}",
                            cursor=cur)   # 同事务审计（B1）
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_restore 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"恢复失败 (trace: {trace_id})")
    return KbRestoreResponse(
        doc_id=req.doc_id, restored=True,
        note="已恢复上线：重新激活并标记待重索引；若退役后 HA3 仍在则即时可检索，否则下次维护重索引后恢复")


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


# ── 跨部门文档检索授权申请（Phase C 记录层）─────────────────────────────────
# 申请人 = 部门管理员（在「全部门」浏览里对其他部门 dept_internal 文档发起）；
# 审批方 = 文档所属部门管理员（owner_dept ∈ 其 managed）或 kb_admin（_kb_can_manage）。
# ⚠️ 审批通过【只记录决策】，不立即放行检索——真正让申请部门检索到该文档 = Phase D
#    （把授予部门写进 allowed_depts 并接入 retriever HA3 ACL，不可逆 HA3 改动，单独授权）。
class KbAccessRequestSubmit(BaseModel):
    doc_id: str
    owner_dept: Optional[str] = None   # 客户端值仅参考；owner_dept 一律以 DB 现查为准
    reason: Optional[str] = None


class KbAccessDecisionRequest(BaseModel):
    id: str
    reason: Optional[str] = None


class KbAccessRequestSubmitResponse(BaseModel):
    id: str = ""
    status: str = "pending"
    already: bool = False


class KbAccessDecisionResponse(BaseModel):
    id: str = ""
    status: str = ""
    decided: bool = False
    already: bool = False


class KbAccessRequestItem(BaseModel):
    id: str = ""
    doc_id: str = ""
    doc_title: str = ""
    owner_dept: str = ""
    requester_dept: str = ""
    requester_name: str = ""
    permission_level: str = "dept_internal"
    reason: str = ""
    created_at: str = ""


class KbAccessRequestListResponse(BaseModel):
    items: List[KbAccessRequestItem] = Field(default_factory=list)


class KbAccessGrantItem(BaseModel):
    """审批方侧的【已放行】跨部门授权（status='approved'）——供「已授权清单」展示 + 撤销。"""
    id: str = ""
    doc_id: str = ""
    doc_title: str = ""
    owner_dept: str = ""
    requester_dept: str = ""        # 获授权检索的组码（requester_depts）
    requester_name: str = ""
    permission_level: str = "dept_internal"
    reason: str = ""
    decided_at: str = ""            # 批准时间（授权生效时点）


class KbAccessGrantListResponse(BaseModel):
    items: List[KbAccessGrantItem] = Field(default_factory=list)


# ── Phase F：成员/角色管理（kb_admin 维护 dept_admin 写授权；三分授权 读≠管理≠授权）──
class KbAdminItem(BaseModel):
    user_id: str = ""
    user_name: str = ""
    role: str = ""                                            # dept_admin / kb_admin
    managed_owner_depts: List[str] = Field(default_factory=list)  # dept_admin 显式授权；kb_admin=全部(空数组表示全量)


class KbAdminListResponse(BaseModel):
    items: List[KbAdminItem] = Field(default_factory=list)
    grantable_owner_depts: List[str] = Field(default_factory=list)  # 表单可选项（写白名单单一来源）


class KbAdminGrantRequest(BaseModel):
    user_id: str = ""                                         # 钉钉 staffId
    user_name: str = ""
    owner_depts: List[str] = Field(default_factory=list)      # 授予可管理的 owner_dept（权威全集，提交即覆盖）
    note: str = ""


class KbAdminRevokeRequest(BaseModel):
    user_id: str = ""
    owner_dept: str = ""                                      # 空 = 撤销该用户全部授权并降级 employee


class KbAdminGrantResponse(BaseModel):
    user_id: str = ""
    role: str = ""
    managed_owner_depts: List[str] = Field(default_factory=list)
    ok: bool = True


class MyAccessRequestItem(BaseModel):
    id: str = ""
    doc_id: str = ""
    doc_title: str = ""
    owner_dept: str = ""
    requester_dept: str = ""        # 本次授予的组码（requester_depts）
    status: str = ""               # pending / approved / rejected
    sync_state: str = ""           # n/a | pending_sync（已批准·待同步）| projected（已放行）
    reason: str = ""
    created_at: str = ""
    decided_at: str = ""


class MyAccessRequestListResponse(BaseModel):
    items: List[MyAccessRequestItem] = Field(default_factory=list)


@app.post("/api/kb/access-requests", response_model=KbAccessRequestSubmitResponse)
def kb_access_request_submit(req: KbAccessRequestSubmit, request: Request,
                             identity: Optional[Identity] = Depends(current_identity)):
    """部门管理员对【其他部门】dept_internal 文档发起检索授权申请。

    硬规则（fail-closed）：只 dept_internal 可申请（public 本就可读、restricted 不可外露）；
    本部门文档无需申请；kb_admin 直接管理无需申请；同 (doc, 申请人) 已有 pending → 幂等返回。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    if kb.role == ROLE_KB_ADMIN:
        raise HTTPException(status_code=400, detail="知识库管理员可直接管理全部文档，无需申请授权")
    managed = set(managed_owner_depts(kb))
    if not managed:
        raise HTTPException(status_code=403, detail="无管理部门，无法代部门申请授权")
    assert_metadata_write_allowed("kb_access_request_submit", get_config().rds.host, kind="rds")
    trace_id = uuid.uuid4().hex[:8]
    owner_dept = ""
    requester_depts = ",".join(sorted(managed))
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT owner_dept, permission_level, status FROM fuling_knowledge.document_meta "
                            "WHERE doc_id=%s LIMIT 1", (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm, status = (row[0] or ""), (row[1] or ""), (row[2] or "active")
                if str(status).lower() != "active":
                    raise HTTPException(status_code=400, detail="该文档非在线状态，无法申请")
                if perm == "public":
                    raise HTTPException(status_code=400, detail="公开文档全公司可检索，无需申请")
                if perm != "dept_internal":
                    raise HTTPException(status_code=403, detail="该文档不可申请授权")
                if owner_dept in managed:
                    raise HTTPException(status_code=400, detail="本部门文档无需申请")
                # 幂等：已有同 (doc, 申请人) pending → 返回既有，不重复入队
                cur.execute("SELECT id FROM fuling_knowledge.kb_access_request "
                            "WHERE doc_id=%s AND requester_id=%s AND status='pending' LIMIT 1",
                            (req.doc_id, kb.user_id))
                ex = cur.fetchone()
                if ex:
                    conn.commit()
                    return KbAccessRequestSubmitResponse(id=str(ex[0]), status="pending", already=True)
                cur.execute(
                    "INSERT INTO fuling_knowledge.kb_access_request "
                    "(doc_id, owner_dept, requester_id, requester_name, requester_depts, reason, status) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'pending')",
                    (req.doc_id, owner_dept, kb.user_id, kb.name, requester_depts, (req.reason or "")[:512]),
                )
                new_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_access_request_submit 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"提交授权申请失败 (trace: {trace_id})")
    write_audit(doc_id=req.doc_id, version_no=None, action_type="ACCESS_REQUEST_SUBMIT",
                operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                message=f"owner={owner_dept} requester_depts={requester_depts}")
    return KbAccessRequestSubmitResponse(id=str(new_id), status="pending", already=False)


@app.get("/api/kb/access-requests", response_model=KbAccessRequestListResponse)
def kb_access_requests_list(request: Request,
                            identity: Optional[Identity] = Depends(current_identity)):
    """审批方待办：列出【我有权审批】的 pending 申请（owner_dept ∈ 我 managed；kb_admin 全部）。只读。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    clause, params = "", []
    if kb.role != ROLE_KB_ADMIN:
        owners = managed_owner_depts(kb)
        if not owners:
            return KbAccessRequestListResponse(items=[])
        clause = "AND r.owner_dept IN (" + ",".join(["%s"] * len(owners)) + ")"
        params = list(owners)
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT r.id, r.doc_id, m.title, r.owner_dept, r.requester_depts,
                           r.requester_name, m.permission_level, r.reason, r.created_at
                    FROM fuling_knowledge.kb_access_request r
                    JOIN fuling_knowledge.document_meta m ON m.doc_id = r.doc_id
                    WHERE r.status='pending' {clause}
                    ORDER BY r.created_at DESC
                    LIMIT 100
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_access_requests_list 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"授权申请队列查询失败 (trace: {trace_id})")
    items = [
        KbAccessRequestItem(
            id=str(r[0]), doc_id=r[1] or "", doc_title=r[2] or "", owner_dept=r[3] or "",
            requester_dept=r[4] or "", requester_name=r[5] or "",
            permission_level=r[6] or "dept_internal", reason=r[7] or "",
            created_at=str(r[8]) if r[8] else "",
        )
        for r in rows
    ]
    return KbAccessRequestListResponse(items=items)


@app.get("/api/kb/access-grants", response_model=KbAccessGrantListResponse)
def kb_access_grants_list(request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """审批方侧：列出【我可管理】文档上现行有效（status='approved'）的跨部门检索授权，供撤销。

    owner_dept ∈ 我 managed（kb_admin 全部）。与待审批队列（pending）区分：此处是已放行的【存量】，
    撤销动作走 POST /api/kb/access-requests/revoke。只读。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, managed_owner_depts
    clause, params = "", []
    if kb.role != ROLE_KB_ADMIN:
        owners = managed_owner_depts(kb)
        if not owners:
            return KbAccessGrantListResponse(items=[])
        clause = "AND r.owner_dept IN (" + ",".join(["%s"] * len(owners)) + ")"
        params = list(owners)
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT r.id, r.doc_id, m.title, r.owner_dept, r.requester_depts,
                           r.requester_name, m.permission_level, r.reason, r.decided_at
                    FROM fuling_knowledge.kb_access_request r
                    JOIN fuling_knowledge.document_meta m ON m.doc_id = r.doc_id
                    WHERE r.status='approved' {clause}
                    ORDER BY r.decided_at DESC
                    LIMIT 200
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_access_grants_list 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"已授权清单查询失败 (trace: {trace_id})")
    items = [
        KbAccessGrantItem(
            id=str(r[0]), doc_id=r[1] or "", doc_title=r[2] or "", owner_dept=r[3] or "",
            requester_dept=r[4] or "", requester_name=r[5] or "",
            permission_level=r[6] or "dept_internal", reason=r[7] or "",
            decided_at=str(r[8]) if r[8] else "",
        )
        for r in rows
    ]
    return KbAccessGrantListResponse(items=items)


@app.get("/api/kb/my-access-requests", response_model=MyAccessRequestListResponse)
def kb_my_access_requests(request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """申请人侧：列出【我提交】的授权申请 + 派生同步态。只读。

    派生（不存列，Phase D constraint 7）：approved 且该 doc current-version active chunk 全
    INDEXED 且 chunk_meta.allowed_depts ⊇ 本次授予组码 → 'projected'（已放行）；否则
    'pending_sync'（已批准·待同步）。pending/rejected → 'n/a'。flag 关时投影恒空 → approved
    恒显 pending_sync（如实，未真正放行）。INDEXED 在生产 parity-verify 开时 = HA3 物理存在态。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    items: List[MyAccessRequestItem] = []
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        from opensearch_pipeline.access_grants import current_allowed_for_doc
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.id, r.doc_id, m.title, r.owner_dept, r.requester_depts, r.status,
                           r.reason, r.created_at, r.decided_at, m.current_version_no
                    FROM fuling_knowledge.kb_access_request r
                    LEFT JOIN fuling_knowledge.document_meta m ON m.doc_id = r.doc_id
                    WHERE r.requester_id = %s
                    ORDER BY r.created_at DESC
                    LIMIT 100
                    """,
                    (kb.user_id,),
                )
                rows = cur.fetchall()
                for r in rows:
                    doc_id = r[1] or ""
                    rdepts = r[4] or ""
                    status = r[5] or ""
                    sync = "n/a"
                    if status == "approved" and doc_id:
                        try:
                            ver = int(r[9] or 1)
                            cur.execute(
                                "SELECT COUNT(*), SUM(index_status='INDEXED') "
                                "FROM fuling_knowledge.chunk_meta "
                                "WHERE doc_id=%s AND version_no=%s AND is_active=1", (doc_id, ver))
                            cnt_row = cur.fetchone() or (0, 0)
                            cnt = int(cnt_row[0] or 0)
                            n_idx = int(cnt_row[1] or 0)
                            allowed = set(current_allowed_for_doc(cur, doc_id, ver))
                            granted = {g.strip() for g in rdepts.split(",") if g.strip()}
                            projected = bool(cnt and cnt == n_idx and granted and granted <= allowed)
                            sync = "projected" if projected else "pending_sync"
                        except Exception as _re:   # noqa: BLE001 — 单行派生失败（如脏 allowed_depts JSON）→
                            # 降级该行为 n/a 并继续，绝不连累整张列表 500（与 reconcile 逐文档兜底同型）。
                            logger.warning("my-access 同步态派生失败 doc=%s，降级 n/a: %s", doc_id, _re)
                            sync = "n/a"
                    items.append(MyAccessRequestItem(
                        id=str(r[0]), doc_id=doc_id, doc_title=r[2] or "", owner_dept=r[3] or "",
                        requester_dept=rdepts, status=status, sync_state=sync, reason=r[6] or "",
                        created_at=str(r[7]) if r[7] else "", decided_at=str(r[8]) if r[8] else ""))
        finally:
            conn.close()
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_my_access_requests 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"我的授权申请查询失败 (trace: {trace_id})")
    return MyAccessRequestListResponse(items=items)


def _kb_access_decide(req: KbAccessDecisionRequest, request: Request,
                      identity: Optional[Identity], decision: str,
                      *, from_status: str = "pending") -> KbAccessDecisionResponse:
    """审批 / 撤销一条申请。授权：文档所属部门管理员（_kb_can_manage）或 kb_admin。

    状态机（单向）：pending→approved / pending→rejected（审批）；approved→revoked（撤销已批授权）。
    `from_status` = 本次操作要求的前态——非该前态 → 幂等返回（不重复改、不误转）。

    改 kb_access_request.status，并（flag 开）在同事务内经 materialize_doc_allowed_depts 把该 doc 的
    allowed_depts 投影标脏。撤销（approved→revoked）后该行不再 status='approved' → 重算时被剔除 →
    投影收窄/清空 → stage-3 下次 drain 从 HA3 收回（这正是「无撤销路径」缺口的修复）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if not req.id:
        raise HTTPException(status_code=400, detail="缺少 id")
    assert_metadata_write_allowed(f"kb_access_request_{decision}", get_config().rds.host, kind="rds")
    trace_id = uuid.uuid4().hex[:8]
    owner_dept = ""
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT owner_dept, status, doc_id FROM fuling_knowledge.kb_access_request "
                            "WHERE id=%s FOR UPDATE", (req.id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="申请不存在")
                owner_dept, status, doc_id = (row[0] or ""), (row[1] or ""), (row[2] or "")
                # 审批权：文档所属部门管理员（owner_dept ∈ managed）或 kb_admin
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权操作该申请（非文档所属部门管理员）")
                if status != from_status:
                    conn.commit()       # 幂等：非目标前态（已决 / 非 approved）→ 返回既有态
                    return KbAccessDecisionResponse(id=req.id, status=status, decided=False, already=True)
                cur.execute("UPDATE fuling_knowledge.kb_access_request "
                            "SET status=%s, decided_by=%s, decided_at=NOW(), decision_note=%s WHERE id=%s",
                            (decision, kb.user_id, (req.reason or "")[:512], req.id))
                # Phase D（flag 开）：同事务内把该 doc 的 allowed_depts 投影【标脏】——经共享注入点
                # materialize_doc_allowed_depts 重算 authority（含刚改的本行 status，读己写：approve→纳入、
                # reject/revoke→剔除）→ 版本限定 gate 到 dept_internal → diff →（变更）写 chunk_meta.allowed_depts +
                # index_status='NOT_INDEXED'，stage-3 下次 drain 据此重推 HA3。helper 内置 2h PROCESSING
                # 反抢锁（与对账同口径）：current version 正在 stage-3 装载时跳过标脏，交对账下轮重对，杜绝
                # 标脏被 stage-3 写回 INDEXED 覆盖而 HA3 仍旧 ACL 的自愈失败漂移。**绝不写 HA3 / 不
                # re-embed**（重活留给 stage-3）。flag 关 = no-op；失败只记日志、**不回滚 status**
                # （allowed_depts_reconcile 每轮 stage-3 兜底）。
                if get_config().rag.allowed_depts_acl and doc_id:
                    from opensearch_pipeline.access_grants import (
                        enqueue_acl_projection, materialize_doc_allowed_depts,
                    )
                    # 持久入队（同事务、不吞异常）：权威变更与投影意图原子提交——enqueue 失败则整笔回滚，
                    # 绝不出现「权威已改而无 outbox 行」的撕裂。stage-3 outbox drain 据此定向幂等重试至成功。
                    enqueue_acl_projection(cur, doc_id, reason=decision)
                    # 内联标脏 = best-effort 快路径：成功则本轮 stage-3 即可重推；抛/skipped_locked → 上面
                    # 的 outbox 行兜底（+ allowed_depts_reconcile 全扫）。失败只记日志、**不回滚 status**。
                    try:
                        materialize_doc_allowed_depts(cur, doc_id)
                    except Exception as _pe:
                        logger.warning("decide allowed_depts 内联标脏失败（outbox+reconciler 兜底）doc=%s: %s",
                                       doc_id, _pe)
                # 审计行入【同事务】（commit 前、同 cursor）：与 status 变更 + outbox 入队原子提交（B1）。
                write_audit(doc_id=doc_id, version_no=None, action_type=f"ACCESS_REQUEST_{decision.upper()}",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"req_id={req.id} owner={owner_dept}", cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_access_request_%s 失败 [trace=%s]: %s", decision, trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"操作失败 (trace: {trace_id})")
    return KbAccessDecisionResponse(id=req.id, status=decision, decided=True, already=False)


@app.post("/api/kb/access-requests/approve", response_model=KbAccessDecisionResponse)
def kb_access_request_approve(req: KbAccessDecisionRequest, request: Request,
                              identity: Optional[Identity] = Depends(current_identity)):
    """通过申请（仅记录决策；真正放行检索 = Phase D allowed_depts）。"""
    return _kb_access_decide(req, request, identity, decision="approved")


@app.post("/api/kb/access-requests/reject", response_model=KbAccessDecisionResponse)
def kb_access_request_reject(req: KbAccessDecisionRequest, request: Request,
                             identity: Optional[Identity] = Depends(current_identity)):
    """驳回申请。"""
    return _kb_access_decide(req, request, identity, decision="rejected")


@app.post("/api/kb/access-requests/revoke", response_model=KbAccessDecisionResponse)
def kb_access_request_revoke(req: KbAccessDecisionRequest, request: Request,
                             identity: Optional[Identity] = Depends(current_identity)):
    """撤销一条【已批准】的跨部门授权（approved→revoked）。授权同审批方（owner-dept 管理员 / kb_admin）。

    复用 decide 机制：同事务把该 doc 的 allowed_depts 重算（剔除本撤销行、保留其余 approved 授权）→
    收窄/清空投影 + 标脏，stage-3 下次 drain 从 HA3 收回放行。这是「approved 无法经 API 撤销」缺口的
    一等修复——此前 reject 对 approved 行因 status!='pending' 幂等无效，只能直接改库 + 等夜间对账。
    撤销后申请人可重新申请（revoked 同 rejected，不阻 submit 去重——后者只挡 pending）。
    """
    return _kb_access_decide(req, request, identity, decision="revoked", from_status="approved")


# ═══════════════════════════════════════════════════════════════
# Phase F — 成员/角色管理（kb_admin 专属）：维护 dept_admin 角色 + 其 owner_dept 写授权。
#   权威表：fuling_knowledge.user_role.role + dept_admin_grant（resolve_kb_identity 现查,撤销即时生效）。
#   三分授权：读组(acl_groups) ≠ 可管理(dept_admin_grant) ≠ 可授权(本组端点=kb_admin)。
#   守卫：kb_admin 用户不经本 UI 改（防误降级/锁死）；不能改自己；owner_dept 经 sanitize fail-closed。
# ═══════════════════════════════════════════════════════════════
@app.get("/api/kb/admin-grants", response_model=KbAdminListResponse)
def kb_admin_grants_list(request: Request,
                         identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 查看现行管理员名单（dept_admin + kb_admin）及各自可管理的 owner_dept。只读。"""
    _enforce_rate_limit(request, identity, scope="aux")
    _require_kb_admin(identity)
    from opensearch_pipeline.kb_authz import _valid_owner_depts
    items: List[KbAdminItem] = []
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT user_id, user_name, dept_code, role FROM fuling_knowledge.user_role "
                            "WHERE is_active=1 AND role IS NOT NULL AND role<>'employee' ORDER BY role, user_id")
                roles = cur.fetchall()
                cur.execute("SELECT user_id, managed_owner_dept FROM fuling_knowledge.dept_admin_grant "
                            "WHERE is_active=1")
                grants: Dict[str, List[str]] = {}
                for r in cur.fetchall():
                    if r and r[0]:
                        grants.setdefault(r[0], []).append(r[1])
                for r in roles:
                    uid = r[0] or ""
                    items.append(KbAdminItem(
                        user_id=uid, user_name=r[1] or "", role=r[3] or "",
                        managed_owner_depts=sorted(grants.get(uid, []))))
        finally:
            conn.close()
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("kb_admin_grants_list 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"成员名单查询失败 (trace: {trace_id})")
    return KbAdminListResponse(items=items, grantable_owner_depts=sorted(_valid_owner_depts()))


@app.post("/api/kb/admin-grants", response_model=KbAdminGrantResponse)
def kb_admin_grant(req: KbAdminGrantRequest, request: Request,
                   identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 授予/更新一名【部门管理员】可管理的 owner_dept（owner_depts = 权威全集,提交即覆盖）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_admin(identity)
    from opensearch_pipeline.kb_authz import sanitize_owner_depts, ROLE_DEPT_ADMIN, ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    uid = (req.user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="缺少 user_id（钉钉 staffId）")
    if uid == kb.user_id:
        raise HTTPException(status_code=400, detail="不能修改自己的角色/授权")
    depts = sanitize_owner_depts(req.owner_depts)   # 净化 + 写白名单（fail-closed 丢非法）
    if not depts:
        raise HTTPException(status_code=400, detail="可管理部门为空或全不在白名单（无法授予）")
    assert_metadata_write_allowed("kb_admin_grant", get_config().rds.host, kind="rds")
    trace_id = uuid.uuid4().hex[:8]
    note = (req.note or "")[:255] or None
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 守卫：已是 kb_admin 的用户不经本 UI 改（避免误降级；kb_admin 调整走运维脚本）
                cur.execute("SELECT role FROM fuling_knowledge.user_role WHERE user_id=%s AND is_active=1 "
                            "ORDER BY updated_at DESC, id DESC LIMIT 1", (uid,))
                row = cur.fetchone()
                if row and (row[0] or "") == ROLE_KB_ADMIN:
                    raise HTTPException(status_code=400,
                                        detail="该用户已是知识库管理员（kb_admin），请用运维脚本调整以免误降级")
                # 角色 → dept_admin（dept_code 同步为可管理组 CSV，与 seed 口径一致）
                cur.execute("INSERT INTO fuling_knowledge.user_role (user_id, user_name, dept_code, role, is_active) "
                            "VALUES (%s,%s,%s,%s,1) ON DUPLICATE KEY UPDATE "
                            "user_name=COALESCE(VALUES(user_name), user_name), dept_code=VALUES(dept_code), "
                            "role=VALUES(role), is_active=1, updated_at=NOW()",
                            (uid, (req.user_name or None), ",".join(depts), ROLE_DEPT_ADMIN))
                # 权威全集语义：先软撤销本次【未包含】的旧授权,再 upsert 本次
                ph = ",".join(["%s"] * len(depts))
                cur.execute(f"UPDATE fuling_knowledge.dept_admin_grant SET is_active=0, updated_at=NOW() "
                            f"WHERE user_id=%s AND is_active=1 AND managed_owner_dept NOT IN ({ph})",
                            (uid, *depts))
                for owner in depts:
                    cur.execute("INSERT INTO fuling_knowledge.dept_admin_grant "
                                "(user_id, managed_owner_dept, granted_by, note, is_active) VALUES (%s,%s,%s,%s,1) "
                                "ON DUPLICATE KEY UPDATE is_active=1, granted_by=VALUES(granted_by), "
                                "note=VALUES(note), updated_at=NOW()",
                                (uid, owner, kb.user_id, note))
                # 同事务审计（B1）：与角色/授权变更原子提交。
                write_audit(doc_id=None, version_no=None, action_type="KB_ADMIN_GRANT",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"grant dept_admin {uid} → {','.join(depts)}", cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_admin_grant 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"授予部门管理员失败 (trace: {trace_id})")
    return KbAdminGrantResponse(user_id=uid, role=ROLE_DEPT_ADMIN, managed_owner_depts=depts, ok=True)


@app.post("/api/kb/admin-grants/revoke", response_model=KbAdminGrantResponse)
def kb_admin_grant_revoke(req: KbAdminRevokeRequest, request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 撤销部门管理员授权：owner_dept 指定→撤该一项；为空→撤全部并降级 employee。
    无活跃授权剩余时把 user_role.role 降为 employee（即时失去管理入口）。kb_admin/自身不可经此撤销。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_admin(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, ROLE_EMPLOYEE
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    uid = (req.user_id or "").strip()
    owner = (req.owner_dept or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="缺少 user_id")
    if uid == kb.user_id:
        raise HTTPException(status_code=400, detail="不能撤销自己的授权")
    assert_metadata_write_allowed("kb_admin_grant_revoke", get_config().rds.host, kind="rds")
    trace_id = uuid.uuid4().hex[:8]
    demoted = False
    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT role FROM fuling_knowledge.user_role WHERE user_id=%s AND is_active=1 "
                            "ORDER BY updated_at DESC, id DESC LIMIT 1", (uid,))
                row = cur.fetchone()
                if row and (row[0] or "") == ROLE_KB_ADMIN:
                    raise HTTPException(status_code=400, detail="不能经本 UI 撤销知识库管理员（kb_admin）")
                if owner:
                    cur.execute("UPDATE fuling_knowledge.dept_admin_grant SET is_active=0, updated_at=NOW() "
                                "WHERE user_id=%s AND managed_owner_dept=%s AND is_active=1", (uid, owner))
                else:
                    cur.execute("UPDATE fuling_knowledge.dept_admin_grant SET is_active=0, updated_at=NOW() "
                                "WHERE user_id=%s AND is_active=1", (uid,))
                cur.execute("SELECT COUNT(*) FROM fuling_knowledge.dept_admin_grant "
                            "WHERE user_id=%s AND is_active=1", (uid,))
                remaining = int(cur.fetchone()[0] or 0)
                if remaining == 0:
                    cur.execute("UPDATE fuling_knowledge.user_role SET role=%s, updated_at=NOW() "
                                "WHERE user_id=%s", (ROLE_EMPLOYEE, uid))
                    demoted = True
                # 同事务审计（B1）：与撤销/降级变更原子提交。
                write_audit(doc_id=None, version_no=None, action_type="KB_ADMIN_REVOKE",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"revoke {uid} owner={owner or 'ALL'} demoted={demoted}", cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_admin_grant_revoke 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"撤销部门管理员授权失败 (trace: {trace_id})")
    return KbAdminGrantResponse(user_id=uid, role=(ROLE_EMPLOYEE if demoted else "dept_admin"), ok=True)


# ═══════════════════════════════════════════════════════════════
# 控制台托管（P7 切换）：
#   · /console            = 新 Vite SPA（默认入口）；无尾斜杠 → 307 到 /console/（保留 query）
#   · /console/{path}     = SPA 静态 + 作用域 SPA 回退（构建 base 须为 /console/）
#   · /console-legacy     = 旧·自包含 H5 控制台（退居此处，保留 ≥1 发布周期，P8 退役）
#   · /console-next[/...] = 并行阶段路径 → 307 重定向到 /console/...（向后兼容，保留 query）
#   缓存（修正#9）：hash 资源 immutable，index.html / SPA 回退 no-cache。
#   作用域（修正#3）：回退仅作用于 /console 与 /console/*，不匹配 /api/* → 未知 API 仍 JSON 404。
#   小程序兼容：既有 web-view 链接 /console?token=&doc_id=... 零改动命中新 SPA（query 原样保留）。
# ═══════════════════════════════════════════════════════════════

_NEXT_DIST = Path(__file__).resolve().parent / "webconsole" / "next-dist"
_KB_CONSOLE_HTML_CACHE: Dict[str, Any] = {"html": None}


def _serve_console_spa(rel: str) -> Response:
    """从 next-dist 安全返回文件；越界/不存在 → index.html（SPA 回退，no-cache）。构建 base 须为 /console/。"""
    base = _NEXT_DIST
    index = base / "index.html"
    if rel:
        target = (base / rel).resolve()
        # 路径穿越守卫：解析后必须仍落在 next-dist 之内
        if (target == base or base in target.parents) and target.is_file():
            cache = "public, max-age=31536000, immutable" if rel.startswith("assets/") else "no-cache"
            return FileResponse(target, headers={"Cache-Control": cache})
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<h1>/console 尚未构建（在 console-app 下 CONSOLE_BASE=/console/ npm run build）</h1>", status_code=404)


def _redirect_to_console(path: str, request: Request) -> RedirectResponse:
    """重定向到 /console/<path>，原样保留 query（小程序 ?token=&doc_id= 深链不可丢）。"""
    target = f"/console/{path}" if path else "/console/"
    q = request.url.query
    if q:
        target += f"?{q}"
    return RedirectResponse(url=target, status_code=307)


@app.get("/console", include_in_schema=False)
def kb_console_root(request: Request):
    """无尾斜杠 → 307 到 /console/（与构建 base 对齐；保留 query，避免 vue-router base 归一化歧义）。"""
    return _redirect_to_console("", request)


@app.get("/console/{path:path}", include_in_schema=False)
def kb_console_spa(path: str):
    return _serve_console_spa(path)


@app.get("/console-legacy", response_class=HTMLResponse, include_in_schema=False)
def kb_console_legacy():
    """旧·自包含 H5 控制台单页：jsapi 免登 → /api/auth/dingtalk → /api/kb/*（同源调用）。P8 退役。"""
    if _KB_CONSOLE_HTML_CACHE["html"] is None:
        p = Path(__file__).resolve().parent / "webconsole" / "console.html"
        try:
            _KB_CONSOLE_HTML_CACHE["html"] = p.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("console.html 读取失败: %s", e)
            _KB_CONSOLE_HTML_CACHE["html"] = "<h1>知识库控制台页面缺失</h1>"
    return HTMLResponse(_KB_CONSOLE_HTML_CACHE["html"])


@app.get("/console-next", include_in_schema=False)
def kb_console_next_root(request: Request):
    return _redirect_to_console("", request)


@app.get("/console-next/{path:path}", include_in_schema=False)
def kb_console_next_redirect(path: str, request: Request):
    """并行阶段 /console-next/* → 统一 307 到 /console/*（保留子路径 + query）。"""
    return _redirect_to_console(path, request)


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
