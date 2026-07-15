# -*- coding: utf-8 -*-
"""test_stream_gate.py — 低置信带流式门控（评审补充 5）。

覆盖三分支：拒答开场 → 改道统一失败序列；正常开场 → 按原序透传（held flush）；
短流（未攒够 48 字符即 done）→ 按已有开场裁决。外加链路级：
flag-off / 中高置信带下 gate 完全不介入（I4 / 主路径零影响）。
"""

import json
from unittest.mock import patch

import pytest

from opensearch_pipeline.api import _gate_probe
from opensearch_pipeline.auth_token import issue_session_token


def _chunk(text):
    return f"data: {json.dumps({'type': 'chunk', 'content': text}, ensure_ascii=False)}\n\n"


def _done(model="qwen-test"):
    return f"data: {json.dumps({'type': 'done', 'model': model, 'usage': {}}, ensure_ascii=False)}\n\n"


_REFUSAL_TEXT = "抱歉，当前知识库中未找到相关信息"
_NORMAL_LONG = "根据员工手册第三章的规定，出差住宿标准按城市档位执行，具体如下：一线城市每晚上限……"


# ──────────────────────────────────────────────────────────────
# 1. _gate_probe 单元
# ──────────────────────────────────────────────────────────────

def test_probe_refusal_opening_verdict_true():
    events = [_chunk("抱歉，当前知识库中未"), _chunk("找到相关信息"), _done()]
    verdict, held, rest = _gate_probe(iter(events))
    assert verdict is True


def test_probe_normal_opening_flushes_in_order():
    events = [_chunk(_NORMAL_LONG[:30]), _chunk(_NORMAL_LONG[30:60]),
              _chunk(_NORMAL_LONG[60:]), _done()]
    verdict, held, rest = _gate_probe(iter(events))
    assert verdict is False
    # held + rest 严格等于原事件序列（透传零丢帧、零乱序）
    assert held + list(rest) == events


def test_probe_short_stream_decides_on_done():
    events = [_chunk("是的。"), _done()]
    verdict, held, rest = _gate_probe(iter(events))
    assert verdict is False
    assert held == events


def test_probe_short_refusal_decides_on_done():
    events = [_chunk(_REFUSAL_TEXT), _done()]
    verdict, held, rest = _gate_probe(iter(events))
    assert verdict is True


def test_probe_non_chunk_frames_pass_into_held():
    src = f"data: {json.dumps({'type': 'sources', 'sources': []})}\n\n"
    events = [src, _chunk(_NORMAL_LONG[:60]), _done()]
    verdict, held, rest = _gate_probe(iter(events))
    assert verdict is False
    assert held[0] == src


# ──────────────────────────────────────────────────────────────
# 2. 链路级（低置信带 chunks：score 远低于 medium 阈值）
# ──────────────────────────────────────────────────────────────

_LOW_CHUNK = [{"chunk_text": "..", "title": "出差报销制度", "doc_id": "D1", "score": 0.01}]
_HIGH_CHUNK = [{"chunk_text": "..", "title": "出差报销制度", "doc_id": "D1", "score": 99.0}]


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from opensearch_pipeline.api import app
    return TestClient(app)


@pytest.fixture
def general_on(monkeypatch):
    import opensearch_pipeline.config as CONF
    monkeypatch.setenv("RAG_GENERAL_ABILITY_MODE", "office")
    monkeypatch.setenv("RAG_GUIDED_REFUSAL", "true")
    CONF._config = None
    yield
    CONF._config = None


def _auth():
    return {"Authorization": "Bearer " + issue_session_token("U100", dept=["it"])}


def _fake_stream(*texts, model="qwen-test"):
    def _gen(*a, **k):
        for t in texts:
            yield _chunk(t)
        yield _done(model)
        yield "data: [DONE]\n\n"
    return _gen


class TestStreamGateE2E:
    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.generate_answer_stream")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_low_band_refusal_rerouted(self, mock_retrieve, mock_stream, mock_log,
                                       client, general_on):
        """低置信带 + 拒答开场 → 吞掉拒答流，改道引导话术（sim 分诊 → enterprise）。"""
        mock_retrieve.return_value = _LOW_CHUNK
        mock_stream.side_effect = _fake_stream("抱歉，当前知识库中未", "找到相关信息")
        resp = client.post("/api/ask/stream", json={"question": "外派驻场怎么报销"},
                           headers=_auth())
        body = resp.text
        assert "出差报销制度" in body          # 引导话术带相近标题
        assert '"no_result": true' in body
        assert '"suggest_titles"' in body
        # 原始拒答句不再以 chunk 形式出现（改道后只有引导话术开头的"抱歉，知识库…"）
        assert _REFUSAL_TEXT not in body
        kw = mock_log.call_args.kwargs
        assert kw["answer_status"] == "REFUSAL"   # 口径不漂移（句式翻转）
        assert kw["intent_type"] == "refuse_uncovered_enterprise"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.generate_answer_stream")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_low_band_normal_passthrough(self, mock_retrieve, mock_stream, mock_log,
                                         client, general_on):
        """低置信带 + 正常开场 → 全量透传（gate 只延迟首帧，不改内容）。"""
        mock_retrieve.return_value = _LOW_CHUNK
        parts = [_NORMAL_LONG[:20], _NORMAL_LONG[20:40], _NORMAL_LONG[40:]]
        mock_stream.side_effect = _fake_stream(*parts)
        resp = client.post("/api/ask/stream", json={"question": "出差住宿标准"},
                           headers=_auth())
        body = resp.text
        for p in parts:
            assert json.dumps(p, ensure_ascii=False)[1:-1] in body   # 每帧原样在
        assert '"guard": true' in body
        assert mock_log.call_args.kwargs["answer_status"] == "SUCCESS"
        assert mock_log.call_args.kwargs["intent_type"] is None

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.generate_answer_stream")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_high_band_refusal_not_gated(self, mock_retrieve, mock_stream, mock_log,
                                         client, general_on):
        """中高置信带不进 gate：拒答文本照旧流出（主路径零影响），
        done 帧补引导富化字段（no_result/suggest_titles）。"""
        mock_retrieve.return_value = _HIGH_CHUNK
        mock_stream.side_effect = _fake_stream(_REFUSAL_TEXT)
        resp = client.post("/api/ask/stream", json={"question": "外派驻场怎么报销"},
                           headers=_auth())
        body = resp.text
        assert _REFUSAL_TEXT in body            # 原文流出（无法替换）
        assert '"no_result": true' in body      # done 帧富化
        assert '"suggest_titles"' in body
        assert mock_log.call_args.kwargs["answer_status"] == "REFUSAL"

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.generate_answer_stream")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_flag_off_refusal_passthrough_unchanged(self, mock_retrieve, mock_stream,
                                                    mock_log, client):
        """I4：flag 全关时低置信带拒答照旧流出，done 帧无新增字段。"""
        mock_retrieve.return_value = _LOW_CHUNK
        mock_stream.side_effect = _fake_stream(_REFUSAL_TEXT)
        resp = client.post("/api/ask/stream", json={"question": "外派驻场怎么报销"})
        body = resp.text
        assert _REFUSAL_TEXT in body
        assert '"suggest_titles"' not in body
        assert '"source"' not in body
        assert mock_log.call_args.kwargs["answer_status"] == "REFUSAL"
        assert mock_log.call_args.kwargs["intent_type"] is None

    @patch("opensearch_pipeline.api.log_qa_session")
    @patch("opensearch_pipeline.api.generate_answer_stream")
    @patch("opensearch_pipeline.api.retrieve_and_enrich")
    def test_gate_env_off_disables_reroute(self, mock_retrieve, mock_stream, mock_log,
                                           client, general_on, monkeypatch):
        """RAG_GENERAL_STREAM_GATE=false：即使 flag-on 也不改道（运维独立开关）。"""
        import opensearch_pipeline.config as CONF
        monkeypatch.setenv("RAG_GENERAL_STREAM_GATE", "false")
        CONF._config = None
        mock_retrieve.return_value = _LOW_CHUNK
        mock_stream.side_effect = _fake_stream(_REFUSAL_TEXT)
        resp = client.post("/api/ask/stream", json={"question": "外派驻场怎么报销"},
                           headers=_auth())
        assert _REFUSAL_TEXT in resp.text
