# -*- coding: utf-8 -*-
"""sse — SSE 消费端：逐帧打点 + 帧序校验 + 提前断开钩子。

服务端线协议（routes/agent.py::_stream_events 与 api.py::/api/ask/stream 同族）：
`data: {json}\n\n` 纯数据帧，JSON 带 type 字段
（session/chunk/tool_call/tool_result/sources/approval/done/content_blocks/error/reasoning），
终止符 `data: [DONE]`。帧序不变量（G6/S0 断言）：
  ① session 必为首帧；② done 之后只允许 content_blocks 与 [DONE]；
  ③ tool_result 必与前序 tool_call 的 call_id 配对；④ error 与 done 互斥（同流二者取一）。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx


@dataclass
class AskResult:
    """一次 /api/agent/ask 或 /api/ask/stream 的完整观测。时间全为相对请求发出的秒。"""

    status: int = 0
    t_start: float = 0.0                  # time.monotonic() 起点（绝对）
    ttfb_s: Optional[float] = None        # 首字节
    t_session_s: Optional[float] = None   # session 帧
    t_first_chunk_s: Optional[float] = None
    t_first_tool_call_s: Optional[float] = None
    t_first_tool_result_s: Optional[float] = None
    t_done_s: Optional[float] = None
    t_total_s: Optional[float] = None     # 流关闭
    frames: List[Dict[str, Any]] = field(default_factory=list)   # [{type, t, ...摘要}]
    run_id: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    tool_elapsed_ms: List[int] = field(default_factory=list)
    error_message: str = ""
    body_text: str = ""                   # 非 200 时的响应体（429/503 归因）
    order_violations: List[str] = field(default_factory=list)
    aborted: bool = False                 # 我们主动断开（abandon 场景）
    transport_error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.t_done_s is not None and not self.order_violations

    def frame_types(self) -> List[str]:
        return [f["type"] for f in self.frames]


def _validate_order(res: AskResult) -> None:
    types = res.frame_types()
    if types and types[0] != "session":
        res.order_violations.append(f"首帧={types[0]}（应为 session）")
    if "done" in types:
        after = types[types.index("done") + 1:]
        bad = [t for t in after if t not in ("content_blocks",)]
        if bad:
            res.order_violations.append(f"done 之后出现 {bad}")
        if "error" in types:
            res.order_violations.append("done 与 error 同流")
    pending: List[str] = []
    for f in res.frames:
        if f["type"] == "tool_call":
            pending.append(f.get("call_id") or "")
        elif f["type"] == "tool_result":
            cid = f.get("call_id") or ""
            if cid in pending:
                pending.remove(cid)
            else:
                res.order_violations.append(f"tool_result 无配对 tool_call: {cid}")


async def ask_sse(client: httpx.AsyncClient, url: str, token: str, payload: Dict[str, Any],
                  abandon_after: Optional[str] = None,
                  timeout_s: float = 120.0) -> AskResult:
    """发起一次 SSE 问答并逐帧打点。

    abandon_after：收到指定 type 的帧后立即断开连接（S5 弃单风暴）——服务端设计上
    **不会**因此取消 run（落库在 executor 完成侧），本函数只负责制造断开。
    """
    res = AskResult(t_start=time.monotonic())
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with client.stream("POST", url, json=payload, headers=headers,
                                 timeout=httpx.Timeout(timeout_s, connect=10.0)) as resp:
            res.status = resp.status_code
            if resp.status_code != 200:
                res.body_text = (await resp.aread()).decode("utf-8", "replace")[:300]
                return res
            buf = b""
            async for chunk in resp.aiter_bytes():
                now = time.monotonic() - res.t_start
                if res.ttfb_s is None:
                    res.ttfb_s = now
                buf += chunk
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    line = block.strip()
                    if not line.startswith(b"data:"):
                        continue
                    payload_b = line[5:].strip()
                    if payload_b == b"[DONE]":
                        continue
                    try:
                        obj = json.loads(payload_b.decode("utf-8"))
                    except ValueError:
                        continue
                    ftype = str(obj.get("type") or "?")
                    entry: Dict[str, Any] = {"type": ftype, "t": now}
                    if ftype == "session":
                        res.t_session_s = res.t_session_s or now
                        res.run_id = obj.get("run_id") or ""
                    elif ftype == "chunk":
                        res.t_first_chunk_s = res.t_first_chunk_s or now
                    elif ftype == "tool_call":
                        res.t_first_tool_call_s = res.t_first_tool_call_s or now
                        entry["call_id"] = obj.get("call_id")
                    elif ftype == "tool_result":
                        res.t_first_tool_result_s = res.t_first_tool_result_s or now
                        entry["call_id"] = obj.get("call_id")
                        entry["status"] = obj.get("status")
                        if obj.get("elapsed_ms") is not None:
                            res.tool_elapsed_ms.append(int(obj["elapsed_ms"]))
                    elif ftype == "done":
                        res.t_done_s = now
                        res.usage = obj.get("usage") or {}
                    elif ftype == "error":
                        res.error_message = str(obj.get("message") or "")[:200]
                    res.frames.append(entry)
                    if abandon_after and ftype == abandon_after:
                        res.aborted = True
                        raise _Abandon()
    except _Abandon:
        pass
    except (httpx.HTTPError, httpx.StreamError) as e:
        res.transport_error = f"{type(e).__name__}: {e}"[:200]
    res.t_total_s = time.monotonic() - res.t_start
    _validate_order(res)
    return res


class _Abandon(Exception):
    """内部信号：abandon_after 命中，主动断流。"""


def parse_sse_text(text: str) -> List[Dict[str, Any]]:
    """单测用：解析一段完整 SSE 文本为 JSON 帧列表（忽略 [DONE]）。"""
    frames = []
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            continue
        try:
            frames.append(json.loads(body))
        except ValueError:
            continue
    return frames
