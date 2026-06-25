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
    expires: Optional[int] = None,
    method: str = "GET",
) -> str:
    """
    将 OSS 对象 key 转为带签名的公开访问 URL。

    Args:
        oss_key: OSS 对象路径，如 'processing/assets/dept/doc_id/v1/image.jpg'
        expires: 签名有效期（秒）；None 取 config.oss.signed_url_expires
                 （RAG_OSS_URL_EXPIRES，默认 3600 = 1 小时）
        method: HTTP 方法，默认 GET

    Returns:
        签名 URL 字符串。失败时返回空字符串。
    """
    if not oss_key:
        return ""

    try:
        from opensearch_pipeline.config import get_config
        config = get_config()
        if expires is None:
            expires = config.oss.signed_url_expires

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


_SIM_HEAD_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
}


def _sim_head_object(oss_key: str) -> dict:
    """simulate 模式下的合成 HEAD：无真实 OSS 时也能让 kb register 的"存在性 + 大小 + etag"在本地跑通
    （满足 CLAUDE.md「改动先在 simulate 验证」——此前 head_object 在 sim 下恒返 None，register 永远 400）。

    大小优先取 RAG_SIM_OSS_HEAD_SIZE（让 0 字节 / 超限分支可被确定性测试），否则默认 1024（非空）。
    etag 优先取 RAG_SIM_OSS_HEAD_ETAG（让内容查重命中可被确定性测试），否则按 oss_key 派生——
    必须【按 key 不同】，否则所有 sim 上传 etag 相同会在内容查重里假撞。content_type 由扩展名粗推。
    """
    import hashlib
    import os
    raw = os.environ.get("RAG_SIM_OSS_HEAD_SIZE", "")
    try:
        size = int(raw) if raw != "" else 1024
    except ValueError:
        size = 1024
    ext = oss_key.rsplit(".", 1)[-1].lower() if "." in oss_key else ""
    etag = os.environ.get("RAG_SIM_OSS_HEAD_ETAG") or hashlib.sha256(oss_key.encode("utf-8")).hexdigest()[:32].upper()
    return {"size": size, "content_type": _SIM_HEAD_MIME.get(ext, "application/octet-stream"), "etag": etag}


def head_object(oss_key: str) -> Optional[dict]:
    """对 OSS 对象做 HEAD：存在返回 {size, content_type, etag}，不存在/失败返回 None。

    供 kb register 校验"客户端确已把文件直传到后端钦定的 raw_key"。只读，无写副作用。
    """
    if not oss_key:
        return None
    try:
        from opensearch_pipeline.config import get_config
        config = get_config()
        # simulate：无真实 OSS → 返回合成 HEAD（让 register 在 sim 下可跑；真实凭据缺失时也走此分支）。
        if getattr(config, "simulate_oss", False):
            return _sim_head_object(oss_key)
        access_id = config.oss.access_key_id
        access_secret = config.oss.access_key_secret
        if not access_id or access_id.strip() in ("xxx", ""):
            return None
        import oss2
    except ImportError:
        logger.warning("oss2 未安装，无法 head_object")
        return None
    try:
        public_endpoint = _ensure_public_endpoint(config.oss.endpoint)
        bucket = oss2.Bucket(oss2.Auth(access_id, access_secret), public_endpoint, config.oss.bucket_name)
        meta = bucket.head_object(oss_key)
        return {
            "size": int(meta.content_length or 0),
            "content_type": meta.content_type or "",
            "etag": (meta.etag or "").strip('"'),
        }
    except Exception as e:
        logger.info("head_object(%s) 未命中/失败: %s", oss_key[:80], e)
        return None


def generate_signed_urls_batch(
    oss_keys: list,
    expires: Optional[int] = None,
) -> dict:
    """
    批量生成签名 URL。

    Args:
        oss_keys: OSS key 列表
        expires: 签名有效期（秒）；None 取 config.oss.signed_url_expires

    Returns:
        {oss_key: signed_url} 字典。生成失败的 key 值为空字符串。
    """
    result = {}
    for key in oss_keys:
        result[key] = generate_signed_url(key, expires=expires)
    return result
