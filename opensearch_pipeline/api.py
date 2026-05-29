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
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from opensearch_pipeline.llm_generator import generate_answer, generate_answer_stream
from opensearch_pipeline.retriever import search_chunks, retrieve_and_enrich
from opensearch_pipeline.dingtalk_bot import router as dingtalk_router, _resolve_user_dept
from opensearch_pipeline.qa_logger import generate_message_id, log_qa_session
from opensearch_pipeline.feedback_handler import handle_feedback

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="RAG 知识库问答 API",
    description="基于 OpenSearch HA3 向量检索 + Qwen LLM 的 RAG 问答服务",
    version="0.1.0",
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
    user_id: Optional[str] = Field(None, description="用户 ID（钉钉 staffId），用于权限过滤")
    user_dept: Optional[str] = Field(
        None,
        description="用户部门代码，直接传入时优先使用",
        pattern=r'^[\w\-\u4e00-\u9fff]{0,64}$',
    )


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="搜索查询")
    top_k: int = Field(5, ge=1, le=50, description="返回结果数")
    user_id: Optional[str] = Field(None, description="用户 ID（钉钉 staffId），用于权限过滤")
    user_dept: Optional[str] = Field(
        None,
        description="用户部门代码，直接传入时优先使用",
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


# ═══════════════════════════════════════════════════════════════
# 会话存储（从 session_store 模块导入，与 dingtalk_bot 共用）
# ═══════════════════════════════════════════════════════════════

from opensearch_pipeline.session_store import get_or_create_session, append_to_history

# 保持向后兼容（内部调用仍使用下划线命名）
_get_or_create_session = get_or_create_session
_append_to_history = append_to_history


# ═══════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health_check():
    """健康检查。"""
    return {"status": "ok", "service": "rag-qa-api"}


@app.get("/api/debug/rds")
async def debug_rds():
    """诊断 RDS 连接（临时调试用，上线前删除）。"""
    import socket
    host = os.environ.get("RAG_RDS_HOST", "localhost")
    port = int(os.environ.get("RAG_RDS_PORT", "3306"))
    result = {"host": host, "port": port}

    # 1. DNS 解析
    try:
        ips = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        result["dns_resolved"] = [addr[4][0] for addr in ips]
    except Exception as e:
        result["dns_error"] = str(e)
        return result

    # 2. TCP 连接测试
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.close()
        result["tcp_connect"] = "OK"
    except Exception as e:
        result["tcp_error"] = str(e)

    # 3. PyMySQL 连接测试
    try:
        import pymysql
        conn = pymysql.connect(
            host=host,
            port=port,
            user=os.environ.get("RAG_RDS_USER", ""),
            password=os.environ.get("RAG_RDS_PASSWORD", ""),
            database=os.environ.get("RAG_RDS_DATABASE", ""),
            connect_timeout=5,
        )
        result["mysql_connect"] = "OK"
        conn.close()
    except Exception as e:
        result["mysql_error"] = str(e)

    return result


@app.post("/api/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    """纯检索接口 — 只返回相关文档片段，不调用 LLM。"""
    t0 = time.time()
    try:
        user_dept = req.user_dept
        if not user_dept and req.user_id:
            user_dept = _resolve_user_dept(req.user_id)
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
async def ask(req: AskRequest):
    """非流式问答接口 — 检索 + LLM 一次性返回。"""
    t0 = time.time()

    # 1. 会话管理
    session_id, session_history = _get_or_create_session(req.session_id)

    # 合并客户端传入的 history 和服务端 session history
    merged_history = list(session_history)
    if req.history:
        # 客户端显式传了 history 则优先使用
        merged_history = [{"role": m.role, "content": m.content} for m in req.history]

    # 2. 检索
    try:
        user_dept = req.user_dept
        if not user_dept and req.user_id:
            user_dept = _resolve_user_dept(req.user_id)
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
            user_id=req.user_id or "",
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

    return AskResponse(
        answer=result["answer"],
        sources=[SourceInfo(**s) for s in result["sources"]],
        session_id=session_id,
        message_id=msg_id,
        model=result["model"],
        usage=result["usage"],
        latency_ms=latency,
    )


@app.post("/api/ask/stream")
async def ask_stream(req: AskRequest):
    """SSE 流式问答接口 — 检索 + LLM 逐 token 输出。

    SSE 事件格式：
        data: {"type": "sources", "sources": [...]}
        data: {"type": "chunk", "content": "..."}
        data: {"type": "done", "model": "...", "usage": {...}}
        data: [DONE]
    """
    # 1. 会话管理
    session_id, session_history = _get_or_create_session(req.session_id)

    merged_history = list(session_history)
    if req.history:
        merged_history = [{"role": m.role, "content": m.content} for m in req.history]

    # 2. 检索（同步阶段，在生成器外完成）
    try:
        user_dept = req.user_dept
        if not user_dept and req.user_id:
            user_dept = _resolve_user_dept(req.user_id)
        chunks = retrieve_and_enrich(req.question, top_k=req.top_k, user_dept=user_dept)
    except Exception as e:
        trace_id = uuid.uuid4().hex[:8]
        logger.error("Search failed [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败，请联系管理员 (trace: {trace_id})")

    if not chunks:
        import json

        async def empty_gen():
            yield f"data: {json.dumps({'type': 'chunk', 'content': '抱歉，当前知识库中未找到与您问题相关的信息。'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'model': 'N/A', 'usage': {}}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(empty_gen(), media_type="text/event-stream")

    # 3. SSE 流式生成
    def event_generator():
        import json

        # 先发 session_id
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id}, ensure_ascii=False)}\n\n"

        collected_answer = []
        try:
            for event in generate_answer_stream(
                req.question,
                chunks,
                history=merged_history if merged_history else None,
                max_tokens=req.max_tokens or 2048,
                temperature=req.temperature or 0.1,
            ):
                yield event

                # 收集完整回答用于写入 history
                if "chunk" in event and '"type": "chunk"' in event:
                    try:
                        payload = event.replace("data: ", "", 1).strip()
                        d = json.loads(payload)
                        if d.get("type") == "chunk" and d.get("content"):
                            collected_answer.append(d["content"])
                    except (json.JSONDecodeError, KeyError):
                        pass
        except Exception as e:
            trace_id = uuid.uuid4().hex[:8]
            logger.error("Stream generation failed [trace=%s]: %s", trace_id, e, exc_info=True)
            error_msg = json.dumps({"type": "error", "message": f"回答生成失败，请联系管理员 (trace: {trace_id})"}, ensure_ascii=False)
            yield f"data: {error_msg}\n\n"
            yield "data: [DONE]\n\n"
            return

        # 写入会话历史
        full_answer = "".join(collected_answer)
        if full_answer:
            _append_to_history(session_id, req.question, full_answer)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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
async def submit_feedback(req: FeedbackRequest):
    """反馈接口 — 供前端/管理后台使用。"""
    success = handle_feedback(
        message_id=req.message_id,
        user_id=req.user_id,
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
