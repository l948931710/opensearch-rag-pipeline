# -*- coding: utf-8 -*-
"""B12 / B13 / B15（2026-07-25）。

B12：drain 只有次数上限（默认十万 = 无界），加**批次边界**墙钟预算；到点 raise 专用异常
     而非 exit 0（「绝不静默成功」是既有不变量）。刻意不用 signal/线程异步中断当前批。
     不改 RAG_DRAIN_MAX_ITERS 默认值（缺队列规模证据，有墙钟后价值也很低）。
B13：stage-3 毒 chunk 死信只改 chunk_status，dv.index_status 仍 SUCCESS → 控制台显示
     「已上线」，管理员看不出文档缺内容。窄口修：NEEDS_REVIEW 排在「已上线」之前。
B15：HA3 路径上的 NDJSON payload 是死重（推送消费内存对象，payload_oss_key 无读回方）。
     加 RAG_BULK_PAYLOAD_ARCHIVE（默认 on），关时不物化；**切批边界与 payload_size
     必须逐字节一致**，且只允许 HA3 后端关。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("RAG_ENV", "test")

import pytest

from opensearch_pipeline.bulk_helpers import build_opensearch_bulk_actions


# ═══════════════════ B15：materialize on/off 逐字节对拍 ═══════════════════

class _Chunk:
    def __init__(self, cid, text):
        self.chunk_id = cid
        self._text = text

    def to_opensearch_doc(self):
        return {"chunk_id": self.chunk_id, "chunk_text": self._text}


@pytest.mark.parametrize("texts", [
    ["ascii only", "another one"],
    ["中文内容，含多字节字符", "混合 mixed 中英文 content"],
    ["emoji 🚀 与生僻字 龘齉", "x" * 500],
])
def test_b15_batching_and_size_identical_with_and_without_materialize(texts):
    chunks = [_Chunk(f"c{i}", t) for i, t in enumerate(texts)]
    on = build_opensearch_bulk_actions(chunks, max_bulk_size_bytes=1_500_000)
    off = build_opensearch_bulk_actions(chunks, max_bulk_size_bytes=1_500_000, materialize=False)
    assert len(on) == len(off)
    for a, b in zip(on, off):
        assert [c.chunk_id for c in a["chunks"]] == [c.chunk_id for c in b["chunks"]]
        assert a["payload_size"] == b["payload_size"], "payload_size 必须逐字节一致"
        assert b["payload"] == "", "materialize=False 不得拼字符串"


def test_b15_split_boundary_is_identical_at_the_limit():
    """刚好等于上限 / 超 1 byte 两种边界必须与物化版切得一样。"""
    chunks = [_Chunk(f"c{i}", "中" * 50) for i in range(6)]
    one = build_opensearch_bulk_actions(chunks, max_bulk_size_bytes=1)[0]
    limit = one["payload_size"] * 3          # 恰好装下三个
    for cap in (limit, limit + 1, limit - 1):
        on = build_opensearch_bulk_actions(chunks, max_bulk_size_bytes=cap)
        off = build_opensearch_bulk_actions(chunks, max_bulk_size_bytes=cap, materialize=False)
        assert [len(b["chunks"]) for b in on] == [len(b["chunks"]) for b in off], f"cap={cap}"
        assert [b["payload_size"] for b in on] == [b["payload_size"] for b in off], f"cap={cap}"


def test_b15_size_is_utf8_bytes_not_chars():
    """payload_size 的口径是「等价 NDJSON 未压缩 UTF-8 字节数」，不是字符数。"""
    b = build_opensearch_bulk_actions([_Chunk("c0", "中文")], materialize=False)[0]
    on = build_opensearch_bulk_actions([_Chunk("c0", "中文")])[0]
    assert b["payload_size"] == len(on["payload"].encode("utf-8"))
    assert b["payload_size"] > len(on["payload"])      # 多字节 ⇒ 字节数 > 字符数


def test_b15_backend_guard_and_null_key():
    import opensearch_pipeline.pipeline_nodes as pn
    src = " ".join(inspect.getsource(pn.node_build_opensearch_payload).split())
    assert "RAG_BULK_PAYLOAD_ARCHIVE" in src
    # 只允许 HA3 后端关（标准 OpenSearch 的 client.bulk 直接消费 payload；simulate 要写本地文件）
    assert "push_documents" in src and "is_simulated or not _is_ha3" in src
    # 未归档写 NULL，不编造路径
    assert 'batch["oss_key"] or None' in src


def test_b15_defaults_to_archiving():
    """默认必须保持现状（on）。"""
    import opensearch_pipeline.pipeline_nodes as pn
    src = inspect.getsource(pn.node_build_opensearch_payload)
    assert 'os.environ.get("RAG_BULK_PAYLOAD_ARCHIVE", "true")' in src


# ═══════════════════ B12：批次边界墙钟 ═══════════════════

def _drain(monkeypatch, max_seconds, batches_before_drain=3):
    import opensearch_pipeline.dataworks_orchestrator as orch
    state = {"n": 0, "clock": 0.0}
    monkeypatch.setenv("RAG_INGEST_SINGLETON_LOCK", "false")
    monkeypatch.setenv("RAG_DRAIN_MAX_SECONDS", str(max_seconds))
    monkeypatch.setattr(orch, "_reset_stale_stage2_locks", lambda: None)
    monkeypatch.setattr(orch, "_probe_unclaimable_rows", lambda stage: [])

    def _count(stage):
        return max(0, batches_before_drain - state["n"])

    def _run(stage, bizdate, simulate, cost_breaker=None):
        state["n"] += 1
        state["clock"] += 10.0        # 每批耗时 10s（假时钟）

    monkeypatch.setattr(orch, "_count_pending_rows", _count)
    monkeypatch.setattr(orch, "run_stage", _run)
    monkeypatch.setattr(orch.time, "monotonic", lambda: state["clock"])
    return orch, state


def test_b12_wall_clock_raises_dedicated_exception(monkeypatch):
    import opensearch_pipeline.dataworks_orchestrator as orch
    o, state = _drain(monkeypatch, max_seconds=15, batches_before_drain=10)
    with pytest.raises(orch.DrainTimeBudgetExceeded, match=r"RAG_DRAIN_MAX_SECONDS"):
        o.run_stage_drained(stage=2, bizdate="20260725", simulate=False)
    assert state["n"] >= 1, "必须先跑过至少一批才可能触预算"


def test_b12_not_enabled_by_default(monkeypatch):
    """默认 0 = 关：机制先就位，取值是待实验的运维决策（缺 P95 证据不硬设）。"""
    o, _ = _drain(monkeypatch, max_seconds=0, batches_before_drain=3)
    monkeypatch.delenv("RAG_DRAIN_MAX_SECONDS", raising=False)
    o.run_stage_drained(stage=2, bizdate="20260725", simulate=False)   # 不得抛


def test_b12_checks_only_at_batch_boundary():
    """不得用 signal/线程异步中断当前批（会在任意语句处撕开事务）。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    src = inspect.getsource(orch._run_stage_drained_locked)
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "signal" not in code and "Timer(" not in code
    i_check = src.index("RAG_DRAIN_MAX_SECONDS")
    i_run = src.index("_batch_ctx = run_stage(")
    assert i_check < i_run, "检查必须在下一批开跑之前"


def test_b12_max_iters_default_unchanged():
    """缺队列规模证据，且有墙钟后价值很低 —— 本批不改它。"""
    import opensearch_pipeline.dataworks_orchestrator as orch
    assert '"RAG_DRAIN_MAX_ITERS", "100000"' in inspect.getsource(orch._run_stage_drained_locked)


# ═══════════════════ B13：徽章窄口 ═══════════════════

@pytest.mark.parametrize("chunk_status,index_status,expect", [
    ("NEEDS_REVIEW", "SUCCESS", "未入索引"),     # stage-3 毒 chunk 死信：此前显示「已上线」
    ("DONE", "SUCCESS", "已上线"),
    ("EMPTY", "SUCCESS", "未入索引"),
])
def test_b13_needs_review_badge(chunk_status, index_status, expect):
    from opensearch_pipeline.api import _kb_status_badge
    assert _kb_status_badge("DONE", index_status, "active",
                            chunk_status=chunk_status) == expect


def test_b13_sql_mirror_kept_in_sync():
    from opensearch_pipeline.api import _KB_BADGE_CASE_SQL
    assert "'NEEDS_REVIEW' THEN '未入索引'" in _KB_BADGE_CASE_SQL
    # 顺序：必须排在「已上线」之前，否则永远命不中
    assert (_KB_BADGE_CASE_SQL.index("NEEDS_REVIEW")
            < _KB_BADGE_CASE_SQL.index("'已上线'"))


def test_b13_needs_review_not_added_to_bad_badges():
    """不得把 NEEDS_REVIEW 整体当异常：VLM 降级路径明写"文本照常可检索"。"""
    from opensearch_pipeline.api import _KB_BAD_BADGES
    assert "未入索引" in _KB_BAD_BADGES        # 既有取值不变
    assert len(_KB_BAD_BADGES) == 4
