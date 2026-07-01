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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
from opensearch_pipeline.request_context import RequestIdMiddleware, get_request_id
from opensearch_pipeline.session_store import (
    MAX_HISTORY_TURNS,
    append_to_history,
    clear_session,
    get_or_create_session,
)

logger = logging.getLogger(__name__)


def _kb_db() -> str:
    """知识库库名（document_meta/version/chunk_meta/kb_* 等所在库）；经 RAG_RDS_DATABASE 配置
    （STAGING=fuling_knowledge_stg）。SQL 一律用 {_kb_db()}./{_op_db()}. 前缀，使 RAG_ENV=staging
    指向 *_stg 库（环境隔离，P0-01）；_op_db 复用 qa_logger 同名 helper。镜像该 helper，惰性读 config。"""
    return get_config().rds.database


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

# 请求级 correlation id（统一 trace，OBS-trace）：纯 ASGI 中间件，入站读/生成 X-Request-Id 存入
# ContextVar（端点与嵌套 retriever/llm_generator 调用可见）、响应头回写。最后 add → 最外层 → 最先跑。
app.add_middleware(RequestIdMiddleware)

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


def _anon_uid(request: Optional[Request]) -> str:
    """匿名请求的日志归属身份：``anon:<客户端 IP 短哈希>``。

    绝不采信客户端自报的 req.user_id 作为落库 user_id —— /api/history 严格按令牌
    identity.user_id（真实钉钉 staffId）过滤本人记录；若匿名 /api/ask 能写入任意 user_id，
    攻击者即可枚举他人 staffId、把伪造问答注入受害者的私有历史与审计链。anon: 命名空间与
    真实 staffId 天然不相交（永不出现在任何人历史里），又保留按客户端聚合排查的能力。"""
    import hashlib
    ip = _client_ip(request)
    return "anon:" + hashlib.sha256(ip.encode("utf-8")).hexdigest()[:12]


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


@app.get("/api/version")
async def version_info():
    """部署版本指纹（canary 校验 / 回滚确认）。git_commit 是核心字段：灰度时 curl 该端点比对目标
    SHA 即可确认实例跑的是哪个 build；SAE 原生灰度路由 + 健康检查回滚负责流量切换。复用既有
    versions.git_commit()（RAG_GIT_SHA env 优先；Dockerfile 构建期烤入）。仅暴露短 SHA + 模型版本 +
    环境标签，低敏感，与 /api/health、/api/ready、/api/kb/config 同为公开探针。"""
    from opensearch_pipeline.versions import EMBEDDING_MODEL_VERSION, git_commit
    cfg = get_config()
    return {
        "git_commit": git_commit(),
        "embedding_model_version": EMBEDDING_MODEL_VERSION,
        "environment": getattr(cfg, "environment", "unknown"),
        "simulate": getattr(cfg, "simulate", False),
    }


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
        from opensearch_pipeline.db import _get_db_conn
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
                # 维度读 config（勿硬编码 1024）：HA3 重建为非默认维度时硬编码会让探针抛错→
                # /api/ready 误报 503→健康实例被摘出。与全代码库其它零向量探针一致。
                table_name=cfg.alibaba_vector.table_name,
                vector=[0.0] * cfg.embedding.dimension, top_k=1,
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
        trace_id = get_request_id()
        logger.error("Search failed [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败，请联系管理员 (trace: {trace_id})")

    latency = int((time.time() - t0) * 1000)
    return SearchResponse(
        results=[SearchResult(**r) for r in results],
        total=len(results),
        latency_ms=latency,
    )


def _prepare_ask(req: AskRequest, identity: Optional["Identity"], *,
                 request: Optional[Request] = None, cosurface_images: bool = False):
    """/api/ask 与 /api/ask/stream 共用的前置段：会话管理、客户端历史合并、
    身份/部门解析（仅信 Bearer 令牌）、检索 + 计时。

    检索失败统一抛 HTTPException(500)（流式端点也要求在返回 StreamingResponse 之前抛出）。
    retrieve_and_enrich 经本模块全局名调用，保持测试 monkeypatch(api.retrieve_and_enrich) 接缝。
    """
    # 会话归属校验：'miniapp:<staffId>' 是可预测命名空间（chat.js 用 'miniapp:'+userId 构造），
    # 必须校验令牌归属，防止匿名/他人读取或污染他人会话上下文（与 /api/session/clear 同策略）。
    # 其余 session_id（服务端 UUID / 钉钉会话 key，不可枚举）按持有即所有处理。
    if req.session_id and req.session_id.startswith("miniapp:"):
        if not identity or req.session_id != f"miniapp:{identity.user_id}":
            raise HTTPException(status_code=403, detail="无权访问该会话")

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

    # 落库归属：仅令牌身份可作 user_id 主键。匿名请求一律落 anon:<ip 短哈希>，绝不采信
    # 客户端自报的 req.user_id —— 否则攻击者可传他人 staffId 把伪造问答注入受害者 /api/history。
    uid = identity.user_id if identity else _anon_uid(request)
    # 权限部门仅来自已验证的 Bearer 令牌；无令牌一律按匿名处理（仅 public 文档）。
    user_dept = identity.acl_groups if identity else None

    # 2. 检索
    try:
        chunks = retrieve_and_enrich(
            req.question, top_k=req.top_k, user_dept=user_dept,
            cosurface_images=cosurface_images,
        )
    except Exception as e:
        trace_id = get_request_id()
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
     chunks, t_retrieval, retrieval_latency_ms) = _prepare_ask(req, identity, request=request)

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
        trace_id = get_request_id()
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
        req, identity, request=request, cosurface_images=not _pure)
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
                    # 思考过程帧只下发给【显式请求 thinking】的调用方：防 RAG_STREAM_REASONING 全局 flag
                    # 把思维链广播给任何 SSE 客户端（小程序走 /api/ask 不受影响；钉钉只收 chunk；但杜绝未知
                    # SSE 调用方拿到 CoT）。reasoning 只在 thinking 时产生，故此处按 req.thinking 收口即可。
                    if frame is not None and frame.get("type") == "reasoning" and not req.thinking:
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
                trace_id = get_request_id()
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


def _parse_asset_doc_id(key: str) -> Optional[str]:
    """从 processing/assets/{dept}/{doc_id}/v{n}/{file} 解析 doc_id；非该形态 → None。"""
    parts = key.split("/")
    if (len(parts) < 6 or parts[0] != "processing" or parts[1] != "assets"
            or not parts[4].startswith("v")):
        return None
    return parts[3] or None


def _resign_visible_doc_ids(doc_ids: set, identity: Optional[Identity]) -> set:
    """resign-images 可见性鉴权 —— 复用检索的同一权限边界（安全边界单一来源）。

    public 放行；dept_internal 仅当 owner_dept ∈ 调用者组展开的 owner 集，或（Phase D）文档
    allowed_depts 含调用者任一组码；restricted 永不放行；未知 doc_id 不放行。DB 异常 → fail-closed
    （全部拒签）：本接口产出可读图字节，绝不 fail-open。
    """
    if not doc_ids:
        return set()
    from opensearch_pipeline import retriever as _R
    groups = _R._normalize_acl_groups(identity.acl_groups if identity else None)
    owners = set(_R._expand_groups_to_owners(groups))
    phase_d = bool(get_config().rag.allowed_depts_acl)
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.warning("resign-images 鉴权 DB 连接失败（fail-closed 全拒）: %s", e)
        return set()
    visible: set = set()
    try:
        ids = sorted(doc_ids)
        ph = ",".join(["%s"] * len(ids))
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT doc_id, permission_level, owner_dept FROM {_kb_db()}.document_meta "
                f"WHERE doc_id IN ({ph})", tuple(ids))
            meta = {r[0]: ((r[1] or "").strip().lower(), (r[2] or "")) for r in cur.fetchall()}
            for d in ids:
                pl, owner = meta.get(d, (None, None))
                if pl is None:
                    continue                       # 未知文档 → 拒签
                if pl == "public":
                    visible.add(d); continue
                if pl == "restricted":
                    continue                       # 永不服务
                if owner in owners:                # dept_internal 等：owner 展开命中
                    visible.add(d); continue
                if phase_d and groups:             # Phase D 跨部门授权
                    from opensearch_pipeline import access_grants
                    if set(access_grants.resolve_allowed_depts_one(d, cur)) & set(groups):
                        visible.add(d)
    except Exception as e:
        logger.warning("resign-images 鉴权查询失败（fail-closed 全拒）: %s", e)
        return set()
    finally:
        conn.close()
    return visible


@app.post("/api/resign-images")
def resign_images(req: ResignImagesRequest, request: Request,
                  identity: Optional[Identity] = Depends(current_identity)):
    """过期图片重签：OSS 签名 URL 默认 1 小时过期，客户端凭 blocks 里的
    oss_key 换取新签名 URL（「图片已过期 · 点按重新加载」的真实后半段）。

    鉴权：除白名单（前缀/扩展名/无路径穿越）外，还按【文档可见性】逐 key 校验——与检索同一
    权限边界（public/owner 展开/Phase-D allowed_depts，restricted 永不）；无权或未知文档返回空串。
    否则任何人凭可枚举的 dept 段 + 带外获取的 doc_id 即可绕过 HA3 权限过滤读取他部门文档抽取图。
    单 key 失败/非法不影响其它 key（返回空串，客户端保留过期占位态）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    from opensearch_pipeline.oss_url import generate_signed_url

    urls: Dict[str, str] = {}
    key_doc: Dict[str, str] = {}
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
        doc_id = _parse_asset_doc_id(key)
        if not doc_id:
            logger.warning("resign-images 无法解析 doc_id，拒签: %r", key[:128])
            urls[key] = ""
            continue
        key_doc[key] = doc_id

    visible_docs = _resign_visible_doc_ids(set(key_doc.values()), identity)
    for key, doc_id in key_doc.items():
        if doc_id not in visible_docs:
            logger.warning("resign-images 拒绝越权 key（无文档可见权 doc=%s）: %r", doc_id, key[:128])
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
        from opensearch_pipeline.db import _get_db_conn
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
        trace_id = get_request_id()
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
        from opensearch_pipeline.db import _get_db_conn
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
        trace_id = get_request_id()
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
        from opensearch_pipeline.db import _get_db_conn
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
        trace_id = get_request_id()
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
        from opensearch_pipeline.db import _get_db_conn
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
        trace_id = get_request_id()
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
        from opensearch_pipeline.db import _get_db_conn
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
        from opensearch_pipeline.db import _get_db_conn
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
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT m.doc_id, m.title, m.owner_dept
                    FROM {_kb_db()}.document_meta m
                    JOIN {_kb_db()}.document_version v
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



# （F-A2 搬移遗留位）KbDupDoc 上移至驻留区：_kb_content_dups（驻留助手）返回它，
# 而消费方 kb_register 已搬去 routes/kb_console.py。
class KbDupDoc(BaseModel):
    doc_id: str
    title: str = ""
    owner_dept: str = ""




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


# ═══════════════════════════════════════════════════════════════
# 冷域路由（F-A2 结构债拆分，2026-07-01）
#   KB 控制台管理 / 跨部门授权 / 知识贡献 / 控制台静态托管 按 APIRouter
#   拆到 routes/ 包（机械搬移，行为不变）。热路径（ask/stream/session/
#   feedback/history/hot-questions）留在本文件——tests 对其协作者做
#   `api.<name>` monkeypatch，搬移会断 patch 目标（规则见 routes/__init__.py）。
#   ⚠️ 本块必须保持在文件底部：路由模块顶层 from-import 本模块的共享件，
#   依赖上方全部名字已定义。
# ═══════════════════════════════════════════════════════════════
from opensearch_pipeline.routes import console as _routes_console  # noqa: E402
from opensearch_pipeline.routes import contribution as _routes_contribution  # noqa: E402
from opensearch_pipeline.routes import kb_access as _routes_kb_access  # noqa: E402
from opensearch_pipeline.routes import kb_console as _routes_kb_console  # noqa: E402

# 注册顺序 = 原文件内出现顺序（路径无重叠，仅求 diff 稳定）。
app.include_router(_routes_kb_console.router)
app.include_router(_routes_kb_access.router)
app.include_router(_routes_contribution.router)
app.include_router(_routes_console.router)

# re-export：tests 直接调用 api.<endpoint>(...) / 引用 api.Kb* 模型与域内常量。
# —— routes/kb_console.py ——
KbWhoamiResponse = _routes_kb_console.KbWhoamiResponse
kb_whoami = _routes_kb_console.kb_whoami
kb_org_tree = _routes_kb_console.kb_org_tree
kb_my_docs = _routes_kb_console.kb_my_docs
kb_browse = _routes_kb_console.kb_browse
KbStatsResponse = _routes_kb_console.KbStatsResponse
kb_stats = _routes_kb_console.kb_stats
_KB_INSIGHTS_WINDOW_DAYS = _routes_kb_console._KB_INSIGHTS_WINDOW_DAYS
_KB_QA_OWNER_JOIN = _routes_kb_console._KB_QA_OWNER_JOIN
KbTopDocItem = _routes_kb_console.KbTopDocItem
KbGapQueryItem = _routes_kb_console.KbGapQueryItem
KbInsightsResponse = _routes_kb_console.KbInsightsResponse
kb_insights = _routes_kb_console.kb_insights
KbEmbedRunItem = _routes_kb_console.KbEmbedRunItem
KbDeptCoverageItem = _routes_kb_console.KbDeptCoverageItem
KbFeedbackDay = _routes_kb_console.KbFeedbackDay
KbDownvoteReason = _routes_kb_console.KbDownvoteReason
KbFileType = _routes_kb_console.KbFileType
KbGovernanceResponse = _routes_kb_console.KbGovernanceResponse
kb_governance = _routes_kb_console.kb_governance
KbConfigResponse = _routes_kb_console.KbConfigResponse
kb_config = _routes_kb_console.kb_config
kb_version_history = _routes_kb_console.kb_version_history
kb_doc_status = _routes_kb_console.kb_doc_status
KbUploadUrlRequest = _routes_kb_console.KbUploadUrlRequest
KbUploadUrlResponse = _routes_kb_console.KbUploadUrlResponse
KbRegisterRequest = _routes_kb_console.KbRegisterRequest
KbRegisterResponse = _routes_kb_console.KbRegisterResponse
KbApprovalRequest = _routes_kb_console.KbApprovalRequest
KbRetireRequest = _routes_kb_console.KbRetireRequest
KbRetireResponse = _routes_kb_console.KbRetireResponse
KbRestoreResponse = _routes_kb_console.KbRestoreResponse
kb_upload_url = _routes_kb_console.kb_upload_url
kb_register = _routes_kb_console.kb_register
kb_approve = _routes_kb_console.kb_approve
kb_reject = _routes_kb_console.kb_reject
kb_retire = _routes_kb_console.kb_retire
kb_restore = _routes_kb_console.kb_restore
KbPendingItem = _routes_kb_console.KbPendingItem
KbPendingResponse = _routes_kb_console.KbPendingResponse
kb_pending_approvals = _routes_kb_console.kb_pending_approvals
# —— routes/kb_access.py ——
KbAccessRequestSubmit = _routes_kb_access.KbAccessRequestSubmit
KbAccessDecisionRequest = _routes_kb_access.KbAccessDecisionRequest
KbAccessRequestSubmitResponse = _routes_kb_access.KbAccessRequestSubmitResponse
KbAccessDecisionResponse = _routes_kb_access.KbAccessDecisionResponse
KbAccessRequestItem = _routes_kb_access.KbAccessRequestItem
KbAccessRequestListResponse = _routes_kb_access.KbAccessRequestListResponse
KbAccessGrantItem = _routes_kb_access.KbAccessGrantItem
KbAccessGrantListResponse = _routes_kb_access.KbAccessGrantListResponse
KbAdminItem = _routes_kb_access.KbAdminItem
KbAdminListResponse = _routes_kb_access.KbAdminListResponse
KbAdminGrantRequest = _routes_kb_access.KbAdminGrantRequest
KbAdminRevokeRequest = _routes_kb_access.KbAdminRevokeRequest
KbAdminGrantResponse = _routes_kb_access.KbAdminGrantResponse
MyAccessRequestItem = _routes_kb_access.MyAccessRequestItem
MyAccessRequestListResponse = _routes_kb_access.MyAccessRequestListResponse
kb_access_request_submit = _routes_kb_access.kb_access_request_submit
kb_access_requests_list = _routes_kb_access.kb_access_requests_list
kb_access_grants_list = _routes_kb_access.kb_access_grants_list
_APPROVAL_HISTORY_LIMIT = _routes_kb_access._APPROVAL_HISTORY_LIMIT
_TZ_PACIFIC_TO_BJ = _routes_kb_access._TZ_PACIFIC_TO_BJ
_parse_admin_target = _routes_kb_access._parse_admin_target
KbApprovalHistoryItem = _routes_kb_access.KbApprovalHistoryItem
KbApprovalHistoryResponse = _routes_kb_access.KbApprovalHistoryResponse
kb_approval_history = _routes_kb_access.kb_approval_history
kb_my_access_requests = _routes_kb_access.kb_my_access_requests
_kb_access_decide = _routes_kb_access._kb_access_decide
kb_access_request_approve = _routes_kb_access.kb_access_request_approve
kb_access_request_reject = _routes_kb_access.kb_access_request_reject
kb_access_request_revoke = _routes_kb_access.kb_access_request_revoke
kb_admin_grants_list = _routes_kb_access.kb_admin_grants_list
kb_admin_grant = _routes_kb_access.kb_admin_grant
kb_admin_grant_revoke = _routes_kb_access.kb_admin_grant_revoke
# —— routes/contribution.py ——
_CONTRIB_COLS = _routes_contribution._CONTRIB_COLS
_CONTRIB_WINDOW_DAYS = _routes_contribution._CONTRIB_WINDOW_DAYS
_CONTRIB_CANDIDATE_CAP = _routes_contribution._CONTRIB_CANDIDATE_CAP
KbGapItem = _routes_contribution.KbGapItem
KbGapsSummary = _routes_contribution.KbGapsSummary
KbGapsResponse = _routes_contribution.KbGapsResponse
KbContributionItem = _routes_contribution.KbContributionItem
KbContributionListResponse = _routes_contribution.KbContributionListResponse
KbContributionSubmitRequest = _routes_contribution.KbContributionSubmitRequest
KbContributionAcceptRequest = _routes_contribution.KbContributionAcceptRequest
KbContributionRejectRequest = _routes_contribution.KbContributionRejectRequest
KbContributionActionResponse = _routes_contribution.KbContributionActionResponse
KbHeroItem = _routes_contribution.KbHeroItem
KbHeroesResponse = _routes_contribution.KbHeroesResponse
_contrib_item = _routes_contribution._contrib_item
_reconcile_contributions_searchable = _routes_contribution._reconcile_contributions_searchable
_materialize_contribution = _routes_contribution._materialize_contribution
_finish_contribution_ingestion = _routes_contribution._finish_contribution_ingestion
kb_contribution_submit = _routes_contribution.kb_contribution_submit
kb_contributions_mine = _routes_contribution.kb_contributions_mine
kb_contributions_pending = _routes_contribution.kb_contributions_pending
kb_contribution_accept = _routes_contribution.kb_contribution_accept
kb_contribution_reject = _routes_contribution.kb_contribution_reject
kb_contribution_retry = _routes_contribution.kb_contribution_retry
kb_contribution_heroes = _routes_contribution.kb_contribution_heroes
kb_gaps = _routes_contribution.kb_gaps
# —— routes/console.py ——
_NEXT_DIST = _routes_console._NEXT_DIST
_KB_CONSOLE_HTML_CACHE = _routes_console._KB_CONSOLE_HTML_CACHE
_serve_console_spa = _routes_console._serve_console_spa
_redirect_to_console = _routes_console._redirect_to_console
kb_console_root = _routes_console.kb_console_root
kb_console_spa = _routes_console.kb_console_spa
kb_console_legacy = _routes_console.kb_console_legacy
kb_console_next_root = _routes_console.kb_console_next_root
kb_console_next_redirect = _routes_console.kb_console_next_redirect
