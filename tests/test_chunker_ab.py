# -*- coding: utf-8 -*-
"""tests/test_chunker_ab.py — Step C 框架单元测试.

Coverage:
  - TopologyFingerprint pairing(byte-equal / image_refs 差异 / text_len 差 / 缺 key)
  - SemanticChunkSig.is_compatible 边界
  - _chunk_to_sig 兼容 dict + dataclass
  - _parse_arm_env 字符串解析
  - ComparisonReport.to_markdown 不爆
  - CLI dispatch QUICK_INJECT / FULL_REINDEX 抛 NotImplementedError 指引
"""
from __future__ import annotations

import pytest

from eval_harness.chunker_ab import (
    Arm,
    ChunkerAB,
    ComparisonReport,
    Mode,
    SemanticChunkSig,
    TopologyFingerprint,
    _chunk_to_sig,
    _parse_arm_env,
    _span_overlap,
    build_topology,
    check_topology_pairing,
)


# ── _span_overlap ──

def test_span_overlap_full():
    assert _span_overlap((1, 5), (1, 5)) == 1.0


def test_span_overlap_partial():
    # (1,5) ∩ (3,7) = (3,5) → 长度 3 / 短者 5 = 0.6
    assert abs(_span_overlap((1, 5), (3, 7)) - 0.6) < 1e-6


def test_span_overlap_disjoint():
    assert _span_overlap((1, 3), (5, 7)) == 0.0


# ── SemanticChunkSig.is_compatible ──

def test_sig_compatible_identical():
    a = SemanticChunkSig("step_card", (1, 1), ("intro",), 100, ())
    b = SemanticChunkSig("step_card", (1, 1), ("intro",), 100, ())
    ok, reason = a.is_compatible(b)
    assert ok, reason


def test_sig_compatible_image_refs_diff_allowed():
    """D8 改动核心:image_refs 差异允许(text/topology 不变)."""
    a = SemanticChunkSig("step_card", (1, 1), ("s",), 200, ((("a",),)))
    b = SemanticChunkSig("step_card", (1, 1), ("s",), 200,
                         ((("a",), ("b",))))   # ON 加了一张图
    ok, _ = a.is_compatible(b)
    assert ok


def test_sig_compatible_text_len_15pct_ok():
    a = SemanticChunkSig("step_card", (1, 1), ("s",), 100, ())
    b = SemanticChunkSig("step_card", (1, 1), ("s",), 115, ())  # +15% < 20%
    ok, _ = a.is_compatible(b)
    assert ok


def test_sig_compatible_text_len_25pct_rejected():
    a = SemanticChunkSig("step_card", (1, 1), ("s",), 100, ())
    b = SemanticChunkSig("step_card", (1, 1), ("s",), 125, ())  # +25% > 20%
    ok, reason = a.is_compatible(b)
    assert not ok
    assert "text_len" in reason


def test_sig_compatible_chunk_type_diff_rejected():
    a = SemanticChunkSig("step_card", (1, 1), ("s",), 100, ())
    b = SemanticChunkSig("text_chunk", (1, 1), ("s",), 100, ())
    ok, _ = a.is_compatible(b)
    assert not ok


def test_sig_compatible_section_diff_rejected():
    a = SemanticChunkSig("step_card", (1, 1), ("intro",), 100, ())
    b = SemanticChunkSig("step_card", (1, 1), ("outro",), 100, ())
    ok, _ = a.is_compatible(b)
    assert not ok


def test_sig_compatible_page_overlap_low_rejected():
    a = SemanticChunkSig("step_card", (1, 5), ("s",), 100, ())
    b = SemanticChunkSig("step_card", (10, 14), ("s",), 100, ())  # disjoint
    ok, _ = a.is_compatible(b)
    assert not ok


# ── _chunk_to_sig 兼容性 ──

def test_chunk_to_sig_from_dict():
    chunk = {
        "chunk_type": "step_card",
        "chunk_text": "x" * 120,
        "seq_no": 3,
        "extra": {
            "step_no": 2, "sub_no": 1, "section_no": "2.1",
            "page_num": 5,
            "section_path": ["a", "b"],
            "image_refs": [
                {"oss_key": "raw/a.png", "image_index": 5, "page_num": 5,
                 "source_image": "p5_5"},
            ],
        },
    }
    key, sig = _chunk_to_sig(chunk)
    assert key == ("step_card", 2, 1, "2.1", 3)
    assert sig.chunk_type == "step_card"
    assert sig.page_span == (5, 5)
    assert sig.section_path == ("a", "b")
    assert sig.text_len == 120
    assert len(sig.image_ref_keys) == 1


def test_chunk_to_sig_from_dataclass_like():
    class Chunk:
        chunk_type = "text_chunk"
        chunk_text = "abc"
        seq_no = 1
        extra = {"section_path": []}
    key, sig = _chunk_to_sig(Chunk())
    assert key[0] == "text_chunk"
    assert sig.text_len == 3
    assert sig.image_ref_keys == ()


# ── build_topology + check_topology_pairing ──

def _mk_chunks(spec):
    """[(step, sub, sec_no, type, text, img_keys)] → dict chunks."""
    out = []
    for i, (step, sub, sec, typ, text, img_keys) in enumerate(spec):
        out.append({
            "chunk_type": typ, "chunk_text": text, "seq_no": i,
            "extra": {
                "step_no": step, "sub_no": sub, "section_no": sec,
                "section_path": [],
                "image_refs": [{"oss_key": k, "image_index": idx,
                                 "page_num": 1, "source_image": k,
                                 "anchor_row": None}
                                for idx, k in enumerate(img_keys)],
            },
        })
    return out


def test_topology_pairing_byte_equal():
    chunks = _mk_chunks([
        (1, None, None, "step_card", "step one " * 30, []),
        (2, None, None, "step_card", "step two " * 30, []),
    ])
    topo = {"off": {"doc_A": build_topology("doc_A", chunks)},
            "on":  {"doc_A": build_topology("doc_A", chunks)}}
    r = check_topology_pairing(topo, ["off", "on"])
    assert r["all_pairable"], r["doc_failures"]
    assert r["n_pairable"] == 1


def test_topology_pairing_d8_image_refs_diff_ok():
    """D8 场景:ON arm 多绑了几张图,topology key 一致 → 应 pairable."""
    off = _mk_chunks([
        (1, None, None, "step_card", "step one " * 30, ["raw/a.png"]),
        (2, None, None, "step_card", "step two " * 30, []),
    ])
    on = _mk_chunks([
        (1, None, None, "step_card", "step one " * 30,
         ["raw/a.png", "raw/b.png", "raw/c.png"]),         # ON 多 2 图
        (2, None, None, "step_card", "step two " * 30,
         ["raw/d.png"]),                                    # ON 多 1 图
    ])
    topo = {"off": {"d": build_topology("d", off)},
            "on":  {"d": build_topology("d", on)}}
    r = check_topology_pairing(topo, ["off", "on"])
    assert r["all_pairable"], r["doc_failures"]


def test_topology_pairing_chunk_count_diff_rejected():
    """两 arm chunk 数不同(假改 split sop_size 800→1200 场景)→ 应拒."""
    off = _mk_chunks([(1, None, None, "step_card", "x" * 100, [])])
    on = _mk_chunks([
        (1, None, None, "step_card", "x" * 100, []),
        (2, None, None, "step_card", "y" * 100, []),
    ])
    topo = {"off": {"d": build_topology("d", off)},
            "on":  {"d": build_topology("d", on)}}
    r = check_topology_pairing(topo, ["off", "on"])
    assert not r["all_pairable"]
    assert "semantic key mismatch" in r["doc_failures"][0]


def test_topology_pairing_text_len_diff_rejected():
    """同 topology key 但 text 差 >20% → 应拒(避免大改 chunk_size 同时跑)."""
    off = _mk_chunks([(1, None, None, "step_card", "x" * 100, [])])
    on = _mk_chunks([(1, None, None, "step_card", "x" * 200, [])])  # +100%
    topo = {"off": {"d": build_topology("d", off)},
            "on":  {"d": build_topology("d", on)}}
    r = check_topology_pairing(topo, ["off", "on"])
    assert not r["all_pairable"]


# ── _parse_arm_env ──

def test_parse_arm_env_simple():
    name, env = _parse_arm_env("off:")
    assert name == "off"
    assert env == {}


def test_parse_arm_env_one_kv():
    name, env = _parse_arm_env("on:RAG_IMAGE_CONTENT_OVERRIDE=1")
    assert name == "on"
    assert env == {"RAG_IMAGE_CONTENT_OVERRIDE": "1"}


def test_parse_arm_env_two_kv():
    name, env = _parse_arm_env("on:A=1,B=2")
    assert env == {"A": "1", "B": "2"}


def test_parse_arm_env_no_colon_raises():
    with pytest.raises(ValueError):
        _parse_arm_env("off")


def test_parse_arm_env_no_equals_raises():
    with pytest.raises(ValueError):
        _parse_arm_env("on:NO_EQUALS")


# ── ComparisonReport.to_markdown ──

def test_comparison_report_markdown_renders(tmp_path):
    r = ComparisonReport(
        mode="binding_only", arms=["off", "on"],
        metrics={"off": {"mean_jaccard_pdf": 0.83}, "on": {"mean_jaccard_pdf": 0.93}},
        deltas={"mean_jaccard_pdf": {"delta": 0.10}},
        win_tie_loss={"jaccard_pdf": {"win": 8, "tie": 2, "loss": 1}},
        per_case=[{"doc_id": "d", "gt_label": "x", "delta": 0.1}],
        topology_check={"all_pairable": True, "n_docs": 1, "n_pairable": 1,
                         "n_failed": 0, "doc_failures": []},
        validity_notes=["Tier 0 funnel image_index, not semantic anchor."],
        meta={"git_commit": "abc123def456", "timestamp": "2026-06-14T11:00:00",
              "seed": 20260614, "arms": []},
    )
    md = r.to_markdown()
    assert "binding_only" in md
    assert "off" in md and "on" in md
    assert "0.8300" in md and "0.9300" in md
    assert "Δ" in md or "+0.1000" in md  # delta column rendered
    assert "win=8" in md
    saved = r.save(tmp_path)
    assert saved.exists()
    assert (tmp_path / "per_case.json").exists()


# ── ChunkerAB dispatch ──

def test_chunker_ab_requires_two_arms():
    with pytest.raises(ValueError, match="双 arm"):
        ChunkerAB(mode=Mode.BINDING_ONLY,
                  arms=[Arm("off")],
                  out_dir="/tmp/chunker_ab_test")


def test_chunker_ab_quick_inject_raises_with_guide():
    runner = ChunkerAB(mode=Mode.QUICK_INJECT,
                       arms=[Arm("off"), Arm("on")],
                       out_dir="/tmp/chunker_ab_test")
    with pytest.raises(NotImplementedError, match="Tier 1"):
        runner.run_quick_inject()


def test_chunker_ab_full_reindex_raises_with_guide():
    runner = ChunkerAB(mode=Mode.FULL_REINDEX,
                       arms=[Arm("off"), Arm("on")],
                       out_dir="/tmp/chunker_ab_test")
    with pytest.raises(NotImplementedError, match="Tier 2"):
        runner.run_full_reindex()
