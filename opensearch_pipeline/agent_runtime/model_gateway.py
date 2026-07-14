# -*- coding: utf-8 -*-
"""
model_gateway.py — 境内多 provider Model Gateway（v2 报告 §7 模块 F）

硬约束 1/3：模型层 provider 无关、**仅限境内**；业务代码只声明 task_category，不出现
provider/模型名（allowlist 与链内容全在配置）。境外模型一律不用（config 已有生产禁 Gemini 守卫）。

本 Gateway：
- `ModelProvider` 契约（每个境内 provider/自托管端点一个实现）；`DashScopeProvider` 复用 http_session；
- category（deep/default/quick/sql）→ fallbackChain 路由；解析期选链 + 运行期沿链 fallback；
- per-provider 熔断器（借 cost_breaker 形态）；
- `make_model_fn` 把 Gateway 适配成 DefaultAgentLoop 的 model_fn（ChatResponse→ModelTurn）。

作用域（本批）：**新建 Gateway 供 agent 链路用；不碰 live RAG 主链路的 4 处手写 chat**——
报告 §7 明确其收敛属 WS2（降低爆炸半径）。DEFER（已注）：提示词模拟 function-calling
（DashScope qwen 原生支持 tool_calls）。已落地：llm_call_log 记账（023）、**chat_stream 真流式**
（complete_stream：SSE 增量 → loop ModelDelta → console 打字机；RAG_AGENT_STREAM 默认开）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from opensearch_pipeline.agent_runtime.context import ExecutionContext
from opensearch_pipeline.agent_runtime.events import Usage
from opensearch_pipeline.agent_runtime.loop import ModelTurn, Msg, ProposedCall
from opensearch_pipeline.agent_runtime.tool import ToolSpec
from opensearch_pipeline.http_session import http_post as _http_post

logger = logging.getLogger(__name__)


# ── 类型 ─────────────────────────────────────────────────────────
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class ChatRequest:
    messages: List[Msg]                       # OpenAI 格式消息
    tools: Optional[List[Dict[str, Any]]] = None   # OpenAI 格式 tool schema（None=无工具）
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResponse:
    text: str
    tool_calls: List[ToolCall]
    usage: Usage
    model: str
    finish_reason: str = ""
    reasoning: str = ""        # 思考模式 reasoning_content（非流式一并返回；light 档为空）
    # 线上原始 usage dict（含 total_tokens / prompt_tokens_details.cached_tokens 等 Usage
    # 类型化视图之外的字段）——serving 流式收敛（Tier B）要把 done 帧 usage 逐字节保真下发。
    usage_raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StreamDelta:
    """真流式的一个增量帧（provider.chat_stream / gateway.complete_stream 逐个 yield；
    loop 转 ModelDelta 事件 → SSE chunk 打字机）。"""

    text: str = ""
    reasoning: str = ""


@dataclass(frozen=True)
class ModelCapabilities:
    native_tool_calls: bool
    json_mode: bool
    thinking: bool
    context_window: int
    supports_stream: bool


class ModelError(RuntimeError):
    """provider 调用错误。retryable=True 时 Gateway 沿链 fallback。"""

    def __init__(self, message: str, *, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ModelUnavailable(RuntimeError):
    """整条 fallback 链都不可用（对用户明确降级文案，复用钉钉/console 三级降级）。"""


class BudgetExceeded(RuntimeError):
    """run 预算已耗尽（deadline 过期）→ fail-closed 拒绝新模型调用。"""


class ModelProvider(Protocol):
    name: str

    def capabilities(self, model: str) -> ModelCapabilities: ...

    def chat(self, model: str, req: ChatRequest) -> ChatResponse: ...


# ── DashScope provider（境内，OpenAI 兼容）─────────────────────────
def _is_retryable_status(status: int) -> bool:
    # 429 限流 / 5xx 服务端 → 沿链重试；4xx（除 429）= 配置/鉴权错，不重试
    return status == 429 or 500 <= status < 600


class DashScopeProvider:
    """境内 DashScope（compatible-mode，OpenAI 格式）。端点/密钥来自 config.llm.*。"""

    name = "dashscope"

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = 60.0):
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        self._key = api_key if api_key is not None else cfg.llm.api_key
        self._base = (base_url if base_url is not None else cfg.llm.api_base_url).rstrip("/")
        self._timeout = timeout

    def capabilities(self, model: str) -> ModelCapabilities:
        m = model.lower()
        return ModelCapabilities(
            native_tool_calls=m.startswith("qwen"),   # qwen3.x 原生 function-calling
            json_mode=True,
            thinking="max" in m or "think" in m,
            context_window=131072 if "max" in m else 32768,
            supports_stream=True,
        )

    def _body(self, model: str, req: ChatRequest) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": model, "messages": req.messages,
                                "temperature": req.temperature}
        if req.max_tokens:
            body["max_tokens"] = req.max_tokens
        if req.tools:
            body["tools"] = req.tools
            body["tool_choice"] = "auto"
        body.update(req.extra)
        return body

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def chat(self, model: str, req: ChatRequest) -> ChatResponse:
        url = f"{self._base}/chat/completions"
        try:
            resp = _http_post(url, json=self._body(model, req), headers=self._headers(),
                              timeout=self._timeout)
        except Exception as e:   # 连接/超时 → 可重试
            raise ModelError(f"dashscope 传输失败: {e}", retryable=True) from e
        if resp.status_code != 200:
            raise ModelError(f"dashscope HTTP {resp.status_code}: {resp.text[:200]}",
                             retryable=_is_retryable_status(resp.status_code), status=resp.status_code)
        return _parse_openai_response(resp.json(), model)

    def chat_stream(self, model: str, req: ChatRequest):
        """真流式（OpenAI 兼容 SSE）：生成器 yield StreamDelta，return 组装好的 ChatResponse。

        - `stream_options.include_usage`：终帧携带 usage（预算/记账不因流式丢账）；
        - tool_calls 以分片（index + arguments 字符串片段）到达 → 按 index 累积、结束时整体
          json.loads（与非流式 _parse_openai_response 同样的容错：坏 JSON → {}）；
        - 首字节前的失败（连接/非 200）抛可重试 ModelError（gateway 可 retry/fallback）；
          流中断（已下发增量后）同样抛 ModelError——但 gateway 见「已下发」不再重试（拼接流不可行）。
        """
        url = f"{self._base}/chat/completions"
        body = self._body(model, req)
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        try:
            resp = _http_post(url, json=body, headers=self._headers(),
                              timeout=self._timeout, stream=True)
        except Exception as e:
            raise ModelError(f"dashscope 传输失败: {e}", retryable=True) from e
        if resp.status_code != 200:
            try:
                detail = resp.text[:200]
            finally:
                resp.close()
            raise ModelError(f"dashscope HTTP {resp.status_code}: {detail}",
                             retryable=_is_retryable_status(resp.status_code),
                             status=resp.status_code)
        text_parts: List[str] = []
        reason_parts: List[str] = []
        frags: Dict[int, Dict[str, Any]] = {}       # index → {id, name, args(str 累积)}
        usage = Usage()
        usage_raw: Dict[str, Any] = {}
        finish = ""
        got_model = model
        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue                        # 坏帧跳过（fail-open，终帧 usage/组装兜底）
                got_model = data.get("model") or got_model
                u = data.get("usage")
                if u:                               # include_usage 终帧（choices 为空）
                    usage = Usage(tokens_prompt=int(u.get("prompt_tokens") or 0),
                                  tokens_completion=int(u.get("completion_tokens") or 0))
                    usage_raw = u
                choice = (data.get("choices") or [{}])[0]
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta") or {}
                for tc in (delta.get("tool_calls") or []):
                    idx = int(tc.get("index") or 0)
                    slot = frags.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
                txt = delta.get("content") or ""
                rsn = delta.get("reasoning_content") or ""
                if txt:
                    text_parts.append(txt)
                if txt or rsn:
                    yield StreamDelta(text=txt, reasoning=rsn)
                if rsn:
                    reason_parts.append(rsn)
        except ModelError:
            raise
        except Exception as e:                      # 流中断（读超时/连接重置）
            raise ModelError(f"dashscope 流中断: {e}", retryable=True) from e
        finally:
            try:
                resp.close()
            except Exception:   # noqa: BLE001
                pass
        tool_calls: List[ToolCall] = []
        for idx in sorted(frags):
            slot = frags[idx]
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
                if not isinstance(args, dict):
                    args = {}
            except ValueError:
                args = {}
            tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))
        return ChatResponse(text="".join(text_parts), tool_calls=tool_calls, usage=usage,
                            model=got_model, finish_reason=finish,
                            reasoning="".join(reason_parts), usage_raw=usage_raw)


def _parse_openai_response(data: Dict[str, Any], model: str) -> ChatResponse:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    tool_calls: List[ToolCall] = []
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (ValueError, TypeError):
            args = {}
        tool_calls.append(ToolCall(id=tc.get("id") or "", name=fn.get("name") or "", arguments=args))
    u = data.get("usage") or {}
    usage = Usage(tokens_prompt=int(u.get("prompt_tokens") or 0),
                  tokens_completion=int(u.get("completion_tokens") or 0))
    return ChatResponse(text=text, tool_calls=tool_calls, usage=usage,
                        model=data.get("model") or model,
                        finish_reason=choice.get("finish_reason") or "",
                        reasoning=msg.get("reasoning_content") or "",
                        usage_raw=u)


# ── 熔断器（per provider+model，借 cost_breaker 形态）───────────────
class _Breaker:
    def __init__(self, threshold: int = 5, cooldown_s: float = 30.0):
        self._t = threshold
        self._cd = cooldown_s
        self._fail: Dict[str, int] = {}
        self._open_until: Dict[str, float] = {}

    def is_open(self, key: str) -> bool:
        until = self._open_until.get(key, 0.0)
        if until and time.monotonic() < until:
            return True
        return False

    def record_success(self, key: str) -> None:
        self._fail.pop(key, None)
        self._open_until.pop(key, None)

    def record_failure(self, key: str) -> None:
        n = self._fail.get(key, 0) + 1
        self._fail[key] = n
        if n >= self._t:
            self._open_until[key] = time.monotonic() + self._cd


# ── 路由 ─────────────────────────────────────────────────────────
RouteEntry = Tuple[str, str]     # (provider_name, model)

# 按**思考深度**分档（正交：模型档 + thinking 参数）。light=不思考(快/省)；high/xhigh/max=开
# 思考、预算递增。模型 ID **须小写**（真调验证：qwen3.7-plus / qwen3.7-max-2026-06-08 可用；
# 大写 Qwen3.7-* 与旧 qwen3.6-turbo 均 404 model_not_found）。高阶档 429/5xx 退 plus。
_DEFAULT_TIER = "light"
_DEFAULT_ROUTES: Dict[str, List[RouteEntry]] = {
    "light": [("dashscope", "qwen3.7-plus")],
    "high":  [("dashscope", "qwen3.7-plus")],
    "xhigh": [("dashscope", "qwen3.7-max-2026-06-08"), ("dashscope", "qwen3.7-plus")],
    "max":   [("dashscope", "qwen3.7-max-2026-06-08"), ("dashscope", "qwen3.7-plus")],
}
# 各档 thinking 参数（合进 DashScope 请求体 extra）。⚠️ qwen3.7 **默认开思考**——light 要真快/省
# 必须**显式 enable_thinking=False**（真调验证：无参数=694字 reasoning、False=0字直接答）。budget 递增。
_DEFAULT_TIER_PARAMS: Dict[str, Dict[str, Any]] = {
    "light": {"enable_thinking": False},
    "high":  {"enable_thinking": True, "thinking_budget": 1024},
    "xhigh": {"enable_thinking": True, "thinking_budget": 4096},
    "max":   {"enable_thinking": True, "thinking_budget": 16384},
}


class ModelGateway:
    """category → 链路路由 + 沿链 fallback + 熔断 + budget deadline fail-closed。

    业务只声明 category；provider/model 全在路由配置（境内 allowlist）。
    """

    def __init__(self, providers: Dict[str, ModelProvider],
                 routes: Optional[Dict[str, List[RouteEntry]]] = None,
                 breaker: Optional[_Breaker] = None,
                 call_logger: Optional[Callable] = None,
                 max_retries: int = 2, backoff_base: float = 0.5,
                 backoff_cap: float = 8.0, sleep_fn: Optional[Callable[[float], None]] = None,
                 tier_params: Optional[Dict[str, Dict[str, Any]]] = None):
        self._providers = providers
        self._routes = routes or _DEFAULT_ROUTES
        self._tier_params = tier_params if tier_params is not None else _DEFAULT_TIER_PARAMS
        self._breaker = breaker or _Breaker()
        self._call_logger = call_logger   # Optional[Callable]：llm_call_log 记账（fail-open）
        # 瞬时错（429/5xx/传输）**同 provider 退避重试**：一次 429 不该整 run 挂（staging 首跑教训）。
        # 耗尽重试才沿链 fallback；sleep_fn 可注入（测试传 no-op 免真 sleep）。
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep_fn if sleep_fn is not None else time.sleep

    def _backoff(self, attempt: int) -> float:
        """指数退避（含轻量抖动，用 monotonic 小数位当无依赖抖动源）。"""
        base = min(self._backoff_cap, self._backoff_base * (2 ** attempt))
        jitter = (time.monotonic() % 0.05)   # 0–50ms 抖动，避免同步重试惊群
        return base + jitter

    def _record_call(self, ctx, category, provider, model, usage, latency_ms, status) -> None:
        if self._call_logger is None:
            return
        try:
            groups = getattr(ctx, "acl_groups", None)
            self._call_logger(
                run_id=getattr(ctx, "run_id", None), request_id=getattr(ctx, "request_id", None),
                provider=provider, model=model, category=category,
                prompt_version=getattr(ctx, "prompt_version", None),
                tokens_prompt=usage.tokens_prompt if usage else None,
                tokens_completion=usage.tokens_completion if usage else None,
                cost_estimate=None, latency_ms=latency_ms, status=status,
                user_id=getattr(ctx, "user_id", None),
                dept_group=(groups[0] if groups else None))
        except Exception:   # noqa: BLE001 — 记账失败绝不影响模型调用
            logger.warning("llm_call_log 记账失败（fail-open）", exc_info=True)

    def _deadline_exceeded(self, ctx: ExecutionContext, now: Optional[Any] = None) -> bool:
        """budget deadline fail-closed 判定（token-cap 由 executor 经 run_store 强制，见 B8）。"""
        if ctx.budget.deadline is None:
            return False
        import datetime as _dt
        cur = now if isinstance(now, _dt.datetime) else _dt.datetime.now(_dt.timezone.utc)
        return ctx.budget.is_past_deadline(cur)

    def complete(self, ctx: ExecutionContext, category: str, req: ChatRequest,
                 now: Optional[float] = None) -> ChatResponse:
        if self._deadline_exceeded(ctx, now):
            raise BudgetExceeded(f"run {ctx.run_id} 已超 deadline")

        chain = self._routes.get(category) or self._routes.get(_DEFAULT_TIER) or []
        # 合并该思考深度档的 thinking 参数进请求体（req.extra 优先，允许调用方覆盖）
        tp = self._tier_params.get(category)
        if tp:
            import dataclasses
            req = dataclasses.replace(req, extra={**tp, **req.extra})
        last_err: Optional[Exception] = None
        for provider_name, model in chain:
            provider = self._providers.get(provider_name)
            if provider is None:
                continue
            key = f"{provider_name}:{model}"
            if self._breaker.is_open(key):
                logger.warning("熔断打开，跳过 %s", key)
                continue
            # 同一 provider **退避重试**（瞬时 429/5xx/传输）；耗尽才沿链 fallback。
            for attempt in range(self._max_retries + 1):
                t0 = time.monotonic()
                try:
                    resp = provider.chat(model, req)
                    self._breaker.record_success(key)
                    self._record_call(ctx, category, provider_name, model, resp.usage,
                                      int((time.monotonic() - t0) * 1000), "ok")
                    return resp
                except ModelError as e:
                    self._record_call(ctx, category, provider_name, model, None,
                                      int((time.monotonic() - t0) * 1000), "error")
                    last_err = e
                    if not e.retryable:
                        raise   # 非可重试（配置/鉴权）→ 立即抛
                    self._breaker.record_failure(key)
                    if attempt < self._max_retries and not self._deadline_exceeded(ctx, now):
                        logger.warning("provider %s 瞬时错，退避重试 %d/%d: %s",
                                       key, attempt + 1, self._max_retries, e)
                        self._sleep(self._backoff(attempt))
                        continue                      # 重试同一 provider
                    logger.warning("provider %s 重试耗尽，沿链 fallback: %s", key, e)
                    break                             # → 下一链路项
                except Exception as e:   # noqa: BLE001
                    self._record_call(ctx, category, provider_name, model, None,
                                      int((time.monotonic() - t0) * 1000), "error")
                    last_err = e
                    self._breaker.record_failure(key)
                    if attempt < self._max_retries and not self._deadline_exceeded(ctx, now):
                        self._sleep(self._backoff(attempt))
                        continue
                    logger.warning("provider %s 异常且重试耗尽，沿链 fallback: %s", key, e)
                    break
        raise ModelUnavailable(f"category={category} 整条链不可用: {last_err}")

    def complete_stream(self, ctx: ExecutionContext, category: str, req: ChatRequest,
                        now: Optional[float] = None):
        """真流式：生成器 yield StreamDelta，return 最终 ChatResponse。

        与 complete() 同一套 deadline/熔断/退避/记账语义，唯一分叉在重试边界：
        **首个增量下发之前**的失败照常同 provider 退避重试、耗尽沿链 fallback；
        **已下发增量之后**的流中断直接抛（不可能把两条流拼成一个答案，重启=前端看到答案重播）——
        run 落 failed，用户看到 error 帧，durable 侧不落半截答案。
        provider 无 chat_stream / capabilities 不支持流式 → 该链路项退化为同步 chat（零增量、
        返回全文）；loop 见零文本增量把 RunCompleted.streamed 置 False → SSE 照旧整段下发，
        故假 provider/测试桩零改动。
        """
        if self._deadline_exceeded(ctx, now):
            raise BudgetExceeded(f"run {ctx.run_id} 已超 deadline")
        chain = self._routes.get(category) or self._routes.get(_DEFAULT_TIER) or []
        tp = self._tier_params.get(category)
        if tp:
            import dataclasses
            req = dataclasses.replace(req, extra={**tp, **req.extra})
        last_err: Optional[Exception] = None
        for provider_name, model in chain:
            provider = self._providers.get(provider_name)
            if provider is None:
                continue
            key = f"{provider_name}:{model}"
            if self._breaker.is_open(key):
                logger.warning("熔断打开，跳过 %s", key)
                continue
            stream_fn = getattr(provider, "chat_stream", None)
            try:
                caps = provider.capabilities(model)
            except Exception:   # noqa: BLE001 — capabilities 只是提示，坏了当支持处理
                caps = None
            if stream_fn is None or (caps is not None and not getattr(caps, "supports_stream", True)):
                # 同步退化：本链路项无流式能力 → 走 chat（沿用 complete 的单项重试语义）
                stream_fn = None
            for attempt in range(self._max_retries + 1):
                t0 = time.monotonic()
                emitted = False
                try:
                    if stream_fn is None:
                        resp = provider.chat(model, req)
                    else:
                        gen = stream_fn(model, req)
                        resp = None
                        while True:
                            try:
                                d = next(gen)
                            except StopIteration as fin:
                                resp = fin.value
                                break
                            emitted = True
                            yield d
                        if resp is None:
                            raise ModelError("provider chat_stream 未返回 ChatResponse",
                                             retryable=False)
                    self._breaker.record_success(key)
                    self._record_call(ctx, category, provider_name, model, resp.usage,
                                      int((time.monotonic() - t0) * 1000), "ok")
                    return resp
                except ModelError as e:
                    self._record_call(ctx, category, provider_name, model, None,
                                      int((time.monotonic() - t0) * 1000), "error")
                    last_err = e
                    if emitted or not e.retryable:
                        raise               # 已下发增量（不可重启）/ 配置鉴权错 → 立即抛
                    self._breaker.record_failure(key)
                    if attempt < self._max_retries and not self._deadline_exceeded(ctx, now):
                        logger.warning("provider %s 流式瞬时错，退避重试 %d/%d: %s",
                                       key, attempt + 1, self._max_retries, e)
                        self._sleep(self._backoff(attempt))
                        continue
                    logger.warning("provider %s 流式重试耗尽，沿链 fallback: %s", key, e)
                    break
                except Exception as e:   # noqa: BLE001
                    self._record_call(ctx, category, provider_name, model, None,
                                      int((time.monotonic() - t0) * 1000), "error")
                    last_err = e
                    if emitted:
                        raise
                    self._breaker.record_failure(key)
                    if attempt < self._max_retries and not self._deadline_exceeded(ctx, now):
                        self._sleep(self._backoff(attempt))
                        continue
                    logger.warning("provider %s 流式异常且重试耗尽，沿链 fallback: %s", key, e)
                    break
        raise ModelUnavailable(f"category={category} 整条链不可用: {last_err}")


# ── ToolSpec → OpenAI tool schema ─────────────────────────────────
def tool_spec_to_openai(spec: ToolSpec) -> Dict[str, Any]:
    return {"type": "function",
            "function": {"name": spec.name, "description": spec.description,
                         "parameters": spec.input_schema}}


# ── loop 的 model_fn 适配器 ────────────────────────────────────────
def _loop_msgs_to_openai(msgs: List[Msg]) -> List[Msg]:
    """DefaultAgentLoop 的简化 msg 格式 → OpenAI 格式（assistant tool_calls / tool 结果）。"""
    out: List[Msg] = []
    for m in msgs:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content"),
                        "tool_calls": [{"id": c["call_id"], "type": "function",
                                        "function": {"name": c["name"],
                                                     "arguments": json.dumps(c.get("arguments", {}),
                                                                             ensure_ascii=False)}}
                                       for c in m["tool_calls"]]})
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("call_id"),
                        "content": m.get("content", "")})
        else:
            out.append(m)
    return out


def _stream_enabled() -> bool:
    """真流式默认开（打字机是 console 转真用户的基础体验）；RAG_AGENT_STREAM=false 回退同步。"""
    return os.environ.get("RAG_AGENT_STREAM", "true").strip().lower() in ("1", "true", "yes", "on")


def _resp_to_turn(resp: ChatResponse) -> ModelTurn:
    if resp.tool_calls:
        # A4（复核批次3）：provider 缺 id 的兜底 call_id 带随机片段消跨轮碰撞——纯位置
        # 序号（call_0）在多轮 run 里必撞：幂等键与审批 grant 键都按 call_id 组键。
        # 片段一次生成后随 ProposedCall 稳定传递（checkpoint/resume 原样携带，不再生成）。
        import uuid
        return ModelTurn(
            tool_calls=[ProposedCall(
                call_id=tc.id or f"call_{i}_{uuid.uuid4().hex[:8]}", tool_name=tc.name,
                arguments=tc.arguments)
                for i, tc in enumerate(resp.tool_calls)],
            usage=resp.usage)
    return ModelTurn(text=resp.text, usage=resp.usage)


# A2：思考档对全局熔断按 thinking_cap_weight 加权（与准入侧 P2-11「cap≈计费量」同口径）
_THINKING_TIERS = frozenset({"high", "xhigh", "max"})


def _charge_model_call(ctx: ExecutionContext, category: str) -> None:
    """A2 逐模型调用计费（审计复核批次1）：每次真实模型调用**前**扣减全局熔断 +
    每用户日计数——准入层的一次性 count_llm 只护 ask 边界，多轮 run、空终轮重试与
    审批续跑段此前对熔断零扣减。先扣后调：超帽的那次调用根本不发出去（账单保护取
    保守侧），抛 BudgetExceeded → loop→executor 落 failed（状态诚实，同预算超限）。
    计费件自身故障的 fail-open 在 rate_limiter.charge_llm_call 内部（run 级预算兜底）。

    挂点选这里而非 executor 驱动器：空终轮重试发生在 loop._drive_model 内部、不产生
    独立事件，executor 只能看到轮边界——per-call 语义唯有 model_fn/gateway 层数得准。"""
    from opensearch_pipeline import rate_limiter
    if not rate_limiter.charge_llm_call(getattr(ctx, "user_id", None),
                                        thinking=category in _THINKING_TIERS):
        raise BudgetExceeded(
            "今日模型调用额度已用完（全局或个人日上限），请明天再试或联系管理员")


def make_model_fn(gateway: ModelGateway, ctx: ExecutionContext,
                  category: str = "light",
                  stream: Optional[bool] = None,
                  charge_llm: bool = True) -> Callable[[List[Msg], List[ToolSpec]], Any]:
    """把 Gateway 适配成 DefaultAgentLoop 的 model_fn。

    stream=None → 按 RAG_AGENT_STREAM（默认开）。流式模式下 model_fn 返回**生成器**
    （yield StreamDelta，return ModelTurn）——loop._drive_model 双形态识别；provider 没有
    chat_stream 时 complete_stream 自动退化同步（零增量），全部既有测试桩零改动。
    charge_llm：A2 逐调用计费开关（serving 恒 True）；eval_harness/agent 传 False——
    本地受控跑批不烧准入配额（251 题×多轮会中途撞每用户日帽把评测打死）。
    """
    if stream is None:
        stream = _stream_enabled()
    use_stream = bool(stream) and hasattr(gateway, "complete_stream")

    def _req(msgs: List[Msg], tools: List[ToolSpec]) -> ChatRequest:
        return ChatRequest(
            messages=_loop_msgs_to_openai(msgs),
            tools=[tool_spec_to_openai(t) for t in tools] if tools else None,
            max_tokens=None,
        )

    if not use_stream:
        def _model_fn(msgs: List[Msg], tools: List[ToolSpec]) -> ModelTurn:
            if charge_llm:
                _charge_model_call(ctx, category)
            return _resp_to_turn(gateway.complete(ctx, category, _req(msgs, tools)))
        return _model_fn

    def _model_stream_fn(msgs: List[Msg], tools: List[ToolSpec]):
        def _gen():
            if charge_llm:
                _charge_model_call(ctx, category)   # 生成器体：首个 next() 时（即真调用前）扣
            resp = yield from gateway.complete_stream(ctx, category, _req(msgs, tools))
            return _resp_to_turn(resp)
        return _gen()

    return _model_stream_fn


def default_gateway(call_logger: Optional[Callable] = None) -> ModelGateway:
    """生产工厂：注册境内 provider（当前 DashScope）+ 路由 + 可选 llm_call_log 记账。

    按**思考深度**分档 light/high/xhigh/max。默认（真调验证）：light/high=qwen3.7-plus，
    xhigh/max=qwen3.7-max-2026-06-08；light 不思考，high/xhigh/max 开思考、budget 1k/4k/16k
    （高阶档 429/5xx 退 plus）。env 覆盖：`RAG_AGENT_MODEL_{LIGHT,HIGH,XHIGH,MAX}` 换模型、
    `RAG_AGENT_THINK_{HIGH,XHIGH,MAX}` 换思考预算。模型 ID 须小写（大写 Qwen3.7-* / 旧 turbo 均 404）。
    """
    allow = os.environ.get("RAG_AGENT_PROVIDER_ALLOWLIST", "dashscope,selfhosted_vllm").split(",")
    providers: Dict[str, ModelProvider] = {}
    if "dashscope" in allow:
        providers["dashscope"] = DashScopeProvider()
    # 其它境内/自托管 provider 在此注册（新增 Provider 实现即插）

    def _budget(key: str, default: int) -> int:
        try:
            return int(os.environ.get(key, default))
        except (ValueError, TypeError):
            return default

    plus, mx = "qwen3.7-plus", "qwen3.7-max-2026-06-08"
    routes: Dict[str, List[RouteEntry]] = {
        "light": [("dashscope", os.environ.get("RAG_AGENT_MODEL_LIGHT", plus))],
        "high":  [("dashscope", os.environ.get("RAG_AGENT_MODEL_HIGH", plus))],
        "xhigh": [("dashscope", os.environ.get("RAG_AGENT_MODEL_XHIGH", mx)), ("dashscope", plus)],
        "max":   [("dashscope", os.environ.get("RAG_AGENT_MODEL_MAX", mx)), ("dashscope", plus)],
    }
    tier_params: Dict[str, Dict[str, Any]] = {
        "light": {"enable_thinking": False},      # qwen3.7 默认开思考，显式关才真快/省
        "high":  {"enable_thinking": True, "thinking_budget": _budget("RAG_AGENT_THINK_HIGH", 1024)},
        "xhigh": {"enable_thinking": True, "thinking_budget": _budget("RAG_AGENT_THINK_XHIGH", 4096)},
        "max":   {"enable_thinking": True, "thinking_budget": _budget("RAG_AGENT_THINK_MAX", 16384)},
    }
    return ModelGateway(providers, routes=routes, tier_params=tier_params, call_logger=call_logger)
