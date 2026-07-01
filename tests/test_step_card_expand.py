# -*- coding: utf-8 -*-
"""
test_step_card_expand.py — expand_step_context 批量化 (A2) + XLSX 无 parent 图片修复 (C2)
+ image_refs 归一化统一 (C3)

- _normalize_image_refs 保留全部契约键并互相兜底
- 无 parent 的 step_card（XLSX procedure_image_guide）现在仍能带出其绑定的 image_refs
- 有 parent 的 step_card 兄弟步骤用单次批量查询取齐
"""

import json
from unittest.mock import MagicMock, patch

from opensearch_pipeline import retriever
from opensearch_pipeline.retriever import _normalize_image_refs


def test_normalize_preserves_contract_keys_and_folds():
    out = _normalize_image_refs(json.dumps([{
        "oss_key": "k.png", "visual_summary": "vs", "ocr_text": "ot",
        "caption": "cap", "image_index": 3,
    }]))
    r = out[0]
    assert r["oss_key"] == "k.png" and r["source_image"] == "k.png"  # source_image 兜底取 oss_key
    assert r["visual_summary"] == "vs" and r["ocr_text"] == "ot"
    assert r["caption"] == "cap" and r["image_index"] == 3
    # 仅 source_image 时反向兜底 oss_key
    o2 = _normalize_image_refs([{"source_image": "s.png"}])
    assert o2[0]["oss_key"] == "s.png" and o2[0]["source_image"] == "s.png"


def test_normalize_robust_to_junk():
    assert _normalize_image_refs(None) == []
    assert _normalize_image_refs("not json") == []
    out = _normalize_image_refs([1, "x", {"oss_key": "y"}])  # 仅保留 dict 项
    assert len(out) == 1 and out[0]["oss_key"] == "y" and out[0]["order"] == 2


class _RoutingCursor:
    """按 SQL 内容路由返回不同行集，并记录调用次数（用于断言批量化）。"""

    def __init__(self, meta_rows, sibling_rows, vk_rows):
        self.meta_rows, self.sibling_rows, self.vk_rows = meta_rows, sibling_rows, vk_rows
        self._last = []
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append(sql)
        if "parent_chunk_id, step_no, extra_json" in sql:
            self._last = self.meta_rows
        elif "parent_chunk_id IN" in sql:
            self._last = self.sibling_rows
        elif "image_refs_json FROM chunk_meta WHERE chunk_id IN" in sql:
            self._last = self.vk_rows
        else:
            self._last = []

    def fetchall(self):
        return list(self._last)

    def fetchone(self):
        return None

    def close(self):
        pass


def _conn(cursor):
    c = MagicMock()
    c.cursor.return_value = cursor
    c.close.return_value = None
    return c


def test_xlsx_step_card_without_parent_carries_image_refs():
    """C2: parent_chunk_id 为 NULL 的 step_card（XLSX）必须仍带出其绑定的 image_refs。"""
    meta = [{"chunk_id": "S1", "parent_chunk_id": None, "step_no": 1, "extra_json": None,
             "image_refs_json": json.dumps([{"oss_key": "step1.png", "visual_summary": "第一步截图"}])}]
    cur = _RoutingCursor(meta, [], [])
    chunks = [{"chunk_type": "step_card", "chunk_id": "S1", "score": 1.0, "chunk_text": "步骤1"}]
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=_conn(cur)):
        out = retriever.expand_step_context(chunks, query="怎么操作")
    refs = out[0].get("image_refs")
    assert refs and refs[0]["oss_key"] == "step1.png"
    assert refs[0]["visual_summary"] == "第一步截图"   # 归一化保留 visual_summary
    # 批量化：所有 step_card 的元数据只查一次
    assert len([s for s in cur.calls if "parent_chunk_id, step_no, extra_json" in s]) == 1
    assert not [s for s in cur.calls if "parent_chunk_id IN" in s]  # 无 parent → 不查兄弟


def test_step_card_with_parent_expands_siblings_in_one_query():
    """A2: 有 parent 的 step_card 兄弟步骤用单次批量查询；归一化保留 visual_summary。"""
    meta = [{"chunk_id": "S1", "parent_chunk_id": "P1", "step_no": 2,
             "extra_json": None, "image_refs_json": None}]
    siblings = [
        {"chunk_id": "S0", "chunk_text": "步骤0", "step_no": 1, "section_title": "",
         "extra_json": None, "image_refs_json": None, "parent_chunk_id": "P1"},
        {"chunk_id": "S1", "chunk_text": "步骤1", "step_no": 2, "section_title": "",
         "extra_json": None, "parent_chunk_id": "P1",
         "image_refs_json": json.dumps([{"oss_key": "s1.png", "visual_summary": "图"}])},
    ]
    cur = _RoutingCursor(meta, siblings, [])
    chunks = [{"chunk_type": "step_card", "chunk_id": "S1", "score": 1.0}]
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=_conn(cur)):
        out = retriever.expand_step_context(chunks, query="完整流程怎么操作")  # full_procedure
    assert {c["chunk_id"] for c in out} == {"S0", "S1"}
    s1 = [c for c in out if c["chunk_id"] == "S1"][0]
    assert s1["image_refs"][0]["visual_summary"] == "图"
    assert len([s for s in cur.calls if "parent_chunk_id IN" in s]) == 1  # 单次兄弟查询


def test_full_procedure_keeps_hit_when_truncated_out():
    """F-21：命中卡在 full_procedure 的 siblings[:max_steps] 位置截断之外时，必须仍被强制保留——
    否则最佳匹配文本从未进 LLM 上下文，答案只讲前 N 步。11 步家族、命中第 10 步、max_steps=8。"""
    meta = [{"chunk_id": "S10", "parent_chunk_id": "P1", "step_no": 10,
             "extra_json": None, "image_refs_json": None}]
    # 11 个兄弟 step_no=1..11（SQL 序即 step_no 升序）；命中 S10 排在 siblings[:8] 之外
    siblings = [
        {"chunk_id": f"S{i}", "chunk_text": f"步骤{i}", "step_no": i, "section_title": "",
         "extra_json": None, "image_refs_json": None, "parent_chunk_id": "P1"}
        for i in range(1, 12)
    ]
    cur = _RoutingCursor(meta, siblings, [])
    chunks = [{"chunk_type": "step_card", "chunk_id": "S10", "score": 1.0}]
    with patch("opensearch_pipeline.pipeline_nodes._get_db_conn", return_value=_conn(cur)):
        out = retriever.expand_step_context(chunks, query="完整流程怎么操作")  # full_procedure
    out_ids = {c["chunk_id"] for c in out}
    assert "S10" in out_ids, f"命中卡 S10 被意图筛选裁掉：{sorted(out_ids)}"
    # 家族 11 ≤ cap(12) 不触发防洪；前 8 步仍在，且命中卡额外保留（共 9）
    assert {"S1", "S8"}.issubset(out_ids)
