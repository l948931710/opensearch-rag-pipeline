# -*- coding: utf-8 -*-
"""test_visibility_intent.py — C9 方案 B′：可见范围意图的版本级持久化 + 与 stage-2 互斥。

Sam 2026-08-03 拍板 B′，§4.3 三行业务裁决：
  a) 升版中 →restricted（收紧/紧急下线）⇒ **立即**作用于在线旧版本
  b) 升版中 public→dept_internal（收紧）  ⇒ **立即**
  c) 升版中 dept_internal→public（**放宽**）⇒ **只对新版本**（不动在线投影）

机械根因（核码坐实）：本端点只锁 document_meta、stage-2 只锁 document_version
（`FOR UPDATE OF dv SKIP LOCKED`）⇒ 两个写方锁**不相交的行**、彼此零互斥。
"""
import pytest


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


class _Cur:
    """按 SQL 前缀路由的桩游标；记录全部 execute。"""

    def __init__(self, conn):
        self.conn = conn
        self._res = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.conn.calls.append((s, params))
        if "FROM document_meta" in s or f"FROM {self.conn.db}.document_meta" in s:
            self._res = [self.conn.meta_row]
        elif "information_schema.COLUMNS" in s:
            self._res = [(1 if self.conn.has_override else 0,)]
        elif "SELECT content_process_status, index_status" in s:
            self._res = [self.conn.dv_row]
        elif "content_process_status NOT IN" in s:
            self._res = [(self.conn.pending_versions,)]
        else:
            self._res = []

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)


class _Conn:
    def __init__(self, *, meta_row, dv_row=("DONE", "SUCCESS"),
                 has_override=True, pending_versions=0):
        self.meta_row = meta_row
        self.dv_row = dv_row
        self.has_override = has_override
        self.pending_versions = pending_versions
        self.calls = []
        self.db = "fuling_knowledge"

    def cursor(self):
        return _Cur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _call(monkeypatch, conn, target, user="kb1"):
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "absent")
    return api.kb_set_visibility(api.KbSetVisibilityRequest(doc_id="D1", permission_level=target),
                                 request=None, identity=api.Identity(user_id=user))


def _sqls(conn, prefix):
    return [(s, p) for s, p in conn.calls if s.startswith(prefix)]


def _meta(perm="dept_internal", status="active", ver=2, rev=7):
    # R1：SELECT 末位追加了 acl_revision（capability=absent ⇒ 索引 4）。
    # 追加在末位是硬约束——既有索引 0..3 逐字不变。
    return ("hr", perm, status, ver, rev)


# ── 互斥闸：正在入库/推索引 ⇒ 409（且必须是可重试语义）────────────────────────

@pytest.mark.parametrize("cps,ixs", [("LOADING", "SUCCESS"), ("PROCESSING", "SUCCESS"),
                                     ("DONE", "PROCESSING")])
def test_in_flight_version_rejects_with_retryable_409(monkeypatch, cps, ixs):
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta(), dv_row=(cps, ixs))
    with pytest.raises(Exception) as ei:
        _call(monkeypatch, conn, "public")
    assert getattr(ei.value, "status_code", None) == 409
    assert "稍后重试" in str(getattr(ei.value, "detail", "")), (
        "409 必须是可重试语义——stale-lock 2h 会把 LOADING/PROCESSING 复位，"
        "文案不说清会让管理员以为这篇永远改不了")
    assert not _sqls(conn, "UPDATE"), "409 之前不得有任何写"


def test_gate_takes_row_lock_on_current_version(monkeypatch):
    """🔴 FOR UPDATE 是这条修复的**主要机制**——不是为了读那两列。

    少了它，只加 override 列仍是 last-writer-wins（stage-2 的认领是
    `FOR UPDATE OF dv SKIP LOCKED`，两边必须锁同一行才互斥）。
    """
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta())
    _call(monkeypatch, conn, "public")
    gate = [s for s, _ in conn.calls if "SELECT content_process_status, index_status" in s]
    assert gate and "FOR UPDATE" in gate[0], f"当前版本行未加锁：{gate}"


# ── 意图持久化 ────────────────────────────────────────────────────────────────

def test_override_written_to_all_active_versions(monkeypatch):
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta())
    _call(monkeypatch, conn, "public")
    ups = [(s, p) for s, p in conn.calls if "permission_override" in s and s.startswith("UPDATE")]
    assert len(ups) == 1, "必须写 override，否则 stage-2 下一轮按 raw_key 覆盖回去"
    assert "status='active'" in ups[0][0] and ups[0][1] == ("public", "D1")


def test_override_degrades_when_063_not_applied(monkeypatch):
    """063 未 apply ⇒ 探测后**跳过**，绝不 1054 打挂整个端点（先部署后 apply 安全）。"""
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta(), has_override=False)
    _call(monkeypatch, conn, "public")
    assert not [s for s, _ in conn.calls if "permission_override" in s and s.startswith("UPDATE")]


# ── §4.3 三行业务裁决 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("cur_perm,target", [("public", "restricted"),
                                             ("public", "dept_internal"),
                                             ("dept_internal", "restricted")])
def test_tightening_applies_to_online_version_immediately(monkeypatch, cur_perm, target):
    """a/b：收紧方向**立即**改在线投影——即便还有待处理新版本。"""
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta(perm=cur_perm), pending_versions=1)
    _call(monkeypatch, conn, target)
    proj = [(s, p) for s, p in conn.calls
            if s.startswith("UPDATE") and "chunk_meta SET permission_level" in s]
    assert proj, f"{cur_perm}→{target} 是收紧，必须立即作用于在线版本"
    assert proj[0][1][0] == target


def test_widening_with_pending_version_defers_online_projection(monkeypatch):
    """c：放宽 + 有待处理新版本 ⇒ **不动**在线投影（只落意图）。"""
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta(perm="dept_internal"), pending_versions=1)
    _call(monkeypatch, conn, "public")
    proj = [s for s, _ in conn.calls
            if s.startswith("UPDATE") and "chunk_meta SET permission_level" in s]
    assert not proj, "放宽不得立即扩大在线版本的暴露面"
    assert [s for s, _ in conn.calls if "permission_override" in s], "但意图必须落库"


def test_widening_without_pending_version_applies_immediately(monkeypatch):
    """c 的边界：没有待处理版本时放宽必须立即生效——否则该变更**永远不会生效**。"""
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta(perm="dept_internal"), pending_versions=0)
    _call(monkeypatch, conn, "public")
    proj = [s for s, _ in conn.calls
            if s.startswith("UPDATE") and "chunk_meta SET permission_level" in s]
    assert proj, "无待处理版本时放宽被延后 = 永远不生效"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── R1（Sam 2026-08-04 拍板「纳入 CAS」）───────────────────────────────────────

def test_stale_acl_revision_rejected_with_409(monkeypatch):
    """★ 带了 expected_acl_revision 且与库中不符 ⇒ 409，且**409 之前不得有任何写**。"""
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta(rev=9))
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "absent")
    with pytest.raises(Exception) as ei:
        api.kb_set_visibility(
            api.KbSetVisibilityRequest(doc_id="D1", permission_level="public",
                                       expected_acl_revision=3),   # 陈旧
            request=None, identity=api.Identity(user_id="kb1"))
    assert getattr(ei.value, "status_code", None) == 409
    assert "9" in str(getattr(ei.value, "detail", "")), "409 文案应带当前版本号供前端提示"
    assert not _sqls(conn, "UPDATE"), "CAS 落空前不得有任何写"


def test_matching_acl_revision_passes(monkeypatch):
    """对照：版本号匹配 ⇒ 照常放行。"""
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta(rev=9))
    _call_with_rev = None
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "absent")
    api.kb_set_visibility(
        api.KbSetVisibilityRequest(doc_id="D1", permission_level="public",
                                   expected_acl_revision=9),
        request=None, identity=api.Identity(user_id="kb1"))
    assert _sqls(conn, "UPDATE"), "版本号匹配却没放行"


def test_omitted_revision_still_bumps(monkeypatch):
    """★ 没带 CAS 字段（批量路径）⇒ 按现状放行，但**必须仍然 bump**。

    不 bump 的话，doc-meta 侧的 CAS 对可见范围变更**完全感知不到** —— 那正是 R1 要修的。
    """
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta(rev=9))
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "absent")
    api.kb_set_visibility(
        api.KbSetVisibilityRequest(doc_id="D1", permission_level="public"),   # 不带
        request=None, identity=api.Identity(user_id="kb1"))
    meta_ups = [s for s, _ in conn.calls
                if s.startswith("UPDATE") and "document_meta SET permission_level" in s]
    assert meta_ups and "acl_revision=acl_revision+1" in meta_ups[0], (
        "可见范围变更未 bump acl_revision ⇒ doc-meta 的 CAS 感知不到它")


def test_acl_revision_index_is_capability_aware(monkeypatch):
    """★ capability='present' 时 SELECT 多出 acl_mode/owner_dept_id 两列
    ⇒ acl_revision 落在索引 **6** 而非 4。索引写死会静默读错列（读到 owner_dept_id）。

    ⚠️ 本仓这类「条件列 + 位置索引」已出过多次错位；此处显式覆盖 present 分支，
    否则只测 absent 会让「索引写死 4」这个回归完全隐形（反证实测过：确实不打红）。
    """
    _skip_if_not_sim()
    # present 形态：(owner, perm, status, ver, acl_mode, owner_dept_id, acl_revision)
    conn = _Conn(meta_row=("hr", "dept_internal", "active", 2, "legacy", None, 11))
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "present")
    with pytest.raises(Exception) as ei:
        api.kb_set_visibility(
            api.KbSetVisibilityRequest(doc_id="D1", permission_level="public",
                                       expected_acl_revision=3),
            request=None, identity=api.Identity(user_id="kb1"))
    assert getattr(ei.value, "status_code", None) == 409
    assert "11" in str(getattr(ei.value, "detail", "")), (
        "present 分支读到的不是 acl_revision（很可能读成了 owner_dept_id）")


def test_authz_is_checked_before_cas(monkeypatch):
    """★ 无权用户即使带陈旧版本号，也必须得 **403 而非 409**。

    CAS 若排在授权之前，会泄露「这篇存在且刚被人改过」，并且把安全检查次序打乱。
    （本条源于一次真实回归：我把 CAS 放到了 `_kb_can_manage_doc` 之前，被既有用例抓出。）
    """
    _skip_if_not_sim()
    conn = _Conn(meta_row=_meta(rev=9))
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")   # 与文档 owner "hr" 不符
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_kb_node_capability", lambda cur: "absent")
    with pytest.raises(Exception) as ei:
        api.kb_set_visibility(
            api.KbSetVisibilityRequest(doc_id="D1", permission_level="dept_internal",
                                       expected_acl_revision=3),   # 陈旧，但不该轮到它说话
            request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403, "CAS 抢在了授权之前"
