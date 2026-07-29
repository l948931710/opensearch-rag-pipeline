# -*- coding: utf-8 -*-
"""node-ACL 在 retriever 侧的接线测试(过滤器节点分支 + 撤销复核节点感知)。

最关键的两条:
  ① `acl_ctx=None`(当前全部调用点)⇒ 过滤串与历史**逐字节一致**,零行为漂移;
  ② 撤销复核不再做纯组码相交 —— 否则 node 授权命中会被整批误丢,node 文档在生产
     **根本查不出来**(现网 RAG_ALLOWED_DEPTS_ACL 已开,那是活代码 ⇒ 阻断项)。
"""
import pytest

from opensearch_pipeline.acl_policy import (
    ACL_MODE_LEGACY, ACL_MODE_NODE, AclContext, DocAcl, InvalidFlagPosture,
)


def _stub(monkeypatch, *, phase_d=True, grant=False, enforce=True):
    from opensearch_pipeline import retriever

    class _Rag:
        allowed_depts_acl = phase_d
        node_acl_grant = grant
        node_acl_enforce = enforce

    class _Cfg:
        rag = _Rag()

    monkeypatch.setattr(retriever, "get_config", lambda: _Cfg())
    return retriever


CTX = AclContext(groups=("hr",), ancestor_dept_ids=(34265162, 599502818),
                 direct_dept_ids=(34265162,))


# ── ① 无 acl_ctx ⇒ 逐字节不变 ────────────────────────────────────────────────
@pytest.mark.parametrize("grant", [False, True])
def test_filter_without_ctx_is_byte_identical(monkeypatch, grant):
    r = _stub(monkeypatch, grant=grant)
    base = r._build_permission_filter("hr")
    assert "d:" not in base and "dx:" not in base
    assert base == r._build_permission_filter("hr", acl_ctx=None)


def test_filter_with_ctx_but_grant_off_adds_nothing(monkeypatch):
    """GRANT=false ⇒ 不产生新的节点命中(正向通道关闭)。"""
    r = _stub(monkeypatch, grant=False)
    assert r._build_permission_filter("hr", acl_ctx=CTX) == r._build_permission_filter("hr")


# ── 节点分支 ─────────────────────────────────────────────────────────────────
def test_filter_grant_on_appends_node_terms(monkeypatch):
    r = _stub(monkeypatch, grant=True)
    out = r._build_permission_filter("hr", acl_ctx=CTX)
    assert out.startswith(r._build_permission_filter("hr")), "节点分支只在末尾追加,不改既有子句"
    assert 'allowed_depts="d:34265162"' in out
    assert 'allowed_depts="d:599502818"' in out
    assert 'allowed_depts="dx:34265162"' in out


def test_node_terms_keep_colon_not_sanitized(monkeypatch):
    """★ 净化器会删冒号;节点值必须原样出现在表达式里。"""
    r = _stub(monkeypatch, grant=True)
    out = r._build_permission_filter("hr", acl_ctx=CTX)
    assert "d:34265162" in out and "d34265162" not in out.replace("d:34265162", "")


def test_org_wide_reader_gets_candidate_branch(monkeypatch):
    """总经办:事后放行不够——HA3 主过滤器压根不召回 node 文档,必须给候选分支。"""
    r = _stub(monkeypatch, grant=True)
    ctx = AclContext(groups=("admin",), org_wide_reader=True)
    out = r._build_permission_filter("admin", acl_ctx=ctx)
    assert out.endswith(' OR (permission_level="dept_internal")')


def test_org_wide_reader_gets_no_branch_when_grant_off(monkeypatch):
    r = _stub(monkeypatch, grant=False)
    ctx = AclContext(groups=("admin",), org_wide_reader=True)
    assert r._build_permission_filter("admin", acl_ctx=ctx) == r._build_permission_filter("admin")


def test_untrusted_chain_yields_no_node_terms(monkeypatch):
    r = _stub(monkeypatch, grant=True)
    ctx = AclContext(groups=("hr",), ancestor_dept_ids=(5,), node_channel_ok=False)
    assert r._build_permission_filter("hr", acl_ctx=ctx) == r._build_permission_filter("hr")


def test_illegal_flag_posture_raises(monkeypatch):
    r = _stub(monkeypatch, grant=True, enforce=False)
    with pytest.raises(InvalidFlagPosture):
        r._build_permission_filter("hr", acl_ctx=CTX)


# ── ② 撤销复核:node 感知(阻断项) ────────────────────────────────────────────
NODE_HIT = {"doc_id": "D1", "permission_level": "dept_internal",
            "owner_dept": "__acl_node_mode_v1__", "chunk_id": "C1"}
OWN_HIT = {"doc_id": "D2", "permission_level": "dept_internal",
           "owner_dept": "hr", "chunk_id": "C2"}
PUB_HIT = {"doc_id": "D3", "permission_level": "public", "owner_dept": "x", "chunk_id": "C3"}


def _patch_authority(monkeypatch, acl_by_doc, *, raises=False):
    from opensearch_pipeline import access_grants, db

    class _Conn:
        def cursor(self):
            class _C:
                def __enter__(self_in):
                    return self_in
                def __exit__(self_in, *a):
                    return False
            return _C()
        def close(self):
            pass

    monkeypatch.setattr(db, "_get_db_conn", lambda: _Conn())

    def _fake(doc_ids, cur):
        if raises:
            raise RuntimeError("权威不可达")
        return {d: acl_by_doc[d] for d in doc_ids if d in acl_by_doc}

    monkeypatch.setattr(access_grants, "resolve_doc_acl", _fake)


def test_node_authorized_hit_survives_recheck(monkeypatch):
    """★ 阻断项修复:纯组码相交会把这条误丢,node 文档在生产将完全查不出来。"""
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_NODE, permission_level="dept_internal", node_ids=(34265162,))})
    out = r._deny_revoked_cross_dept([NODE_HIT, OWN_HIT, PUB_HIT], "hr", acl_ctx=CTX)
    assert [x["chunk_id"] for x in out] == ["C1", "C2", "C3"]


def test_node_hit_dropped_when_grant_off(monkeypatch):
    """GRANT=false ⇒ 无条件拒绝(即使权威仍有授权)——回滚期的真 public-only。"""
    r = _stub(monkeypatch, grant=False)
    _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_NODE, permission_level="dept_internal", node_ids=(34265162,))})
    out = r._deny_revoked_cross_dept([NODE_HIT, OWN_HIT], "hr", acl_ctx=CTX)
    assert [x["chunk_id"] for x in out] == ["C2"]


def test_revoked_node_grant_dropped(monkeypatch):
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_NODE, permission_level="dept_internal", node_ids=())})   # 已撤销
    out = r._deny_revoked_cross_dept([NODE_HIT, OWN_HIT], "hr", acl_ctx=CTX)
    assert [x["chunk_id"] for x in out] == ["C2"]


def test_authority_unreachable_fails_closed(monkeypatch):
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {}, raises=True)
    out = r._deny_revoked_cross_dept([NODE_HIT, OWN_HIT, PUB_HIT], "hr", acl_ctx=CTX)
    assert [x["chunk_id"] for x in out] == ["C2", "C3"]   # 跨部门命中全丢,自有/public 保留


def test_legacy_doc_still_checked_by_group_semantics(monkeypatch):
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_LEGACY, permission_level="dept_internal",
        owner_dept="quality", groups=("hr",))})
    hit = dict(NODE_HIT, owner_dept="quality")
    assert [x["chunk_id"] for x in r._deny_revoked_cross_dept([hit], "hr", acl_ctx=CTX)] == ["C1"]


def test_recheck_skipped_entirely_without_ctx(monkeypatch):
    """未接线的调用点走原 legacy 路径(不碰新代码)——保证分批接线期零漂移。"""
    r = _stub(monkeypatch, phase_d=False)
    same = [NODE_HIT, OWN_HIT]
    assert r._deny_revoked_cross_dept(same, "hr") is same
