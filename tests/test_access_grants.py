# -*- coding: utf-8 -*-
"""test_access_grants.py — Phase D allowed_depts 聚合 helper（单一注入点）+ to_ha3_doc 推送门控。

helper 只消费调用方游标（不建池），故无需 DB：桩游标返回 (doc_id, requester_depts) 行。
"""


class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._rows


def test_resolve_allowed_depts_aggregates_and_whitelists():
    from opensearch_pipeline import access_grants
    rows = [
        ("D1", "marketing"),
        ("D1", "production,quality"),   # 多 approved 行 → 并集
        ("D2", "hr"),
        ("D3", "typo_dept"),            # 非白名单 → fail-closed 丢弃 → D3 不出现
    ]
    cur = _Cur(rows)
    out = access_grants.resolve_allowed_depts(["D1", "D2", "D3"], cur)
    assert out["D1"] == ["marketing", "production", "quality"]   # 并集 + 去重 + 稳定排序
    assert out["D2"] == ["hr"]
    assert "D3" not in out                                        # 全非白名单 → 无授权
    assert "status='approved'" in cur.sql and "IN (" in cur.sql   # 只查 approved + 参数化 IN
    assert tuple(cur.params) == ("D1", "D2", "D3")


def test_resolve_allowed_depts_dedup_and_stable_sort():
    from opensearch_pipeline import access_grants
    cur = _Cur([("D1", "quality,marketing"), ("D1", "marketing")])
    assert access_grants.resolve_allowed_depts(["D1"], cur)["D1"] == ["marketing", "quality"]


def test_resolve_allowed_depts_empty_inputs():
    from opensearch_pipeline import access_grants
    assert access_grants.resolve_allowed_depts([], _Cur([])) == {}
    assert access_grants.resolve_allowed_depts_one("DX", _Cur([])) == []


def test_resolve_allowed_depts_one():
    from opensearch_pipeline import access_grants
    assert access_grants.resolve_allowed_depts_one("DX", _Cur([("DX", "finance")])) == ["finance"]


def test_resolve_allowed_depts_partial_bad_codes_kept_clean():
    """一文档里混白名单 + 非白名单：保留白名单部分，丢弃坏码（不整条作废）。"""
    from opensearch_pipeline import access_grants
    cur = _Cur([("D1", "marketing,typo_dept,hr")])
    assert access_grants.resolve_allowed_depts(["D1"], cur)["D1"] == ["hr", "marketing"]


# ── to_ha3_doc 推送门控（默认不推；显式 True 才推）──
def test_to_ha3_doc_gates_allowed_depts():
    from opensearch_pipeline.chunker import Chunk
    c = Chunk(chunk_id="c1", doc_id="D1", version_no=1, chunk_index=0,
              chunk_type="text", chunk_text="x", token_count=1,
              owner_dept="hr", permission_level="dept_internal",
              allowed_depts=["marketing", "production"])
    assert "allowed_depts" not in c.to_ha3_doc("id")                 # 默认不推（HA3 加字段前安全）
    d = c.to_ha3_doc("id", include_allowed_depts=True)
    assert d["allowed_depts"] == ["marketing", "production"]         # 显式 True → 推组码数组

    c2 = Chunk(chunk_id="c2", doc_id="D2", version_no=1, chunk_index=0,
               chunk_type="text", chunk_text="x", token_count=1)
    assert c2.to_ha3_doc("id", include_allowed_depts=True)["allowed_depts"] == []  # 空授权 → 推空数组（可清）
