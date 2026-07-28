# -*- coding: utf-8 -*-
"""`RAG_IMAGE_CONTENT_OVERRIDE` 默认翻 ON（2026-07-25）+ 有效值进 chunk provenance。

依据（L4-ing pdf 支柱，6 篇 GT / 48 strong 行，同一 warm-cache 快照）：
暖缓存 OFF 0.89236 → ON 0.96528；冷缓存（caption 全部重掷）OFF 0.84722 → ON 0.89931
—— 两种 caption 制度下增益都为正，0 条退化，docx/xlsx/img_dup_p95 两侧均不动。

本文件钉三件事：
  1. 解析语义：默认 ON、显式空串 ON、只有 off-list 关闭、未知值 ON（默认 ON 的开关
     若把未知值也当关，配错一个字就会静默退回旧行为）；
  2. 有效**布尔值**进 provenance（不是原始字符串，也不是 "true"/"false" 字符串）；
  3. **四个 gate 确实改用了 helper** —— 只断言 helper 返回值证明不了这点，所以走
     真实注入路径（Path C 正例）做 gate 级断言。
"""
import json

import pytest

from opensearch_pipeline.ingest_flags import image_content_override_enabled
from opensearch_pipeline.versions import build_run_provenance

ENV = "RAG_IMAGE_CONTENT_OVERRIDE"


# ── 1. 解析语义 ──────────────────────────────────────────────────────────────

def test_default_is_on_when_unset(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert image_content_override_enabled() is True


def test_empty_string_is_on(monkeypatch):
    """控制台把变量设成空值 ≡ 未设 —— 都是 ON，不能一个 ON 一个 OFF。"""
    monkeypatch.setenv(ENV, "")
    assert image_content_override_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE", "  Off  ", "NO"])
def test_explicit_off_values(monkeypatch, val):
    monkeypatch.setenv(ENV, val)
    assert image_content_override_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "  1 ", "enabled", "乱填"])
def test_on_and_unknown_values_stay_on(monkeypatch, val):
    """未知值保持 ON：默认 ON 的开关若"配错就关"，会静默退回旧行为且无人察觉。"""
    monkeypatch.setenv(ENV, val)
    assert image_content_override_enabled() is True


# ── 2. provenance ───────────────────────────────────────────────────────────

def test_provenance_carries_effective_bool(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    prov = build_run_provenance()
    assert prov["image_content_override"] is True
    monkeypatch.setenv(ENV, "0")
    assert build_run_provenance()["image_content_override"] is False


def test_provenance_serialises_as_json_bool(monkeypatch):
    """必须是 JSON 布尔 true/false，不是字符串 —— 审计侧按布尔读。"""
    monkeypatch.delenv(ENV, raising=False)
    assert '"image_content_override": true' in json.dumps(build_run_provenance())
    monkeypatch.setenv(ENV, "0")
    assert '"image_content_override": false' in json.dumps(build_run_provenance())


def test_chunk_provenance_whitelist_includes_the_flag():
    """chunk `_provenance` 是**固定白名单** —— 往 build_run_provenance() 加 key
    不会自动落到 chunk_meta.extra_json。这条钉死白名单确实含该字段。"""
    import pathlib
    src = pathlib.Path("opensearch_pipeline/pipeline_nodes.py").read_text(encoding="utf-8")
    seg = src[src.index('_provenance = {k: _prov.get(k) for k in ('):][:400]
    assert '"image_content_override"' in seg, "chunk _provenance 白名单缺该字段"


def test_flag_not_in_pipeline_run_columns():
    """本次刻意**不**进 pipeline_run（逐列清单，加列需迁移 059）。

    这条是范围钉子：日后有人加列时会看到它失败，从而记得同步文档与迁移。
    """
    import pathlib
    src = pathlib.Path("opensearch_pipeline/pipeline_run.py").read_text(encoding="utf-8")
    assert "image_content_override" not in src


# ── 3. gate 级：默认姿态真的作用到覆写路径 ──────────────────────────────────
#
# 只断言 helper 返回值证明不了"四个 gate 都改用了它"，所以必须走真实注入路径。
# 用 Path C 的既有正例（图上真印圈号 ②③④⑤ + 跨页 step 含 "②-⑤ 步" 区间引用）：
# 覆写开启时该图应被救回 step 3；关闭时按几何留在 step 4。

def _path_c_binding(monkeypatch, env_value):
    from opensearch_pipeline.chunker import DocumentChunker
    from opensearch_pipeline.pipeline_nodes import _inject_image_ref_blocks
    if env_value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, env_value)
    blocks = [
        {"block_type": "paragraph", "page_num": 2,
         "text": "步骤3：进入扫码报检界面操作 ②-⑤ 步,如下图所示完成填表。",
         "extra": {"y0": 100, "y1": 130}},
        {"block_type": "paragraph", "page_num": 3,
         "text": "步骤4：备注 — 报检后核对。", "extra": {"y0": 50, "y1": 80}},
    ]
    asset = {"image_index": 5, "page_num": 3, "bbox": [42, 90, 500, 200],
             "status": "ROUTE_TO_VECTOR", "filename": "img5.jpeg", "oss_key": "k5",
             "ocr_text": "② 扫码区 ③ 输入货号 ④ 选择班次 ⑤ 点击报检",
             "visual_summary": "U8 扫码报检界面操作流程图,显示扫码、输入、选择、点击四步",
             "vlm_annotation_map": {}}
    enriched = _inject_image_ref_blocks([dict(b) for b in blocks], [dict(asset)],
                                        {"doc_id": "T", "version_no": 1,
                                         "source_key": "raw/x", "owner_dept": "eval"})
    chunks = DocumentChunker(split_mode="step", min_chunk_chars=5).chunk_from_blocks(
        enriched, "T", 1, metadata={"title": "t.pdf"})
    for c in chunks:
        if c.chunk_type == "step_card" and "②-⑤" in (c.chunk_text or ""):
            return {r.get("image_index") for r in (c.extra.get("image_refs") or [])}
    return set()


def test_gate_default_unset_enables_override(monkeypatch):
    """未设 env ⇒ 覆写路径生效（钉死 gate 真的读了 helper，而非仍是旧的 allow-list）。"""
    assert 5 in _path_c_binding(monkeypatch, None)


def test_gate_explicit_off_disables_override(monkeypatch):
    assert 5 not in _path_c_binding(monkeypatch, "0")


def test_gate_explicit_on_matches_default(monkeypatch):
    assert _path_c_binding(monkeypatch, "1") == _path_c_binding(monkeypatch, None)


def test_no_gate_still_reads_env_directly():
    """源码级：四个 gate 不得再各自 os.getenv —— 否则解析语义会再次分叉。"""
    import pathlib
    src = pathlib.Path("opensearch_pipeline/pipeline_nodes.py").read_text(encoding="utf-8")
    body = src.split("def _insert_image_refs_heuristic")[1]
    assert 'os.getenv("RAG_IMAGE_CONTENT_OVERRIDE"' not in body and \
           'os.environ.get("RAG_IMAGE_CONTENT_OVERRIDE"' not in body, \
        "仍有 gate 直接读 env，未走 ingest_flags helper"
    assert body.count("image_content_override_enabled()") >= 4
