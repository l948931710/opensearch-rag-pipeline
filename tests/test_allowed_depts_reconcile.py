# -*- coding: utf-8 -*-
"""test_allowed_depts_reconcile.py — Phase D Step 5 投影对账 + 共享 diff helper。

allowed_depts_reconcile.reconcile_allowed_depts：从 approved authority 重算投影、materialize +
retract、flag 关 no-op。access_grants.current_allowed_for_doc：现存投影 diff 口径。
用可编程桩游标按 SQL 片段应答（无真实 DB）。
"""
import json


# ── access_grants.current_allowed_for_doc ──
class _AllowedCur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        self.params = params

    def fetchall(self):
        return self._rows


def test_current_allowed_for_doc_parses_dedup_sorts():
    from opensearch_pipeline.access_grants import current_allowed_for_doc
    cur = _AllowedCur([('["quality","finance"]',), ('["finance"]',), (None,)])
    assert current_allowed_for_doc(cur, "D1", 1) == ["finance", "quality"]   # 并集去重稳定排序
    assert current_allowed_for_doc(_AllowedCur([]), "DX", 1) == []           # 无 chunk → []
    assert current_allowed_for_doc(_AllowedCur([(["hr"],)]), "D2", 1) == ["hr"]  # 已是 list 直接用


# ── reconcile_allowed_depts：可编程桩 DB ──
class _Cur:
    def __init__(self, st):
        self.st = st
        self._r = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.lower().split())
        p = tuple(params or ())
        if "requester_depts" in s:                                  # resolve_allowed_depts
            ids = set(p)
            self._r = [(d, ",".join(g)) for d, g in self.st["approved"].items() if d in ids]
        elif "kb_access_request" in s and "distinct doc_id" in s:   # approved 列表
            self._r = [(d,) for d in self.st["approved"]]
        elif "group_concat(distinct permission_level)" in s:        # gate 守卫 perm 查询
            ids = set(p)
            self._r = [(d, self.st["perm"][d]) for d in ids if d in self.st["perm"]]
        elif "allowed_depts is not null" in s:                      # have_ad（retract 候选）
            self._r = [(d,) for d, a in self.st["have"].items() if a]
        elif "current_version_no" in s:                             # per-doc 版本 + 反抢锁
            d = p[0]
            self._r = [(self.st["ver"].get(d, 1),)] if d in self.st["ver"] else []
        elif "distinct allowed_depts" in s:                         # current_allowed_for_doc
            d = p[0]
            cur = self.st["have"].get(d) or []
            self._r = [(json.dumps(cur),)] if cur else []
        elif s.startswith("update"):                                # 写
            self.st.setdefault("updates", []).append(p)
            self.rowcount = 1
            self._r = []
        else:
            self._r = []

    def fetchall(self):
        return list(self._r)

    def fetchone(self):
        return self._r[0] if self._r else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, st):
        self.st = st

    def cursor(self):
        return _Cur(self.st)

    def commit(self):
        self.st["commits"] = self.st.get("commits", 0) + 1

    def rollback(self):
        pass

    def close(self):
        pass


def _run(monkeypatch, st, flag=True, commit=True):
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline import pipeline_nodes
    monkeypatch.setattr(get_config().rag, "allowed_depts_acl", flag, raising=False)
    monkeypatch.setattr(pipeline_nodes, "_get_db_conn", lambda *a, **k: _Conn(st))
    from opensearch_pipeline.allowed_depts_reconcile import reconcile_allowed_depts
    return reconcile_allowed_depts(commit=commit)


def test_reconcile_flag_off_skipped(monkeypatch):
    """flag 关 → skipped、不连库（_get_db_conn 抛则说明被调用了）。"""
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline import pipeline_nodes

    def _boom(*a, **k):
        raise AssertionError("flag 关时不应连库")

    monkeypatch.setattr(get_config().rag, "allowed_depts_acl", False, raising=False)
    monkeypatch.setattr(pipeline_nodes, "_get_db_conn", _boom)
    from opensearch_pipeline.allowed_depts_reconcile import reconcile_allowed_depts
    out = reconcile_allowed_depts(commit=True)
    assert out["skipped"] is True and out["materialized"] == 0 and out["retracted"] == 0


def test_reconcile_materialize_and_retract(monkeypatch):
    """D1 approved+dept_internal+未物化 → materialize；D2 有残留 allowed_depts 但不再 approved → retract。"""
    st = {
        "approved": {"D1": ["finance"]},     # D1 获批授权 finance，尚未物化
        "perm": {"D1": "dept_internal"},     # D1 仍 dept_internal → gate 保留
        "have": {"D2": ["hr"]},              # D2 残留 allowed_depts=[hr]，已不在 approved
        "ver": {"D1": 1, "D2": 1},
    }
    out = _run(monkeypatch, st)
    assert out["materialized"] == 1 and out["retracted"] == 1
    ups = {u[1]: u[0] for u in st.get("updates", [])}    # {doc_id: allowed_depts_json|None}
    assert ups["D1"] == json.dumps(["finance"], ensure_ascii=False)   # 物化 finance
    assert ups["D2"] is None                                          # 清空（撤销残留）


def test_reconcile_gate_drops_restricted(monkeypatch):
    """approved 但当前 permission_level=restricted（改判）→ gate 丢弃 → 不物化（want=[]）。"""
    st = {
        "approved": {"D3": ["quality"]},
        "perm": {"D3": "restricted"},        # 改判 restricted → gate 丢弃
        "have": {"D3": ["quality"]},         # 旧残留 → 应被清空（retract）
        "ver": {"D3": 1},
    }
    out = _run(monkeypatch, st)
    assert out["retracted"] == 1 and out["materialized"] == 0
    ups = {u[1]: u[0] for u in st.get("updates", [])}
    assert ups["D3"] is None                                          # restricted → 清空


def test_reconcile_unchanged_no_write(monkeypatch):
    """已正确物化（want==have）→ unchanged、零写。"""
    st = {
        "approved": {"D1": ["finance"]},
        "perm": {"D1": "dept_internal"},
        "have": {"D1": ["finance"]},         # 已物化且一致
        "ver": {"D1": 1},
    }
    out = _run(monkeypatch, st)
    assert out["unchanged"] == 1 and out["materialized"] == 0 and out["retracted"] == 0
    assert "updates" not in st                                        # 零写
