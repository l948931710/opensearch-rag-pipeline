# -*- coding: utf-8 -*-
"""
test_miniapp_serving.py — 钉钉小程序服务端改动测试

覆盖：
  - auth_token 会话令牌签发/校验/防篡改/过期
  - build_mini_program_blocks 图文块重映射（含全文 caption）
  - /api/auth/dingtalk 免登（模拟模式）
  - /api/ask 部门一律服务端解析、绝不信任请求体 user_dept（越权防护）
  - /api/feedback user_id 取自令牌而非请求体
  - 2026-06-10 契约轮（真档活测回归）：<<IMG:N>> 不泄漏、切分句首标点回挂、
    no_result/guard 标志、sources[].level 档位、PDF section 页码回退
"""

import os

# 模拟模式 + 固定签名密钥（须在导入 api 之前设置）
os.environ.setdefault("RAG_SIMULATE", "true")
os.environ.setdefault("RAG_SESSION_SIGNING_KEY", "test-signing-key")

import pytest

from opensearch_pipeline import auth_token
from opensearch_pipeline import content_blocks_builder as cb


# ── auth_token ───────────────────────────────────────────────

def test_token_roundtrip():
    t = auth_token.issue_session_token("U1", dept="行政部", name="张三")
    p = auth_token.verify_session_token(t)
    assert p and p["uid"] == "U1" and p["dept"] == "行政部" and p["name"] == "张三"


def test_token_tamper_and_garbage():
    t = auth_token.issue_session_token("U1", dept="行政部")
    flipped = t[:-2] + ("aa" if not t.endswith("aa") else "bb")
    assert auth_token.verify_session_token(flipped) is None
    assert auth_token.verify_session_token("garbage") is None
    assert auth_token.verify_session_token("") is None


def test_token_expired():
    t = auth_token.issue_session_token("U1", ttl=-10)
    assert auth_token.verify_session_token(t) is None


# ── build_mini_program_blocks ────────────────────────────────

def test_blocks_pure_text_returns_empty():
    assert cb.build_mini_program_blocks("纯文字答案，无图片引用", []) == []


def test_blocks_image_remap_full_caption(monkeypatch):
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=3600: "https://oss/" + key)
    long_summary = "登录界面截图说明" * 30  # > 100 字，验证不被截断
    chunks = [{"chunk_type": "image", "source_image": "k/a.png",
               "visual_summary": long_summary, "title": "x"}]
    blocks = cb.build_mini_program_blocks("第一步看这里<<IMG:1>>然后完成", chunks)
    types = [b["type"] for b in blocks]
    assert types == ["text", "image", "text"]
    img = blocks[1]
    assert img["url"] == "https://oss/k/a.png"
    assert img["caption"] == img["alt"] == long_summary  # 全文，未截断到 100
    assert blocks[0]["format"] == "markdown" and blocks[0]["text"] == "第一步看这里"


# ── HTTP endpoints ───────────────────────────────────────────

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    import opensearch_pipeline.api as api
    return TestClient(api.app)


def test_auth_dingtalk_simulate(client):
    r = client.post("/api/auth/dingtalk", json={"auth_code": "anything"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["user_id"]          # 模拟模式 → SIM_USER
    assert j["token"]            # 已签发令牌
    # 令牌可被服务端校验
    assert auth_token.verify_session_token(j["token"])["uid"] == j["user_id"]


def test_ask_uses_token_dept_not_body(monkeypatch):
    """请求体里伪造 user_dept/user_id 必须被忽略；部门取自令牌。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient

    captured = {}

    def fake_retrieve(question, top_k=7, user_dept=None, **kw):
        captured["dept"] = user_dept
        return [{"doc_id": "d1", "title": "T", "section_title": "S", "score": 9.0, "chunk_text": "c"}]

    def fake_generate(question, chunks, history=None, max_tokens=2048, temperature=0.1, pure_text=None):
        return {"answer": "答案正文",
                "sources": [{"doc_id": "d1", "title": "T", "section": "S", "score": 9.0}],
                "model": "qwen-test", "usage": {}}

    monkeypatch.setattr(api, "retrieve_and_enrich", fake_retrieve)
    monkeypatch.setattr(api, "generate_answer", fake_generate)
    monkeypatch.setattr(api, "build_mini_program_blocks",
                        lambda ans, chunks: [{"type": "text", "format": "plain", "text": ans}])
    monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)

    tok = auth_token.issue_session_token("U9", dept="行政部", name="李四")
    c = TestClient(api.app)
    r = c.post("/api/ask",
               json={"question": "q", "user_id": "ATTACKER", "user_dept": "财务部"},
               headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text
    assert captured["dept"] == "行政部"   # 取自令牌，而非请求体的 财务部
    j = r.json()
    assert j["message_id"]
    assert j["blocks"] == [{"type": "text", "format": "plain", "text": "答案正文"}]


def test_ask_no_token_is_anonymous_public_only(monkeypatch):
    """无令牌按匿名处理：请求体 user_id 不得反查部门（防伪造 staffId 跨部门读取）。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient

    captured = {}

    def fake_retrieve(question, top_k=7, user_dept=None, **kw):
        captured["dept"] = user_dept
        return [{"doc_id": "d1", "title": "T", "section_title": "S", "score": 9.0, "chunk_text": "c"}]

    monkeypatch.setattr(api, "retrieve_and_enrich", fake_retrieve)
    monkeypatch.setattr(api, "generate_answer",
                        lambda *a, **k: {"answer": "x", "sources": [], "model": "m", "usage": {}})
    monkeypatch.setattr(api, "build_mini_program_blocks", lambda ans, chunks: [])
    monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)

    c = TestClient(api.app)
    r = c.post("/api/ask", json={"question": "q", "user_id": "EMP1", "user_dept": "财务部"})
    assert r.status_code == 200, r.text
    assert captured["dept"] is None   # 无令牌 → 匿名（仅 public）；user_id 不授予部门权限


def test_ask_token_dept_marketing_center(monkeypatch):
    """钉钉部门「营销中心」走令牌 → 检索收到的 user_dept 原样为「营销中心」。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient

    captured = {}

    def fake_retrieve(question, top_k=7, user_dept=None, **kw):
        captured["dept"] = user_dept
        return [{"doc_id": "d1", "title": "T", "section_title": "S", "score": 9.0, "chunk_text": "c"}]

    monkeypatch.setattr(api, "retrieve_and_enrich", fake_retrieve)
    monkeypatch.setattr(api, "generate_answer",
                        lambda *a, **k: {"answer": "x", "sources": [], "model": "m", "usage": {}})
    monkeypatch.setattr(api, "build_mini_program_blocks", lambda ans, chunks: [])
    monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)

    tok = auth_token.issue_session_token("MKT001", dept="营销中心", name="测试用户")
    c = TestClient(api.app)
    r = c.post("/api/ask", json={"question": "q"},
               headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text
    assert captured["dept"] == "营销中心"


def test_feedback_uses_token_user(monkeypatch):
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient

    captured = {}

    def fake_handle(message_id, user_id, action, reason=None, comment=None, **kw):
        captured.update(message_id=message_id, user_id=user_id, action=action)
        return True

    monkeypatch.setattr(api, "handle_feedback", fake_handle)
    tok = auth_token.issue_session_token("U9", dept="行政部")
    c = TestClient(api.app)
    r = c.post("/api/feedback",
               json={"message_id": "m1", "user_id": "SPOOF", "feedback_type": "upvote"},
               headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text
    assert captured["user_id"] == "U9"   # 取自令牌，而非请求体 SPOOF
    assert captured["action"] == "upvote"


# ═══════════════════════════════════════════════════════════════
# 2026-06-10 契约轮：标记清理 / 标点回挂 / no_result / guard / level / 页码回退
# （真档活测发现的 4 个问题的回归测试，见 prototype/README.md「Port 前必须先定的三件事」）
# ═══════════════════════════════════════════════════════════════

# ── strip_image_markers / 标点回挂（builder 层，跑真实 builder） ──

def test_strip_image_markers_helper():
    assert cb.strip_image_markers("请看 <<IMG:1>> 和 <IMG:2> 说明") == "请看  和  说明"
    assert "IMG" not in cb.strip_image_markers("a<<IMG:3>>b")
    assert cb.strip_image_markers("<<IMG:1>> 开头与结尾 <IMG:2>") == "开头与结尾"
    assert cb.strip_image_markers(None) == ""
    assert cb.strip_image_markers("") == ""
    assert cb.strip_image_markers("无标记原样") == "无标记原样"


def _img_chunk(key="k/a.png", summary="截图说明"):
    return {"chunk_type": "image", "source_image": key,
            "visual_summary": summary, "title": "x"}


def test_blocks_punct_relocated_to_prev_text(monkeypatch):
    """「…图标 <<IMG:1>>。\\n第二步…」：句号回挂上一文本块，下一块干净开头。"""
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=3600: "https://oss/" + key)
    blocks = cb.build_mini_program_blocks("第一步看这里 <<IMG:1>>。\n第二步继续", [_img_chunk()])
    assert [b["type"] for b in blocks] == ["text", "image", "text"]
    assert blocks[0]["text"].endswith("。")          # 句号回挂到上一块
    assert blocks[2]["text"] == "第二步继续"          # 不再以「。」开头


def test_blocks_lone_trailing_punct_dropped(monkeypatch):
    """「点击图标 <<IMG:1>>。」：不再产生孤立的「。」尾块。"""
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=3600: "https://oss/" + key)
    blocks = cb.build_mini_program_blocks("点击图标 <<IMG:1>>。", [_img_chunk()])
    assert [b["type"] for b in blocks] == ["text", "image"]
    assert blocks[0]["text"] == "点击图标。"


def test_blocks_image_first_leading_punct_dropped(monkeypatch):
    """图片开头（前面没有文本块）：句首标点无处回挂，直接丢弃。"""
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=3600: "https://oss/" + key)
    blocks = cb.build_mini_program_blocks("<<IMG:1>>。后续说明", [_img_chunk()])
    assert [b["type"] for b in blocks] == ["image", "text"]
    assert blocks[1]["text"] == "后续说明"


# ── /api/ask 响应契约（真实 builder + api 接缝 monkeypatch） ──

def _wire_ask(monkeypatch, api, chunks, answer, sources=None):
    """通用接缝：检索/生成/落库打桩；blocks 走【真实 builder】以覆盖标记清理路径。"""
    monkeypatch.setattr(api, "retrieve_and_enrich", lambda *a, **kw: chunks)
    monkeypatch.setattr(
        api, "generate_answer",
        lambda *a, **kw: {"answer": answer, "sources": sources or [],
                          "model": "qwen-test", "usage": {}})
    monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=3600: "https://oss/" + key)


def test_ask_marker_stripped_when_blocks_empty(monkeypatch):
    """活测复现：LLM 引了不存在的图 → blocks=[]，answer 里的 <<IMG:N>> 必须清掉
    （小程序 blocks 为空时把 answer 当纯文本渲染 —— 用户曾会看到字面标记）。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient
    chunks = [{"doc_id": "d1", "title": "T", "section_title": "S",
               "score": 0.85, "rerank_score": 0.85, "chunk_text": "c"}]   # 纯文本 chunk，无图
    _wire_ask(monkeypatch, api, chunks, "请看 <<IMG:1>> <<IMG:2>> 中的说明")
    r = TestClient(api.app).post("/api/ask", json={"question": "q"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["blocks"] == []
    assert "IMG" not in j["answer"]
    assert j["no_result"] is False and j["guard"] is False


def test_ask_marker_stripped_when_blocks_nonempty(monkeypatch):
    """有图场景：blocks 正常构建（用原始 answer），同时响应 answer 也无标记。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient
    chunks = [_img_chunk()]
    chunks[0].update({"doc_id": "d1", "score": 0.92, "rerank_score": 0.92, "chunk_text": ""})
    _wire_ask(monkeypatch, api, chunks, "第一步看这里 <<IMG:1>>。\n第二步继续")
    r = TestClient(api.app).post("/api/ask", json={"question": "q"})
    j = r.json()
    assert [b["type"] for b in j["blocks"]] == ["text", "image", "text"]
    assert "IMG" not in j["answer"]
    assert j["blocks"][2]["text"] == "第二步继续"     # 标点回挂在 API 路径同样生效


def test_ask_no_result_on_empty_retrieval(monkeypatch):
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api, "retrieve_and_enrich", lambda *a, **kw: [])
    monkeypatch.setattr(api, "log_qa_session", lambda **kw: None)
    r = TestClient(api.app).post("/api/ask", json={"question": "q"})
    j = r.json()
    assert j["no_result"] is True and j["guard"] is False
    assert j["sources"] == []
    assert "未找到" in j["answer"]


def test_ask_no_result_on_refusal_with_weak_sources(monkeypatch):
    """活测发现的第二种「未找到」：LLM 拒答但带弱相关来源（top rerank < 0.8）。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient
    chunks = [{"doc_id": f"d{i}", "title": f"T{i}", "section_title": "",
               "score": 0.7 + i * 0.01, "rerank_score": 0.7 + i * 0.01,
               "chunk_text": "c"} for i in range(6)]
    _wire_ask(monkeypatch, api, chunks, "抱歉，当前知识库中未找到相关信息",
              sources=[{"doc_id": "d0", "title": "T0", "section": "", "score": 0.7, "level": "low"}])
    r = TestClient(api.app).post("/api/ask", json={"question": "q"})
    j = r.json()
    assert j["no_result"] is True
    assert j["guard"] is True                 # 全部 < 0.8 → 低置信带
    assert len(j["sources"]) == 1             # 弱来源保留，呈现方式由客户端决定


def test_ask_guard_band_independent_of_env_flag(monkeypatch):
    """guard 字段只看检索分带，与 RAG_LOW_CONFIDENCE_GUARD 开关无关（默认 off 也要置位）。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient
    c = TestClient(api.app)

    def ask_with(score):
        chunks = [{"doc_id": "d1", "title": "T", "section_title": "S",
                   "score": score, "rerank_score": score, "chunk_text": "c"}]
        _wire_ask(monkeypatch, api, chunks, "正常的回答内容，依据文档给出操作说明。")
        return c.post("/api/ask", json={"question": "q"}).json()

    assert ask_with(0.79)["guard"] is True
    assert ask_with(0.85)["guard"] is False
    # 融合分量纲（无 rerank_score 键）
    chunks = [{"doc_id": "d1", "title": "T", "section_title": "S", "score": 9.0, "chunk_text": "c"}]
    _wire_ask(monkeypatch, api, chunks, "正常的回答内容，依据文档给出操作说明。")
    assert c.post("/api/ask", json={"question": "q"}).json()["guard"] is False


def test_ask_sources_level_passthrough(monkeypatch):
    """generate_answer 下发的 level 原样透传到响应（SourceInfo 新字段）。"""
    import opensearch_pipeline.api as api
    from fastapi.testclient import TestClient
    chunks = [{"doc_id": "d1", "title": "T", "section_title": "S",
               "score": 0.92, "rerank_score": 0.92, "chunk_text": "c"}]
    _wire_ask(monkeypatch, api, chunks, "回答正文",
              sources=[{"doc_id": "d1", "title": "T", "section": "第3页",
                        "score": 0.92, "level": "high"}])
    j = TestClient(api.app).post("/api/ask", json={"question": "q"}).json()
    assert j["sources"][0]["level"] == "high"
    assert j["sources"][0]["section"] == "第3页"


# ── llm_generator：score_level / 档位标签同源 / 页码回退 / 置信带拆分 ──

def test_score_level_rerank_scale():
    from opensearch_pipeline import llm_generator as G

    def mk(s):
        return {"score": s, "rerank_score": s}

    assert G.score_level(mk(0.91)) == "high"
    assert G.score_level(mk(0.85)) == "mid"
    assert G.score_level(mk(0.70)) == "low"
    assert G.score_level({"score": "n/a"}) == ""


def test_score_level_fused_scale():
    from opensearch_pipeline import llm_generator as G
    assert G.score_level({"score": 9.0}) == "high"
    assert G.score_level({"score": 6.0}) == "mid"
    assert G.score_level({"score": 4.0}) == "low"


def test_format_context_label_parity():
    """重构后 prompt 中文标签不漂移（相关度: 高 0.91）。"""
    from opensearch_pipeline import llm_generator as G
    chunk = {"title": "T", "chunk_text": "正文", "score": 0.91, "rerank_score": 0.91}
    ctx = G._format_context([chunk])
    assert "(相关度: 高 0.91)" in ctx
    ctx2 = G._format_context([{"title": "T", "chunk_text": "正文", "score": 6.0}])
    assert "(相关度: 中 6.00)" in ctx2


def test_extract_sources_page_fallback_and_level():
    from opensearch_pipeline import llm_generator as G
    srcs = G._extract_sources([
        {"doc_id": "a", "title": "A", "section_title": "", "page_num": 12,
         "score": 0.91, "rerank_score": 0.91},
        {"doc_id": "b", "title": "B", "section_title": "", "page_num": 0, "score": 6.0},
        {"doc_id": "c", "title": "C", "section_title": "第2章", "page_num": 5, "score": 4.0},
        {"doc_id": "d", "title": "D", "section_title": "", "page_num": "7", "score": 9.0},
    ])
    by_id = {s["doc_id"]: s for s in srcs}
    assert by_id["a"]["section"] == "第12页" and by_id["a"]["level"] == "high"
    assert by_id["b"]["section"] == "" and by_id["b"]["level"] == "mid"
    assert by_id["c"]["section"] == "第2章"          # 已有章节名不被页码覆盖
    assert by_id["d"]["section"] == "第7页"           # 字符串页码容错
    assert by_id["d"]["level"] == "high"


def test_low_confidence_band_vs_guard_flag():
    """is_low_confidence_band 不看开关；_is_low_confidence = 开关 AND 带内（默认开关 off → False）。"""
    from opensearch_pipeline import llm_generator as G
    weak = [{"score": 0.79, "rerank_score": 0.79}]
    assert G.is_low_confidence_band(weak) is True
    assert G.is_low_confidence_band([{"score": 0.85, "rerank_score": 0.85}]) is False
    assert G.is_low_confidence_band([]) is False
    assert G._is_low_confidence(weak) is False   # RAG_LOW_CONFIDENCE_GUARD 默认 off
