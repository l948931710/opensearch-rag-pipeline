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
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from opensearch_pipeline.llm_generator import generate_answer, generate_answer_stream
from opensearch_pipeline.retriever import search_chunks

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════════

app = FastAPI(
    title="RAG 知识库问答 API",
    description="基于 OpenSearch HA3 向量检索 + Qwen LLM 的 RAG 问答服务",
    version="0.1.0",
)

# CORS — 开发阶段允许所有来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════
# Pydantic Models
# ═══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role: str = Field(..., description="消息角色: user / assistant")
    content: str = Field(..., description="消息内容")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    top_k: int = Field(5, ge=1, le=20, description="检索返回的文档数量")
    history: Optional[List[ChatMessage]] = Field(None, description="对话历史")
    session_id: Optional[str] = Field(None, description="会话 ID，用于追踪对话")
    temperature: Optional[float] = Field(0.1, ge=0.0, le=2.0, description="生成温度")
    max_tokens: Optional[int] = Field(2048, ge=100, le=8192, description="最大生成 token 数")


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="搜索查询")
    top_k: int = Field(5, ge=1, le=50, description="返回结果数")


class SourceInfo(BaseModel):
    doc_id: str
    title: str
    section: str = ""
    score: float = 0.0


class AskResponse(BaseModel):
    answer: str
    sources: List[SourceInfo]
    session_id: str
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
# 内存会话存储（测试用，生产应替换为 Redis）
# ═══════════════════════════════════════════════════════════════

_sessions: Dict[str, List[Dict[str, str]]] = {}

MAX_HISTORY_TURNS = 10  # 保留最近 N 轮对话


def _get_or_create_session(session_id: Optional[str]) -> tuple[str, List[Dict[str, str]]]:
    """获取或创建会话，返回 (session_id, history)。"""
    if session_id and session_id in _sessions:
        return session_id, _sessions[session_id]

    sid = session_id or str(uuid.uuid4())
    _sessions[sid] = []
    return sid, _sessions[sid]


def _append_to_history(session_id: str, user_msg: str, assistant_msg: str):
    """将当前轮对话追加到会话历史。"""
    if session_id not in _sessions:
        _sessions[session_id] = []

    history = _sessions[session_id]
    history.append({"role": "user", "content": user_msg})
    history.append({"role": "assistant", "content": assistant_msg})

    # 裁剪超出的轮数（保留最近 N 轮 = 2N 条消息）
    max_messages = MAX_HISTORY_TURNS * 2
    if len(history) > max_messages:
        _sessions[session_id] = history[-max_messages:]


# ═══════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════

@app.get("/api/health")
async def health_check():
    """健康检查。"""
    return {"status": "ok", "service": "rag-qa-api"}


@app.post("/api/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    """纯检索接口 — 只返回相关文档片段，不调用 LLM。"""
    t0 = time.time()
    try:
        results = search_chunks(req.query, top_k=req.top_k)
    except Exception as e:
        logger.error("Search failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败: {e}")

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
        chunks = search_chunks(req.question, top_k=req.top_k)
    except Exception as e:
        logger.error("Search failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败: {e}")

    if not chunks:
        latency = int((time.time() - t0) * 1000)
        return AskResponse(
            answer="抱歉，当前知识库中未找到与您问题相关的信息。请尝试换一种方式描述您的问题。",
            sources=[],
            session_id=session_id,
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
        logger.error("LLM generation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"回答生成失败: {e}")

    # 4. 更新会话历史
    _append_to_history(session_id, req.question, result["answer"])

    latency = int((time.time() - t0) * 1000)
    return AskResponse(
        answer=result["answer"],
        sources=[SourceInfo(**s) for s in result["sources"]],
        session_id=session_id,
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
        chunks = search_chunks(req.question, top_k=req.top_k)
    except Exception as e:
        logger.error("Search failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"检索失败: {e}")

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
            logger.error("Stream generation failed: %s", e, exc_info=True)
            error_msg = json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False)
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
