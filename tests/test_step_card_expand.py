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
    with patch("opensearch_pipeline.db._get_db_conn", return_value=_conn(cur)):
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
    with patch("opensearch_pipeline.db._get_db_conn", return_value=_conn(cur)):
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
    with patch("opensearch_pipeline.db._get_db_conn", return_value=_conn(cur)):
        out = retriever.expand_step_context(chunks, query="完整流程怎么操作")  # full_procedure
    out_ids = {c["chunk_id"] for c in out}
    assert "S10" in out_ids, f"命中卡 S10 被意图筛选裁掉：{sorted(out_ids)}"
    # 家族 11 ≤ cap(12) 不触发防洪；前 8 步仍在，且命中卡额外保留（共 9）
    assert {"S1", "S8"}.issubset(out_ids)


# ═══════════════════════════════════════════════════════════════
# #F-mm4a 带图兄弟保底（RAG_EXPAND_IMAGE_KEEP）
# ═══════════════════════════════════════════════════════════════

def _sib(cid, step_no, imgs=False, section=""):
    return {
        "chunk_id": cid, "chunk_text": f"步骤{cid}", "step_no": step_no,
        "section_title": section, "extra_json": None, "parent_chunk_id": "P1",
        "image_refs_json": json.dumps([{"oss_key": f"{cid}.png"}]) if imgs else None,
    }


def _set_rag(monkeypatch, **kw):
    from opensearch_pipeline.config import get_config
    rag = get_config().rag
    for k, v in kw.items():
        monkeypatch.setattr(rag, k, v)


def _expand(cur, chunks, query):
    with patch("opensearch_pipeline.db._get_db_conn", return_value=_conn(cur)):
        return retriever.expand_step_context(chunks, query=query)


def test_image_keep_off_by_default(monkeypatch):
    """K=0（默认）：general ±1 窗口无图时不补任何带图兄弟——行为与历史一致。"""
    _set_rag(monkeypatch, expand_image_keep=0)
    meta = [{"chunk_id": "S05", "parent_chunk_id": "P1", "step_no": 5,
             "extra_json": None, "image_refs_json": None}]
    siblings = [_sib(f"S{i:02d}", i, imgs=(i in (2, 9))) for i in range(1, 11)]
    cur = _RoutingCursor(meta, siblings, [])
    out = _expand(cur, [{"chunk_type": "step_card", "chunk_id": "S05", "score": 1.0}], "报检作业")
    assert {c["chunk_id"] for c in out} == {"S04", "S05", "S06"}


def test_image_keep_adds_nearest_image_sibling(monkeypatch):
    """K=1：入选窗口全无图 → 按步号最近补入带图兄弟（S02 距 3 胜 S09 距 4）。"""
    _set_rag(monkeypatch, expand_image_keep=1)
    meta = [{"chunk_id": "S05", "parent_chunk_id": "P1", "step_no": 5,
             "extra_json": None, "image_refs_json": None}]
    siblings = [_sib(f"S{i:02d}", i, imgs=(i in (2, 9))) for i in range(1, 11)]
    cur = _RoutingCursor(meta, siblings, [])
    out = _expand(cur, [{"chunk_type": "step_card", "chunk_id": "S05", "score": 1.0}], "报检作业")
    out_ids = {c["chunk_id"] for c in out}
    assert "S02" in out_ids and "S09" not in out_ids
    s02 = [c for c in out if c["chunk_id"] == "S02"][0]
    assert s02["image_refs"] and s02["image_refs"][0]["oss_key"] == "S02.png"


def test_image_keep_empty_json_not_treated_as_image(monkeypatch):
    """'[]' / 'null' 是真值字符串——必须不算带图行（裸 truthiness 判定的经典坑）。"""
    _set_rag(monkeypatch, expand_image_keep=2)
    meta = [{"chunk_id": "S05", "parent_chunk_id": "P1", "step_no": 5,
             "extra_json": None, "image_refs_json": None}]
    siblings = [_sib(f"S{i:02d}", i) for i in range(1, 11)]
    siblings[1]["image_refs_json"] = "[]"      # S02
    siblings[2]["image_refs_json"] = "null"    # S03
    siblings[8]["image_refs_json"] = json.dumps([{"oss_key": "real.png"}])  # S09 真带图
    cur = _RoutingCursor(meta, siblings, [])
    out = _expand(cur, [{"chunk_type": "step_card", "chunk_id": "S05", "score": 1.0}], "报检作业")
    out_ids = {c["chunk_id"] for c in out}
    assert "S09" in out_ids            # 真带图行补入
    assert "S02" not in out_ids and "S03" not in out_ids  # 空 JSON 不冒充带图行


def test_image_keep_noop_when_window_already_has_image(monkeypatch):
    """入选窗口已有带图行 → 不再补（宁缺毋滥，保底只救『恒零图』形态）。"""
    _set_rag(monkeypatch, expand_image_keep=2)
    meta = [{"chunk_id": "S05", "parent_chunk_id": "P1", "step_no": 5,
             "extra_json": None, "image_refs_json": None}]
    siblings = [_sib(f"S{i:02d}", i, imgs=(i in (4, 9))) for i in range(1, 11)]
    cur = _RoutingCursor(meta, siblings, [])
    out = _expand(cur, [{"chunk_type": "step_card", "chunk_id": "S05", "score": 1.0}], "报检作业")
    out_ids = {c["chunk_id"] for c in out}
    assert "S04" in out_ids and "S09" not in out_ids


def test_image_keep_respects_locate_field_intent(monkeypatch):
    """locate_field 的设计语义=仅保留命中步：保底不得为其扩展。"""
    _set_rag(monkeypatch, expand_image_keep=2)
    meta = [{"chunk_id": "S05", "parent_chunk_id": "P1", "step_no": 5,
             "extra_json": None, "image_refs_json": None}]
    siblings = [_sib(f"S{i:02d}", i, imgs=(i == 9)) for i in range(1, 11)]
    cur = _RoutingCursor(meta, siblings, [])
    out = _expand(cur, [{"chunk_type": "step_card", "chunk_id": "S05", "score": 1.0}], "这个字段在哪里填写")
    assert {c["chunk_id"] for c in out} == {"S05"}


def test_image_keep_survives_family_cap_slice(monkeypatch):
    """防洪收缩 + 末端 [:_cap] 切片后带图保底行必须存活（被挤出时从尾部替换回来）。"""
    _set_rag(monkeypatch, expand_image_keep=1, step_expand_family_cap=12)
    # 20 个兄弟全部 step_no=0（大规模平局）→ general ±1 全家族入选 → 触发防洪。
    # S01..S15 同 section A（含命中 S01），S19 带图、section B。
    meta = [{"chunk_id": "S01", "parent_chunk_id": "P1", "step_no": 0,
             "extra_json": None, "image_refs_json": None}]
    siblings = [
        _sib(f"S{i:02d}", 0, imgs=(i == 19), section=("A" if i <= 15 else "B"))
        for i in range(1, 21)
    ]
    cur = _RoutingCursor(meta, siblings, [])
    out = _expand(cur, [{"chunk_type": "step_card", "chunk_id": "S01", "score": 1.0}], "报检作业")
    out_ids = {c["chunk_id"] for c in out}
    assert len(out) == 12                      # cap 生效
    assert "S01" in out_ids                    # 命中永存
    assert "S19" in out_ids, f"带图保底行被 cap 切片挤掉：{sorted(out_ids)}"
    s19 = [c for c in out if c["chunk_id"] == "S19"][0]
    assert s19["image_refs"]


# ═══════════════════════════════════════════════════════════════
# #F-mm4b procedure_parent 展开子卡归位 step_card
# ═══════════════════════════════════════════════════════════════

def _parent_fixture():
    meta = []  # 命中是 parent，不查 step_card 元数据
    children = [
        {"chunk_id": "C1", "chunk_text": "子步骤1", "step_no": 1, "section_title": "",
         "extra_json": None, "parent_chunk_id": "P1",
         "image_refs_json": json.dumps([{"oss_key": "c1.png", "caption": "子步骤1截图"}])},
        {"chunk_id": "C2", "chunk_text": "子步骤2", "step_no": 2, "section_title": "",
         "extra_json": None, "parent_chunk_id": "P1", "image_refs_json": None},
    ]
    chunks = [{"chunk_type": "procedure_parent", "chunk_id": "P1", "score": 1.0,
               "chunk_text": "流程概览"}]
    return meta, children, chunks


def test_parent_children_keep_parent_type_by_default(monkeypatch):
    """flag OFF（默认）：子卡继承 procedure_parent —— 行为与历史逐字节一致。"""
    _set_rag(monkeypatch, parent_child_as_stepcard=False)
    meta, children, chunks = _parent_fixture()
    cur = _RoutingCursor(meta, children, [])
    out = _expand(cur, chunks, "入职流程")
    by_id = {c["chunk_id"]: c for c in out}
    assert by_id["C1"]["chunk_type"] == "procedure_parent"
    assert by_id["C1"]["image_refs"][0]["oss_key"] == "c1.png"  # 装载本身一直发生


def test_parent_children_become_step_cards_when_enabled(monkeypatch):
    """flag ON：子卡归位 step_card（可进 <<IMG:N>> 标记与渲染提图链路），父卡本体不动。"""
    _set_rag(monkeypatch, parent_child_as_stepcard=True)
    meta, children, chunks = _parent_fixture()
    cur = _RoutingCursor(meta, children, [])
    out = _expand(cur, chunks, "入职流程")
    by_id = {c["chunk_id"]: c for c in out}
    assert by_id["P1"]["chunk_type"] == "procedure_parent"     # 父卡本体保持
    assert by_id["C1"]["chunk_type"] == "step_card"
    assert by_id["C2"]["chunk_type"] == "step_card"
    assert by_id["C1"]["image_refs"][0]["oss_key"] == "c1.png"
    assert by_id["C1"]["is_expanded"] and by_id["C1"]["expansion_reason"] == "parent_children"


def test_parent_children_stepcard_reaches_render_layer(monkeypatch):
    """端到端契约：归位后的子卡在 content_blocks_builder 的 step_card 分支可提图。"""
    _set_rag(monkeypatch, parent_child_as_stepcard=True)
    meta, children, chunks = _parent_fixture()
    cur = _RoutingCursor(meta, children, [])
    out = _expand(cur, chunks, "入职流程")
    import opensearch_pipeline.content_blocks_builder as cb
    monkeypatch.setattr(cb, "generate_signed_url", lambda key, expires=None: "https://oss/" + key)
    # C1 在 out 中的 1-based 位置即 <<IMG:N>> 的 N
    n = next(i for i, c in enumerate(out, 1) if c["chunk_id"] == "C1")
    blocks = cb.build_content_blocks(f"第一步如图 <<IMG:{n}>>。", out, max_images=3)
    imgs = [b for b in blocks if b["type"] == "image"]
    assert len(imgs) == 1 and imgs[0]["oss_key"] == "c1.png"
