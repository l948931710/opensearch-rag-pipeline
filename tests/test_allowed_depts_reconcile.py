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


def test_current_allowed_for_doc_skips_bad_json_not_abort():
    """单行坏 JSON 不得抛异常 abort 整篇 doc 的投影/对账——跳过坏行、保留好行。"""
    from opensearch_pipeline.access_grants import current_allowed_for_doc
    cur = _AllowedCur([('["finance"]',), ("{坏的 json",), ('["quality"]',)])
    assert current_allowed_for_doc(cur, "D3", 1) == ["finance", "quality"]   # 坏行被跳过，不 raise


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
        elif "group_concat(distinct permission_level)" in s:        # gate 守卫 perm 查询（helper 单列、版本限定）
            d = p[0]
            self._r = [(self.st["perm"][d],)] if d in self.st.get("perm", {}) else []
        elif "kb_doc_node_grant" in s:                              # node 权威候选（零投影那一类）
            if self.st.get("node_grant_table_missing"):
                raise RuntimeError("Table 'kb_doc_node_grant' doesn't exist")
            self._r = [(d,) for d in self.st.get("node_granted", [])]
        elif "allowed_depts is not null" in s:                      # have_ad（retract 候选）
            self._r = [(d,) for d, a in self.st["have"].items() if a]
        elif "current_version_no" in s:                             # per-doc 版本 + 反抢锁
            d = p[0]
            self._r = [(self.st["ver"].get(d, 1),)] if d in self.st["ver"] else []
        elif "distinct allowed_depts" in s:                         # current_allowed_for_doc
            d = p[0]
            cur = self.st["have"].get(d) or []
            self._r = [(json.dumps(cur),)] if cur else []
        elif "distinct owner_dept" in s:                            # _current_owner_for_doc（投影轴）
            d = p[0]
            self._r = [(self.st.get("chunk_owner", {}).get(d, ""),)]
        elif "select owner_dept from" in s and "document_meta" in s:  # 归属轴（阶段 B 对称投影）
            d = p[0]
            self._r = [(self.st.get("owner", {}).get(d, ""),)]
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
    # 阶段 B：投影 UPDATE 现在同时写 owner（mode 对称投影，params=(aj, owner, doc, ver)）
    ups = {u[2]: (u[0], u[1]) for u in st.get("updates", [])}   # {doc_id: (allowed_json|None, owner)}
    assert ups["D1"][0] == json.dumps(["finance"], ensure_ascii=False)   # 物化 finance
    assert ups["D2"][0] is None                                          # 清空（撤销残留）


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
    ups = {u[2]: (u[0], u[1]) for u in st.get("updates", [])}
    assert ups["D3"][0] is None                                          # restricted → 清空


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


# ── node-ACL：候选集必须纳入 kb_doc_node_grant（设计稿 §4）──────────────────
def _spy_targets(monkeypatch):
    """spy 掉逐文档物化 helper —— 本组测试只关心【候选集选了谁】，不关心物化结果。"""
    from opensearch_pipeline import access_grants
    seen = []

    def _fake(cur, doc_id, *, apply=True):
        seen.append(doc_id)
        return {"status": "unchanged", "reset_chunks": 0, "version_no": 1}

    monkeypatch.setattr(access_grants, "materialize_doc_allowed_depts", _fake)
    return seen


def test_reconcile_covers_never_projected_node_doc(monkeypatch):
    """★ 零投影的 node 文档必须进候选。

    它既没有 kb_access_request 行（那是 legacy 权威），chunk_meta.allowed_depts 也还是
    NULL（decide 端点漏入队 / outbox 没 drain / 直接改库授权）⇒ 旧的
    `approved ∪ have_ad` 正好跳过它 ⇒ 管理员勾了组织节点，文档却永远搜不到。
    """
    seen = _spy_targets(monkeypatch)
    st = {"approved": {}, "have": {}, "ver": {"DN": 1}, "perm": {"DN": "dept_internal"},
          "node_granted": ["DN"]}
    _run(monkeypatch, st)
    assert "DN" in seen, "零投影的 node 文档没有进入对账候选"


def test_reconcile_candidate_set_is_the_union(monkeypatch):
    """候选 = approved ∪ 有残留投影 ∪ node 授权（三源并集，互不遮蔽）。"""
    seen = _spy_targets(monkeypatch)
    st = {"approved": {"DA": ["hr"]}, "have": {"DH": ["hr"]}, "node_granted": ["DN"],
          "ver": {"DA": 1, "DH": 1, "DN": 1},
          "perm": {"DA": "dept_internal", "DH": "dept_internal", "DN": "dept_internal"}}
    _run(monkeypatch, st)
    assert set(seen) == {"DA", "DH", "DN"}


def test_reconcile_survives_missing_node_grant_table(monkeypatch):
    """schema/060 未 apply ⇒ 表不存在，只跳过该候选源，不阻断整轮对账。"""
    seen = _spy_targets(monkeypatch)
    st = {"approved": {"D1": ["hr"]}, "have": {}, "ver": {"D1": 1},
          "perm": {"D1": "dept_internal"}, "node_grant_table_missing": True}
    out = _run(monkeypatch, st)
    assert out["skipped"] is False and not out["errors"]
    assert seen == ["D1"], "legacy 候选仍应照常处理"
