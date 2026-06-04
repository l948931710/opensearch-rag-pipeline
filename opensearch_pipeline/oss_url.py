# -*- coding: utf-8 -*-
"""
oss_url.py — 阿里云 OSS 签名 URL 生成器

将 OSS 对象 key 转为带签名的临时公开访问 URL。
用于在钉钉消息中展示存储在 OSS 上的图片。

失败时返回空字符串，不阻断主流程。
"""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def _ensure_public_endpoint(endpoint: str) -> str:
    """
    将 OSS 内网 endpoint 转为公网 endpoint。

    阿里云 OSS 内网域名包含 '-internal'，例如:
      oss-cn-chengdu-internal.aliyuncs.com  → oss-cn-chengdu.aliyuncs.com

    钉钉客户端需要通过公网访问图片，内网域名无法加载。
    """
    if not endpoint:
        return endpoint
    # 移除 -internal 后缀（位于区域名和 .aliyuncs.com 之间）
    return re.sub(r'-internal(?=\.)', '', endpoint)


def generate_signed_url(
    oss_key: str,
    expires: int = 3600,
    method: str = "GET",
) -> str:
    """
    将 OSS 对象 key 转为带签名的公开访问 URL。

    Args:
        oss_key: OSS 对象路径，如 'processing/assets/dept/doc_id/v1/image.jpg'
        expires: 签名有效期（秒），默认 3600（1 小时）
        method: HTTP 方法，默认 GET

    Returns:
        签名 URL 字符串。失败时返回空字符串。
    """
    if not oss_key:
        return ""

    try:
        from opensearch_pipeline.config import get_config
        config = get_config()

        access_id = config.oss.access_key_id
        access_secret = config.oss.access_key_secret
        endpoint = config.oss.endpoint
        bucket_name = config.oss.bucket_name

        # 凭据缺失时跳过
        if not access_id or access_id.strip() in ("xxx", ""):
            print(f"[OSS] ❌ credentials not configured: access_key_id='{access_id[:8] if access_id else ''}...', endpoint='{endpoint}'", flush=True)
            return ""

        import oss2
    except ImportError:
        print("[OSS] ❌ oss2 library not installed", flush=True)
        logger.warning("oss2 library not installed, cannot generate signed URLs")
        return ""

    try:
        # 确保使用公网 endpoint（钉钉客户端需要公网访问）
        public_endpoint = _ensure_public_endpoint(endpoint)

        auth = oss2.Auth(access_id, access_secret)
        bucket = oss2.Bucket(auth, public_endpoint, bucket_name)

        url = bucket.sign_url(method, oss_key, expires)

        # 强制 HTTPS — 钉钉客户端要求图片 URL 必须是 HTTPS
        if url.startswith("http://"):
            url = "https://" + url[7:]

        logger.debug("Generated signed URL for %s (expires=%ds)", oss_key, expires)
        return url

    except Exception as e:
        print(f"[OSS] ❌ sign_url failed: endpoint={endpoint}, bucket={bucket_name}, key={oss_key[:80]}, error={e}", flush=True)
        logger.error("Failed to generate signed URL for '%s': %s", oss_key, e, exc_info=True)
        return ""


def generate_signed_urls_batch(
    oss_keys: list,
    expires: int = 3600,
) -> dict:
    """
    批量生成签名 URL。

    Args:
        oss_keys: OSS key 列表
        expires: 签名有效期（秒）

    Returns:
        {oss_key: signed_url} 字典。生成失败的 key 值为空字符串。
    """
    result = {}
    for key in oss_keys:
        result[key] = generate_signed_url(key, expires=expires)
    return result
