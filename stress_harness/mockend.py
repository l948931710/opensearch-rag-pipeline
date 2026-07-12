# -*- coding: utf-8 -*-
"""mockend — 单端口可控 mock 后端（DashScope chat + embedding + OpenSearch + 告警 webhook）。

一个 ThreadingHTTPServer 按路径分发四种角色：
  - POST */chat/completions            → mock DashScope 兼容模式（流式 + 非流式，含 tool_calls）
  - POST */services/embeddings/*       → mock DashScope 原生 embedding（dense+sparse）
  - GET/POST */_search                 → mock OpenSearch（_search_chunks_opensearch 消费）
  - POST /robot/send*                  → mock 钉钉告警 webhook（alerting.send_ops_alert 投递点）
  - GET  /__stats                      → 计数器快照（放大系数等测量的真相源）

故障注入（场景代码直接改 `MockBackend.cfg`，无需 HTTP 控制面——mock 与 runner 同进程）：
延迟/抖动、429/500 窗口（可按 model 过滤，观测 max→plus fallback）、空补全模式
（触发 loop 的 5× empty-retry）、挂起。所有请求计数带单调时钟时间戳，支持按窗口切片。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

_ANSWER = ("根据知识库检索结果，PET 吸塑托盘的成型温度应控制在 120–140℃ 区间，"
           "预热段 6–8 秒；脱模后需在常温静置 30 分钟以上再入库堆叠，防止应力变形。"
           "详细参数见《吸塑车间工艺规程》第 3.2 节与设备点检表。")
_REASONING = "先检索工艺规程，再核对温度与静置时长两个关键参数，最后按引用格式组织答案。"


@dataclass
class MockConfig:
    """可热改的行为配置（runner/场景线程改，handler 线程读；字段级原子读写足够）。"""

    llm_latency_s: float = 0.3          # 每次模型调用的基础时延
    tool_turns: int = 1                 # 先出几轮 knowledge_search tool_call 再给终稿
    answer_text: str = _ANSWER
    reasoning_text: str = _REASONING
    prompt_tokens: int = 800
    completion_tokens: int = 120
    empty_completion: bool = False      # 空补全（text='' 且无 tool_calls）→ 触发 empty-retry
    fail_status: int = 0                # 0=关闭；429/500 → 窗口内该状态拒绝
    fail_until: float = 0.0             # time.monotonic() 截止；<=now 视为关闭
    fail_models: tuple = ()             # 仅这些 model 生效（空=全部）
    emb_latency_s: float = 0.02
    search_latency_s: float = 0.05
    search_hits: int = 3


class MockBackend:
    """启动/停止 mock 服务器 + 线程安全计数器。"""

    def __init__(self, port: int = 0):
        self.cfg = MockConfig()
        self._lock = threading.Lock()
        self.llm_calls: List[Dict[str, Any]] = []
        self.emb_calls: List[Dict[str, Any]] = []
        self.search_calls: List[Dict[str, Any]] = []
        self.webhook_calls: List[Dict[str, Any]] = []
        backend = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):   # noqa: N802 — 静默访问日志
                pass

            def _body(self) -> Dict[str, Any]:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    return json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    return {}

            def _json(self, obj: Any, status: int = 200) -> None:
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            # ── 分发 ─────────────────────────────────────────────
            def do_POST(self):   # noqa: N802
                path = self.path.split("?")[0]
                if path.endswith("/chat/completions"):
                    backend._handle_llm(self)
                elif "/services/embeddings/" in path:
                    backend._handle_embedding(self)
                elif path.endswith("/_search"):
                    backend._handle_search(self)
                elif path.startswith("/robot/send"):
                    body = self._body()
                    with backend._lock:
                        backend.webhook_calls.append({"t": time.monotonic(), "body": body})
                    self._json({"errcode": 0, "errmsg": "ok"})
                else:
                    self._json({}, 200)   # 宽容兜底（opensearch-py 握手等）

            def do_GET(self):   # noqa: N802
                path = self.path.split("?")[0]
                if path == "/__stats":
                    self._json(backend.stats())
                elif path.endswith("/_search"):
                    backend._handle_search(self)
                else:
                    # opensearch-py 版本握手等 GET / → 给个像样的版本对象
                    self._json({"version": {"number": "2.12.0", "distribution": "opensearch"}})

            def do_HEAD(self):   # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread: Optional[threading.Thread] = None

    # ── 生命周期 ─────────────────────────────────────────────────
    def start(self) -> "MockBackend":
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="mock-backend", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    # ── 统计 ────────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"llm": len(self.llm_calls), "emb": len(self.emb_calls),
                    "search": len(self.search_calls), "webhook": len(self.webhook_calls)}

    def reset_stats(self) -> None:
        with self._lock:
            self.llm_calls.clear()
            self.emb_calls.clear()
            self.search_calls.clear()
            self.webhook_calls.clear()

    def counts_since(self, t0: float) -> Dict[str, int]:
        with self._lock:
            return {"llm": sum(1 for c in self.llm_calls if c["t"] >= t0),
                    "emb": sum(1 for c in self.emb_calls if c["t"] >= t0),
                    "search": sum(1 for c in self.search_calls if c["t"] >= t0),
                    "webhook": sum(1 for c in self.webhook_calls if c["t"] >= t0)}

    def fail_window(self, status: int, seconds: float, models: tuple = ()) -> None:
        """开一段故障窗口（429/500）。models 非空时只对这些模型生效。"""
        self.cfg.fail_models = models
        self.cfg.fail_status = status
        self.cfg.fail_until = time.monotonic() + seconds

    def clear_faults(self) -> None:
        self.cfg.fail_status = 0
        self.cfg.fail_until = 0.0
        self.cfg.fail_models = ()
        self.cfg.empty_completion = False

    # ── LLM ─────────────────────────────────────────────────────
    def _handle_llm(self, h) -> None:
        cfg = self.cfg
        body = h._body()
        model = str(body.get("model") or "")
        stream = bool(body.get("stream"))
        thinking = bool(body.get("enable_thinking"))
        msgs = body.get("messages") or []
        has_tools = bool(body.get("tools"))
        tool_rounds = sum(1 for m in msgs if m.get("role") == "tool")

        now = time.monotonic()
        failing = (cfg.fail_status and now < cfg.fail_until
                   and (not cfg.fail_models or model in cfg.fail_models))
        if failing:
            with self._lock:
                self.llm_calls.append({"t": now, "model": model, "stream": stream,
                                       "thinking": thinking, "kind": "fail",
                                       "status": cfg.fail_status})
            h._json({"error": {"message": "mock overload", "code": "throttled"}},
                    status=cfg.fail_status)
            return

        time.sleep(max(0.0, cfg.llm_latency_s))
        if cfg.empty_completion:
            kind = "empty"
        elif has_tools and tool_rounds < cfg.tool_turns:
            kind = "tool_call"
        else:
            kind = "final"
        with self._lock:
            self.llm_calls.append({"t": now, "model": model, "stream": stream,
                                   "thinking": thinking, "kind": kind, "status": 200})

        query = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                query = str(m.get("content") or "")[:80]
                break
        usage = {"prompt_tokens": cfg.prompt_tokens, "completion_tokens": cfg.completion_tokens,
                 "total_tokens": cfg.prompt_tokens + cfg.completion_tokens}

        if not stream:
            if kind == "tool_call":
                msg = {"content": None, "tool_calls": [
                    {"id": f"call_mock_{tool_rounds + 1}", "type": "function",
                     "function": {"name": "knowledge_search",
                                  "arguments": json.dumps({"query": query},
                                                          ensure_ascii=False)}}]}
                finish = "tool_calls"
            elif kind == "empty":
                msg, finish = {"content": ""}, "stop"
            else:
                msg = {"content": cfg.answer_text}
                if thinking:
                    msg["reasoning_content"] = cfg.reasoning_text
                finish = "stop"
            h._json({"model": model, "choices": [{"message": msg, "finish_reason": finish}],
                     "usage": usage})
            return

        # 流式：data: 帧 → usage 终帧 → [DONE]（与 DashScope include_usage 行为一致）
        frames: List[Dict[str, Any]] = []
        if kind == "tool_call":
            args = json.dumps({"query": query}, ensure_ascii=False)
            frames.append({"model": model, "choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": f"call_mock_{tool_rounds + 1}",
                 "function": {"name": "knowledge_search", "arguments": args[:12]}}]},
                "finish_reason": None}]})
            frames.append({"model": model, "choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": args[12:]}}]},
                "finish_reason": None}]})
            frames.append({"model": model,
                           "choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        elif kind == "empty":
            frames.append({"model": model, "choices": [{"delta": {}, "finish_reason": "stop"}]})
        else:
            if thinking:
                frames.append({"model": model, "choices": [
                    {"delta": {"reasoning_content": cfg.reasoning_text},
                     "finish_reason": None}]})
            text = cfg.answer_text
            step = max(1, len(text) // 4)
            for i in range(0, len(text), step):
                frames.append({"model": model, "choices": [
                    {"delta": {"content": text[i:i + step]}, "finish_reason": None}]})
            frames.append({"model": model, "choices": [{"delta": {}, "finish_reason": "stop"}]})
        frames.append({"model": model, "choices": [], "usage": usage})

        payload = b"".join(b"data: " + json.dumps(f, ensure_ascii=False).encode("utf-8")
                           + b"\n\n" for f in frames) + b"data: [DONE]\n\n"
        h.send_response(200)
        # charset 必带：requests iter_lines(decode_unicode=True) 对无 charset 的 text/*
        # 按 latin-1 解码 → 中文 query 乱码 → 投机检索 query_matches_question 永 miss
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        try:
            h.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass   # S5/S8 弃单场景客户端中途断连——预期内，非错误

    # ── Embedding ───────────────────────────────────────────────
    def _handle_embedding(self, h) -> None:
        cfg = self.cfg
        body = h._body()
        texts = ((body.get("input") or {}).get("texts")) or [""]
        dim = int(((body.get("parameters") or {}).get("dimension")) or 1024)
        with self._lock:
            self.emb_calls.append({"t": time.monotonic(), "n_texts": len(texts)})
        time.sleep(max(0.0, cfg.emb_latency_s))
        embs = [{"text_index": i, "embedding": [0.1] * dim,
                 "sparse_embedding": [{"index": 0, "token": "mock", "value": 0.001}]}
                for i in range(len(texts))]
        h._json({"output": {"embeddings": embs}, "usage": {"total_tokens": 8}})

    # ── OpenSearch ──────────────────────────────────────────────
    def _handle_search(self, h) -> None:
        cfg = self.cfg
        with self._lock:
            self.search_calls.append({"t": time.monotonic()})
        time.sleep(max(0.0, cfg.search_latency_s))
        hits = []
        for i in range(cfg.search_hits):
            hits.append({"_id": f"mockchunk-{i}", "_score": 9.5 - i, "_source": {
                "chunk_id": f"mockchunk-{i}", "id": str(1000 + i), "doc_id": "mockdoc-1",
                "chunk_text": f"【工艺规程 §3.{i + 1}】" + _ANSWER,
                "title": "吸塑车间工艺规程", "section_title": f"3.{i + 1} 成型参数",
                "chunk_index": i, "page_num": i + 3, "kb_type": "public",
                "permission_level": "public", "owner_dept": "production",
                "category_l1": "生产工艺", "chunk_type": "text",
                "source_image": "", "visual_summary": "",
            }})
        h._json({"took": 1, "timed_out": False,
                 "hits": {"total": {"value": len(hits)}, "hits": hits}})


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p
