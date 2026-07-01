# -*- coding: utf-8 -*-
"""
test_embedding_client.py — 共享 DashScope dense+sparse embedding 客户端

覆盖查询侧/入库侧合并后的关键行为：URL 幂等去重、sparse 解析与兜底、text_index 对齐、
429 退避重试、400 立即失败。HTTP 全部 mock。
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from opensearch_pipeline.embedding_client import (
    build_native_embedding_url,
    embed_texts_native,
)


def test_url_idempotent_on_api_v1():
    assert build_native_embedding_url("https://x.com").endswith(
        "/api/v1/services/embeddings/text-embedding/text-embedding")
    # 已含 /api/v1 → 不重复拼接
    u = build_native_embedding_url("https://x.com/api/v1")
    assert u == "https://x.com/api/v1/services/embeddings/text-embedding/text-embedding"
    assert u.count("/api/v1") == 1
    # 末尾斜杠归一
    assert build_native_embedding_url("https://x.com/") == build_native_embedding_url("https://x.com")


def _resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    if status >= 400:
        err = requests.exceptions.HTTPError(response=MagicMock(status_code=status))
        err.response.text = "boom"
        r.raise_for_status.side_effect = err
    else:
        r.raise_for_status.return_value = None
    return r


def _payload(*embs):
    # embs: list of (text_index, dense, sparse_list)
    return {"output": {"embeddings": [
        {"text_index": ti, "embedding": d, "sparse_embedding": s} for ti, d, s in embs
    ]}}


def test_dense_and_sparse_parsed_sorted():
    pl = _payload((0, [0.1, 0.2], [{"index": 5, "value": 0.9}, {"index": 1, "value": 0.3}]))
    with patch("opensearch_pipeline.embedding_client._http_post", return_value=_resp(pl)):
        out = embed_texts_native(["q"], api_key="k", model="m", dimension=2, api_base_url="https://x")
    dense, sidx, sval = out[0]
    assert dense == [0.1, 0.2]
    assert sidx == [1, 5] and sval == [0.3, 0.9]  # sorted by index


def test_sparse_fallback_only_when_requested():
    pl = _payload((0, [0.1], []))
    with patch("opensearch_pipeline.embedding_client._http_post", return_value=_resp(pl)):
        q = embed_texts_native(["q"], api_key="k", model="m", dimension=1,
                               api_base_url="https://x", sparse_fallback=False)
        ing = embed_texts_native(["q"], api_key="k", model="m", dimension=1,
                                 api_base_url="https://x", sparse_fallback=True)
    assert q[0][1] == [] and q[0][2] == []           # query: empty sparse
    assert ing[0][1] == [0] and ing[0][2] == [0.001]  # ingest: [0]/[0.001] fallback


def test_text_index_alignment_and_missing_slot_is_none():
    # 响应乱序 + 缺第 1 个 → 结果按 text_index 对齐，缺的为 None
    pl = _payload((2, [0.3], []), (0, [0.1], []))
    with patch("opensearch_pipeline.embedding_client._http_post", return_value=_resp(pl)):
        out = embed_texts_native(["a", "b", "c"], api_key="k", model="m", dimension=1, api_base_url="https://x")
    assert out[0][0] == [0.1]
    assert out[1] is None       # text_index 1 未返回
    assert out[2][0] == [0.3]


def test_retries_on_429_then_succeeds():
    pl = _payload((0, [0.1], []))
    calls = {"n": 0}

    def _post(*a, **k):
        calls["n"] += 1
        return _resp(pl) if calls["n"] >= 2 else _resp({}, status=429)

    with patch("opensearch_pipeline.embedding_client._http_post", side_effect=_post), \
         patch("opensearch_pipeline.embedding_client.time.sleep"):
        out = embed_texts_native(["q"], api_key="k", model="m", dimension=1,
                                 api_base_url="https://x", max_retries=2)
    assert calls["n"] == 2 and out[0][0] == [0.1]


def test_400_fails_immediately_no_retry():
    calls = {"n": 0}

    def _post(*a, **k):
        calls["n"] += 1
        return _resp({}, status=400)

    with patch("opensearch_pipeline.embedding_client._http_post", side_effect=_post), \
         patch("opensearch_pipeline.embedding_client.time.sleep"):
        with pytest.raises(requests.exceptions.HTTPError):
            embed_texts_native(["q"], api_key="k", model="m", dimension=1,
                               api_base_url="https://x", max_retries=3)
    assert calls["n"] == 1  # no retry on 400


def test_missing_api_key_raises():
    with pytest.raises(RuntimeError):
        embed_texts_native(["q"], api_key="", model="m", dimension=1, api_base_url="https://x")
