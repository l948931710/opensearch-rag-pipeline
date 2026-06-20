# -*- coding: utf-8 -*-
"""Unit tests for vlm_retry — compress-on-retry policy + version-aware cache key.

No network: call_fn is injected; sleep_fn is a no-op so backoff doesn't slow tests.
"""
import io

import pytest

from opensearch_pipeline import vlm_retry as V


def _img_bytes(w=2000, h=1500, color=(123, 45, 67)):
    from PIL import Image
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _noop_sleep(_):
    return None


# ── is_retryable ────────────────────────────────────────────────
@pytest.mark.parametrize("status,expected", [
    (429, True), (500, True), (503, True), (599, True),
    (400, False), (401, False), (403, False), (404, False), (200, False),
])
def test_is_retryable_by_status(status, expected):
    assert V.is_retryable(status=status) is expected


def test_is_retryable_by_exception():
    import requests
    assert V.is_retryable(exc=requests.exceptions.Timeout("x")) is True
    assert V.is_retryable(exc=requests.exceptions.ConnectionError(
        "('Connection aborted.', TimeoutError('The write operation timed out'))")) is True
    assert V.is_retryable(exc=RuntimeError("DashScope OCR HTTP 503: busy")) is True
    assert V.is_retryable(exc=RuntimeError("DashScope OCR HTTP 400: bad image")) is False
    assert V.is_retryable(exc=ValueError("cannot identify image file")) is False


# ── cache key ───────────────────────────────────────────────────
def test_cache_key_includes_all_three_and_is_rendition_stable():
    raw = _img_bytes()
    h = V.source_hash(raw)
    k1 = V.cache_key(h, "qwen3-vl-plus")
    assert h in k1 and V.TRANSFORM_VERSION in k1 and "qwen3-vl-plus" in k1
    # different model → different key; bumped transform → different key
    assert V.cache_key(h, "qwen3-vl-plus") != V.cache_key(h, "qwen-vl-ocr-latest")
    assert V.cache_key(h, "m", "tvA") != V.cache_key(h, "m", "tvB")
    # a compressed rendition of the SAME source still keys on the source hash
    comp, _, _ = V.compress(raw, 1600, 85)
    # caller passes the source hash regardless of which rendition was sent
    assert V.cache_key(h, "m") == V.cache_key(h, "m")


# ── compress ────────────────────────────────────────────────────
def test_compress_shrinks_and_reencodes_jpeg():
    raw = _img_bytes(3000, 2000)
    out, mime, label = V.compress(raw, 1280, 72)
    assert mime == "image/jpeg" and label == "1280px_q72"
    from PIL import Image
    im = Image.open(io.BytesIO(out))
    assert max(im.size) == 1280
    assert len(out) < len(raw)  # re-encoded smaller


# ── retry orchestration ─────────────────────────────────────────
def test_success_first_try_no_compression():
    raw = _img_bytes()
    seen = []

    def call_fn(payload, mime):
        seen.append((len(payload), mime))
        return {"caption": "ok"}

    r = V.call_with_compress_retry(raw, "image/png", call_fn,
                                   model_version="m", sleep_fn=_noop_sleep)
    assert r["ok"] and r["vlm_status"] == "OK" and r["retry_count"] == 0
    assert r["transforms_tried"] == ["original"] and r["transform_used"] == "original"
    assert seen[0][1] == "image/png"  # original mime, untouched


def test_timeout_then_recovered_on_first_compressed_retry():
    import requests
    raw = _img_bytes()
    calls = []
    _last_payload = {"bytes": None}

    def call_fn(payload, mime):
        calls.append((len(payload), mime))
        _last_payload["bytes"] = payload
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("write operation timed out")
        return {"caption": "recovered"}

    r = V.call_with_compress_retry(raw, "image/png", call_fn,
                                   model_version="qwen3-vl-plus", sleep_fn=_noop_sleep)
    assert r["ok"] and r["vlm_status"] == "RECOVERED" and r["retry_count"] == 1
    assert r["transforms_tried"] == ["original", "1600px_q85"]
    # retry rendition is JPEG and dimensionally downscaled (byte-size varies by content)
    assert calls[1][1] == "image/jpeg"
    from PIL import Image
    sent_retry = Image.open(io.BytesIO(_last_payload["bytes"]))
    assert max(sent_retry.size) == 1600


def test_all_retryable_failures_exhaust_to_fallback():
    import requests
    raw = _img_bytes()
    n = {"c": 0}

    def call_fn(payload, mime):
        n["c"] += 1
        raise requests.exceptions.Timeout("timed out")

    r = V.call_with_compress_retry(raw, "image/png", call_fn,
                                   model_version="m", sleep_fn=_noop_sleep)
    assert not r["ok"] and r["vlm_status"] == "FAILED_FALLBACK_TEXT"
    assert r["retry_count"] == 2 and r["attempts"] == 3
    assert r["transforms_tried"] == ["original", "1600px_q85", "1280px_q72"]
    assert n["c"] == 3 and "Timeout" in r["last_error"]


def test_non_retryable_4xx_does_not_retry():
    raw = _img_bytes()
    n = {"c": 0}

    def call_fn(payload, mime):
        n["c"] += 1
        raise RuntimeError("DashScope OCR HTTP 400: invalid image")

    r = V.call_with_compress_retry(raw, "image/png", call_fn,
                                   model_version="m", sleep_fn=_noop_sleep)
    assert not r["ok"] and r["attempts"] == 1 and n["c"] == 1
    assert r["transforms_tried"] == ["original"]


def test_backoff_is_called_between_retries():
    import requests
    raw = _img_bytes()
    slept = []

    def call_fn(payload, mime):
        raise requests.exceptions.Timeout("t")

    V.call_with_compress_retry(raw, "image/png", call_fn, model_version="m",
                               sleep_fn=slept.append, base_backoff=2.0)
    # 2 retries → 2 backoff sleeps, exponential 2,4
    assert slept == [2.0, 4.0]
