# -*- coding: utf-8 -*-
"""VLM call hardening — bounded *compress-on-retry* with a version-aware cache key.

Why: a prod HR-doc image (~381 KB, just under the funnel's 500 KB compress
threshold) hit a repeated *upload* write-timeout (`Connection aborted / write
operation timed out`) — so it never got compressed and the VLM semantic was lost
(it fell back to OCR text). The funnel's single 500 KB threshold is not robust;
the fix is to **shrink the image and retry** on transient failures.

Policy (per the 2026-06-20 hardening spec):
  attempt 0 : original bytes
  retry 1   : long side → ~1600 px, JPEG q≈85
  retry 2   : long side → ~1280 px, JPEG q≈70–75
  ≤ 2 retries, exponential backoff, **only** on timeout / connection-abort /
  429 / 5xx. 4xx (≠429), decode/format/bad-image errors are NOT retried.
  On exhaustion: keep the caller's OCR fallback and report
  vlm_status=FAILED_FALLBACK_TEXT + retry_count + last_error + transforms_tried.

The cache key includes **source_image_hash + transform_version + model_version**
so (a) different compression renditions of the same source never collude on a
stale annotation, and (b) bumping the transform ladder or the model invalidates
cleanly. Successful annotations must be written **atomically** (caller's
responsibility — this module never leaves a half-updated cache).

Pure/dependency-light: PIL is imported lazily; `requests` only for exception
typing (also lazy). Designed to be unit-testable by injecting `call_fn` + `sleep_fn`.
"""
from __future__ import annotations

import hashlib
import io
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

# Bump when the transform ladder below changes (part of the cache key).
TRANSFORM_VERSION = "ctr-v1"

# (max_long_side_px, jpeg_quality) applied on retry 1, retry 2 respectively.
RETRY_TRANSFORMS: List[Tuple[int, int]] = [(1600, 85), (1280, 72)]

_HTTP_IN_MSG = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)


def _status_from_exc(exc: Exception) -> Optional[int]:
    """Best-effort HTTP status from an exception (explicit attr or "HTTP <code>" text)."""
    st = getattr(exc, "status_code", None)
    if isinstance(st, int):
        return st
    m = _HTTP_IN_MSG.search(str(exc) or "")
    return int(m.group(1)) if m else None


def is_retryable(*, exc: Optional[BaseException] = None, status: Optional[int] = None) -> bool:
    """True only for *transient* failures: timeout / connection abort / 429 / 5xx.

    Explicit 4xx (other than 429) and decode/format/value errors are terminal.
    """
    if status is not None:
        if status == 429 or 500 <= status < 600:
            return True
        if 400 <= status < 500:
            return False
    if exc is not None:
        try:
            import requests
            if isinstance(exc, (requests.exceptions.Timeout,
                                requests.exceptions.ConnectionError,
                                requests.exceptions.ChunkedEncodingError)):
                return True
        except Exception:
            pass
        m = str(exc).lower()
        if any(k in m for k in ("timed out", "timeout", "connection aborted",
                                "connection reset", "broken pipe", "write operation")):
            return True
        st = _status_from_exc(exc)
        if st is not None:
            return st == 429 or 500 <= st < 600
        # Unknown / decode / format / bad-image → do not retry.
        return False
    return False


def _retry_after_seconds(resp) -> Optional[float]:
    """Parse a numeric Retry-After header (seconds). HTTP-date form is ignored."""
    try:
        v = resp.headers.get("Retry-After")
        return float(int(v)) if v is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def post_json_with_retry(
    url: str,
    *,
    json: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: Any = 60,
    max_retries: int = 2,
    base_backoff: float = 2.0,
    sleep_fn: Optional[Callable[[float], None]] = None,
    label: str = "",
    post_fn: Optional[Callable[..., Any]] = None,
):
    """POST with bounded retry on *transient* failures (timeout / conn-abort / 429 / 5xx).

    Why: DashScope 瞬时失败（尤其 429，本账户已知脆弱点）曾散落多份手写重试且已漂移。
    2026-07-03 收敛后这是唯一的 DashScope 瞬时重试策略：classify / OCR / VLM funnel /
    embedding_client.embed_texts_native / pipeline_nodes Gemini 嵌入循环全部走这里 ——
    429 + 全部 5xx + 瞬时网络异常重试，4xx(≠429) 与终止性异常立即上抛。This is a
    drop-in for ``requests.post``: it returns the ``requests.Response`` untouched
    (the caller still inspects ``status_code`` / calls ``raise_for_status``).
    Honors a numeric ``Retry-After`` header.
    （_push_chunks_to_ha3 是 HA3 Tea SDK 调用、非 requests.post，刻意保持自有重试
    ——见该函数内注释。）

    ``post_fn`` defaults to ``requests.post`` but call sites SHOULD pass their own
    module's patch seam — ``requests.post``, or ``_http_post`` for modules on the
    http_session 连接池 — so existing test monkeypatches keep working.
    """
    import time as _time
    if post_fn is None:
        import requests as _requests
        post_fn = _requests.post
    if sleep_fn is None:
        sleep_fn = _time.sleep

    attempt = 0
    while True:
        try:
            resp = post_fn(url, json=json, headers=headers, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — re-raised below if not transient/retries left
            if attempt < max_retries and is_retryable(exc=e):
                wait = base_backoff * (2 ** attempt)
                if label:
                    print(f"    ⚠️ {label} attempt {attempt + 1} transient error: "
                          f"{type(e).__name__}: {str(e)[:120]} → retry in {wait:.0f}s", flush=True)
                sleep_fn(wait)
                attempt += 1
                continue
            raise
        # tests/sim 可能给出无 status_code 的响应替身（裸 MagicMock 等）——只有真 int
        # 才按状态码判瞬时；其余一律当非瞬时原样返回（与 requests.Response 行为无差异）。
        _st = getattr(resp, "status_code", None)
        if isinstance(_st, int) and (_st == 429 or 500 <= _st < 600) and attempt < max_retries:
            wait = _retry_after_seconds(resp) or base_backoff * (2 ** attempt)
            if label:
                print(f"    ⚠️ {label} attempt {attempt + 1} HTTP {resp.status_code} "
                      f"→ retry in {wait:.0f}s", flush=True)
            sleep_fn(wait)
            attempt += 1
            continue
        return resp


def source_hash(image_bytes: bytes) -> str:
    """Stable hash of the *original* source image (before any transform)."""
    return hashlib.sha256(image_bytes).hexdigest()


def cache_key(src_hash: str, model_version: str,
              transform_version: str = TRANSFORM_VERSION) -> str:
    """Annotation cache key = source_image_hash + transform_version + model_version.

    Same source under a different compression rendition still maps to the SAME
    key (the annotation describes the source image, not a rendition) — so we never
    re-call the VLM for an already-annotated source; but a bumped transform ladder
    or model_version produces a different key, invalidating cleanly.
    """
    return f"{src_hash}|tv={transform_version}|mv={model_version}"


def compress(image_bytes: bytes, max_side: int, quality: int) -> Tuple[bytes, str, str]:
    """Resize (long side ≤ max_side) + JPEG re-encode. Returns (bytes, mime, label)."""
    from PIL import Image
    im = Image.open(io.BytesIO(image_bytes))
    if im.mode in ("RGBA", "P", "LA"):
        im = im.convert("RGB")
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), "image/jpeg", f"{max_side}px_q{quality}"


def call_with_compress_retry(
    image_bytes: bytes,
    mime_type: str,
    call_fn: Callable[[bytes, str], Any],
    *,
    model_version: str,
    src_hash: Optional[str] = None,
    max_retries: int = 2,
    sleep_fn: Optional[Callable[[float], None]] = None,
    base_backoff: float = 2.0,
) -> Dict[str, Any]:
    """Call `call_fn(payload_bytes, mime)` with bounded compress-on-retry.

    `call_fn` must return a truthy result on success and raise on failure
    (Timeout/ConnectionError, or RuntimeError("... HTTP <code>: ...")). It is
    given progressively smaller renditions on each retry.

    Returns a dict (never raises for transport failures):
      ok            : bool
      result        : the call_fn return value (None on failure)
      vlm_status    : "OK" | "RECOVERED" | "FAILED_FALLBACK_TEXT"
      retry_count   : number of *retries* performed (0 = succeeded first try)
      attempts      : total attempts (retries + 1)
      transform_used: label of the rendition that the final attempt used
      transforms_tried: ["original", "1600px_q85", ...]
      last_error    : str | None
      cache_key     : str | None (when src_hash given)
    """
    if sleep_fn is None:
        import time
        sleep_fn = time.sleep
    if src_hash is None:
        src_hash = source_hash(image_bytes)

    # ladder: attempt 0 = original, then the configured retry transforms
    ladder: List[Optional[Tuple[int, int]]] = [None] + RETRY_TRANSFORMS[:max_retries]
    tried: List[str] = []
    last_error: Optional[str] = None

    for i, step in enumerate(ladder):
        if step is None:
            payload, mime, label = image_bytes, mime_type, "original"
        else:
            try:
                payload, mime, label = compress(image_bytes, step[0], step[1])
            except Exception as e:  # compression itself failed → terminal (bad image)
                return _result(False, None, "FAILED_FALLBACK_TEXT", i, tried,
                               f"compress failed: {type(e).__name__}: {str(e)[:120]}",
                               label="compress_error", src_hash=src_hash, model_version=model_version)
        tried.append(label)
        try:
            res = call_fn(payload, mime)
            status = "OK" if i == 0 else "RECOVERED"
            return _result(True, res, status, i, tried, last_error,
                           label=label, src_hash=src_hash, model_version=model_version)
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:160]}"
            retryable = is_retryable(exc=e, status=_status_from_exc(e))
            if i < len(ladder) - 1 and retryable:
                sleep_fn(base_backoff * (2 ** i))
                continue
            return _result(False, None, "FAILED_FALLBACK_TEXT", i, tried, last_error,
                           label=label, src_hash=src_hash, model_version=model_version)
    # unreachable
    return _result(False, None, "FAILED_FALLBACK_TEXT", 0, tried, last_error,
                   label="none", src_hash=src_hash, model_version=model_version)


def _result(ok, result, vlm_status, retry_idx, tried, last_error, *,
            label, src_hash, model_version) -> Dict[str, Any]:
    return {
        "ok": ok,
        "result": result,
        "vlm_status": vlm_status,
        "retry_count": retry_idx,
        "attempts": retry_idx + 1,
        "transform_used": label,
        "transforms_tried": list(tried),
        "last_error": last_error,
        "cache_key": cache_key(src_hash, model_version) if src_hash else None,
    }
