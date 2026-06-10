# -*- coding: utf-8 -*-
"""
vlm_endpoint.py — Qwen-VL / DashScope 端点路由的单一实现。

此前同一套「compat or native」判定 + URL 拼装 + 请求体 + 响应解析散落在
image_funnel_processor / extraction.vlm_rebuilder / extraction.ocr_client /
spot_checker 四处手写拷贝，且 ocr_client 漏掉 compat 分支——配置 qwen3-vl-*
做 OCR 会打到原生端点直接报错。

路由规则（与原 funnel/rebuilder 行为一致）：
- qwen3 系列模型只在 compatible-mode（OpenAI 风格）端点提供 → compat；
- base_url 本身已是 compatible 端点 → compat；
- 其余（qwen-vl-ocr-latest / qwen-vl-plus 等旧系列）→ DashScope 原生多模态端点。
"""

import re
from typing import Any, Dict, Optional

_DEFAULT_DASHSCOPE_DOMAIN = "dashscope.aliyuncs.com"


def use_compat_mode(model_name: str, api_base_url: str) -> bool:
    """qwen3 系列或显式 compatible base → OpenAI 兼容端点。"""
    return "qwen3" in (model_name or "").lower() or "compatible" in (api_base_url or "").lower()


def compat_chat_completions_url(api_base_url: str) -> str:
    """compatible-mode chat/completions URL。

    按域名重建路径：base 可能是 /api/v1 原生路径（compatible 路径不能往它后面拼）。
    base 已是完整 chat/completions URL 则原样返回。
    """
    base = api_base_url or ""
    if "chat/completions" in base:
        return base
    m = re.search(r"https?://([^/]+)", base)
    domain = m.group(1) if m else _DEFAULT_DASHSCOPE_DOMAIN
    return f"https://{domain}/compatible-mode/v1/chat/completions"


def native_multimodal_url(api_base_url: str) -> str:
    """DashScope 原生多模态生成端点。"""
    return (api_base_url or "").rstrip("/") + "/services/aigc/multimodal-generation/generation"


def resolve_vlm_url(api_base_url: str, use_compat: bool) -> str:
    if use_compat:
        return compat_chat_completions_url(api_base_url)
    return native_multimodal_url(api_base_url)


def auth_headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def build_image_chat_payload(
    model_name: str,
    prompt: str,
    b64_data: str,
    mime_type: str,
    use_compat: bool,
    temperature: Optional[float] = None,
) -> Dict[str, Any]:
    """单图 + 文字 prompt 的请求体（compat=OpenAI 风格；native=DashScope 原生，图在前）。"""
    data_url = f"data:{mime_type};base64,{b64_data}"
    if use_compat:
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }],
        }
        if temperature is not None:
            payload["temperature"] = temperature
        return payload
    payload = {
        "model": model_name,
        "input": {"messages": [{
            "role": "user",
            "content": [{"image": data_url}, {"text": prompt}],
        }]},
    }
    if temperature is not None:
        payload["parameters"] = {"temperature": temperature}
    return payload


def extract_vlm_text(data: Dict[str, Any], use_compat: bool) -> str:
    """从响应 JSON 提取文本；native 兼容旧版 list 形式 content。

    字段缺失按上抛处理（KeyError/IndexError），降级策略由调用方决定。
    """
    if use_compat:
        content = data["choices"][0]["message"]["content"]
    else:
        content = data["output"]["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
    return content if isinstance(content, str) else str(content)
