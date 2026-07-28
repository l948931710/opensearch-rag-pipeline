# -*- coding: utf-8 -*-
"""M3/M4（2026-07-25）：L4-ing 评测口径指纹 + many-to-one 碰撞留痕。

M3 的存在理由：GT→产出卡的匹配判定链自身就会移动 `l4ing.jaccard.*`，而 `code_commit`
虽被 `_regime()` 记录却刻意不在 `_REGIME_KEYS` 里（它对任何无关提交都变，进闸即噪声）。
结果"改了尺子"与"改了管线"在差量网里此前完全无法区分 —— 新尺子静默比对旧 baseline。

M4 的边界：只把 many-to-one **记下来**，不阻止它。一张产出卡合法覆盖多个 GT 子步骤时
（chunker 真把两个子步骤合成了一张卡），两条 GT 都该打在它上面、各得部分分，那正是
对"合并"的正确惩罚；引入 consumed set 强行拆开是假阴性。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from eval_harness import baseline as bl
from eval_harness.binding import ingestion_binding as ib
from eval_harness.binding.gt_loader import GtChunk


KEY = "l4_ingestion_evaluator_version"


# ── M3：regime 指纹 ─────────────────────────────────────────────────────────

def test_evaluator_version_is_a_regime_key():
    assert KEY in bl._REGIME_KEYS


def test_evaluator_version_is_not_lenient():
    """刻意不进宽容窗口：跨口径比较必须硬失败、强制重冻。

    vlm_model / l4_gt_sha 等键当初进宽容窗口是因为"存量 baseline 缺键但口径其实没变"；
    本键相反 —— 缺键恰恰意味着那份 baseline 产自旧口径，**不可比**。
    """
    assert KEY not in bl._LENIENT_REGIME_KEYS
    assert KEY not in getattr(bl, "_BILATERAL_LENIENT_KEYS", frozenset())


def _regime(**kw):
    base = {k: None for k in bl._REGIME_KEYS}
    base.update(kw)
    return base


def test_regime_matches_on_same_evaluator_version():
    a = _regime(**{KEY: "2.0.0"})
    assert bl.regime_matches(a, dict(a))[0] is True


def test_regime_mismatches_on_different_evaluator_version():
    a = _regime(**{KEY: "2.0.0"})
    b = _regime(**{KEY: "3.0.0"})
    ok, diff = bl.regime_matches(a, b)
    assert ok is False and any(KEY in d for d in diff)


def test_regime_mismatches_when_old_baseline_lacks_the_key():
    """存量 baseline 没有该键 → 必须 mismatch（它产自 1.0.0 口径）。"""
    old = {k: v for k, v in _regime(**{KEY: "2.0.0"}).items() if k != KEY}
    cur = _regime(**{KEY: "2.0.0"})
    ok, diff = bl.regime_matches(old, cur)
    assert ok is False and any(KEY in d for d in diff)


def test_run_eval_regime_carries_the_version():
    from eval_harness.run_eval import _l4_ingestion_evaluator_version
    assert _l4_ingestion_evaluator_version() == ib.L4_INGESTION_EVALUATOR_VERSION


# ── M4：碰撞留痕 ───────────────────────────────────────────────────────────

@dataclass
class FakeChunk:
    chunk_text: str
    chunk_id: Optional[str] = None
    chunk_type: str = "step_card"
    page_num: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def _run_eval_doc(monkeypatch, chunks, gt_chunks):
    from eval_harness.binding.gt_loader import GtDoc
    monkeypatch.setattr(ib, "_extract_and_chunk", lambda *a, **k: chunks)
    doc = GtDoc(label="d", fmt="pdf", doc_sha256=None, extractor_version=None,
                manifest_path=None, degraded=False, gt_chunks=gt_chunks)
    return ib.evaluate_doc("d", doc, "/nonexistent.pdf")


def test_shared_chunk_is_recorded_on_both_gt_rows(monkeypatch):
    """两条 GT 命中同一张卡 → 双方 per_chunk 各自列出对方，且**分数不变**。"""
    merged = FakeChunk("4.2 填写完后 设备 班次 数量 人员", chunk_id="c-42",
                       extra={"step_no": 4, "image_refs": [{"image_index": 9},
                                                           {"image_index": 10}]})
    gts = [
        GtChunk(label="步骤4.1", chunk_type="step_card", keywords=["设备", "班次"],
                expected_image_refs=[], has_strong_refs=True),
        GtChunk(label="步骤4.2", chunk_type="step_card", keywords=["人员", "设备"],
                expected_image_refs=[], has_strong_refs=True),
    ]
    out = _run_eval_doc(monkeypatch, [merged], gts)
    rows = {r["gt_label"]: r for r in out["per_chunk"]}
    assert rows["步骤4.1"]["shared_with_gt"] == ["步骤4.2"]
    assert rows["步骤4.2"]["shared_with_gt"] == ["步骤4.1"]
    assert rows["步骤4.1"]["matched_produced_chunk_id"] == "c-42"
    # 关键：留痕不改分 —— 两条 GT 仍各自独立按同一张卡计分（不引入 consumed set）
    assert rows["步骤4.1"]["jaccard"] == rows["步骤4.2"]["jaccard"]


def test_unshared_match_records_empty_shared_list(monkeypatch):
    a = FakeChunk("步骤1 清点 抄录 货号", chunk_id="c-1", extra={"step_no": 1})
    b = FakeChunk("步骤2 收集 核对 交货单", chunk_id="c-2", extra={"step_no": 2})
    gts = [
        GtChunk(label="步骤1", chunk_type="step_card", keywords=["清点", "抄录"],
                expected_image_refs=[], has_strong_refs=True),
        GtChunk(label="步骤2", chunk_type="step_card", keywords=["收集", "核对"],
                expected_image_refs=[], has_strong_refs=True),
    ]
    out = _run_eval_doc(monkeypatch, [a, b], gts)
    for r in out["per_chunk"]:
        assert r["shared_with_gt"] == [], "未共享必须固定输出空列表，不是缺键"


def test_sharing_keyed_by_chunk_id_not_text(monkeypatch):
    """同文本不同卡不算共享 —— 判据必须是稳定 chunk_id，不是截断文本。"""
    a = FakeChunk("完全相同的正文 清点 抄录", chunk_id="c-1", extra={"step_no": 1})
    b = FakeChunk("完全相同的正文 清点 抄录", chunk_id="c-2", extra={"step_no": 2})
    gts = [
        GtChunk(label="步骤1", chunk_type="step_card", keywords=["清点", "抄录"],
                expected_image_refs=[], has_strong_refs=True),
        GtChunk(label="步骤2", chunk_type="step_card", keywords=["清点", "抄录"],
                expected_image_refs=[], has_strong_refs=True),
    ]
    out = _run_eval_doc(monkeypatch, [a, b], gts)
    ids = {r["gt_label"]: r["matched_produced_chunk_id"] for r in out["per_chunk"]}
    assert ids["步骤1"] != ids["步骤2"]
    for r in out["per_chunk"]:
        assert r["shared_with_gt"] == []


def test_abstention_emits_auditable_bundle_entry(monkeypatch):
    """M2 弃权：记 0 分且进分母（语义不变），但要在 bundle 里留下规范化空条目。"""
    from eval_harness.binding.ref_keys import ImageRef
    unrelated = FakeChunk("毫不相干的一段话", chunk_id="c-x")
    gts = [GtChunk(label="步骤9", chunk_type="step_card",
                   keywords=["扫码", "报检", "交货单"],
                   expected_image_refs=[ImageRef(fmt="pdf", image_index=3)],
                   has_strong_refs=True)]
    out = _run_eval_doc(monkeypatch, [unrelated], gts)
    row = out["per_chunk"][0]
    assert row["jaccard"] == 0.0
    assert row["match_status"] == ib.MATCH_STATUS_BELOW_THRESHOLD
    assert out["n_strong_chunks"] == 1, "弃权仍进分母"
    item = out["judge_items"][0]
    assert item["produced_chunk_type"] is None
    assert item["produced_chunk_id"] is None
    assert item["produced_chunk_text_excerpt"] == ""
    assert item["produced_image_refs"] == []
    assert item["match_status"] == ib.MATCH_STATUS_BELOW_THRESHOLD
