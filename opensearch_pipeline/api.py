# -*- coding: utf-8 -*-
"""
api.py — RAG 问答 FastAPI 应用

端点：
  POST /api/ask           非流式问答
  POST /api/ask/stream    SSE 流式问答
  POST /api/search        纯检索（不调用 LLM）
  GET  /api/health        健康检查
"""

import itertools
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Header, Depends, Request
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
# ⚠️ 限流器必须按模块属性访问（rate_limiter.LIMITER），不能 from-import 按值绑定——
# 否则 _set_limiter_for_test 替换全局后本模块仍持旧引用，测试接缝假绿（深度审查 F 组）。
from opensearch_pipeline import rate_limiter as _rate_limiter
from opensearch_pipeline.rate_limiter import resolve_client_ip
from opensearch_pipeline.answer_flow import (
    ACTION_GENERAL_LLM,
    ACTION_PASSTHROUGH,
    ACTION_SENSITIVE_BLOCK,
    ACTION_SMALLTALK,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    GENERAL_QUOTA_MESSAGE,
    GENERAL_TIER_OFF_MESSAGE,
    NO_RESULT_MESSAGE,
    REALTIME_BLOCK_MESSAGE,
    SENSITIVE_BLOCK_MESSAGE,
    build_failure_action,
    build_qa_log_kwargs,
    history_answer_text,
    is_refusal_answer,
    should_append_history,
    suggest_titles,
)
from opensearch_pipeline.general_answerer import CANNED_SMALLTALK, answer_general
from opensearch_pipeline.intent_router import (
    ROUTE_KB,
    ROUTE_OFFICE,
    ROUTE_REALTIME,
    ROUTE_SENSITIVE,
    ROUTE_SMALLTALK,
    pre_route,
    triage_failed_query,
)
from opensearch_pipeline.config import get_config
from opensearch_pipeline.db import DBPoolExhausted
from opensearch_pipeline.http_hardening import HttpsRedirectMiddleware
from opensearch_pipeline.request_context import (
    RequestIdMiddleware,
    get_request_id,
    install_request_id_logging,
)
from opensearch_pipeline.session_store import (
    MAX_HISTORY_TURNS,
    SessionOwnershipError,
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

# P2-8（盲区审计）：摄取↔服务 embedding 模型契约失配标志。启动时经 runtime_contract 比对
# RDS 契约行（stage-3 每次真实嵌入 UPSERT）与本进程 get_config().embedding；失配 →
# logger.critical + /api/ready 报 degraded/503。刻意不 fail-closed 拒绝启动（本地联调/
# 未迁移环境零影响）—— 503 的摘流量语义由 readiness 探针承载。
_EMBEDDING_CONTRACT_MISMATCH = False


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """应用生命周期：启动时注册钉钉互动卡片 HTTP 回调地址（若已配置 DINGTALK_CARD_CALLBACK_URL），
    并按 DINGTALK_STREAM_MODE 启动钉钉 Stream 客户端（出站 WSS 接收机器人消息+卡片回调）。

    把本服务的 /dingtalk/card/callback 注册到 callbackRouteKey，使反馈按钮点击可回调。
    Stream 模式开启后 HTTP 注册仍保留：存量卡片（创建时 callbackType=HTTP）的按钮
    点击仍走 HTTP 路由，双模并存直到旧卡片自然老化。
    均非致命：失败只记日志、不阻断启动。多 worker 下每个进程各注册一次（幂等）。
    """
    # 性能第一梯队 #4：调大 AnyIO 线程令牌。Starlette 把 sync def 端点丢进
    # anyio 默认线程池（40 token 全局硬上限）——~40 个用户同窗口提问即打满，
    # 之后 feedback/webhook/鉴权全排队。这些线程 99% 在等网络（DashScope/HA3/RDS），
    # 代价仅每线程 ~8MB 栈上限的虚存。必须在事件循环内设置，故放 lifespan。
    try:
        import anyio.to_thread
        _tokens = int(os.environ.get("RAG_THREADPOOL_TOKENS", "120"))
        if _tokens > 0:
            anyio.to_thread.current_default_thread_limiter().total_tokens = _tokens
            logger.info("AnyIO 线程令牌: %d (RAG_THREADPOOL_TOKENS)", _tokens)
    except Exception:
        logger.warning("设置 AnyIO 线程令牌失败（忽略，用默认 40）", exc_info=True)
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
        logger.info("Serving 限流配置：%s", _rate_limiter.LIMITER.describe())
    except Exception:
        logger.warning("读取限流配置失败（忽略）", exc_info=True)
    # P2-8：比对摄取侧写入的 embedding 契约行（模型名/维度，schema/018）。失配 = 查询向量
    # 与库内向量不同模型/维度 —— 同维升级时相似度是垃圾但不报错，必须喊响并让 readiness
    # 摘流量。simulate / 表未 apply / 读失败：check 返回 None，零影响。
    global _EMBEDDING_CONTRACT_MISMATCH
    try:
        from opensearch_pipeline.runtime_contract import check_embedding_contract
        _mm = check_embedding_contract()
        if _mm:
            _EMBEDDING_CONTRACT_MISMATCH = True
            logger.critical(
                "embedding 摄取↔服务契约失配（%s）—— 查询向量与索引向量不可比，"
                "/api/ready 已降级 503。请对齐 RAG_EMBEDDING_MODEL/RAG_EMBEDDING_DIMENSION "
                "或重建索引后重启。", _mm,
            )
    except Exception:
        logger.warning("embedding 契约比对失败（忽略，不影响启动）", exc_info=True)
    yield


def _docs_urls(environment: str, enable_override: bool) -> dict:
    """生产/staging 默认关闭 OpenAPI 文档面（/docs /redoc /openapi.json → 404），
    避免公网匿名枚举 API 面（HTTPS 域名接入收口，2026-07-14）。
    逃生门：RAG_API_DOCS_ENABLE=true 显式恢复；开发/SIM 不受影响。"""
    if (environment or "").lower() in ("production", "staging") and not enable_override:
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {}


app = FastAPI(
    title="RAG 知识库问答 API",
    description="基于 OpenSearch HA3 向量检索 + Qwen LLM 的 RAG 问答服务",
    version="0.1.0",
    lifespan=_lifespan,
    **_docs_urls(
        get_config().environment,
        os.environ.get("RAG_API_DOCS_ENABLE", "").strip().lower() in ("1", "true", "yes"),
    ),
)

# 生产公网域名（HTTPS 在 CLB 终结）：production 未显式配置时,CORS 与强制 HTTPS
# 的域名默认均取它——部署零新增 env;换域名时改这里或用 env 覆盖。
_PROD_PUBLIC_HOST = "rag.fulingplastics.com.cn"

# CORS — 通过环境变量 CORS_ALLOWED_ORIGINS 配置允许的来源（逗号分隔）
# 未配置时：production 默认锁到公司域名（console 与 API 同源,不受影响;显式设
# env 可覆盖）；开发默认允许所有来源但禁用 credentials（防 Origin 反射）。
_cors_origins_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
if not _cors_origins_raw and (get_config().environment or "").lower() == "production":
    _cors_origins_raw = f"https://{_PROD_PUBLIC_HOST}"
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

def _resolve_force_https_hosts(env_value, environment) -> list:
    """RAG_FORCE_HTTPS_HOSTS 显式值优先（空串/"off" = 显式关闭）；**未设置时 production
    默认收口公司域名**（部署零 env）。scheme 判定只认 CLB 注入的 X-Forwarded-Proto
    （缺失一律放行——直连 IP 的小程序/钉钉回调不受影响），边界见 http_hardening.py。"""
    if env_value is None:
        env_value = _PROD_PUBLIC_HOST if (environment or "").lower() == "production" else ""
    return [h.strip() for h in env_value.split(",") if h.strip() and h.strip().lower() != "off"]


_force_https_hosts = _resolve_force_https_hosts(
    os.environ.get("RAG_FORCE_HTTPS_HOSTS"), get_config().environment
)
if _force_https_hosts:
    app.add_middleware(HttpsRedirectMiddleware, hosts=_force_https_hosts)

# 请求级 correlation id（统一 trace，OBS-trace）：纯 ASGI 中间件，入站读/生成 X-Request-Id 存入
# ContextVar（端点与嵌套 retriever/llm_generator 调用可见）、响应头回写。最后 add → 最外层 → 最先跑。
app.add_middleware(RequestIdMiddleware)
# P3-9：把 RequestIdLogFilter 真正挂上 root handlers（此前定义未安装，全部日志 rid='-'）。
# bot（HTTP/Stream）与 serving 同进程，这里一次安装即全覆盖。
install_request_id_logging()

# 钉钉机器人路由
app.include_router(dingtalk_router)


@app.exception_handler(DBPoolExhausted)
async def _db_pool_exhausted_handler(request: Request, exc: DBPoolExhausted):
    """P1-07：连接池获取超时 → 503 DB_POOL_EXHAUSTED（而非无限挂起 / 裸 500）。

    读路径的绝大多数 DB 用途（邻居缝合 / qa 落库 / 读时 acl 复核）本就 fail-open，会各自
    吞掉本异常降级作答；本处理器只兜住那些把 DB 当关键依赖、未 fail-open 的端点（如 /api/history、
    kb 控制台），给出明确的可重试信号而非模糊的高尾延迟。"""
    from fastapi.responses import JSONResponse
    logger.warning("DB 连接池获取超时 [trace=%s]: %s", get_request_id(), exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "服务繁忙，请稍后重试", "code": "DB_POOL_EXHAUSTED",
                 "trace_id": get_request_id()},
        headers={"Retry-After": "2"},
    )




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
    # max_length 对齐 qa_session_log.session_id VARCHAR(128)：超长 id 在 MySQL strict 下
    # 让审计 INSERT 1406、被 qa_logger 兜底吞掉——任何调用方可自选逃审计（2026-07-17 P1）
    session_id: Optional[str] = Field(None, max_length=128, description="会话 ID，用于追踪对话")
    conversation_id: Optional[str] = Field(None, max_length=128, description="客户端会话 ID（控制台会话历史归属；仅 RAG_CONVERSATION_HISTORY 开启时落库）")
    temperature: Optional[float] = Field(DEFAULT_TEMPERATURE, ge=0.0, le=2.0, description="生成温度")
    max_tokens: Optional[int] = Field(DEFAULT_MAX_TOKENS, ge=100, le=8192, description="最大生成 token 数")
    user_id: Optional[str] = Field(None, max_length=128, description="用户 ID（钉钉 staffId）；仅用于日志归因，权限部门只从 Bearer 令牌解析")
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
    user_id: Optional[str] = Field(None, max_length=128, description="用户 ID（钉钉 staffId）；仅用于日志归因，权限部门只从 Bearer 令牌解析")
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
    # 回答来源（通用能力分级开放）："kb" 知识库（默认，存量客户端无感）｜"smalltalk" 寒暄
    # 模板｜"general" 通用助手（含确定性计算）｜"guard" 引导/拦截话术。
    source: str = "kb"


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
    # 与 /api/kb/whoami 同源（kb_authz.managed_owner_depts）。缺席即前端上传「归属部门」下拉为空：
    # 钉钉容器免登路径 setIdentity 直接用本响应、不再补拉 whoami，字段必须在此路径也齐全。
    managed_owner_depts: List[str] = Field(
        default_factory=list,
        description="可上传/升版/退役的 owner_dept（kb_admin=全量写白名单；仅 UI 提示，特权接口仍服务端再裁决）")
    # 两条免登路径(本响应 + /api/kb/whoami)字段必须同源齐全——81a25d1 免登下拉恒空的教训。
    upload_target_depts: List[str] = Field(
        default_factory=list,
        description="上传「归属部门」下拉选项(= managed + 生产子线细化,2026-07-17;仅 UI 提示,服务端 authorize_upload 再裁决)")


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
        strict = os.environ.get("RAG_ACL_FAIL_CLOSED", "").strip().lower() in (
            "1", "true", "yes", "on")
        if strict:
            live, db_ok = _resolve_user_dept_cached(uid, with_status=True)
            if live is not None:
                groups = live
            elif not db_ok:
                # P0-04 fail-closed：DB **失败**时不信任令牌内嵌组，降级到仅 public
                # （无在册行 db_ok=True → 保留令牌组，短 TTL 兜底，不因未缓存降级）。
                groups = []
            else:
                # P1-08（外审核查 2026-07-16）墓碑：查询成功但无活跃行——区分两态：
                # 有行且全部 is_active=0 = 显式停用 → 令牌立即失效（此前只能等 2h TTL
                # 自然过期，停用用户拿旧令牌可继续用令牌内嵌组）；真无行维持文档化的
                # 保留令牌组语义（未缓存 ≠ 停用）。用既有 is_active 列，零 schema 变更。
                from opensearch_pipeline.dingtalk_identity import user_row_revoked
                if user_row_revoked(uid):
                    logger.warning("已停用用户 %s 持有效令牌访问——拒绝（tombstone）", uid)
                    return None
        else:
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


def _require_auth_enabled() -> bool:
    """P0-03（报告1）强制认证总开关。默认 **off** = 现网行为不变（匿名按 public 处理）。
    开启后知识读取端点对无/无效令牌返回 401——**开 flag 前必须确认所有内部调用方
    （钉钉 bot / 小程序 / 控制台 / 评测）都带 Bearer**，否则当场 401。建议先 staging 灰度。"""
    return os.environ.get("RAG_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes", "on")


def require_identity(identity: Optional[Identity] = Depends(current_identity)) -> Optional[Identity]:
    """知识读取端点的认证守卫。RAG_REQUIRE_AUTH 开 → 无有效身份返回 401；
    关（默认）→ 透传 identity（含 None，匿名按 public，历史行为）。

    公司内知识库的 `public` 语义是"全员可见"，不是"全互联网可调用"——本守卫把
    产品语义对齐到代码。健康探针/版本/钉钉免登入口不挂本守卫（另有公开签名）。"""
    if _require_auth_enabled() and (identity is None or not identity.user_id):
        raise HTTPException(status_code=401, detail="需要登录（Bearer 令牌）")
    return identity


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
    限流器异常时的降级姿态【按 scope 分叉】（P1-03）：辅助端点（无成本）fail-open 放行——防护组件绝不
    拖垮 UI 辅助链路；问答/检索类（成本路径：embedding+HA3±LLM）fail-CLOSED——限流器是最后一道成本
    防线，异常时放行 = 公网匿名扫描无上限烧百炼预算。异常本应极罕见（纯进程内 dict/锁操作）。
    /api/health 与 /dingtalk/*（验签 + Stream 出站）不经过本函数。
    """
    denial = None
    try:
        if identity and identity.user_id:
            actor, is_user = f"u:{identity.user_id}", True
        else:
            actor, is_user = f"ip:{_client_ip(request)}", False
        if scope == "ask":
            denial = _rate_limiter.LIMITER.admit_ask(actor, is_user=is_user,
                                       thinking=thinking, count_llm=count_llm)
        else:
            # 登录用户走 aux 宽档（RAG_RATE_AUX_USER_PER_MIN）：管理台单次挂载 ~13 个
            # /api/kb/* 并发加载，与匿名同档时 kb_admin 切几次 tab 即全面 429（雪崩修复）。
            denial = _rate_limiter.LIMITER.admit_aux(actor, is_user=is_user)
    except Exception:
        if scope == "ask":
            logger.error("限流器内部异常（成本路径 fail-CLOSED 拒绝）", exc_info=True)
            raise HTTPException(status_code=503, detail="服务暂时繁忙，请稍后再试",
                                headers={"Retry-After": "5"})
        logger.warning("限流器内部异常（辅助端点 fail-open 放行）", exc_info=True)
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
    from opensearch_pipeline.versions import embedding_regime_version, git_commit
    # P2-05：仅回灰度/回滚必需的 git_commit + 模型版本。此前额外暴露 environment / simulate
    # 给任何匿名调用方——泄露部署形态（是否 sim、prod/staging），无运维价值、增侦察面，故移除。
    # P3-7：模型版本改报运行时解析的制度指纹（model@dimension），不再报手工常量。
    return {
        "git_commit": git_commit(),
        "embedding_model_version": embedding_regime_version(),
    }


@app.get("/api/ready")
def readiness_check():
    """OBS-1 deep readiness probe: RDS + HA3 (critical) + DashScope (config-only, no live cost).

    200 when the critical deps (RDS + HA3) are reachable, else 503 with the failing component — so a
    load balancer / SAE / k8s readiness probe can stop routing to an instance that can't actually
    answer (the old /api/health never probed anything). Simulate mode returns 200/skipped (no live
    calls). DashScope is reported by config presence only (a live ping would cost quota).

    用 def（非 async）声明：探针全是同步阻塞 I/O（_get_db_conn 池签出带重试等待、pymysql
    SELECT 1、HA3 client.query）。async 下这些阻塞会冻住唯一事件循环（workers=1）——且恰在
    RDS/HA3 降级、探针最需要工作时冻得最久（connect/read 超时全程）。def 走线程池，与
    /api/ask 同一理由。
    """
    from fastapi.responses import JSONResponse
    cfg = get_config()
    if getattr(cfg, "simulate", False):
        return {"status": "ok", "mode": "simulate", "rds": "skipped", "ha3": "skipped", "dashscope": "skipped"}

    # P2-05：对外只报组件 up/down（ok/error/skipped），绝不回灌异常原文（DB host / 驱动错误 /
    # 索引错误经 str(e) 泄露内部拓扑）。完整异常写内部日志，响应带 trace_id 供运维对账。
    trace_id = get_request_id()
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
        logger.warning("readiness: RDS 探针失败 [trace=%s]: %s", trace_id, e)
        checks["rds"] = "error"

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
        logger.warning("readiness: HA3 探针失败 [trace=%s]: %s", trace_id, e)
        checks["ha3"] = "error"

    # P0-6（重评审计）④ DashScope：默认 config-only（零外呼零配额，历史行为）；
    # RAG_READY_DASHSCOPE_LIVE=true → TTL 缓存的 models 列表真探针（密钥有效+服务可达+
    # 配置模型在列）。live 结果只报告不摘流量（外部全局故障时摘光实例只会雪上加霜）。
    from opensearch_pipeline import readiness as _readiness
    checks["dashscope"] = _readiness.dashscope_status(
        getattr(cfg.embedding, "api_key", None), getattr(cfg.llm, "model", None))

    # P0-6 ①②③：agent/ontology 表族存在（对应 flag 开时判定，off→skipped）、
    # 工具注册表可构建、schema_migrations checksum 漂移（台账 vs 本地 schema/ 文件）。
    checks["agent_tables"] = _readiness.agent_tables_status()
    checks["ontology_tables"] = _readiness.ontology_tables_status()
    checks["tool_registry"] = _readiness.tool_registry_status()
    checks["schema_migrations"] = _readiness.schema_migrations_status()
    # 批次5（unknown-unknowns P0-06）：关键列契约（表在≠契约在，critical）、
    # kill-switch 读路径（HIGH_WRITE 开时 critical）、038 回填缺口（report-only）
    checks["schema_contract"] = _readiness.schema_contract_status()
    checks["kill_switch"] = _readiness.kill_switch_status()
    checks["ontology_backfill"] = _readiness.ontology_backfill_status()
    # P1-14（外审核查 2026-07-16）：LEGACY-OPEN 逃生口自报（report-only）——设了 ack 的
    # 实例在就绪面持续可见，姿态缺口不被遗忘（启动断言另在 config，当日 ack 才放行）
    checks["security_posture"] = _readiness.legacy_open_posture_status()

    # WS0 状态外置：任一状态后端切了 redis → Redis PING 纳入就绪判定（此前 redis_client.ping
    # 是死代码，深度审查多实例运维组）。判据：限流 redis 后端是 fail-closed（Redis 挂 → ask
    # 全拒），会话/去重/token 虽 fail-open 也已降级——该实例都不该继续接流量。
    # 全 memory 后端 → skipped（单实例语义，Redis 与就绪无关）。
    _redis_backends = ("RAG_SESSION_BACKEND", "RAG_RATE_LIMIT_BACKEND",
                       "RAG_MSG_DEDUP_BACKEND", "RAG_TOKEN_CACHE_BACKEND")
    if any(os.environ.get(k, "memory").strip().lower() == "redis" for k in _redis_backends):
        try:
            from opensearch_pipeline import redis_client
            checks["redis"] = "ok" if redis_client.ping() else "error"
        except Exception as e:  # noqa: BLE001 — 探针必须报告而非抛出
            logger.warning("readiness: Redis 探针失败 [trace=%s]: %s", trace_id, e)
            checks["redis"] = "error"
    else:
        checks["redis"] = "skipped"

    # B2（复核批次5）ready 与 Redis 解耦：Redis 故障默认**不**摘实例——一次 30s 的
    # failover 会让所有副本同时 ready=503、LB 把整个集群摘空，比「ask 路径 fail-closed
    # 503（带 Retry-After，成本护栏保留）」严重得多。默认宽松（报告 error + 每次探针
    # CRITICAL 响亮告警，alerting 侧按此关键字接告警）；RAG_READY_REDIS_STRICT=true
    # 恢复旧语义（Redis error 即摘流量）。
    _redis_ready_ok = checks.get("redis") in ("ok", "skipped")
    if not _redis_ready_ok:
        if os.environ.get("RAG_READY_REDIS_STRICT", "").strip().lower() in ("1", "true", "yes", "on"):
            logger.critical("readiness: Redis error 且 RAG_READY_REDIS_STRICT=true → 摘除本实例 "
                            "[trace=%s]", trace_id)
        else:
            _redis_ready_ok = True
            logger.critical("readiness: Redis error（非关键放行，RAG_READY_REDIS_STRICT 未开）——"
                            "限流 ask 路径将 fail-closed 503（Retry-After 5s），会话/去重降级 "
                            "[trace=%s]", trace_id)

    # P2-8：启动时检出的摄取↔服务 embedding 契约失配 → 关键降级（此实例算出的相似度
    # 不可信，必须被负载均衡摘出）。失配详情已在启动日志 CRITICAL，此处只报状态词。
    checks["embedding_contract"] = "mismatch" if _EMBEDDING_CONTRACT_MISMATCH else "ok"

    critical_ok = (checks.get("rds") == "ok" and checks.get("ha3") in ("ok", "skipped")
                   and _redis_ready_ok
                   and not _EMBEDDING_CONTRACT_MISMATCH
                   # P0-6：flag-on 实例缺 agent/ontology 表或注册表构建即炸 → 对应功能
                   # 首个请求必 500，该实例不该接流量（flag off 时恒 skipped 不参与判定）
                   and checks.get("agent_tables") in ("ok", "skipped")
                   and checks.get("ontology_tables") in ("ok", "skipped")
                   and checks.get("tool_registry") not in ("error", "empty")
                   # 批次5 P0-06：关键列缺失=功能首次触库必 500（与缺表同级）；
                   # kill-switch 读退化仅在 HIGH_WRITE 工具启用时摘流量
                   and checks.get("schema_contract") in ("ok", "skipped")
                   and (checks.get("kill_switch") in ("ok", "skipped")
                        or not _readiness.kill_switch_critical())
                   # schema 台账健康默认只报告（台账抖动不该全量摘流量）；
                   # RAG_READY_SCHEMA_STRICT=true 后要求 **ok**——drift/unapplied/
                   # unavailable/no_local_files 一律不就绪（批次5 P0-06d 收紧）
                   and (not _readiness.schema_strict()
                        or checks.get("schema_migrations") == "ok"))
    body = {"status": "ok" if critical_ok else "degraded", "trace_id": trace_id, **checks}
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
    from opensearch_pipeline.kb_authz import can_access_console, managed_owner_depts, upload_target_depts
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
        managed_owner_depts=managed_owner_depts(kb_ident),
        upload_target_depts=upload_target_depts(kb_ident),
    )


@app.post("/api/search", response_model=SearchResponse)
def search(req: SearchRequest, request: Request,
           identity: Optional[Identity] = Depends(require_identity)):
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


def _prepare_session(req: AskRequest, identity: Optional["Identity"], *,
                     request: Optional[Request] = None):
    """_prepare_ask 的检索前段：会话管理、客户端历史合并、身份/部门解析（仅信 Bearer
    令牌）。单独成函数供通用能力前置路由复用（命中 T0/T1/T2 时跳过检索但会话语义不变）。"""
    # 会话归属校验：'miniapp:<staffId>' 是可预测命名空间（chat.js 用 'miniapp:'+userId 构造），
    # 必须校验令牌归属，防止匿名/他人读取或污染他人会话上下文（与 /api/session/clear 同策略）。
    # 前缀检查保留：它挡的是"条目尚不存在时抢注他人 miniapp 命名空间"，owner 绑定挡不了。
    if req.session_id and req.session_id.startswith("miniapp:"):
        if not identity or req.session_id != f"miniapp:{identity.user_id}":
            raise HTTPException(status_code=403, detail="无权访问该会话")

    t0 = time.time()

    # 1. 会话管理。条目绑定已验证身份（盲区审计 P3-6）：钉钉会话 key 是结构化可构造的
    #    `conversationId:staffId`，没有归属绑定时任何已认证调用者可伪造 key 读走他人
    #    多轮上下文；服务端 UUID（匿名创建，owner=None）仍按持有即所有。
    try:
        session_id, session_history = _get_or_create_session(
            req.session_id, owner=identity.user_id if identity else None)
    except SessionOwnershipError:
        raise HTTPException(status_code=403, detail="无权访问该会话")

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
    return t0, session_id, merged_history, uid, user_dept


def _prepare_ask(req: AskRequest, identity: Optional["Identity"], *,
                 request: Optional[Request] = None, cosurface_images: bool = False):
    """/api/ask 与 /api/ask/stream 共用的前置段：会话管理、客户端历史合并、
    身份/部门解析（仅信 Bearer 令牌）、检索 + 计时。

    检索失败统一抛 HTTPException(500)（流式端点也要求在返回 StreamingResponse 之前抛出）。
    retrieve_and_enrich 经本模块全局名调用，保持测试 monkeypatch(api.retrieve_and_enrich) 接缝。
    """
    (t0, session_id, merged_history, uid, user_dept) = _prepare_session(
        req, identity, request=request)

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


# ═══════════════════════════════════════════════════════════════
# 通用能力分级开放（intent_router / general_answerer / answer_flow 的链路适配层，
# 设计见 docs/general_ability_opening_design.md）。RAG_GENERAL_ABILITY_MODE 与
# RAG_GUIDED_REFUSAL 双关时以下任何函数都不进入决策分支 —— 行为与现状一致。
# ═══════════════════════════════════════════════════════════════

def _general_flags():
    """(mode, guided_on, feature_on) 快照。mode 非法值视同 off。"""
    cfg = get_config().rag
    mode = (cfg.general_ability_mode or "off").lower()
    if mode not in ("smalltalk", "office", "full"):
        mode = "off"
    guided_on = bool(cfg.guided_refusal)
    return mode, guided_on, (mode != "off" or guided_on)


def _execute_general_llm(question: str, *, history, tier: str,
                         identity: Optional[Identity]) -> Optional[Dict[str, Any]]:
    """T2/T3 通用 LLM 回答执行（含日配额准入）。返回 outcome dict；
    LLM 异常 → None（调用方回落今日行为 —— fail-safe）。

    outcome 契约（本文件三处消费：/api/ask、/api/ask/stream、失败序列）：
      answer/model/usage · answer_status（None=调用方维持原状态）· intent_type ·
      risk_level/risk_blocked · no_result（响应 flag）· source（AskResponse.source）
    """
    actor = f"u:{identity.user_id}" if identity else "ip:anon"
    # 本文件铁律（见头部注释）：限流器按模块属性访问，不 from-import 按值绑定
    denial = _rate_limiter.LIMITER.admit_general(actor, is_user=bool(identity))
    if denial is not None:
        text = GENERAL_QUOTA_MESSAGE if denial.reason == "general_quota" \
            else GENERAL_TIER_OFF_MESSAGE
        return {"answer": text, "model": "N/A", "usage": {},
                "answer_status": None, "intent_type": "refuse_quota",
                "risk_level": None, "risk_blocked": False,
                "no_result": True, "source": "guard"}
    try:
        result = answer_general(question, history=history, tier=tier)
    except Exception as e:
        logger.warning("通用回答生成失败（回落知识库拒答行为）: %s", e)
        return None
    intent = "general" if tier == "general" else "office"
    return {"answer": result["answer"], "model": result["model"],
            "usage": result.get("usage") or {},
            "answer_status": "SUCCESS", "intent_type": intent,
            "risk_level": None, "risk_blocked": False,
            "no_result": False, "source": "general"}


def _general_pre_route_decision(req: AskRequest,
                                identity: Optional[Identity]) -> Optional[Dict[str, Any]]:
    """前置确定性路由（检索之前，KB 主路径零延迟）。None = 照常走知识库。

    命中路由的执行：T0① 敏感 → BLOCKED canned；T0③ 实时信息 → 边界说明；
    T1 → canned 模板（零 LLM）；T2 → 确定性计算 / quick 档通用回答（经日配额）。
    T2 执行失败回落知识库（返回 None）。检索从未发生 —— 落库 chunks=None。
    """
    mode, _guided_on, _on = _general_flags()
    if mode == "off":
        return None
    user_dept = identity.acl_groups if identity else None
    decision = pre_route(req.question, is_user=bool(identity), user_dept=user_dept)
    if decision.route == ROUTE_KB:
        return None
    if decision.route == ROUTE_SENSITIVE:
        return {"answer": SENSITIVE_BLOCK_MESSAGE, "model": "N/A", "usage": {},
                "answer_status": "BLOCKED", "intent_type": "refuse_sensitive",
                "risk_level": "sensitive", "risk_blocked": True,
                "no_result": False, "source": "guard"}
    if decision.route == ROUTE_REALTIME:
        # SUCCESS + intent 标注（非 NO_RESULT/REFUSAL：实时信息不是知识库缺口，
        # 不能污染 contribution 缺口挖掘与 SLO 口径；下同 refuse_quota）
        return {"answer": REALTIME_BLOCK_MESSAGE, "model": "N/A", "usage": {},
                "answer_status": "SUCCESS", "intent_type": "refuse_realtime",
                "risk_level": None, "risk_blocked": False,
                "no_result": False, "source": "guard"}
    if decision.route == ROUTE_SMALLTALK:
        text = CANNED_SMALLTALK.get(decision.sub) or CANNED_SMALLTALK["capability"]
        return {"answer": text, "model": "canned", "usage": {},
                "answer_status": "SUCCESS", "intent_type": "smalltalk",
                "risk_level": None, "risk_blocked": False,
                "no_result": False, "source": "smalltalk"}
    if decision.route == ROUTE_OFFICE:
        outcome = _execute_general_llm(req.question, history=None, tier="office",
                                       identity=identity)
        if outcome is not None and outcome["answer_status"] is None:
            outcome["answer_status"] = "SUCCESS"   # 前置路径无"原状态"可维持（配额话术）
        return outcome
    return None


def _general_failure_outcome(question: str, chunks, *, identity: Optional[Identity],
                             user_dept, history) -> Optional[Dict[str, Any]]:
    """知识库失败（检索为空 / LLM 拒答）后的统一失败序列：
    分诊（intent_router，fail-closed）→ 决策（answer_flow.build_failure_action，纯函数）
    → 执行（canned / 通用 LLM / 引导话术）。返回 None = 维持今日行为。

    不变量 I2 由 build_failure_action 保证：enterprise 永不产生 general_llm。
    """
    mode, guided_on, feature_on = _general_flags()
    if not feature_on:
        return None
    titles = suggest_titles(chunks)
    category = triage_failed_query(question, titles)
    rephrase = [] if titles else _suggest_rephrase(question, user_dept=user_dept)
    act = build_failure_action(category, mode=mode, guided_on=guided_on,
                               titles=titles, rephrase=rephrase)
    if act["action"] == ACTION_PASSTHROUGH:
        return None
    if act["action"] == ACTION_SENSITIVE_BLOCK:
        return {"answer": act["text"], "model": "N/A", "usage": {},
                "answer_status": "BLOCKED", "intent_type": act["intent_type"],
                "risk_level": "sensitive", "risk_blocked": True,
                "no_result": False, "source": "guard"}
    if act["action"] == ACTION_SMALLTALK:
        return {"answer": CANNED_SMALLTALK["capability"], "model": "canned", "usage": {},
                "answer_status": "SUCCESS", "intent_type": act["intent_type"],
                "risk_level": None, "risk_blocked": False,
                "no_result": False, "source": "smalltalk"}
    if act["action"] == ACTION_GENERAL_LLM:
        outcome = _execute_general_llm(question, history=history, tier=act["tier"],
                                       identity=identity)
        if outcome is None and guided_on:
            # LLM 失败且引导话术开启：给引导出口而非干拒（answer_status 维持原状态）
            return {"answer": GENERAL_TIER_OFF_MESSAGE, "model": "N/A", "usage": {},
                    "answer_status": None, "intent_type": "refuse_uncovered_general",
                    "risk_level": None, "risk_blocked": False,
                    "no_result": True, "source": "guard"}
        return outcome
    # ACTION_GUIDED_REFUSAL：answer_status=None —— 空结果分支维持 NO_RESULT、
    # 拒答分支维持 REFUSAL（contribution 缺口挖掘口径零漂移）
    return {"answer": act["text"], "model": "N/A", "usage": {},
            "answer_status": None, "intent_type": act["intent_type"],
            "risk_level": None, "risk_blocked": False,
            "no_result": True, "source": "guard"}


_STREAM_GATE_HEAD_CHARS = 48


def _gate_probe(event_iter):
    """低置信带流式门控探针（评审补充 5）：消费事件直到能裁决"开场是否拒答句式"。

    拒答开场判定窗是前 30 字符（answer_flow.is_refusal_answer），缓冲 48 字符足够裁决；
    正常回答（低置信带小流量段）仅首帧延迟到 48 字符/生成结束，其后逐帧透传。
    返回 (verdict, held_events, event_iter)；verdict=True 表示开场命中拒答句式，
    调用方走统一失败序列改道（held 事件丢弃），False 则按原序 flush held + 透传。
    parse_sse_data_frame / is_refusal_answer 均为纯函数不抛异常 —— 探针自身不引入新故障面。
    """
    held: List[str] = []
    head_parts: List[str] = []
    decided = False
    verdict = False
    for event in event_iter:
        held.append(event)
        frame = parse_sse_data_frame(event)
        if frame is not None and frame.get("type") == "chunk" and frame.get("content"):
            head_parts.append(frame["content"])
            if len("".join(head_parts)) >= _STREAM_GATE_HEAD_CHARS:
                verdict = is_refusal_answer("".join(head_parts))
                decided = True
                break
        elif frame is not None and frame.get("type") in ("done", "error"):
            # 未攒够 48 字符流就结束（短回答/异常帧）：按已有开场裁决
            verdict = is_refusal_answer("".join(head_parts))
            decided = True
            break
    if not decided:
        verdict = is_refusal_answer("".join(head_parts))
    return verdict, held, event_iter


@app.post("/api/ask", response_model=AskResponse)
def ask(req: AskRequest, request: Request, background_tasks: BackgroundTasks,
        identity: Optional[Identity] = Depends(require_identity)):
    """非流式问答接口 — 检索 + LLM 一次性返回。

    用 def（非 async）声明：内部全是同步阻塞 I/O（embedding HTTP、HA3、pymysql、LLM
    requests.post），FastAPI 会把 def 处理器放进线程池执行，避免阻塞事件循环、拖垮并发请求。

    落库走 BackgroundTasks（性能第一梯队 #5）：掩码+INSERT 从用户可见延迟中剔除，
    RDS 抖动不再放大 P99。call site 仍在本文件（tests monkeypatch api.log_qa_session；
    名字在 add_task 时按模块全局解析，patch 照常生效；TestClient 会在断言前跑完 bg task）。
    LLM_ERROR 分支保持同步——handler 抛异常时 BackgroundTasks 不会执行，失败必须留痕。
    """
    # 防刷准入：须在 _prepare_ask（embedding/HA3/rerank 开销）之前拒绝
    _enforce_rate_limit(request, identity, scope="ask",
                        thinking=bool(req.thinking), count_llm=True)

    # 通用能力前置路由（RAG_GENERAL_ABILITY_MODE，默认 off 时 _general_pre_route_decision
    # 恒 None）：显而易见的寒暄/办公祈使/敏感/实时信息在检索之前分流 —— KB 主路径零延迟。
    _t0_pre = time.time()   # T2 的 LLM 调用发生在 _prepare_session 之前，计时起点在此
    pre = _general_pre_route_decision(req, identity)
    if pre is not None:
        (_t0_sess, session_id, _merged_history, uid, user_dept) = _prepare_session(
            req, identity, request=request)
        latency = int((time.time() - _t0_pre) * 1000)
        msg_id = generate_message_id()
        if should_append_history(pre["answer"], pre["answer_status"] or "SUCCESS"):
            _append_to_history(session_id, req.question, history_answer_text(pre["answer"]),
                               owner=identity.user_id if identity else None)
        background_tasks.add_task(log_qa_session, **build_qa_log_kwargs(
            session_id=session_id,
            conversation_id=req.conversation_id,
            message_id=msg_id,
            question=req.question,
            user_id=uid,
            user_name=(identity.name or None) if identity else None,
            user_dept=user_dept,
            answer_text=pre["answer"],
            chunks=None,   # 检索未发生（命中数/分数如实缺席）
            latency_ms=latency,
            answer_status=pre["answer_status"] or "SUCCESS",
            model_name=pre["model"],
            intent_type=pre["intent_type"],
            risk_level=pre["risk_level"],
            risk_blocked=pre["risk_blocked"],
        ))
        return AskResponse(
            answer=pre["answer"],
            sources=[],
            session_id=session_id,
            message_id=msg_id,
            model=pre["model"],
            usage=pre["usage"],
            latency_ms=latency,
            no_result=pre["no_result"],
            source=pre["source"],
        )

    (t0, session_id, merged_history, uid, user_dept,
     chunks, t_retrieval, retrieval_latency_ms) = _prepare_ask(req, identity, request=request)

    if not chunks:
        # 统一失败序列（检索为空）：分诊 → 通用回答 / 引导式拒答；flag 全关 → None（现状）
        outcome = _general_failure_outcome(req.question, [], identity=identity,
                                           user_dept=user_dept, history=merged_history)
        latency = int((time.time() - t0) * 1000)
        msg_id = generate_message_id()
        if outcome is not None:
            status = outcome["answer_status"] or "NO_RESULT"
            if should_append_history(outcome["answer"], status):
                _append_to_history(session_id, req.question,
                                   history_answer_text(outcome["answer"]),
                                   owner=identity.user_id if identity else None)
            background_tasks.add_task(log_qa_session, **build_qa_log_kwargs(
                session_id=session_id,
                conversation_id=req.conversation_id,
                message_id=msg_id,
                question=req.question,
                user_id=uid,
                user_name=(identity.name or None) if identity else None,
                user_dept=user_dept,
                answer_text=outcome["answer"],
                chunks=[],
                latency_ms=latency,
                retrieval_latency_ms=retrieval_latency_ms,
                answer_status=status,
                model_name=outcome["model"],
                intent_type=outcome["intent_type"],
                risk_level=outcome["risk_level"],
                risk_blocked=outcome["risk_blocked"],
            ))
            return AskResponse(
                answer=outcome["answer"],
                sources=[],
                session_id=session_id,
                message_id=msg_id,
                model=outcome["model"],
                usage=outcome["usage"],
                latency_ms=int((time.time() - t0) * 1000),
                no_result=outcome["no_result"],
                rephrase=(_suggest_rephrase(req.question, user_dept=user_dept)
                          if outcome["no_result"] else []),
                source=outcome["source"],
            )
        background_tasks.add_task(log_qa_session, **build_qa_log_kwargs(
            session_id=session_id,
            conversation_id=req.conversation_id,
            message_id=msg_id,
            question=req.question,
            user_id=uid,
            user_name=(identity.name or None) if identity else None,
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
            rephrase=_suggest_rephrase(req.question, user_dept=user_dept),
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
            user_name=(identity.name or None) if identity else None,
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

    # 拒答判定（提前到入史之前：统一失败序列可能替换回答，历史必须与用户所见一致）
    answer_out = strip_image_markers(result["answer"])
    resp_no_result = is_refusal_answer(answer_out)   # 拒答形态（可伴随弱相关来源）
    resp_guard = is_low_confidence_band(chunks)      # 不依赖 RAG_LOW_CONFIDENCE_GUARD 开关
    was_refusal = resp_no_result
    resp_source = "kb"
    _log_intent = None
    _log_risk_level = None
    _log_risk_blocked = False
    if resp_no_result:
        # 统一失败序列（LLM 拒答）：分诊 → 通用回答 / 引导式拒答替换；flag 全关 → None
        outcome = _general_failure_outcome(req.question, chunks, identity=identity,
                                           user_dept=user_dept, history=merged_history)
        if outcome is not None:
            result["answer"] = outcome["answer"]     # 替换文本均为纯文本（无 <<IMG>> 标记）
            answer_out = outcome["answer"]
            result["model"] = outcome["model"]
            result["usage"] = outcome["usage"]
            if outcome["source"] in ("general", "smalltalk"):
                result["sources"] = []
            resp_no_result = outcome["no_result"]
            resp_source = outcome["source"]
            _log_intent = outcome["intent_type"]
            _log_risk_level = outcome["risk_level"]
            _log_risk_blocked = outcome["risk_blocked"]
            if outcome["answer_status"] is not None:
                _forced_status = outcome["answer_status"]
            else:
                _forced_status = None
        else:
            _forced_status = None
    else:
        _forced_status = None

    # 4. 更新会话历史（统一策略：仅非空 SUCCESS 回答入史；#F-mm5 入史文本经
    #    history_answer_text —— flag ON 时剥 <<IMG:N>> 防 follow-up 标记模仿。
    #    只包裹实参：result["answer"] 保持原文，下方 blocks 构建依赖原始标记。
    #    拒答照旧按 "SUCCESS" 入史（原行为）；BLOCKED（敏感拦截）不入史。）
    if should_append_history(result["answer"], _forced_status or "SUCCESS"):
        _append_to_history(session_id, req.question, history_answer_text(result["answer"]),
                           owner=identity.user_id if identity else None)

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

    # 5. 落库。拒答型回答（检索有候选但 LLM 按护栏拒答）标 REFUSAL，与 NO_RESULT
    #    （检索为空，前面已 return）分桶 —— 语料排查一句 SQL 区分「缺语料」vs
    #    「语料弱/未召回」。入史策略不变（拒答照旧入史，只改落库状态）。
    #    失败序列替换过的回答按 outcome 状态落库（引导话术维持 REFUSAL —— 口径不漂移；
    #    通用回答 SUCCESS + intent_type；敏感拦截 BLOCKED + risk_blocked）。
    #    kwargs 在请求内构建（纯簿记，µs 级），掩码+INSERT 推迟到响应发出后。
    background_tasks.add_task(log_qa_session, **build_qa_log_kwargs(
        session_id=session_id,
        conversation_id=req.conversation_id,
        message_id=msg_id,
        question=req.question,
        user_id=uid,
        user_name=(identity.name or None) if identity else None,
        user_dept=user_dept,
        answer_text=result["answer"],
        chunks=chunks,
        cited_docs=result.get("sources"),
        latency_ms=latency,
        retrieval_latency_ms=retrieval_latency_ms,
        llm_latency_ms=llm_latency_ms,
        answer_status=_forced_status or ("REFUSAL" if was_refusal else "SUCCESS"),
        model_name=result.get("model"),
        content_blocks_json=content_blocks_to_json(blocks) if blocks else None,
        # P2-20/21/22：LLM 成功路径透传生成元数据（generate_answer[_via_stream] 返回 dict
        # 里的 "gen_meta"；测试 mock 不带该键 → .get 得 None，不炸）
        gen_meta=result.get("gen_meta"),
        intent_type=_log_intent,
        risk_level=_log_risk_level,
        risk_blocked=_log_risk_blocked,
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
        rephrase=_suggest_rephrase(req.question, user_dept=user_dept) if resp_no_result else [],
        source=resp_source,
    )


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@app.post("/api/ask/stream")
def ask_stream(req: AskRequest, request: Request,
               identity: Optional[Identity] = Depends(require_identity)):
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

    # 通用能力前置路由（与 /api/ask 同一决策；默认 off 恒 None）。命中即以复用帧协议
    # 直接下发：session → chunk（全文含免责尾注）→ done(source) → [DONE]，客户端零改造。
    _t0_pre = time.time()
    pre = _general_pre_route_decision(req, identity)
    if pre is not None:
        (_t0_sess, _pre_session_id, _mh, _pre_uid, _pre_dept) = _prepare_session(
            req, identity, request=request)
        _pre_msg_id = generate_message_id()

        def general_pre_gen():
            try:
                yield f"data: {json.dumps({'type': 'session', 'session_id': _pre_session_id, 'message_id': _pre_msg_id}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'chunk', 'content': pre['answer']}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'model': pre['model'], 'usage': pre['usage'], 'no_result': pre['no_result'], 'source': pre['source']}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                if should_append_history(pre["answer"], pre["answer_status"] or "SUCCESS"):
                    _append_to_history(_pre_session_id, req.question,
                                       history_answer_text(pre["answer"]),
                                       owner=identity.user_id if identity else None)
                log_qa_session(**build_qa_log_kwargs(
                    session_id=_pre_session_id,
                    conversation_id=req.conversation_id,
                    message_id=_pre_msg_id,
                    question=req.question,
                    user_id=_pre_uid,
                    user_name=(identity.name or None) if identity else None,
                    user_dept=_pre_dept,
                    answer_text=pre["answer"],
                    chunks=None,   # 检索未发生
                    latency_ms=int((time.time() - _t0_pre) * 1000),
                    answer_status=pre["answer_status"] or "SUCCESS",
                    model_name=pre["model"],
                    intent_type=pre["intent_type"],
                    risk_level=pre["risk_level"],
                    risk_blocked=pre["risk_blocked"],
                ))

        return StreamingResponse(general_pre_gen(), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)

    # 图文模式才补充图片（纯文本模式不展示图片，跳过 co-surfacing 的额外 HA3 查询）
    _pure = req.pure_text if req.pure_text is not None else get_config().rag.pure_text

    # 前置段与 /api/ask 共用；检索失败在返回 StreamingResponse 之前即抛 500
    (t0, session_id, merged_history, uid, user_dept,
     chunks, _t_retrieval, retrieval_latency_ms) = _prepare_ask(
        req, identity, request=request, cosurface_images=not _pure)
    message_id = generate_message_id()

    # 无结果：仍发出 message_id 并落库（NO_RESULT），与 /api/ask 空结果分支保持一致
    if not chunks:
        # 统一失败序列（检索为空）：分诊 → 通用回答 / 引导式拒答；flag 全关 → None（现状）
        _empty_outcome = _general_failure_outcome(req.question, [], identity=identity,
                                                  user_dept=user_dept, history=merged_history)

        def empty_gen():
            # 同步生成器：StreamingResponse 会在线程池迭代它，finally 里的阻塞 log_qa_session
            # 不会阻塞事件循环。落库放 finally：客户端中途断开（GeneratorExit）时仍保证 NO_RESULT
            # 落库，与主流式路径的 finally 收尾保持一致。
            _status = (_empty_outcome["answer_status"] or "NO_RESULT") \
                if _empty_outcome is not None else "NO_RESULT"
            try:
                yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'message_id': message_id}, ensure_ascii=False)}\n\n"
                if _empty_outcome is not None:
                    yield f"data: {json.dumps({'type': 'chunk', 'content': _empty_outcome['answer']}, ensure_ascii=False)}\n\n"
                    done_frame = {"type": "done", "model": _empty_outcome["model"],
                                  "usage": _empty_outcome["usage"],
                                  "no_result": _empty_outcome["no_result"],
                                  "source": _empty_outcome["source"]}
                    if _empty_outcome["no_result"]:
                        done_frame["rephrase"] = _suggest_rephrase(req.question, user_dept=user_dept)
                    yield f"data: {json.dumps(done_frame, ensure_ascii=False)}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'chunk', 'content': NO_RESULT_MESSAGE}, ensure_ascii=False)}\n\n"
                    # done 帧带 no_result + rephrase：让流式前端也能渲染结构化空结果卡（换个说法 + 转人工）
                    yield f"data: {json.dumps({'type': 'done', 'model': 'N/A', 'usage': {}, 'no_result': True, 'rephrase': _suggest_rephrase(req.question, user_dept=user_dept)}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                if _empty_outcome is not None and should_append_history(
                        _empty_outcome["answer"], _status):
                    _append_to_history(session_id, req.question,
                                       history_answer_text(_empty_outcome["answer"]),
                                       owner=identity.user_id if identity else None)
                log_qa_session(**build_qa_log_kwargs(
                    session_id=session_id,
                    conversation_id=req.conversation_id,
                    message_id=message_id,
                    question=req.question,
                    user_id=uid,
                    user_name=(identity.name or None) if identity else None,
                    user_dept=user_dept,
                    answer_text=(_empty_outcome["answer"] if _empty_outcome is not None else None),
                    chunks=[],
                    latency_ms=int((time.time() - t0) * 1000),
                    retrieval_latency_ms=retrieval_latency_ms,
                    answer_status=_status,
                    model_name=(_empty_outcome["model"] if _empty_outcome is not None else None),
                    intent_type=(_empty_outcome["intent_type"] if _empty_outcome is not None else None),
                    risk_level=(_empty_outcome["risk_level"] if _empty_outcome is not None else None),
                    risk_blocked=bool(_empty_outcome and _empty_outcome["risk_blocked"]),
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
        # cited_docs 取自生成器的 sources 帧（_extract_sources(included) —— 截断感知，
        # 与 /api/ask 落库的 result["sources"] 同一计算），别在这里对全量 chunks 重算。
        stream_sources: Optional[List[Dict[str, Any]]] = None
        # 客户端中途断开时 StreamingResponse close 生成器 → GeneratorExit 直穿 except Exception
        # 落进 finally，answer_status 仍是 "SUCCESS"。completed 标记区分「真发完」与「断开截断」，
        # 截断的落库状态改 CLIENT_DISCONNECTED（否则半截回答混进 SUCCESS 分析口径，
        # 还可能被 is_refusal_answer 误翻成 REFUSAL）。
        completed = False
        # P2-20/21/22：生成元数据出参（generate_answer_stream 首帧前回填 "gen_meta"；
        # gen_meta 只落库、绝不进 SSE 线协议 —— 内部阈值/模型配置不外泄给 SSE 客户端。
        # 测试 mock 生成器不回填 → .get 得 None）
        gen_meta_out: Dict[str, Any] = {}
        # 通用能力分级开放的落库标注（默认全 None/False = 存量载荷逐字节不变）
        intent_type_out: Optional[str] = None
        risk_level_out: Optional[str] = None
        risk_blocked_out = False
        forced_status: Optional[str] = None

        try:
            try:
                _stream_guard = bool(is_low_confidence_band(chunks))   # 低置信带：补进 done 帧供前端渲染提示条
                _llm_events = generate_answer_stream(
                    req.question,
                    chunks,
                    history=merged_history if merged_history else None,
                    # 思考挤占 token 预算会截断答案：thinking 时放宽（与 /api/ask 同策略）
                    max_tokens=(max(req.max_tokens or DEFAULT_MAX_TOKENS, 4096)
                                if req.thinking else (req.max_tokens or DEFAULT_MAX_TOKENS)),
                    temperature=req.temperature or DEFAULT_TEMPERATURE,
                    pure_text=req.pure_text,
                    thinking=req.thinking,
                    meta_out=gen_meta_out,
                )
                _held_events: List[str] = []
                _replaced: Optional[Dict[str, Any]] = None
                # 低置信带 stream gate（评审补充 5）：拒答文本一旦流出便无法替换 ——
                # 仅在疑似失败路径（低置信带）缓冲开场 ~48 字符裁决；命中拒答句式则吞掉
                # 拒答流、改道统一失败序列；未命中按原序 flush + 透传（该小流量段仅首帧
                # 延迟）。中高置信带完全不进此分支，KB 流式主路径零影响。
                if (_stream_guard and get_config().rag.general_stream_gate
                        and _general_flags()[2]):
                    _verdict, _held_events, _llm_events = _gate_probe(_llm_events)
                    if _verdict:
                        _replaced = _general_failure_outcome(
                            req.question, chunks, identity=identity,
                            user_dept=user_dept, history=merged_history)
                        if _replaced is not None:
                            for _ev in _llm_events:   # 吞掉剩余拒答流（不下发）
                                pass
                            _held_events = []

                if _replaced is not None:
                    # 改道：以复用帧协议下发替代内容（chunk → done → [DONE]）。
                    # forced_status=None（引导话术）时让 finally 的拒答翻转按句式定 REFUSAL
                    # —— 与非流式口径一致；通用回答显式 SUCCESS；敏感拦截显式 BLOCKED。
                    collected_answer.append(_replaced["answer"])
                    model_name = _replaced["model"]
                    intent_type_out = _replaced["intent_type"]
                    risk_level_out = _replaced["risk_level"]
                    risk_blocked_out = _replaced["risk_blocked"]
                    forced_status = _replaced["answer_status"]
                    yield f"data: {json.dumps({'type': 'chunk', 'content': _replaced['answer']}, ensure_ascii=False)}\n\n"
                    done_frame = {"type": "done", "model": _replaced["model"],
                                  "usage": _replaced["usage"], "guard": _stream_guard,
                                  "no_result": _replaced["no_result"],
                                  "source": _replaced["source"]}
                    if _replaced["no_result"]:
                        _ts = suggest_titles(chunks)
                        if _ts:
                            done_frame["suggest_titles"] = _ts
                        else:
                            done_frame["rephrase"] = _suggest_rephrase(
                                req.question, user_dept=user_dept)
                    yield f"data: {json.dumps(done_frame, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    completed = True
                else:
                    for event in itertools.chain(_held_events, _llm_events):
                        # 截获生成器自带的 [DONE]，改由本函数在 content_blocks 之后统一收尾
                        if event.strip() == "data: [DONE]":
                            continue
                        frame = parse_sse_data_frame(event)
                        # sources 帧：完整字典截留作 cited_docs 落库（与 /api/ask 同源），
                        # 但下发前过滤成 SourceInfo 字段集——REST 靠 response_model 收口，
                        # SSE 透传没有那层，原样转发会把内部 OSS key（source_image）、
                        # visual_summary、chunk_type 泄给任意 SSE 客户端（两前端响应契约也不一致）。
                        if frame is not None and frame.get("type") == "sources":
                            stream_sources = frame.get("sources") or None
                            _fields = set(SourceInfo.model_fields)
                            frame["sources"] = [
                                {k: v for k, v in (s or {}).items() if k in _fields}
                                for s in (frame.get("sources") or [])
                            ]
                            yield f"data: {json.dumps(frame, ensure_ascii=False)}\n\n"
                            continue
                        # done 帧补 guard 后再下发（流式也能显示低置信提示条）；其余帧原样透传
                        if frame is not None and frame.get("type") == "done":
                            model_name = frame.get("model")
                            frame["guard"] = _stream_guard
                            # 引导式拒答富化（RAG_GUIDED_REFUSAL）：拒答文本已流出无法替换，
                            # done 帧补结构化字段（与空结果 done 帧字段对齐，前端已有渲染路径）。
                            # done 是最后一个生成帧，此刻 collected_answer 即全文。
                            if _general_flags()[1]:
                                _cur = strip_doc_citations("".join(collected_answer))
                                if _cur and is_refusal_answer(_cur):
                                    frame["no_result"] = True
                                    _ts = suggest_titles(chunks)
                                    if _ts:
                                        frame["suggest_titles"] = _ts
                                    else:
                                        frame["rephrase"] = _suggest_rephrase(
                                            req.question, user_dept=user_dept)
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
                    completed = True

            except Exception as e:
                answer_status = "LLM_ERROR"
                trace_id = get_request_id()
                error_message = f"[trace={trace_id}] {str(e)[:500]}"
                logger.error("Stream generation failed [trace=%s]: %s", trace_id, e, exc_info=True)
                error_msg = json.dumps({"type": "error", "message": f"回答生成失败，请联系管理员 (trace: {trace_id})"}, ensure_ascii=False)
                yield f"data: {error_msg}\n\n"
                yield "data: [DONE]\n\n"
                completed = True
        finally:
            # 无论正常结束还是客户端中途断开（GeneratorExit），都写历史 + 落库，
            # 保证流式回答可被反馈/分析（落库函数自身吞掉异常，绝不影响回复）
            full_answer = strip_doc_citations("".join(collected_answer))
            # 中途断开（未走到 [DONE] 也未进 except）→ CLIENT_DISCONNECTED：截断回答与
            # 完整 SUCCESS 分桶，且绝不被 is_refusal_answer 误翻 REFUSAL（截断恰止于拒答
            # 句式时会假命中）。入史策略见 should_append_history —— 断开仍入史：collected
            # frames = 用户实际看到的内容，follow-up 语境应与用户所见一致。
            if not completed and answer_status == "SUCCESS":
                answer_status = "CLIENT_DISCONNECTED"
            # 统一策略：非空 SUCCESS / CLIENT_DISCONNECTED 入史 —— 出错的半截回答不污染
            # 后续轮次上下文（#F-mm5 入史文本经 history_answer_text；blocks 已在上方用原文构建完毕）
            if should_append_history(full_answer, answer_status):
                _append_to_history(session_id, req.question, history_answer_text(full_answer),
                                   owner=identity.user_id if identity else None)
            # 拒答型标 REFUSAL（入史判定在上面、用原状态 —— 历史策略不变）；深思加 model 后缀
            if answer_status == "SUCCESS" and is_refusal_answer(full_answer):
                answer_status = "REFUSAL"
            # 失败序列改道的显式状态（SUCCESS/BLOCKED）最后覆盖；引导话术 forced_status=None
            # → 上面的句式翻转已把它标成 REFUSAL（与非流式口径一致）。断开截断不覆盖。
            if forced_status and answer_status != "CLIENT_DISCONNECTED":
                answer_status = forced_status
            if req.thinking and model_name:
                model_name = f"{model_name}+thinking"
            log_qa_session(**build_qa_log_kwargs(
                session_id=session_id,
                conversation_id=req.conversation_id,
                message_id=message_id,
                question=req.question,
                user_id=uid,
                user_name=(identity.name or None) if identity else None,
                user_dept=user_dept,
                answer_text=full_answer,
                chunks=chunks,
                cited_docs=stream_sources,
                latency_ms=int((time.time() - t0) * 1000),
                retrieval_latency_ms=retrieval_latency_ms,
                answer_status=answer_status,
                model_name=model_name,
                error_message=error_message,
                content_blocks_json=content_blocks_json_str,
                gen_meta=gen_meta_out.get("gen_meta"),
                intent_type=intent_type_out,
                risk_level=risk_level_out,
                risk_blocked=risk_blocked_out,
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


def _feedback_owns_message(message_id: str, user_id: str) -> bool:
    """反馈归属校验：message_id 是否为 user_id 本人的问答（qa_session_log 现查绑定 user_id）。

    REST /api/feedback 无从预取上下文（钉钉卡片回调走 _card_callback_authorized + qa_context），
    这里补上同等的归属门控：未命中 → 非本人消息（伪造/越权）→ 拒收。查库异常 → fail-open 放行
    （与卡片回调一致：下游 _save_feedback 写在同一故障下本就会失败，不因瞬时 SELECT 抖动误拒）。"""
    if not message_id or not user_id:
        return False
    from opensearch_pipeline.qa_logger import _op_db
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT 1 FROM {_op_db()}.qa_session_log "
                    f"WHERE message_id = %s AND user_id = %s LIMIT 1",
                    (message_id, user_id),
                )
                return cursor.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("反馈归属校验查库失败（fail-open 放行）: %s", e)
        return True


@app.post("/api/feedback")
def submit_feedback(req: FeedbackRequest, request: Request,
                    identity: Optional[Identity] = Depends(current_identity)):
    """反馈接口 — 供前端/管理后台使用。"""
    _enforce_rate_limit(request, identity, scope="aux")
    # 归属主键：仅令牌身份可作 user_id；匿名一律落 anon:<ip 短哈希>，绝不采信客户端自报的
    # req.user_id 伪造他人 staffId 覆盖/污染其反馈（与 /api/ask 落库归属一致）。#F-fbk-authz
    uid = identity.user_id if identity else _anon_uid(request)
    # 归属门控：反馈只能针对 uid 本人的问答；非本人消息（伪造/越权）拒收。#F-fbk-authz
    if not _feedback_owns_message(req.message_id, uid):
        raise HTTPException(status_code=403, detail="无权对该消息反馈")
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
    其余 session_id 若条目已绑定身份（钉钉/已认证创建），须携带同一身份（P3-6）；
    匿名创建的服务端 UUID 按持有即所有处理。
    幂等：会话不存在/已过期返回 200 + cleared=false（客户端结果一致：下一轮是全新会话）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    if req.session_id.startswith("miniapp:"):
        if not identity or req.session_id != f"miniapp:{identity.user_id}":
            raise HTTPException(status_code=403, detail="无权清除该会话")
    try:
        cleared = clear_session(req.session_id,
                                owner=identity.user_id if identity else None)
    except SessionOwnershipError:
        raise HTTPException(status_code=403, detail="无权清除该会话")
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
                  identity: Optional[Identity] = Depends(require_identity)):
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
# P2-7（盲区审计 2026-07-05）：热门问题/改写池按【请求者部门 cohort】分池。
# 此前两条聚合查询无 user_dept 谓词，把他部门用户的原始提问文本（暴露该部门在问什么、
# 内部术语）跨部门下发——尽管每次检索都正确按部门过滤，这条派生特征是独立的泄露通道。
# cohort 键 = 与 /api/ask 落库同一形态的 user_dept（acl_groups CSV，见 answer_flow）：
# 同权限组集合的用户互见彼此的高频问题；钉钉 bot 路径落的中文部门名与 CSV 不相交 →
# 保守不互见（宁可少推荐，不越界）。匿名/无部门 → 只给静态兜底。
_hot_questions_cache: Dict[str, Dict[str, Any]] = {}    # dept_key -> {ts,data,refreshing}
_DEPT_CACHE_MAX_KEYS = 128    # cohort 键数软上限（防伪造令牌枚举撑爆内存；超限逐最旧）
# 不计入热门的控制指令与联调账号
_HOT_Q_EXCLUDE_TEXT = ("新会话", "重新开始")
_HOT_Q_EXCLUDE_USER_PREFIX = ("miniapp-proto", "eval-", "test-")


def _dept_scope_key(user_dept) -> str:
    """把 identity.acl_groups / user_dept 归一成与 qa_session_log.user_dept 落库同形的
    cohort 键（list → CSV，与 answer_flow.build_qa_log_kwargs 逐字一致）。空 = 无部门。"""
    if isinstance(user_dept, (list, tuple)):
        return ",".join(str(d) for d in user_dept if d)
    return str(user_dept or "").strip()


def _dept_cache_entry(cache: Dict[str, Dict[str, Any]], key: str) -> Dict[str, Any]:
    """取/建 cohort 的 SWR 缓存条目；超上限时逐出 ts 最旧的键。"""
    ent = cache.get(key)
    if ent is None:
        if len(cache) >= _DEPT_CACHE_MAX_KEYS:
            oldest = min(cache, key=lambda k: cache[k].get("ts", 0.0))
            cache.pop(oldest, None)
        ent = cache[key] = {"ts": 0.0, "data": None}
    return ent

# perf#80 serve-stale-while-revalidate：hot-questions / success-pool 两处 1h 缓存
# 过期后的首个请求原本在 chat 首页热路径上同步扛整个 30 天 GROUP BY 聚合（每小时
# 一次的冷启动尖峰）。改为：过期且有旧值 → 立即返回旧值，单飞后台线程刷新（防击穿；
# 刷新异常吞掉保留旧值，下个过期请求再触发重试）；进程启动后首次（无旧值）保持现状
# 同步计算。--workers 1 单进程，进程内单飞锁即全局权威。
_swr_refresh_lock = threading.Lock()


def _swr_schedule_refresh(cache: Dict[str, Any], compute, label: str) -> None:
    """单飞调度一次后台刷新：已有同缓存的刷新在跑则直接返回（防击穿）。"""
    with _swr_refresh_lock:
        if cache.get("refreshing"):
            return
        cache["refreshing"] = True

    def _worker():
        try:
            data = compute()
            # GIL 下逐键赋值原子；data 先于 ts 写入，读侧不会拿到「新鲜时间戳 + 旧数据」
            cache["data"] = data
            cache["ts"] = time.time()
        except Exception:
            logger.warning("%s 后台刷新失败，保留旧缓存", label, exc_info=True)
        finally:
            with _swr_refresh_lock:
                cache["refreshing"] = False

    threading.Thread(target=_worker, name=f"swr-{label}", daemon=True).start()


def _swr_cached(cache: Dict[str, Any], ttl: float, compute, fallback, label: str):
    """serve-stale-while-revalidate 读口（perf#80）：新鲜 → 直接返回；过期但有旧值 →
    返回旧值 + 后台单飞刷新；无旧值（进程首个请求）→ 同步计算（失败回退 fallback）。"""
    now = time.time()
    cached = cache["data"]
    if cached is not None:
        if now - cache["ts"] < ttl:
            return cached
        _swr_schedule_refresh(cache, compute, label)
        return cached
    try:
        data = compute()
    except Exception:
        logger.warning("%s 查询失败，使用兜底", label, exc_info=True)
        data = fallback()
    cache["ts"] = time.time()
    cache["data"] = data
    return data


def _compute_hot_questions(dept_key: str) -> List[str]:
    """近 30 天高频 SUCCESS 问题 top-6 聚合（DB 异常上抛，回退语义由 _swr_cached 决定）。

    P2-7：加 `user_dept = %s` cohort 谓词——聚合池只含与请求者同权限组集合用户的提问，
    他部门的查询意图/内部术语不再经「示例问题」跨部门下发。"""
    from opensearch_pipeline.qa_logger import _op_db
    from opensearch_pipeline.db import _get_db_conn

    questions: List[str] = []
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
                  AND user_dept = %s
                  AND created_at >= NOW() - INTERVAL 30 DAY
                  AND CHAR_LENGTH(query_text) BETWEEN 4 AND 30
                  AND {user_excludes}
                GROUP BY query_text
                HAVING cnt >= 2
                ORDER BY cnt DESC, MAX(id) DESC
                LIMIT 20
                """,
                (dept_key,) + tuple(p + "%" for p in _HOT_Q_EXCLUDE_USER_PREFIX),
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

    # 不足 3 条时用静态默认补齐（去重保序）
    for q in _HOT_QUESTIONS_FALLBACK:
        if len(questions) >= 3:
            break
        if q not in questions:
            questions.append(q)
    return questions


@app.get("/api/hot-questions")
def hot_questions(request: Request,
                  identity: Optional[Identity] = Depends(current_identity)):
    """真实高频问题 top-N，驱动 chat 页「示例问题」快捷栏。

    P2-7：按请求者部门 cohort 分池分缓存；匿名/无部门只给静态兜底（真实提问文本
    是部门界定的可见范围，不对未登录者聚合下发）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    dept_key = _dept_scope_key(identity.acl_groups if identity else None)
    if not dept_key:
        return {"questions": list(_HOT_QUESTIONS_FALLBACK)}
    questions = _swr_cached(
        _dept_cache_entry(_hot_questions_cache, dept_key), _HOT_QUESTIONS_TTL_S,
        lambda: _compute_hot_questions(dept_key),
        lambda: list(_HOT_QUESTIONS_FALLBACK), f"hot-questions[{dept_key[:24]}]")
    return {"questions": questions}


# ── NO_RESULT「换个说法」建议 ─────────────────────────────────────
# 设计依据：模板/LLM 改写都不保证改写后可答（LLM 改写在本链路已 A/B 为 dark），
# 而近 30 天 SUCCESS 过的真实问题可答性有保证 —— 按字符 bigram 重叠推荐相似者，
# 不足时回退「清洗版原问题」。纯启发式、零 LLM 成本、fail open。

_SUCCESS_POOL_TTL_S = 3600
_success_pool_cache: Dict[str, Dict[str, Any]] = {}    # P2-7：dept_key -> SWR 条目
_REPHRASE_NOISE_PREFIX = (
    "请问一下", "请问", "你好", "您好", "帮我查一下", "帮我查", "帮我",
    "我想知道", "我想问一下", "我想问", "想问一下", "问一下", "请教一下", "请教",
)


def _compute_success_pool(dept_key: str) -> List[str]:
    """近 30 天 SUCCESS 问题池聚合（DB 异常上抛，回退语义由 _swr_cached 决定）。
    P2-7：同 cohort 谓词——改写建议只从本部门 cohort 的成功问题里选。"""
    from opensearch_pipeline.qa_logger import _op_db
    from opensearch_pipeline.db import _get_db_conn

    pool: List[str] = []
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
                  AND user_dept = %s
                  AND created_at >= NOW() - INTERVAL 30 DAY
                  AND CHAR_LENGTH(query_text) BETWEEN 4 AND 40
                  AND {user_excludes}
                GROUP BY query_text
                ORDER BY cnt DESC
                LIMIT 200
                """,
                (dept_key,) + tuple(p + "%" for p in _HOT_Q_EXCLUDE_USER_PREFIX),
            )
            for text, _cnt in cursor.fetchall():
                t = (text or "").strip()
                if t and t not in _HOT_Q_EXCLUDE_TEXT:
                    pool.append(t)
    finally:
        conn.close()
    return pool


def _success_question_pool(dept_key: str) -> List[str]:
    """近 30 天 SUCCESS 问题池（去重、4-40 字、排除控制指令/联调账号；缓存 1h，
    perf#80 serve-stale：过期返回旧值 + 后台单飞刷新；查询失败退化为空池=仅清洗原问题）。
    P2-7：按 cohort 分池；无部门 → 空池（只走清洗原问题回退）。"""
    if not dept_key:
        return []
    return _swr_cached(
        _dept_cache_entry(_success_pool_cache, dept_key), _SUCCESS_POOL_TTL_S,
        lambda: _compute_success_pool(dept_key),
        lambda: [], f"success-pool(rephrase)[{dept_key[:24]}]")


def _char_bigrams(s: str) -> set:
    """字符 bigram 集（仅保留中日韩/字母数字 —— 中文无需分词的相似度量纲）。"""
    chars = [c for c in s if c.isalnum() or "一" <= c <= "鿿"]
    return {"".join(chars[i:i + 2]) for i in range(len(chars) - 1)}


def _suggest_rephrase(question: str, limit: int = 2, user_dept=None) -> List[str]:
    """NO_RESULT 出口的「换个说法」建议（fail open 返回 []）。
    P2-7：候选池按请求者部门 cohort 取（user_dept=identity.acl_groups）；无部门 →
    池空，只走清洗原问题回退——他部门的成功问法不再泄给本部门用户。"""
    try:
        q = (question or "").strip()
        if not q:
            return []
        q_grams = _char_bigrams(q)
        out: List[str] = []
        if q_grams:
            scored = []
            for cand in _success_question_pool(_dept_scope_key(user_dept)):
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

# 15 个 ACL 组的中文标签（权限选择器展示用；单一来源 retriever._VALID_ACL_GROUPS；
# 2026-07-03 扩容 5 组，与 console GROUP_LABEL / dept_ancestry 锚表同步）
_KB_ACL_GROUP_LABELS = {
    "finance": "财务", "it": "信息技术", "marketing": "营销", "production": "生产",
    "pmc": "生产计划部", "admin": "行政", "hr": "人力资源", "rd": "研发",
    "quality": "品质技术", "supply": "资材供应",
    "overseas": "海外", "audit": "审计", "legal": "法务",
    "engineering": "工程", "corn_eco": "玉米环保",
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
    # 利用度（qa_retrieved_doc 事实表，RAG_QA_FACT_JOIN 开且表在才填；None=数据不可用，
    # 前端不显示——0 与「不知道」必须可区分，0=真·从未被引用，是退役候选信号）。
    cited_count: Optional[int] = None
    last_cited_at: str = ""
    # 驳回原因（仅 content_process_status=REJECTED 时填，取 content_process_error）：
    # 反馈闭环——原因一直落库但 dept_admin 侧只见红徽章、审批历史 upload 类又只给 kb_admin，
    # 申请人无从得知为什么被驳回。台账「已驳回」行副行直出。
    reject_reason: str = ""


class KbMyDocsResponse(BaseModel):
    items: List[KbDocItem] = Field(default_factory=list)
    has_more: bool = False
    # faceted 状态计数（2026-07-16）：与本次查询同筛选（除 badge 自身）的按徽章计数——
    # 前端 chips/标题总数跟随下拉筛选。None=计数失败/旧后端（前端回退全库口径）。additive。
    badge_counts: Optional[Dict[str, int]] = None


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


# 台账徽章输出词表【封闭集】（批次ε-5 R2 seam 锁）：_kb_status_badge 的全部可能返回值。
# ⚠️ 三处词表人工同步——改这里必须同步：①下方 if 阶梯与 _KB_BADGE_CASE_SQL（parity 测试守）；
# ②前端 console-app/src/lib/kb.ts 的 BADGE_TONE（含 MyContributions displayState 特判词）；
# ③封闭集测试 tests/test_kb_endpoints.py::test_kb_status_badge_closed_set（新增/改名分支
# 未登记进本集合即红，把「漏同步」从静默行为回归降级为 CI 红）。
_KB_BADGE_VOCAB = frozenset({
    "已退役", "已隔离", "未入索引", "已上线", "处理失败",
    "已驳回", "内容未变", "待审核", "排队中", "处理中",
})


def _kb_status_badge(content_status, index_status, doc_status, chunk_active=None,
                     publish_status=None, chunk_status=None) -> str:
    """把管线多字段折叠为用户可读态（输出恒 ∈ _KB_BADGE_VOCAB，封闭集测试锁定）。"""
    cs = (content_status or "").upper()
    ix = (index_status or "").upper()
    if doc_status and str(doc_status).lower() not in ("active", ""):
        return "已退役"
    # PII 隔离优先于"已上线"：隔离件的 index_status 可能残留 'SUCCESS'，但 chunk 已停用、不在检索中，
    # 绝不能显示"已上线"（会被误读为可搜/已脱敏）。统一显示"已隔离"，等脱敏重灌。
    if str(publish_status or "").upper() == "QUARANTINED":
        return "已隔离"
    # 0-chunk / 版本被跳过终态（chunk_status='EMPTY'、publish_status='SKIPPED_*'——低文本图纸、
    # 整篇 PII 隔离弃件、chunk 爆炸弃版等）：永远不会进索引，此前一律落到默认「处理中」，
    # 管理员看不出"传了但永远搜不到"（136 篇未入索引盘点的可见性根因之一）。终态如实显示。
    if (str(chunk_status or "").upper() == "EMPTY"
            or str(publish_status or "").upper().startswith("SKIPPED")):
        return "未入索引"
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


# _kb_status_badge 的 SQL 镜像：供台账按徽章【服务端】筛选（全库口径，不再只筛已加载页）。
# SQL CASE 的「首个 WHEN 命中即返回」语义与上面 Python if 阶梯严格同序同义——**改
# _kb_status_badge 的判定顺序/取值时必须同步改这里**，parity 测试
# test_kb_badge_case_sql_parity 会守卫二者一致。约定：
#   · 引用 m（document_meta）/ v（document_version）别名——my-docs / browse 两处调用都用这对别名。
#   · list 路径 chunk_active 恒 None（列表不查活跃 chunk 数），故此处不含「SUCCESS 但 0 chunk→处理中」
#     分支，与 _kb_status_badge 在 chunk_active=None 时的行为一致（ix∈(INDEXED,SUCCESS) 即已上线）。
#   · SKIPPED 前缀用 LEFT(...,7)='SKIPPED'（**不用 LIKE 'SKIPPED%%'**）——查询带 %s 参数时
#     SQL 里的字面 % 会被 pymysql paramstyle 误读，LEFT 规避这个坑。
_KB_BADGE_CASE_SQL = (
    "CASE"
    " WHEN m.status IS NOT NULL AND LOWER(m.status) NOT IN ('active','') THEN '已退役'"
    " WHEN UPPER(COALESCE(v.publish_status,'')) = 'QUARANTINED' THEN '已隔离'"
    " WHEN UPPER(COALESCE(v.chunk_status,'')) = 'EMPTY'"
    "      OR LEFT(UPPER(COALESCE(v.publish_status,'')),7) = 'SKIPPED' THEN '未入索引'"
    " WHEN UPPER(COALESCE(v.index_status,'')) IN ('INDEXED','SUCCESS') THEN '已上线'"
    " WHEN UPPER(COALESCE(v.content_process_status,'')) = 'FAILED'"
    "      OR UPPER(COALESCE(v.index_status,'')) = 'FAILED' THEN '处理失败'"
    " WHEN UPPER(COALESCE(v.content_process_status,'')) = 'REJECTED' THEN '已驳回'"
    " WHEN UPPER(COALESCE(v.content_process_status,'')) = 'SKIPPED_DUPLICATE' THEN '内容未变'"
    " WHEN UPPER(COALESCE(v.content_process_status,'')) = 'PENDING_APPROVAL' THEN '待审核'"
    " WHEN UPPER(COALESCE(v.content_process_status,'')) IN ('','NOT_STARTED') THEN '排队中'"
    " ELSE '处理中' END"
)

# 「异常」聚合筛选的坏徽章集合（与 console 前端 useKb.BAD_BADGES、待办条「异常文档」同口径）。
_KB_BAD_BADGES = ("未入索引", "处理失败", "已隔离", "已驳回")


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
    """owner_dept 作用域 SQL：kb_admin 不限；dept_admin 限其 managed（production 伞组
    展开覆盖子线 owner——比对既有文档归属，非写目标面）；无授权 → 匹配空集。"""
    from opensearch_pipeline.kb_authz import (
        ROLE_KB_ADMIN, expand_managed_owner_depts, managed_owner_depts,
    )
    if kb.role == ROLE_KB_ADMIN:
        return "", []
    owners = expand_managed_owner_depts(managed_owner_depts(kb))
    if not owners:
        return "AND 1=0", []
    placeholders = ",".join(["%s"] * len(owners))
    return f"AND {col} IN ({placeholders})", list(owners)


def _kb_can_manage(kb, owner_dept: str) -> bool:
    from opensearch_pipeline.kb_authz import (
        ROLE_KB_ADMIN, expand_managed_owner_depts, managed_owner_depts,
    )
    if kb.role == ROLE_KB_ADMIN:
        return True
    return (owner_dept or "") in set(expand_managed_owner_depts(managed_owner_depts(kb)))


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
from opensearch_pipeline.routes import agent as _routes_agent  # noqa: E402  企业 Agent 入口（RAG_AGENT_ENABLE 默认 off；agent_runtime 惰性加载）
from opensearch_pipeline.routes import console as _routes_console  # noqa: E402
from opensearch_pipeline.routes import contribution as _routes_contribution  # noqa: E402
from opensearch_pipeline.routes import kb_access as _routes_kb_access  # noqa: E402
from opensearch_pipeline.routes import kb_console as _routes_kb_console  # noqa: E402
from opensearch_pipeline.routes import ontology as _routes_ontology  # noqa: E402  本体消解工作台（RAG_ONTOLOGY_ENABLE 默认 off；ontology 惰性加载）

# 注册顺序 = 原文件内出现顺序（路径无重叠，仅求 diff 稳定）。
app.include_router(_routes_kb_console.router)
app.include_router(_routes_kb_access.router)
app.include_router(_routes_contribution.router)
app.include_router(_routes_console.router)
app.include_router(_routes_agent.router)   # /api/agent/ask（独立路由，flag-off 时 404）
app.include_router(_routes_ontology.router)   # /api/ontology/*（steward 工作台，flag-off 时 404）

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
_kb_qa_owner_join = _routes_kb_console._kb_qa_owner_join   # perf#3：常量→函数（事实表/JSON_TABLE 二态）
KbTopDocItem = _routes_kb_console.KbTopDocItem
KbGapQueryItem = _routes_kb_console.KbGapQueryItem
KbInsightsResponse = _routes_kb_console.KbInsightsResponse
kb_insights = _routes_kb_console.kb_insights
KbFeedbackDocRef = _routes_kb_console.KbFeedbackDocRef
KbFeedbackReviewItem = _routes_kb_console.KbFeedbackReviewItem
KbFeedbackReviewResponse = _routes_kb_console.KbFeedbackReviewResponse
kb_feedback_review = _routes_kb_console.kb_feedback_review
KbFeedbackResolveRequest = _routes_kb_console.KbFeedbackResolveRequest
kb_feedback_resolve = _routes_kb_console.kb_feedback_resolve
KbEscalationItem = _routes_kb_console.KbEscalationItem
KbEscalationsResponse = _routes_kb_console.KbEscalationsResponse
kb_escalations = _routes_kb_console.kb_escalations
KbEscalationResolveRequest = _routes_kb_console.KbEscalationResolveRequest
kb_escalation_resolve = _routes_kb_console.kb_escalation_resolve
KbReviewTaskItem = _routes_kb_console.KbReviewTaskItem
KbReviewTasksResponse = _routes_kb_console.KbReviewTasksResponse
kb_review_tasks = _routes_kb_console.kb_review_tasks
KbReviewTaskResolveRequest = _routes_kb_console.KbReviewTaskResolveRequest
kb_review_task_resolve = _routes_kb_console.kb_review_task_resolve
KbEmbedRunItem = _routes_kb_console.KbEmbedRunItem
KbDeptCoverageItem = _routes_kb_console.KbDeptCoverageItem
KbFeedbackDay = _routes_kb_console.KbFeedbackDay
KbDownvoteReason = _routes_kb_console.KbDownvoteReason
KbFileType = _routes_kb_console.KbFileType
KbGovernanceResponse = _routes_kb_console.KbGovernanceResponse
kb_governance = _routes_kb_console.kb_governance
KbOpsMetricsResponse = _routes_kb_console.KbOpsMetricsResponse
kb_ops_metrics = _routes_kb_console.kb_ops_metrics
KbConfigResponse = _routes_kb_console.KbConfigResponse
kb_config = _routes_kb_console.kb_config
kb_version_history = _routes_kb_console.kb_version_history
kb_doc_status = _routes_kb_console.kb_doc_status
KbDocPreviewResponse = _routes_kb_console.KbDocPreviewResponse
kb_doc_preview = _routes_kb_console.kb_doc_preview
KbUploadUrlRequest = _routes_kb_console.KbUploadUrlRequest
KbUploadUrlResponse = _routes_kb_console.KbUploadUrlResponse
KbRegisterRequest = _routes_kb_console.KbRegisterRequest
KbRegisterResponse = _routes_kb_console.KbRegisterResponse
KbApprovalRequest = _routes_kb_console.KbApprovalRequest
KbRetireRequest = _routes_kb_console.KbRetireRequest
KbRetireResponse = _routes_kb_console.KbRetireResponse
KbRestoreResponse = _routes_kb_console.KbRestoreResponse
KbSetVisibilityRequest = _routes_kb_console.KbSetVisibilityRequest
KbSetVisibilityResponse = _routes_kb_console.KbSetVisibilityResponse
kb_upload_url = _routes_kb_console.kb_upload_url
kb_register = _routes_kb_console.kb_register
kb_approve = _routes_kb_console.kb_approve
kb_reject = _routes_kb_console.kb_reject
kb_retire = _routes_kb_console.kb_retire
kb_restore = _routes_kb_console.kb_restore
kb_set_visibility = _routes_kb_console.kb_set_visibility
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
KbAccessGrantCreate = _routes_kb_access.KbAccessGrantCreate
KbAccessGrantCreateResponse = _routes_kb_access.KbAccessGrantCreateResponse
KbVisibilityReader = _routes_kb_access.KbVisibilityReader
KbVisibilityExplainResponse = _routes_kb_access.KbVisibilityExplainResponse
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
kb_access_grant_create = _routes_kb_access.kb_access_grant_create
kb_visibility_explain = _routes_kb_access.kb_visibility_explain
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
_GAP_WINDOW_DAYS = _routes_contribution._GAP_WINDOW_DAYS
_GAP_CANDIDATE_CAP = _routes_contribution._GAP_CANDIDATE_CAP
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
KbGapDismissRequest = _routes_contribution.KbGapDismissRequest
KbGapDismissResponse = _routes_contribution.KbGapDismissResponse
KbGapDismissedItem = _routes_contribution.KbGapDismissedItem
KbGapDismissedResponse = _routes_contribution.KbGapDismissedResponse
kb_gap_dismiss = _routes_contribution.kb_gap_dismiss
kb_gap_restore = _routes_contribution.kb_gap_restore
kb_gaps_dismissed = _routes_contribution.kb_gaps_dismissed
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
