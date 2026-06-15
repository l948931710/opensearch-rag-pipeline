# -*- coding: utf-8 -*-
"""ha3_reconcile 单测 —— 聚焦纯函数 _classify_stale 的三道安全闸 + simulate no-op。"""
from opensearch_pipeline.ha3_reconcile import _classify_stale, reconcile_ha3_orphan_pks


def test_classify_dup_orphan_and_g3():
    """dup（替换已在 HA3）删；orphan chunk_id 删；dup（替换缺失，G3）跳过；active 保留。"""
    rds_active_ids = {100, 101, 102}
    rds_active_chunkid = {"A": 100, "B": 101, "C": 102}
    ha3_map = {
        100: ("A", "doc1"),   # kept (G1)
        50:  ("A", "doc1"),   # dup, 替换 100 在 HA3 → 删
        60:  ("X", "doc1"),   # orphan chunk_id（X 不在 active）→ 删
        52:  ("B", "doc1"),   # dup, 替换 101 不在 ha3_map → G3 跳过
        102: ("C", "doc2"),   # kept (G1)
    }
    delete, skipped = _classify_stale(ha3_map, rds_active_ids, rds_active_chunkid)
    assert delete == [50, 60]
    assert skipped["dup_replacement_absent"] == 1


def test_g1_never_deletes_active():
    """G1 硬不变量：active id 永不进删除集（即便它在 HA3 多处出现）。"""
    rds_active_ids = {1, 2, 3}
    rds_active_chunkid = {"a": 1, "b": 2, "c": 3}
    ha3_map = {1: ("a", "d"), 2: ("b", "d"), 3: ("c", "d")}
    delete, _ = _classify_stale(ha3_map, rds_active_ids, rds_active_chunkid)
    assert delete == []


def test_fully_retired_doc_all_stale():
    """文档完全退役（无 active chunk_id）→ 其全部 HA3 行皆删。"""
    delete, _ = _classify_stale(
        {500: ("Z", "deadDoc"), 501: ("Y", "deadDoc")}, set(), {})
    assert delete == [500, 501]


def test_g3_holds_back_until_repush():
    """同一 doc：旧载体的 chunk_id 新 id 缺失 → 全部 G3 跳过，0 删（push-then-purge 不变量）。"""
    rds_active_chunkid = {"A": 900, "B": 901}   # 新 id 900/901
    rds_active_ids = {900, 901}
    ha3_map = {10: ("A", "d"), 11: ("B", "d")}   # 只有旧载体，新 id 还没 push
    delete, skipped = _classify_stale(ha3_map, rds_active_ids, rds_active_chunkid)
    assert delete == []
    assert skipped["dup_replacement_absent"] == 2


def test_reconcile_simulate_is_noop():
    """simulate=True → 不连任何外部资源，返回零报告。"""
    rep = reconcile_ha3_orphan_pks(simulate=True)
    assert rep == {"checked": 0, "stale": 0, "deleted": 0, "skipped": {}, "errors": []}
