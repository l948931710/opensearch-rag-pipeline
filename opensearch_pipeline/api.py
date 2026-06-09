# -*- coding: utf-8 -*-
"""
api.py — RAG 问答 FastAPI 应用

端点：
  POST /api/ask           非流式问答
  POST /api/ask/stream    SSE 流式问答
  POST /api/search        纯检索（不调用 LLM）
  GET  /api/health        健康检查
"""

import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from opensearch_pipeline.llm_generator import generate_answer, generate_answer_stream
from opensearch_pipeline.retriever import search_chunks, retrieve_and_enrich
from opensearch_pipeline.dingtalk_bot import router as dingtalk_router
from opensearch_pipeline.dingtalk_identity import (
    _resolve_user_dept,
    _exchange_authcode_for_userid,
    _resolve_user_identity,
)
from opensearch_pipeline.qa_logger import generate_message_id, log_qa_session
from opensearch_pipeline.feedback_handler import handle_feedback
from opensearch_pipeline.content_blocks_builder import (
    build_content_blocks,
    content_blocks_to_json,
    build_mini_program_blocks,
)
from opensearch_pipeline.auth_token import issue_session_token, verify_session_token
from opensearch_pipeline.config import get_config
from opensearch_pipeline.session_store import get_or_create_session, append_to_history

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════════

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """应用生命周期：启动时注册钉钉互动卡片 HTTP 回调地址（若已配置 DINGTALK_CARD_CALLBACK_URL）。

    把本服务的 /dingtalk/card/callback 注册到 callbackRouteKey，使反馈按钮点击可回调。
    非致命：注册失败只记日志、不阻断启动。多 worker 下每个进程各注册一次（幂等）。
    """
    try:
        from opensearch_pipeline.dingtalk_card import register_card_callback
        register_card_callback()
    except Exception:
        logger.warning("启动时注册卡片回调失败（忽略，不影响服务）", exc_info=True)
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
    role: str = Field(..., description="消息角色: user / assistant")
    content: str = Field(..., description="消息内容")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    top_k: int = Field(7, ge=1, le=20, description="检索返回的文档数量")
    history: Optional[List[ChatMessage]] = Field(None, description="对话历史")
    session_id: Optional[str] = Field(None, description="会话 ID，用于追踪对话")
    temperature: Optional[float] = Field(0.1, ge=0.0, le=2.0, description="生成温度")
    max_tokens: Optional[int] = Field(2048, ge=100, le=8192, description="最大生成 token 数")
    user_id: Optional[str] = Field(None, description="用户 ID（钉钉 staffId）；无 Bearer 令牌时用于服务端解析部门")
    user_dept: Optional[str] = Field(
        None,
        description="[已废弃·服务端忽略] 部门一律由服务端按 Bearer 令牌/user_id 解析（防越权）",
        pattern=r'^[\w\-\u4e00-\u9fff]{0,64}$',
    )
    pure_text: Optional[bool] = Field(
        None,
        description="纯文本开关：None 取全局 RAG_PURE_TEXT；True 仅文字回答（不穿插图片）",
    )


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="搜索查询")
    top_k: int = Field(5, ge=1, le=50, description="返回结果数")
    user_id: Optional[str] = Field(None, description="用户 ID（钉钉 staffId）；无 Bearer 令牌时用于服务端解析部门")
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


class AskResponse(BaseModel):
    answer: str
    sources: List[SourceInfo]
    blocks: List[Dict[str, Any]] = []   # 小程序原生图文渲染块（图文穿插）；纯文字/无引用图片时为 []
    session_id: str
    message_id: str = ""
    model: str
    usage: Dict[str, Any] = {}
    latency_ms: int = 0


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
# Endpoints
# ═══════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health_check():
    """健康检查。"""
    return {"status": "ok", "service": "rag-qa-api"}


@app.post("/api/auth/dingtalk", response_model=DingtalkAuthResponse)
async def auth_dingtalk(req: DingtalkAuthRequest):
    """钉钉小程序免登：authCode → userid → 部门/姓名 → 签发会话令牌。

    部门在服务端解析后写入令牌；客户端只持有短期 authCode 与签发的令牌，
    AppSecret / 签名密钥永不下发到客户端。模拟模式下返回测试用户（见 _exchange_authcode_for_userid）。
    """
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
async def search(req: SearchRequest, identity: Optional[Identity] = Depends(current_identity)):
    """纯检索接口 — 只返回相关文档片段，不调用 LLM。"""
    t0 = time.time()
    try:
        # 部门一律服务端解析（令牌优先），绝不信任请求体里的部门字段
        uid = identity.user_id if identity else (req.user_id or "")
        user_dept = identity.dept if identity else (_resolve_user_dept(uid) if uid else None)
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


@app.post("/api/ask", response_model=AskResponse)
async def ask(req: AskRequest, identity: Optional[Identity] = Depends(current_identity)):
    """非流式问答接口 — 检索 + LLM 一次性返回。"""
    t0 = time.time()

    # 1. 会话管理
    session_id, session_history = _get_or_create_session(req.session_id)

    # 合并客户端传入的 history 和服务端 session history
    merged_history = list(session_history)
    if req.history:
        # 客户端显式传了 history 则优先使用
        merged_history = [{"role": m.role, "content": m.content} for m in req.history]

    # 身份与部门：部门一律服务端解析（令牌优先），绝不信任请求体里的部门字段
    uid = identity.user_id if identity else (req.user_id or "")
    user_dept = identity.dept if identity else (_resolve_user_dept(uid) if uid else None)

    # 2. 检索
    try:
        chunks = retrieve_and_enrich(req.question, top_k=req.top_k, user_dept=user_dept)
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("Search failed [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败，请联系管理员 (trace: {trace_id})")

    t_retrieval = time.time()
    retrieval_latency_ms = int((t_retrieval - t0) * 1000)

    if not chunks:
        latency = int((time.time() - t0) * 1000)
        msg_id = generate_message_id()
        log_qa_session(
            session_id=session_id,
            message_id=msg_id,
            user_id=uid,
            query_text=req.question,
            latency_ms=latency,
            retrieval_latency_ms=retrieval_latency_ms,
            answer_status="NO_RESULT",
            opensearch_hit_count=0,
        )
        return AskResponse(
            answer="抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述您的问题。",
            sources=[],
            session_id=session_id,
            message_id=msg_id,
            model="N/A",
            usage={},
            latency_ms=latency,
        )

    # 3. LLM 生成
    try:
        result = generate_answer(
            req.question,
            chunks,
            history=merged_history if merged_history else None,
            max_tokens=req.max_tokens or 2048,
            temperature=req.temperature or 0.1,
            pure_text=req.pure_text,
        )
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("LLM generation failed [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"回答生成失败，请联系管理员 (trace: {trace_id})")

    # 4. 更新会话历史
    _append_to_history(session_id, req.question, result["answer"])

    t_llm = time.time()
    llm_latency_ms = int((t_llm - t_retrieval) * 1000)
    latency = int((t_llm - t0) * 1000)
    msg_id = generate_message_id()

    # 5. 落库
    top_score = max((c.get("score", 0) for c in chunks), default=None)
    log_qa_session(
        session_id=session_id,
        message_id=msg_id,
        user_id=req.user_id or "",
        query_text=req.question,
        answer_text=result["answer"],
        retrieved_docs=chunks,
        cited_docs=result.get("sources"),
        latency_ms=latency,
        retrieval_latency_ms=retrieval_latency_ms,
        llm_latency_ms=llm_latency_ms,
        answer_status="SUCCESS",
        model_name=result.get("model"),
        opensearch_hit_count=len(chunks),
        top_score=top_score,
    )

    # 小程序图文渲染块（复用 build_content_blocks 核心；纯文字/未引用图片时为 []）
    try:
        blocks = build_mini_program_blocks(result["answer"], chunks)
    except Exception:
        logger.warning("mini-program blocks 构建失败 (non-fatal)", exc_info=True)
        blocks = []

    return AskResponse(
        answer=result["answer"],
        sources=[SourceInfo(**s) for s in result["sources"]],
        blocks=blocks,
        session_id=session_id,
        message_id=msg_id,
        model=result["model"],
        usage=result["usage"],
        latency_ms=latency,
    )


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@app.post("/api/ask/stream")
async def ask_stream(req: AskRequest, identity: Optional[Identity] = Depends(current_identity)):
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

    t0 = time.time()

    # 1. 会话管理
    session_id, session_history = _get_or_create_session(req.session_id)

    merged_history = list(session_history)
    if req.history:
        merged_history = [{"role": m.role, "content": m.content} for m in req.history]

    # 图文模式才补充图片（纯文本模式不展示图片，跳过 co-surfacing 的额外 HA3 查询）
    _pure = req.pure_text if req.pure_text is not None else get_config().rag.pure_text

    # 身份与部门：部门一律服务端解析（令牌优先），绝不信任请求体里的部门字段
    uid = identity.user_id if identity else (req.user_id or "")
    user_dept = identity.dept if identity else (_resolve_user_dept(uid) if uid else None)

    # 2. 检索（同步阶段，在生成器外完成）
    try:
        chunks = retrieve_and_enrich(
            req.question, top_k=req.top_k, user_dept=user_dept,
            cosurface_images=not _pure,
        )
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("Search failed [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败，请联系管理员 (trace: {trace_id})")

    retrieval_latency_ms = int((time.time() - t0) * 1000)
    message_id = generate_message_id()

    # 无结果：仍发出 message_id 并落库（NO_RESULT），与 /api/ask 空结果分支保持一致
    if not chunks:

        async def empty_gen():
            # 落库放 finally：客户端中途断开（GeneratorExit）时仍保证 NO_RESULT 落库，
            # 与主流式路径的 finally 收尾保持一致。
            try:
                yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'message_id': message_id}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'chunk', 'content': '抱歉，当前知识库中未找到与您问题相关的信息。'}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'model': 'N/A', 'usage': {}}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                log_qa_session(
                    session_id=session_id,
                    message_id=message_id,
                    user_id=uid,
                    query_text=req.question,
                    latency_ms=int((time.time() - t0) * 1000),
                    retrieval_latency_ms=retrieval_latency_ms,
                    answer_status="NO_RESULT",
                    opensearch_hit_count=0,
                )

        return StreamingResponse(empty_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)

    # 3. SSE 流式生成
    top_score = max((c.get("score", 0) for c in chunks), default=None)

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
                    max_tokens=req.max_tokens or 2048,
                    temperature=req.temperature or 0.1,
                    pure_text=req.pure_text,
                ):
                    # 截获生成器自带的 [DONE]，改由本函数在 content_blocks 之后统一收尾
                    if event.strip() == "data: [DONE]":
                        continue
                    yield event

                    # 收集完整回答 + 模型名（用于写历史 & 落库）
                    if '"type": "chunk"' in event:
                        try:
                            d = json.loads(event[6:].strip())
                            if d.get("type") == "chunk" and d.get("content"):
                                collected_answer.append(d["content"])
                        except (json.JSONDecodeError, KeyError):
                            pass
                    elif '"type": "done"' in event:
                        try:
                            model_name = json.loads(event[6:].strip()).get("model")
                        except (json.JSONDecodeError, KeyError):
                            pass

                # 正常完成：图文模式下补发 content_blocks 帧（图片须在全文完成后定稿）
                full_answer = "".join(collected_answer)
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
            full_answer = "".join(collected_answer)
            if full_answer:
                _append_to_history(session_id, req.question, full_answer)
            log_qa_session(
                session_id=session_id,
                message_id=message_id,
                user_id=uid,
                query_text=req.question,
                answer_text=full_answer or None,
                retrieved_docs=chunks,
                latency_ms=int((time.time() - t0) * 1000),
                retrieval_latency_ms=retrieval_latency_ms,
                answer_status=answer_status,
                model_name=model_name,
                opensearch_hit_count=len(chunks),
                top_score=top_score,
                error_message=error_message,
                content_blocks_json=content_blocks_json_str,
            )

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
async def submit_feedback(req: FeedbackRequest, identity: Optional[Identity] = Depends(current_identity)):
    """反馈接口 — 供前端/管理后台使用。"""
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
