# -*- coding: utf-8 -*-
"""选项 C（2026-07-26）：Funnel 3 弃图需要 VLM **明确表态**。

依据（`docs/evidence/funnel_c_category_probe_20260726/probe_full.json`，6 篇 GT PDF 过
Funnel 1 的 77 张逐张真调 VLM）：32 张判 `LOW_RELEVANCE`，其中 **26 张
`image_category="unknown"`**，而它们的 caption 全都准确实质（"一只手正在将多色线缆连接
至电脑主板上的CPU散热器风扇接口"）。prompt 给 `LOW_RELEVANCE` 的定义只有「装饰图、封面
Logo、页边留白、空白排版线条」—— 手在主板上操作一条都不沾。模型是在**拒绝归类**，不是在
断言"这是装饰"；在一个非断言上弃图违背 §3 的成本不对称（错弃 = 该步骤永久缺图且无信号）。

本文件钉四类契约：
  1. 分支矩阵：ON/OFF × 类别 × text_heavy 的完整真值表；
  2. **默认 ON**（2026-07-26 翻，Sam 拍板：先重写 GT 再翻），且未知 env 值不得意外关闭；
  3. **缓存隔离**（codex BLOCKER-1）：缓存命中判定发生在 `process_image` **之前**，
     键不带策略就会让 C-ON 被旧 DISCARD 条目短路 —— A/B 变假 ON，反向亦然；
  4. 字段传播：rescue 带上 caption/category/annotation_map，真弃也留结构化 category。
"""
import pytest

import opensearch_pipeline.extraction.unified_extractor as ue
from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor
from opensearch_pipeline.ingest_flags import (
    effective_funnel_policy_version,
    funnel_discard_requires_category_enabled,
)

ENV = "RAG_FUNNEL_DISCARD_REQUIRES_CATEGORY"


@pytest.fixture
def funnel(monkeypatch, tmp_path):
    """一个可注入 VLM 判决与 OCR 文本的 processor；不碰网络。"""
    proc = ImageFunnelProcessor.__new__(ImageFunnelProcessor)
    img = tmp_path / "x.png"
    img.write_bytes(b"x" * 4096)

    state = {"vlm": {"status": "LOW_RELEVANCE", "caption": "一只手正在将线缆接到主板风扇接口",
                     "image_category": "unknown", "annotation_map": {"①": "风扇口"}},
             "ocr": ""}

    monkeypatch.setattr(proc, "screen_static",
                        lambda p: {"width": 500, "height": 333, "file_size_kb": 66.7,
                                   "aspect_ratio": 1.5, "verdict": "OK", "error": None},
                        raising=False)
    monkeypatch.setattr(proc, "ocr_client",
                        type("O", (), {"ocr_image": lambda self, p, d: type(
                            "R", (), {"combined_text": state["ocr"]})()})(),
                        raising=False)
    monkeypatch.setattr(proc, "_vlm_audit_and_summary",
                        lambda **kw: dict(state["vlm"]), raising=False)

    def _run(category="unknown", status="LOW_RELEVANCE", ocr="", env=None, **extra):
        state["vlm"] = {"status": status, "caption": "一只手正在将线缆接到主板风扇接口",
                        "image_category": category, "annotation_map": {"①": "风扇口"}, **extra}
        state["ocr"] = ocr
        if env is None:
            monkeypatch.delenv(ENV, raising=False)
        else:
            monkeypatch.setenv(ENV, env)
        return proc.process_image(str(img), "D", is_public=True)

    return _run


# ── 1. 分支矩阵 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cat", ["unknown", "product_photo", "step_screenshot", "", "test_photo"])
def test_on_rescues_when_vlm_declined_to_assert_decorative(funnel, cat):
    """实测里 26/32 就是这一格：模型拒绝归类却给了准确 caption。"""
    r = funnel(category=cat, env="1")
    assert r["status"] == "ROUTE_TO_VECTOR"
    assert "did not assert decorative" in r["reason"]


@pytest.mark.parametrize("cat", ["decorative", "logo_header"])
def test_on_still_discards_when_vlm_asserts_decorative(funnel, cat):
    """VLM 明确说是装饰 —— 这是断言，照弃。实测 6/32 属于这格（含真 FULING logo）。"""
    r = funnel(category=cat, env="1")
    assert r["status"] == "DISCARD_DECORATIVE"
    assert r["image_category"] == cat


@pytest.mark.parametrize("cat", ["unknown", "decorative", "product_photo"])
def test_explicit_off_restores_legacy_discard(funnel, cat):
    """显式关掉 = 历史行为逐条不变，与类别无关（回滚路径）。"""
    assert funnel(category=cat, env="0")["status"] == "DISCARD_DECORATIVE"


def test_text_heavy_branch_is_untouched_by_c(funnel):
    """C 只碰"非 text_heavy"那一格。文字多的低相关图两种姿态下都走文本路由。"""
    heavy = "字" * 200          # > 120 阈值
    for env in (None, "1"):
        r = funnel(category="unknown", ocr=heavy, env=env)
        assert r["status"] == "ROUTE_TO_TEXT", f"env={env}"


def test_clean_and_sensitive_paths_are_untouched(funnel):
    r = funnel(status="CLEAN", category="step_screenshot", env="1")
    assert r["status"] == "ROUTE_TO_VECTOR" and "did not assert" not in r.get("reason", "")


# ── 2. 默认姿态 ─────────────────────────────────────────────────────────────

def test_default_is_on(monkeypatch):
    """2026-07-26 翻默认（Sam 拍板：先重写 GT 再翻）。依据见 helper docstring：
    解掉 GT 与 manifest 两层循环性后，pdf 0.84919 → 0.91320。"""
    monkeypatch.delenv(ENV, raising=False)
    assert funnel_discard_requires_category_enabled() is True
    assert effective_funnel_policy_version() == "c1"


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "  On ", "", "乱填", "enabled"])
def test_on_and_unknown_values_stay_on(monkeypatch, val):
    """默认 ON 的开关必须"配错也开" —— 若把未知值当关，配错一个字就会静默退回旧判据、
    把步骤图重新弃掉且无人察觉（同 RAG_IMAGE_CONTENT_OVERRIDE 的极性）。"""
    monkeypatch.setenv(ENV, val)
    assert funnel_discard_requires_category_enabled() is True
    assert effective_funnel_policy_version() == "c1"


@pytest.mark.parametrize("val", ["0", "false", "off", "no", "OFF", "  0 "])
def test_explicit_off_values(monkeypatch, val):
    monkeypatch.setenv(ENV, val)
    assert funnel_discard_requires_category_enabled() is False
    assert effective_funnel_policy_version() == ""


# ── 3. 缓存隔离（codex BLOCKER-1）────────────────────────────────────────────

def _entry(status, reason="", cat=""):
    return {"status": status, "reason": reason, "image_category": cat,
            "visual_summary": "s", "ocr_text": "", "width": 500, "height": 333,
            "file_size_kb": 66.7, "vlm_annotation_map": {}}


def test_legacy_key_shape_is_preserved_when_explicitly_off(monkeypatch):
    """显式关掉时键必须**逐字节回到历史形态** —— 存量 5 万条条目靠它继续命中。"""
    monkeypatch.setenv(ENV, "0")
    monkeypatch.setenv("RAG_VLM_CACHE_VERSION", "2")
    assert ue._vlm_cache_ns(True) == "pub:2"
    assert ue._vlm_cache_ns(False) == "sec:2"


def test_default_key_carries_the_policy_suffix(monkeypatch):
    """默认 ON ⇒ 键默认带 :c1。这不是疏忽：命中判定在 process_image **之前**，键不带
    策略的话开了 C 也会被旧 DISCARD 条目短路。代价（对曾被 Funnel-3 弃的图重判）由
    跨策略条件回退限定在有界范围内。"""
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setenv("RAG_VLM_CACHE_VERSION", "2")
    assert ue._vlm_cache_ns(True) == "pub:2:c1"


def test_on_does_not_read_a_pre_c_funnel3_discard(monkeypatch):
    """**核心隔离断言**：命中判定在 process_image 之前，若 C-ON 能读到旧的 Funnel-3
    弃图条目，C 就永远看不到 category，A/B 变成假 ON。"""
    monkeypatch.setenv("RAG_VLM_CACHE_VERSION", "2")
    cache = {"h:pub:2": _entry("DISCARD_DECORATIVE",
                               "Funnel 3: Low Semantic Value or Generic Stock Photo")}
    monkeypatch.delenv(ENV, raising=False)          # 默认即 ON
    assert ue._vlm_cache_lookup(cache, "h", True) is None, "C-ON 复用了 pre-C 的语义弃图"
    monkeypatch.setenv(ENV, "0")
    assert ue._vlm_cache_lookup(cache, "h", True) is not None, "显式 OFF 下该条目应照常命中"


def test_on_reuses_entries_c_cannot_change(monkeypatch):
    """反面同样要紧：不能整体换 namespace。现在挡住制度漂移的**恰恰是** 07-20 那批
    CLEAN 判决（方案 §8.3）—— 把它们一起作废等于亲手拆掉保护。"""
    monkeypatch.setenv("RAG_VLM_CACHE_VERSION", "2")
    monkeypatch.setenv(ENV, "1")
    for status, reason in (("ROUTE_TO_VECTOR", ""), ("ROUTE_TO_TEXT", ""),
                           ("QUARANTINE_SENSITIVE", "Funnel 3: sensitive"),
                           ("DISCARD_DECORATIVE", "Funnel 1: Structural Heuristics")):
        cache = {"h:pub:2": _entry(status, reason)}
        got = ue._vlm_cache_lookup(cache, "h", True)
        assert got is not None, f"{status} 不受 C 影响，应可跨策略复用"
        assert cache.get("h:pub:2:c1") is got, "命中后应迁移到当前策略键"


def test_unattributable_discard_is_re_decided(monkeypatch):
    """认不出 Funnel 级别的 DISCARD ⇒ 保守重判。宁可多付一次调用，也不让一条来路不明的
    旧弃图在新判据下静默沿用 —— 错弃是不可见且不可恢复的那一侧。"""
    monkeypatch.setenv("RAG_VLM_CACHE_VERSION", "2")
    monkeypatch.setenv(ENV, "1")
    for reason in ("", "legacy", "Funnel 2 something"):
        cache = {"h:pub:2": _entry("DISCARD_DECORATIVE", reason)}
        assert ue._vlm_cache_lookup(cache, "h", True) is None, f"reason={reason!r} 应重判"


def test_cross_policy_fallback_respects_pub_sec(monkeypatch):
    """pub/sec 隔离在跨策略回退里同样不能破 —— public 判决绝不能服务隔离文档。"""
    monkeypatch.setenv("RAG_VLM_CACHE_VERSION", "2")
    monkeypatch.setenv(ENV, "1")
    cache = {"h:pub:2": _entry("ROUTE_TO_VECTOR")}
    assert ue._vlm_cache_lookup(cache, "h", False) is None


def test_legacy_policy_fallback_is_a_noop(monkeypatch):
    """历史判据下跨策略回退必须是 no-op（当前键就是基础键，不得重复迁移/自我覆盖）。"""
    monkeypatch.setenv(ENV, "0")
    assert ue._cross_policy_cache_fallback({"h:pub:2": _entry("ROUTE_TO_VECTOR")},
                                           "h", True) is None


# ── 4. 字段传播 ─────────────────────────────────────────────────────────────

def test_rescue_carries_the_already_parsed_fields(funnel):
    """这些字段本来就在同一次 VLM 调用里解析好了，只是被旧分支丢掉（`:129-148`）。"""
    r = funnel(category="unknown", env="1")
    assert r["visual_summary"].startswith("一只手")
    assert r["vlm_annotation_map"] == {"①": "风扇口"}
    assert r["image_category"] == "unknown"
    assert "ocr_text" in r


def test_degraded_propagates_on_both_low_relevance_branches(funnel):
    """契约缺口：LOW_RELEVANCE 两条分支原本都不传 degraded，而缓存写入门正是靠它拒绝
    把一次性故障固化成永久标签（`_vlm_cache_put_allowed`）。"""
    assert funnel(category="unknown", env="1", degraded=True)["degraded"] is True
    assert funnel(category="decorative", env="1", degraded=True)["degraded"] is True
    assert funnel(category="unknown", env=None, degraded=True)["degraded"] is True
    assert funnel(category="unknown", ocr="字" * 200, env=None, degraded=True)["degraded"] is True


def test_true_discard_keeps_structured_category(funnel):
    """真弃也要留结构化 category，否则日后只有一句 free-text reason 声称"VLM 明确表态"，
    没有可复核的证据（codex MAJOR-5）。"""
    assert funnel(category="decorative", env=None)["image_category"] == "decorative"


# ── regime / provenance 接线 ────────────────────────────────────────────────

def test_provenance_carries_funnel_policy(monkeypatch):
    from opensearch_pipeline.versions import build_run_provenance
    monkeypatch.delenv(ENV, raising=False)
    assert build_run_provenance()["funnel_policy"] == "c1"
    monkeypatch.setenv(ENV, "0")
    assert build_run_provenance()["funnel_policy"] == ""


def test_chunk_provenance_whitelist_includes_funnel_policy():
    import pathlib
    src = pathlib.Path("opensearch_pipeline/pipeline_nodes.py").read_text(encoding="utf-8")
    seg = src[src.index('_provenance = {k: _prov.get(k) for k in ('):][:400]
    assert '"funnel_policy"' in seg


def test_regime_hard_fails_across_policies_but_is_invisible_by_default(monkeypatch):
    """跨判据比较必须硬失败；默认姿态下该键要对存量 baseline 隐形（回 None 而非 ""）。"""
    from eval_harness.baseline import _LENIENT_REGIME_KEYS, _REGIME_KEYS, regime_matches
    from eval_harness.run_eval import _funnel_policy
    assert "funnel_policy" in _REGIME_KEYS
    assert "funnel_policy" not in _LENIENT_REGIME_KEYS
    monkeypatch.setenv(ENV, "0")
    assert _funnel_policy() is None                 # 历史判据对存量 baseline 隐形
    assert regime_matches({}, {"funnel_policy": _funnel_policy()})[0] is True
    monkeypatch.delenv(ENV, raising=False)          # 默认即 c1
    ok, diffs = regime_matches({}, {"funnel_policy": _funnel_policy()})
    assert ok is False and "funnel_policy" in diffs, "翻默认值必须对任何历史基线硬失配"
