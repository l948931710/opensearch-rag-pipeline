# -*- coding: utf-8 -*-
"""D1（2026-07-25）：VLM 缓存里形态异常的条目必须 fail-open 成"未命中"，绝不打断抽取。

现网实测的坏形态：scratch/vlm_cache.sqlite3 里有条目的值是 JSON 数字而非 funnel dict，
命中即 `cached.get("ocr_text")` → AttributeError → **整篇文档抽取失败**（不是单图失败）。
两个消费者（嵌入图批量路径 _process_embedded_images、独立图片路径 _extract_image）都在
拿到条目后立刻 .get()/["status"]，所以校验必须收在唯一读入口 _vlm_cache_lookup。

校验到 status 而不止 isinstance(dict)：未知状态不抛异常，但会安静绕过 DISCARD 等值跳过、
带着空字段流进 asset_dict —— 比崩溃更难查。
"""
import os
from types import SimpleNamespace

import pytest

from opensearch_pipeline.extraction.unified_extractor import (
    UnifiedExtractor,
    _vlm_cache_entry_usable,
    _vlm_cache_lookup,
    _vlm_cache_ns,
)
from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor

H = "a" * 64          # SHA-256 形状的主键（现行 file_hash 长度）


def _good(**kw):
    e = {"status": "ROUTE_TO_VECTOR", "visual_summary": "登录页截图", "image_category": "ui",
         "vlm_annotation_map": {}, "reason": "", "width": 640, "height": 480,
         "file_size_kb": 55.0, "ocr_text": "用户名 密码"}
    e.update(kw)
    return e


# ── lookup 语义 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    5421,                       # 现网实测形态：JSON 数字
    ["ROUTE_TO_VECTOR"],
    "ROUTE_TO_VECTOR",
    3.14,
    True,
])
@pytest.mark.parametrize("is_public", [True, False])
def test_namespaced_non_dict_is_treated_as_miss(bad, is_public, monkeypatch):
    """非 dict 条目 = 未命中；消费者因此永远拿不到它，.get()/["status"] 不可达。"""
    monkeypatch.delenv("RAG_VLM_CACHE_VERSION", raising=False)
    cache = {f"{H}:{_vlm_cache_ns(is_public)}": bad}
    assert _vlm_cache_lookup(cache, H, is_public=is_public) is None


@pytest.mark.parametrize("entry", [
    {"visual_summary": "无 status 键"},
    {"status": ""},
    {"status": None},
    {"status": "garbage"},               # ← 钉死：不是"任意非空字符串"就放行
    {"status": "CLEAN"},                 # 似是而非的状态同样拒绝
    {"status": "DISCARD_UNREADABLE"},    # 写入门本就拒它；混进来也要重算（一次性解码失败）
    {"status": ["ROUTE_TO_VECTOR"]},
])
def test_namespaced_dict_with_unusable_status_is_miss(entry, monkeypatch):
    monkeypatch.delenv("RAG_VLM_CACHE_VERSION", raising=False)
    cache = {f"{H}:{_vlm_cache_ns(True)}": entry}
    assert _vlm_cache_lookup(cache, H, is_public=True) is None


@pytest.mark.parametrize("status", ["ROUTE_TO_VECTOR", "ROUTE_TO_TEXT", "DISCARD_DECORATIVE"])
def test_valid_namespaced_entry_still_hits(status, monkeypatch):
    """守卫不得过严：合法条目照常命中（防止把整个缓存打成 miss、白重跑 VLM）。"""
    monkeypatch.delenv("RAG_VLM_CACHE_VERSION", raising=False)
    e = _good(status=status)
    cache = {f"{H}:{_vlm_cache_ns(True)}": e}
    assert _vlm_cache_lookup(cache, H, is_public=True) is e


def test_quarantine_sensitive_must_hit_in_sec_namespace(monkeypatch):
    """写入门（_vlm_cache_put_allowed）并不拒 QUARANTINE_SENSITIVE，它能合法落 :sec。

    若给 namespaced 路径套 legacy public 的三值白名单，敏感图缓存会整体失效 →
    每次重跑 VLM 敏感审计。这条钉死两个集合不得合并。
    """
    monkeypatch.delenv("RAG_VLM_CACHE_VERSION", raising=False)
    e = _good(status="QUARANTINE_SENSITIVE", visual_summary="", ocr_text="")
    cache = {f"{H}:{_vlm_cache_ns(False)}": e}
    assert _vlm_cache_lookup(cache, H, is_public=False) is e


def test_quarantine_sensitive_still_never_reached_via_legacy_bare_key(monkeypatch):
    """legacy public 回退仍必须排除 QUARANTINE_SENSITIVE（复用会跳过敏感审计）。"""
    monkeypatch.delenv("RAG_VLM_CACHE_VERSION", raising=False)
    cache = {H: _good(status="QUARANTINE_SENSITIVE")}
    assert _vlm_cache_lookup(cache, H, is_public=True) is None


def test_malformed_namespaced_falls_through_to_legacy_bare_key(monkeypatch):
    """"视同未命中"必须是 fall-through，不是 early-return —— 否则 legacy 回退被畸形条目挡死。"""
    monkeypatch.delenv("RAG_VLM_CACHE_VERSION", raising=False)
    legacy = _good()
    cache = {f"{H}:{_vlm_cache_ns(True)}": 5421, H: legacy}
    assert _vlm_cache_lookup(cache, H, is_public=True) is legacy
    assert cache[f"{H}:{_vlm_cache_ns(True)}"] is legacy, "回退命中后应把合法条目迁移覆盖坏条目"


def test_entry_usable_contract():
    assert _vlm_cache_entry_usable(_good()) is True
    assert _vlm_cache_entry_usable(_good(status="QUARANTINE_SENSITIVE")) is True
    assert _vlm_cache_entry_usable(5421) is False
    assert _vlm_cache_entry_usable(None) is False
    assert _vlm_cache_entry_usable({"status": "DISCARD_UNREADABLE"}) is False


# ── 两个真实消费者：坏条目命中不得抛异常，且重算后自愈 ────────────────────────

def _mk_asset(tmp_path, name, content=b"img"):
    p = tmp_path / name
    p.write_bytes(content)
    return SimpleNamespace(local_path=str(p), original_name=name, page_num=1, image_index=0)


def _patch_funnel(monkeypatch, cache, result, calls):
    monkeypatch.setattr(ImageFunnelProcessor, "__init__", lambda self, simulate=False: None)
    monkeypatch.setattr(ImageFunnelProcessor, "screen_static",
                        lambda self, p: {"width": 200, "height": 200, "file_size_kb": 50.0,
                                         "aspect_ratio": 1.0, "verdict": "OK", "error": ""})

    def _process(self, local_path, doc_id, is_public=True, doc_title=""):
        calls.append(os.path.basename(local_path))
        return dict(result)

    monkeypatch.setattr(ImageFunnelProcessor, "process_image", _process)
    monkeypatch.setattr(UnifiedExtractor, "_load_vlm_cache", classmethod(lambda cls: cache))
    monkeypatch.setattr(UnifiedExtractor, "_save_vlm_cache",
                        classmethod(lambda cls, force_oss=False: None))


def _poison(sha_by_path, cache, is_public=True):
    for h in sha_by_path:
        cache[f"{h}:{_vlm_cache_ns(is_public)}"] = 5421


def test_embedded_image_path_survives_poisoned_cache(monkeypatch, tmp_path):
    """嵌入图批量路径：坏条目命中不抛（此前 AttributeError 会炸掉整篇文档）。"""
    monkeypatch.delenv("RAG_VLM_CACHE_VERSION", raising=False)
    import hashlib
    a = _mk_asset(tmp_path, "a.png", b"img-a")
    cache = {}
    _poison([hashlib.sha256(b"img-a").hexdigest()], cache)
    calls = []
    _patch_funnel(monkeypatch, cache, _good(), calls)

    ex = object.__new__(UnifiedExtractor)
    ex.simulate = False          # 写入门要求非 simulate，才能验自愈写回
    assets, _blocks, degraded = ex._process_embedded_images(
        [a], {"doc_id": "D", "raw_key": "raw/x.docx"})

    assert calls == ["a.png"], "坏条目应视同未命中 → 重算一次"
    assert [x["filename"] for x in assets] == ["a.png"]
    assert degraded == 0
    key = f"{hashlib.sha256(b'img-a').hexdigest()}:{_vlm_cache_ns(True)}"
    assert isinstance(cache[key], dict) and cache[key]["status"] == "ROUTE_TO_VECTOR", \
        "重算成功后同 key 必须被合法 dict 覆盖（自愈），否则每次抽取都要重付一次 VLM"


def test_standalone_image_path_survives_poisoned_cache(monkeypatch, tmp_path):
    """独立图片路径（A13 接缓存后同形状）：坏条目命中不抛。"""
    monkeypatch.delenv("RAG_VLM_CACHE_VERSION", raising=False)
    import hashlib
    p = tmp_path / "poster.png"
    p.write_bytes(b"poster-bytes")
    cache = {}
    _poison([hashlib.sha256(b"poster-bytes").hexdigest()], cache)
    calls = []
    _patch_funnel(monkeypatch, cache, _good(status="ROUTE_TO_TEXT", ocr_text="磨床操作流程"), calls)

    ex = object.__new__(UnifiedExtractor)
    ex.simulate = False
    res = ex._extract_image({"doc_id": "D", "version_no": 1, "raw_key": "raw/poster.png",
                             "local_path": str(p), "file_ext": "png", "title": "poster"})

    assert calls == ["poster.png"], "坏条目应视同未命中 → 重算一次"
    assert res is not None
    key = f"{hashlib.sha256(b'poster-bytes').hexdigest()}:{_vlm_cache_ns(True)}"
    assert isinstance(cache[key], dict), "独立图片路径同样要自愈写回"
