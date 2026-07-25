# -*- coding: utf-8 -*-
"""Unit tests for the L6 chunk-quality layer — pure metric functions, no I/O.

envboot forces simulate OFF, so the eval harness cannot be run in sim mode; these tests
ARE the offline validation path. Every test feeds synthetic in-memory chunks to the pure
family_* / analyze_corpus / merge_chunk_panel functions and asserts the detector + gate.
"""
from __future__ import annotations

import os as _os

# Importing the L6 layer triggers eval_harness.envboot.boot() at import time, which mutates
# global os.environ (forces RAG_SIMULATE=false + prod endpoints) for the LIVE harness. That
# leaks into other test files collected after this one. Snapshot + restore so the test session
# env is unchanged — the pure metric functions exercised here never read os.environ.
_SAVED_ENV = dict(_os.environ)
from eval_harness.layers import l6_chunk_quality as L6  # noqa: E402  (boots envboot)
from eval_harness import judge  # noqa: E402
_os.environ.clear()
_os.environ.update(_SAVED_ENV)


def _chunk(cid, doc, ctype="text_chunk", text="正常的一段文字内容，足够长以通过校验。",
           section="概述", img=None, fn="a.docx", token=20):
    return {"chunk_id": cid, "doc_id": doc, "chunk_type": ctype, "section_title": section,
            "chunk_text": text, "token_count": token, "image_refs_json": img,
            "original_filename": fn, "category_l1": "manual"}


_GOOD_D7 = {"D1": {"sym_diff": 0, "rds_active": 3078}, "D4": {"orphan_count": 0},
            "D7": {"missing": 0, "duplicate": 0},
            "D6": {"chunk_compliant_all": 712, "total_chunks": 724},
            "D3": {"routed_total": 306, "routed_with_step": 201}}
_CLEAN_H = {"rds_active": 2, "ha3_unique": 2, "truncated": False,
            "missing_in_ha3": 0, "extra_in_ha3": 0, "idset_jaccard": 1.0}


# ── Family B — boundary / size ────────────────────────────────────────────

def test_boundary_oversize_structural_detected():
    chunks = [_chunk("c1", "D1"),
              _chunk("c2", "D1", ctype="procedure_parent", text="步骤" * 3000, token=2400)]
    b = L6.family_boundary(chunks)
    assert b["oversize_count"] == 1
    assert b["oversize_structural_count"] == 1


def test_boundary_midsentence_doc_clustered():
    # D1: one clean + one mid-sentence -> doc mean 0.5; D2 clean -> 0 ; cluster mean 0.25
    chunks = [_chunk("c1", "D1", text="这句话正常结束。"),
              _chunk("c2", "D1", text="这句话没有结束符号被切断"),
              _chunk("c3", "D2", text="完整的句子。")]
    b = L6.family_boundary(chunks)
    assert 0.0 < b["midsentence_cut_rate"] <= 0.5


def test_boundary_token_drift():
    chunks = [_chunk("c1", "D1", text="一二三四五六七八九十", token=999)]
    b = L6.family_boundary(chunks)
    assert b["token_drift_count"] == 1


# ── Family C — self-containedness heuristic ───────────────────────────────

def test_self_containedness_dangling_anaphor():
    chunks = [_chunk("c1", "D1", text="该步骤需要先确认设备状态再继续操作。", section=None),
              _chunk("c2", "D1", text="完整独立的一句话内容。", section="安全")]
    c = L6.family_self_containedness_heuristic(chunks)
    assert c["dangling_anaphor_rate"] is not None and c["dangling_anaphor_rate"] > 0
    assert len(c["sample"]) >= 1


def test_self_containedness_with_section_title_not_flagged():
    # dangling opener but has a section_title -> antecedent resolvable -> not flagged
    chunks = [_chunk("c1", "D1", text="该流程分三步执行完成。", section="发货流程")]
    c = L6.family_self_containedness_heuristic(chunks)
    assert c["dangling_anaphor_rate"] == 0.0


# ── Family E — dedup ──────────────────────────────────────────────────────

def test_dedup_template_vs_twin():
    base = "消防器材应定期检查并保持完好有效随时可用确保安全无隐患，各班组每月需填写检查记录表归档。"
    chunks = [
        _chunk("x1", "DA", text=base + "。"),
        _chunk("x2", "DB", text=base + "！"),   # cross-doc near-dup -> twin contamination
        _chunk("x3", "DA", text=base + "。"),   # same-doc exact -> legit template
    ]
    d = L6.family_dedup(chunks)
    assert d["exact_dup_same_doc"] >= 1
    assert d["near_dup_pairs_cross_doc"] >= 1
    assert d["near_dup_cross_sample"][0]["jaccard"] >= 0.9


# ── Family F — image over-attach ──────────────────────────────────────────

def test_image_binding_overattach():
    chunks = [_chunk("c1", "D1", ctype="step_card",
                     img='[{"image_index":1},{"image_index":1},{"image_index":1}]')]
    f = L6.family_image_binding(chunks)
    assert f["img_dup_factor_max"] == 3.0
    assert f["n_chunks_with_images"] == 1


def test_image_binding_malformed_json():
    chunks = [_chunk("c1", "D1", ctype="step_card", img="{not valid json")]
    f = L6.family_image_binding(chunks)
    assert f["malformed_json"] == 1


# ── Family D — routing (metadata) ─────────────────────────────────────────

def test_routing_policy_doc_producing_faq_is_mismatch():
    # a policy doc (制度) should route clause; producing only faq_chunk is a family mismatch
    chunks = [{"chunk_id": "c1", "doc_id": "D1", "chunk_type": "faq_chunk",
               "section_title": None, "chunk_text": "问：x 答：y" * 5, "token_count": 20,
               "image_refs_json": None, "original_filename": "p.docx",
               "category_l1": "policy", "title": "员工管理制度"}]
    d = L6.family_routing(chunks, {"routed_total": 10, "routed_with_step": 4})
    assert d["mismatch_count"] == 1
    assert d["d3_under_chunk_candidates"] == 6


# ── verdict three-state ───────────────────────────────────────────────────

def test_verdict_go_on_clean_corpus():
    chunks = [_chunk("c1", "D1", text="完整的一句话内容结束。"),
              _chunk("c2", "D2", text="另一段完整内容结束。")]
    out = L6.analyze_corpus(chunks, _GOOD_D7, _CLEAN_H, code_commit="t")
    assert out["state"] == "GO"
    assert out["go_no_go"] is True


def test_verdict_no_go_defect_on_oversize_structural():
    chunks = [_chunk("c1", "D1", text="完整句子。"),
              _chunk("c2", "D2", ctype="procedure_parent", text="步骤" * 3000, token=2400)]
    out = L6.analyze_corpus(chunks, _GOOD_D7, _CLEAN_H, code_commit="t")
    assert out["state"] == "NO_GO_DEFECT"


def test_verdict_incomplete_evidence_without_d7():
    # clean measured gates but D1-D7 JSON absent ⇒ INCOMPLETE, never GO (no fail-open)
    chunks = [_chunk("c1", "D1", text="完整的一句话内容已经结束并且足够长以通过校验。")]
    out = L6.analyze_corpus(chunks, None, _CLEAN_H, code_commit="t")
    assert out["state"] == "NO_GO_INCOMPLETE_EVIDENCE"


def test_verdict_incomplete_on_truncated_idset():
    chunks = [_chunk("c1", "D1", text="完整的一句话内容已经结束并且足够长以通过校验。")]
    h = {"truncated": True}
    out = L6.analyze_corpus(chunks, _GOOD_D7, h, code_commit="t")
    assert out["state"] == "NO_GO_INCOMPLETE_EVIDENCE"


def test_verdict_no_go_defect_on_extra_in_ha3():
    # extra_in_ha3 > 0 ⇒ incomplete purge ⇒ hard fail (measured) ⇒ DEFECT
    chunks = [_chunk("c1", "D1", text="完整的一句话内容已经结束并且足够长以通过校验。")]
    h = {"truncated": False, "missing_in_ha3": 0, "extra_in_ha3": 7, "idset_jaccard": 0.9}
    out = L6.analyze_corpus(chunks, _GOOD_D7, h, code_commit="t")
    assert out["state"] == "NO_GO_DEFECT"


# ── H 门 fetch 二次定性（终局 2026-07-21：存在性唯 fetch 为准）────────────────

_H_GATE = "RDS↔HA3 all-type id-set (H)"
_ONE_CHUNK = [_chunk("c1", "D1", text="完整的一句话内容已经结束并且足够长以通过校验。")]


def _h_fetch(**kw):
    h = {"rds_active": 100, "ha3_unique": 97, "truncated": False,
         "missing_in_ha3": 3, "extra_in_ha3": 0, "idset_jaccard": 0.97}
    h.update(kw)
    return h


def test_verdict_go_on_clean_fetch_recheck():
    """枚举健康 + fetch 复核零缺 → H 门 PASS → GO。"""
    h = _h_fetch(missing_in_ha3=0, fetch_ok=True, missing_confirmed=0, enum_invisible=0,
                 missing_unclassified=0, missing_confirmed_sample=[])
    out = L6.analyze_corpus(_ONE_CHUNK, _GOOD_D7, h, code_commit="t")
    assert out["gates"][_H_GATE]["pass"] is True
    assert out["state"] == "GO"


def test_verdict_incomplete_on_enum_invisible():
    """⚠️ 语义翻转(2026-07-22)：倒排枚举下 enum_invisible>0 = 枚举/is_active 锚点失效
    （extra/orphan 方向不可测）→ INCOMPLETE。零向量时代它是预期伪影、判 PASS。"""
    h = _h_fetch(fetch_ok=True, missing_confirmed=0, enum_invisible=3,
                 missing_unclassified=0, missing_confirmed_sample=[])
    out = L6.analyze_corpus(_ONE_CHUNK, _GOOD_D7, h, code_commit="t")
    assert out["gates"][_H_GATE]["pass"] is None
    assert out["state"] == "NO_GO_INCOMPLETE_EVIDENCE"


def test_verdict_incomplete_on_unhealthy_buckets_even_without_truncation():
    """只查 truncated 会漏掉协议不健康桶（coveredPercent<1/越桶/重复 PK…）→ 破枚举
    也可能 PASS。enum_incomplete 必须是 truncated ∨ unhealthy_buckets。"""
    h = _h_fetch(missing_in_ha3=0, truncated=False,
                 unhealthy_buckets={0: "coveredPercent=0.5"})
    out = L6.analyze_corpus(_ONE_CHUNK, _GOOD_D7, h, code_commit="t")
    g = out["gates"][_H_GATE]
    assert g["pass"] is None and g["value"]["unhealthy_buckets"]
    assert out["state"] == "NO_GO_INCOMPLETE_EVIDENCE"


def test_verdict_defect_on_fetch_confirmed_missing():
    h = _h_fetch(fetch_ok=True, missing_confirmed=2, query_invisible=1,
                 missing_unclassified=0, missing_confirmed_sample=["cX", "cY"])
    out = L6.analyze_corpus(_ONE_CHUNK, _GOOD_D7, h, code_commit="t")
    assert out["gates"][_H_GATE]["pass"] is False
    assert out["state"] == "NO_GO_DEFECT"


def test_verdict_incomplete_on_unclassified():
    """fetch 部分批未定性 → 证据不完整（保守不 GO，也不误判 DEFECT）。"""
    h = _h_fetch(fetch_ok=True, missing_confirmed=0, query_invisible=2,
                 missing_unclassified=1, missing_confirmed_sample=[])
    out = L6.analyze_corpus(_ONE_CHUNK, _GOOD_D7, h, code_commit="t")
    assert out["gates"][_H_GATE]["pass"] is None
    assert out["state"] == "NO_GO_INCOMPLETE_EVIDENCE"


def test_verdict_incomplete_on_fetch_failure_not_defect():
    """fetch 尝试过但整体失败 → INCOMPLETE 而非 DEFECT（codex 共识 blocker：无法定性
    绝不能读成「实测数据丢失」）。"""
    h = _h_fetch(fetch_ok=False, fetch_error="RuntimeError: down")
    out = L6.analyze_corpus(_ONE_CHUNK, _GOOD_D7, h, code_commit="t")
    g = out["gates"][_H_GATE]
    assert g["pass"] is None
    assert g["value"]["fetch_error"] == "RuntimeError: down"
    assert out["state"] == "NO_GO_INCOMPLETE_EVIDENCE"


def test_verdict_extra_beats_fetch_failure():
    """extra 是 query 实际返回行（不受盲区影响）、确凿缺陷——即使 fetch 失败也 DEFECT，
    且 value 同时保留 extra 与 fetch_error 两份证据。"""
    h = _h_fetch(fetch_ok=False, fetch_error="down", extra_in_ha3=7)
    out = L6.analyze_corpus(_ONE_CHUNK, _GOOD_D7, h, code_commit="t")
    g = out["gates"][_H_GATE]
    assert g["pass"] is False
    assert g["value"]["extra"] == 7 and g["value"]["fetch_error"] == "down"
    assert out["state"] == "NO_GO_DEFECT"


def test_verdict_legacy_h_without_fetch_keeps_old_semantics():
    """无 fetch_ok 字段（历史/合成 h dict）→ 旧语义：query missing>0 即 DEFECT。"""
    h = {"truncated": False, "missing_in_ha3": 5, "extra_in_ha3": 0, "idset_jaccard": 0.99}
    out = L6.analyze_corpus(_ONE_CHUNK, _GOOD_D7, h, code_commit="t")
    assert out["gates"][_H_GATE]["pass"] is False
    assert out["state"] == "NO_GO_DEFECT"


# ── H fetch 复核的 I/O 缝（sys.modules 级打桩，无真连接）──────────────────────

def _stub_ha3live(monkeypatch, *, rds_conn):
    """拦截 l6 惰性 `from .. import ha3live` / `from ..ha3live import rds_conn` 两种形态：
    同时盖 sys.modules 与父包属性（父包属性在真模块曾被导入时优先生效）。"""
    import sys
    import types

    import eval_harness
    mod = types.ModuleType("eval_harness.ha3live")
    mod.client = lambda: object()
    mod.table = lambda: "tbl"
    mod.rds_conn = rds_conn
    monkeypatch.setitem(sys.modules, "eval_harness.ha3live", mod)
    monkeypatch.setattr(eval_harness, "ha3live", mod, raising=False)
    return mod


class _MapCursor:
    """把 IN (…) 的 chunk_id 参数映射回 {chunk_id, id}（'c<pk>' → pk）；可裁剪映射面。"""

    def __init__(self, mapped=None, calls=None):
        self._mapped = mapped
        self._calls = calls
        self._p = ()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self._p = params
        if self._calls is not None:
            self._calls.append(len(params))

    def fetchall(self):
        return [{"chunk_id": c, "id": int(c[1:])} for c in self._p
                if self._mapped is None or c in self._mapped]


class _MapConn:
    def __init__(self, mapped=None, calls=None):
        self._mapped, self._calls = mapped, calls

    def cursor(self):
        return _MapCursor(self._mapped, self._calls)

    def close(self):
        pass


def test_fetch_reclassify_idset_maps_and_classifies(monkeypatch):
    """chunk_id↔pk 双向映射 + 分类回写：confirmed/invisible/unclassified 各就其位。"""
    _stub_ha3live(monkeypatch, rds_conn=lambda: _MapConn())
    from opensearch_pipeline import reconcile as rec
    monkeypatch.setattr(rec, "_fetch_reclassify_missing",
                        lambda cli, tbl, missing: {"ok": True, "missing_confirmed": [11],
                                                   "query_invisible": [12], "unclassified": [13],
                                                   "fetch_errors": []})
    out = L6._fetch_reclassify_idset(["c11", "c12", "c13"])
    assert out["fetch_ok"] is True
    assert (out["missing_confirmed"], out["query_invisible"], out["missing_unclassified"]) \
        == (1, 1, 1)
    assert out["missing_confirmed_sample"] == ["c11"]


def test_fetch_reclassify_idset_unmapped_chunkid_counts_unclassified(monkeypatch):
    """corpus 拉取与枚举窗口间的 rechunk race：映射不到 pk 的 chunk_id 保守计 unclassified。"""
    _stub_ha3live(monkeypatch, rds_conn=lambda: _MapConn(mapped={"c11"}))
    from opensearch_pipeline import reconcile as rec
    monkeypatch.setattr(rec, "_fetch_reclassify_missing",
                        lambda cli, tbl, missing: {"ok": True, "missing_confirmed": [],
                                                   "query_invisible": [11], "unclassified": [],
                                                   "fetch_errors": []})
    out = L6._fetch_reclassify_idset(["c11", "cGone99"])
    assert out["fetch_ok"] is True
    assert out["query_invisible"] == 1 and out["missing_unclassified"] == 1
    assert out["missing_confirmed"] == 0


def test_fetch_reclassify_idset_total_fetch_failure_fail_open(monkeypatch):
    _stub_ha3live(monkeypatch, rds_conn=lambda: _MapConn())
    from opensearch_pipeline import reconcile as rec
    monkeypatch.setattr(rec, "_fetch_reclassify_missing",
                        lambda cli, tbl, missing: {"ok": False,
                                                   "error": "all 2 fetch batches failed",
                                                   "fetch_errors": ["batch@0: boom"]})
    out = L6._fetch_reclassify_idset(["c1", "c2"])
    assert out["fetch_ok"] is False
    assert "batches failed" in out["fetch_error"]


def test_fetch_reclassify_idset_exception_fail_open(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    _stub_ha3live(monkeypatch, rds_conn=_boom)
    out = L6._fetch_reclassify_idset(["c1"])
    assert out["fetch_ok"] is False and "db down" in out["fetch_error"]


def test_pk_map_batches_in_query(monkeypatch):
    """501 个 chunk_id → 两次 IN 查询（批 500），映射齐全。"""
    calls = []
    _stub_ha3live(monkeypatch, rds_conn=lambda: _MapConn(calls=calls))
    ids = [f"c{i}" for i in range(501)]
    out = L6._pk_map_for_chunk_ids(ids)
    assert calls == [500, 1]
    assert len(out) == 501 and out["c500"] == 500


def test_family_idset_triggers_fetch_only_when_missing(monkeypatch):
    """miss 非空且未截断才触发 fetch 复核；miss 空/截断均不触发（截断=候选源不可信）。"""
    seen = []

    def _fake_reclassify(miss):
        seen.append(list(miss))
        return {"fetch_ok": True, "missing_confirmed": 0, "query_invisible": len(miss),
                "missing_unclassified": 0, "missing_confirmed_sample": []}

    monkeypatch.setattr(L6, "_fetch_reclassify_idset", _fake_reclassify)
    monkeypatch.setattr(L6, "_ha3_all_active_chunk_ids",
                        lambda: {"ids": {"c1"}, "returned": 1, "truncated": False, "windows": 1})
    h = L6.family_idset_reconciliation({"c1", "c2"})
    assert h["missing_in_ha3"] == 1 and seen == [["c2"]]
    assert h["fetch_ok"] is True and h["query_invisible"] == 1

    seen.clear()
    h2 = L6.family_idset_reconciliation({"c1"})
    assert "fetch_ok" not in h2 and seen == []

    monkeypatch.setattr(L6, "_ha3_all_active_chunk_ids",
                        lambda: {"ids": set(), "returned": 0, "truncated": True, "windows": 0})
    h3 = L6.family_idset_reconciliation({"c1"})
    assert "fetch_ok" not in h3 and h3["truncated"] is True and seen == []


# ── fingerprint ───────────────────────────────────────────────────────────

def test_fingerprint_stable_and_sensitive():
    chunks = [_chunk("c1", "D1"), _chunk("c2", "D1")]
    fp1 = L6.compute_fingerprint(chunks, _GOOD_D7, "abc")
    fp2 = L6.compute_fingerprint(list(reversed(chunks)), _GOOD_D7, "abc")
    assert fp1["chunk_id_set_hash"] == fp2["chunk_id_set_hash"]  # order-independent
    fp3 = L6.compute_fingerprint(chunks + [_chunk("c3", "D2")], _GOOD_D7, "abc")
    assert fp3["chunk_id_set_hash"] != fp1["chunk_id_set_hash"]  # input drift detected
    assert fp1["rubric_version"] == L6.RUBRIC_VERSION


# ── judge bundle bucketing ────────────────────────────────────────────────

def test_judge_bundle_separates_buckets():
    chunks = [_chunk(f"c{i}", f"D{i % 5}") for i in range(60)]
    risk = {"c1", "c2", "c3"}
    bundle = L6.build_chunk_judge_bundle(chunks, risk, n_repr=20, n_risk=3, n_rare=5)
    buckets = {b["bucket"] for b in bundle}
    assert buckets == {"representative", "risk"}
    assert all(b["kind"] == "chunk" for b in bundle)
    assert all(b["rubric_version"] == L6.RUBRIC_VERSION for b in bundle)
    # risk items must not also appear as representative (no double counting)
    repr_ids = {b["item_id"] for b in bundle if b["bucket"] == "representative"}
    risk_ids = {b["item_id"] for b in bundle if b["bucket"] == "risk"}
    assert repr_ids.isdisjoint(risk_ids)


# ── l6_ab fix transforms (the chunker-fix spec) ───────────────────────────

def test_strip_shangwen_removes_breadcrumb_line_anywhere():
    from eval_harness import l6_ab  # clean import (no envboot at module top)
    txt = "【文档:X | 章节:Y】\n[上文] 2、适用范围\n3.2 正文内容在这里。"
    out = l6_ab.strip_shangwen(txt)
    assert "[上文]" not in out
    assert "【文档:X | 章节:Y】" in out and "3.2 正文内容在这里。" in out


def test_drop_stale_section_keeps_doc_drops_section():
    from eval_harness import l6_ab
    txt = "【文档:员工手册 | 章节:七、安全奖惩制度】\n正文是关于面试流程的内容。"
    out = l6_ab.drop_stale_section(txt)
    assert "章节:" not in out and "文档:员工手册" in out
    assert "正文是关于面试流程的内容。" in out


def test_is_weak_prev_and_clause_fragment():
    from eval_harness import l6_ab
    assert l6_ab.is_weak_prev("1、总则") and l6_ab.is_weak_prev("目的：")
    assert not l6_ab.is_weak_prev("3.2 公司合同管理小组负责合同管理工作")
    assert l6_ab.section_is_clause_fragment("5.2 急救药箱配置")
    assert not l6_ab.section_is_clause_fragment("第七章 附则")


def test_merge_chunk_panel_keeps_buckets_separate():
    bundle = [{"item_id": "a", "bucket": "representative", "chunk_type": "text_chunk",
               "rubric_version": "chunk_rubric_v1"},
              {"item_id": "b", "bucket": "risk", "chunk_type": "step_card",
               "rubric_version": "chunk_rubric_v1"}]
    panels = [{"judge": "j1", "verdicts": [
                {"item_id": "a", "self_containedness": 5, "coherence": 5, "type_fidelity": 5,
                 "truncation": 5, "overall": 5, "verdict": "pass", "rationale": "ok"},
                {"item_id": "b", "self_containedness": 2, "coherence": 2, "type_fidelity": 2,
                 "truncation": 2, "overall": 2, "verdict": "fail", "rationale": "cut"}]}]
    m = judge.merge_chunk_panel(bundle, panels)
    assert m["representative"]["pass_rate_overall_ge4"] == 1.0
    assert m["risk_enriched"]["pass_rate_overall_ge4"] == 0.0
    assert set(m["by_chunk_type"]) == {"text_chunk", "step_card"}
