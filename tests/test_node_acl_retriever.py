# -*- coding: utf-8 -*-
"""node-ACL 在 retriever 侧的接线测试(过滤器节点分支 + 撤销复核节点感知)。

最关键的两条:
  ① `acl_ctx=None`(当前全部调用点)⇒ 过滤串与历史**逐字节一致**,零行为漂移;
  ② 撤销复核不再做纯组码相交 —— 否则 node 授权命中会被整批误丢,node 文档在生产
     **根本查不出来**(现网 RAG_ALLOWED_DEPTS_ACL 已开,那是活代码 ⇒ 阻断项)。
"""
import pytest

from opensearch_pipeline import retriever as r_mod
from opensearch_pipeline.acl_policy import (
    ACL_MODE_LEGACY, ACL_MODE_NODE, AclContext, DocAcl, InvalidFlagPosture,
)


def _stub(monkeypatch, *, phase_d=True, grant=False, enforce=True):
    from opensearch_pipeline import retriever

    class _Rag:
        allowed_depts_acl = phase_d
        node_acl_grant = grant
        node_acl_enforce = enforce

    class _AV:                     # cosurface 在拼过滤器【之前】会读 alibaba_vector
        table_name = "t"
        pk_field = "id"

    class _OS:                     # 本地回退链在拼过滤器【之前】会读 opensearch
        host = "localhost"
        port = 9200
        index_name = "t"
        auth_user = None
        auth_password = None
        use_ssl = False
        verify_certs = False

    class _Cfg:
        rag = _Rag()
        alibaba_vector = _AV()
        opensearch = _OS()

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

    seen_strict = []

    def _fake(doc_ids, cur, *, strict=False):
        seen_strict.append(strict)
        if raises:
            raise RuntimeError("权威不可达")
        return {d: acl_by_doc[d] for d in doc_ids if d in acl_by_doc}

    monkeypatch.setattr(access_grants, "resolve_doc_acl", _fake)
    return seen_strict


# D2(OWN_HIT)的权威行。新口径下**全部非 public 命中**都是候选 ⇒ 每条都必须有权威行，
# 否则 strict 解析判定「mode 不可判」而丢弃（现实中每篇被检索到的文档都有 document_meta 行）。
_D2_LEGACY = DocAcl(mode=ACL_MODE_LEGACY, permission_level="dept_internal", owner_dept="hr")


def test_node_authorized_hit_survives_recheck(monkeypatch):
    """★ 阻断项修复:纯组码相交会把这条误丢,node 文档在生产将完全查不出来。"""
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_NODE, permission_level="dept_internal", node_ids=(34265162,)),
        "D2": _D2_LEGACY})
    out = r._deny_revoked_cross_dept([NODE_HIT, OWN_HIT, PUB_HIT], "hr", acl_ctx=CTX)
    assert [x["chunk_id"] for x in out] == ["C1", "C2", "C3"]


def test_node_hit_dropped_when_grant_off(monkeypatch):
    """GRANT=false ⇒ 无条件拒绝(即使权威仍有授权)——回滚期的真 public-only。"""
    r = _stub(monkeypatch, grant=False)
    _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_NODE, permission_level="dept_internal", node_ids=(34265162,)),
        "D2": _D2_LEGACY})
    out = r._deny_revoked_cross_dept([NODE_HIT, OWN_HIT], "hr", acl_ctx=CTX)
    assert [x["chunk_id"] for x in out] == ["C2"]


def test_revoked_node_grant_dropped(monkeypatch):
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_NODE, permission_level="dept_internal", node_ids=()),   # 已撤销
        "D2": _D2_LEGACY})
    out = r._deny_revoked_cross_dept([NODE_HIT, OWN_HIT], "hr", acl_ctx=CTX)
    assert [x["chunk_id"] for x in out] == ["C2"]


def test_authority_unreachable_fails_closed(monkeypatch):
    """权威不可达 ⇒ 全部**非 public** 命中丢弃（含自有部门），public 保留。

    与旧口径的区别:旧代码只丢「跨部门」命中，自有部门命中照留。但 mode 权威读不到时
    根本无法区分 node 与 legacy —— 一条 owner 恰为自有组码的命中可能是投影未收敛的
    node 文档。故只能整体 fail-closed（codex 共识 2026-07-31）。
    """
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {}, raises=True)
    out = r._deny_revoked_cross_dept([NODE_HIT, OWN_HIT, PUB_HIT], "hr", acl_ctx=CTX)
    assert [x["chunk_id"] for x in out] == ["C3"]


def test_legacy_doc_still_checked_by_group_semantics(monkeypatch):
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_LEGACY, permission_level="dept_internal",
        owner_dept="quality", groups=("hr",))})
    hit = dict(NODE_HIT, owner_dept="quality")
    assert [x["chunk_id"] for x in r._deny_revoked_cross_dept([hit], "hr", acl_ctx=CTX)] == ["C1"]


# ── B-1/B-2 安全矩阵（2026-07-31 codex 评审要求）────────────────────────────
def test_node_enforce_runs_even_when_legacy_flag_off(monkeypatch):
    """★ B-1：node ENFORCE 不得随 legacy flag `RAG_ALLOWED_DEPTS_ACL` 一起被关掉。

    设计稿 §8 明确 ENFORCE 是常开闸门；原实现把整段复核挂在该 flag 之下（代码默认 false），
    等于回滚姿态下 node 文档照样投放。
    """
    r = _stub(monkeypatch, phase_d=False, grant=False)      # legacy flag 关
    _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_NODE, permission_level="dept_internal", node_ids=(34265162,))})
    out = r._deny_revoked_cross_dept([NODE_HIT], "hr", acl_ctx=CTX)
    assert [x["chunk_id"] for x in out] == []               # GRANT=false ⇒ 无条件拒


def test_stale_real_owner_equal_to_caller_group_still_denied(monkeypatch):
    """★ B-2：投影未收敛、HA3 里仍是旧真实 owner 且**恰等于调用者组码**的 node 文档必须被拒。

    旧口径按「owner ∉ 自有集」筛候选 ⇒ 这条根本不进复核 ⇒ can_read_doc 没机会执行。
    实测复现过：GRANT=false + owner 'hr' + 调用者组码 'hr' ⇒ 文档仍被投放。
    """
    r = _stub(monkeypatch, grant=False)
    _patch_authority(monkeypatch, {"D9": DocAcl(
        mode=ACL_MODE_NODE, permission_level="dept_internal", owner_dept="hr",
        node_ids=(34265162,))})
    stale = {"doc_id": "D9", "permission_level": "dept_internal",
             "owner_dept": "hr", "chunk_id": "STALE"}       # ← owner 就是调用者组码
    assert r._deny_revoked_cross_dept([stale], "hr", acl_ctx=CTX) == []


def test_legacy_same_owner_hit_kept_without_authority_verdict(monkeypatch):
    """option (b)：确认 legacy 且 owner ∈ 自有集 ⇒ 保留旧语义，不因权威判定收紧而被拒。

    这里刻意给一个 can_read_doc 会拒的 legacy ACL（owner 与命中不一致=投影漂移），
    验证 same-owner 命中仍被保留 —— 不把 owner 漂移即拒这种新行为带进 legacy 路径。
    """
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {"D2": DocAcl(
        mode=ACL_MODE_LEGACY, permission_level="dept_internal",
        owner_dept="quality", groups=())})                  # 权威 owner 已漂到 quality
    assert [x["chunk_id"] for x in r._deny_revoked_cross_dept([OWN_HIT], "hr", acl_ctx=CTX)] == ["C2"]


def test_missing_document_meta_row_denied(monkeypatch):
    """strict：缺 document_meta 行（resolve 不返回该 doc）⇒ 拒，绝不当 legacy 放行。"""
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {})                       # 什么都没有
    assert r._deny_revoked_cross_dept([NODE_HIT], "hr", acl_ctx=CTX) == []


def test_candidate_without_doc_id_denied(monkeypatch):
    """候选没有 doc_id ⇒ 无从按权威判定 ⇒ fail-closed（不能当作『没有候选』放行）。"""
    r = _stub(monkeypatch, grant=True)
    _patch_authority(monkeypatch, {})
    anon = {"permission_level": "dept_internal", "owner_dept": "hr", "chunk_id": "ANON"}
    assert r._deny_revoked_cross_dept([anon, PUB_HIT], "hr", acl_ctx=CTX) == [PUB_HIT]


def test_public_hits_never_enter_authority_lookup(monkeypatch):
    """public 命中既不查权威也不被丢弃（零开销 + 语义不变）。"""
    r = _stub(monkeypatch, grant=True)
    from opensearch_pipeline import access_grants

    def _boom(doc_ids, cur, *, strict=False):
        raise AssertionError("public-only 结果集不得触发权威查询")

    monkeypatch.setattr(access_grants, "resolve_doc_acl", _boom)
    assert r._deny_revoked_cross_dept([PUB_HIT], "hr", acl_ctx=CTX) == [PUB_HIT]


def test_serving_recheck_passes_strict_true(monkeypatch):
    """★ 接线钉死：serving 复核必须传 `strict=True`。

    把生产调用处的 `strict=True` 删掉，其余组合测试仍可能通过（宽松解析下 node 文档
    照样解析得出）—— 但那样「缺行/未知 mode/权威不可达」就会退回按 legacy 放行，
    stale-owner 窗口重新打开。故单独钉这一条。
    """
    r = _stub(monkeypatch, grant=True)
    seen_strict = _patch_authority(monkeypatch, {"D1": DocAcl(
        mode=ACL_MODE_NODE, permission_level="dept_internal", node_ids=(34265162,))})
    r._deny_revoked_cross_dept([NODE_HIT], "hr", acl_ctx=CTX)
    assert seen_strict == [True]


# ── cosurface / 本地回退：node 通道必须同样接通 ──────────────────────────────
def test_cosurface_filter_carries_node_branch(monkeypatch):
    """★ 补图查询必须带节点分支 —— 漏传 acl_ctx ⇒ node 文档【正文可见但配图召不回】。

    cosurface 的过滤器与主检索共用 `_build_permission_filter`，这里钉死它收到了 acl_ctx
    （而不是像此前那样固定用裸 user_dept 调）。用 spy 记录后立刻抛出中断，避免真发 HA3。
    """
    r = _stub(monkeypatch, grant=True)
    seen = {}

    class _Stop(Exception):
        pass

    def _spy(user_dept, *, acl_ctx=None):
        seen["user_dept"], seen["acl_ctx"] = user_dept, acl_ctx
        raise _Stop()

    monkeypatch.setattr(r, "_build_permission_filter", _spy)
    monkeypatch.setattr(r, "get_query_embedding", lambda q: ([0.0], [], []))
    with pytest.raises(_Stop):
        r._fetch_cosurface_images("q", ["D1"], "hr", ([0.0], [], []), acl_ctx=CTX)
    assert seen["acl_ctx"] is CTX, "cosurface 没把 acl_ctx 传给权限过滤器"
    assert seen["user_dept"] == "hr"


def test_cosurface_prefetch_uses_same_acl_ctx():
    """★ F#53 预取路径必须与串行同参：预取命中时其结果被**直接采用**（只比对 doc_ids），
    漏传 acl_ctx 就成了"预取时没图、串行时有图"这种随并发时序漂移的缺图。"""
    import inspect
    src = inspect.getsource(r_mod.retrieve_and_enrich)
    assert "_fetch_cosurface_images, query, _pre_ids, user_dept, _emb" in src
    assert "acl_ctx=acl_ctx" in src.split("_fetch_cosurface_images")[1][:200]


def test_local_opensearch_fallback_has_node_branch(monkeypatch):
    """★ 本地 OpenSearch 回退链的权限子句也要有节点分支（写侧 to_opensearch_doc 已输出该字段）。

    节点分支与组码**互相独立**：无组码映射的员工拿到节点授权后，在回退链上同样必须可见。
    """
    from opensearch_pipeline.acl_policy import AclContext
    r = _stub(monkeypatch, grant=True)
    captured = {}

    class _Stop(Exception):
        pass

    class _FakeOS:
        def __init__(self, *a, **kw): pass
        def search(self, index=None, body=None):
            captured["body"] = body
            raise _Stop()

    # `OpenSearch` 是函数内惰性导入（`from opensearchpy import OpenSearch`）⇒ 必须打**源模块**，
    # 打 retriever.OpenSearch 不生效（那样只会静默跳过、测试形同虚设）。
    import opensearchpy
    monkeypatch.setattr(opensearchpy, "OpenSearch", _FakeOS)
    ctx = AclContext(groups=(), ancestor_dept_ids=(7,), direct_dept_ids=(9,))
    with pytest.raises(_Stop):
        r._search_chunks_opensearch("q", [0.1], 5, None, acl_ctx=ctx)
    import json as _json
    # 只看**权限 filter**那一段（body 里还有 _source 字段清单，含 owner_dept 字面量）
    perm = _json.dumps(captured["body"]["query"]["bool"]["filter"], ensure_ascii=False)
    assert '"allowed_depts": ["d:7", "dx:9"]' in perm, f"回退链权限子句缺节点分支: {perm}"
    assert "owner_dept" not in perm, "无组码时权限子句不应出现 owner 分支"


def test_to_opensearch_doc_emits_allowed_depts():
    """★ 写侧：本地回退索引必须带 allowed_depts，否则查询侧的节点分支永远匹配不上。"""
    from opensearch_pipeline.chunker import Chunk

    def _mk(cid):
        return Chunk(chunk_id=cid, doc_id="D1", version_no=1, chunk_index=0,
                     chunk_type="text_chunk", chunk_text="x", token_count=1)

    c = _mk("c1")
    c.allowed_depts = ["d:7", "dx:9"]
    assert c.to_opensearch_doc()["allowed_depts"] == ["d:7", "dx:9"]
    assert _mk("c2").to_opensearch_doc()["allowed_depts"] == []      # 默认空，不是缺键


# ── groups_that_can_read_owner：_expand_groups_to_owners 的反向 ──────────────
def test_groups_that_can_read_owner_is_inverse_of_expansion():
    """性质测试：对每个合法组码 g 与其展开出的每个 owner o，g 必须出现在反查结果里。"""
    from opensearch_pipeline.retriever import (
        _VALID_ACL_GROUPS, _expand_groups_to_owners, groups_that_can_read_owner,
    )
    for g in _VALID_ACL_GROUPS:
        for o in _expand_groups_to_owners([g]):
            assert g in groups_that_can_read_owner(o), f"组码 {g} 能读 {o}，反查却漏了它"


def test_groups_that_can_read_owner_never_emits_non_group_values():
    """★ owner 值域 ≠ 组码值域：反查结果必须全部是**合法组码**。

    `production_mold` 是合法 owner 但不是组码 —— 直接当组码用会被白名单丢掉，
    过滤器只剩 public（这正是探针曾经误报整篇缺失的原因）。
    """
    from opensearch_pipeline.retriever import (
        _VALID_ACL_GROUPS, _expand_groups_to_owners, groups_that_can_read_owner,
    )
    for g in _VALID_ACL_GROUPS:
        for o in _expand_groups_to_owners([g]):
            assert set(groups_that_can_read_owner(o)) <= set(_VALID_ACL_GROUPS)
    assert groups_that_can_read_owner("production_mold") == ["marketing", "production"]
    assert "production_mold" not in groups_that_can_read_owner("production_mold")
    assert groups_that_can_read_owner("hr") == ["hr"]
    assert groups_that_can_read_owner("  hr  ") == ["hr"]      # 自行 strip
    assert groups_that_can_read_owner("") == [] and groups_that_can_read_owner(None) == []


def test_recheck_checks_out_exactly_one_connection(monkeypatch):
    """候选再多也只建**一次**连接、只调一次 resolve_doc_acl（批量解析，不随候选数增长）。

    B-2 把候选从「跨部门」扩到「全部非 public」后，这条是延迟不失控的结构性前提。
    """
    r = _stub(monkeypatch, grant=True)
    from opensearch_pipeline import access_grants, db
    counters = {"conn": 0, "resolve": 0}

    class _Conn:
        def cursor(self):
            class _C:
                def __enter__(s):
                    return s
                def __exit__(s, *a):
                    return False
            return _C()
        def close(self):
            pass

    def _open():
        counters["conn"] += 1
        return _Conn()

    def _resolve(doc_ids, cur, *, strict=False):
        counters["resolve"] += 1
        return {d: DocAcl(mode=ACL_MODE_NODE, permission_level="dept_internal",
                          node_ids=(34265162,)) for d in doc_ids}

    monkeypatch.setattr(db, "_get_db_conn", _open)
    monkeypatch.setattr(access_grants, "resolve_doc_acl", _resolve)
    hits = [{"doc_id": f"D{i}", "permission_level": "dept_internal",
             "owner_dept": "__acl_node_mode_v1__", "chunk_id": f"C{i}"} for i in range(20)]
    out = r._deny_revoked_cross_dept(hits, "hr", acl_ctx=CTX)
    assert len(out) == 20
    assert counters == {"conn": 1, "resolve": 1}


def test_org_wide_reader_needs_trusted_node_channel(monkeypatch):
    """org_wide_reader 的『放行全部 dept_internal』候选分支必须先过 node_channel_ok。

    与 can_read_doc（acl_policy.py:283 先判 node_channel_ok 再判 org_wide_reader）同序；
    否则组织快照不可信时会把判定压力全推给事后复核。
    """
    r = _stub(monkeypatch, grant=True)
    trusted = AclContext(groups=("hr",), org_wide_reader=True, node_channel_ok=True)
    untrusted = AclContext(groups=("hr",), org_wide_reader=True, node_channel_ok=False)
    assert 'OR (permission_level="dept_internal")' in r._build_permission_filter("hr", acl_ctx=trusted)
    assert r._build_permission_filter("hr", acl_ctx=untrusted) == r._build_permission_filter("hr")


# ── A1：空 groups 不得屏蔽节点通道 ───────────────────────────────────────────
def test_empty_groups_still_gets_node_branch(monkeypatch):
    """★ A1：无组码映射的员工（现网部门映射缺口 26/131）拿到节点授权后必须能检索到。

    原实现在 groups 为空时早退 public-only ⇒ 过滤器比 can_read_doc 更严（后者的 node
    分支压根不读 ctx.groups）。
    """
    r = _stub(monkeypatch, grant=True)
    ctx = AclContext(groups=(), ancestor_dept_ids=(34265162,), direct_dept_ids=(99,))
    out = r._build_permission_filter(None, acl_ctx=ctx)
    assert 'allowed_depts="d:34265162"' in out and 'allowed_depts="dx:99"' in out
    assert "owner_dept=" not in out, "无组码 ⇒ 不得出现任何 owner 分支"


@pytest.mark.parametrize("grant", [False, True])
def test_empty_groups_without_ctx_is_byte_identical(monkeypatch, grant):
    """空 groups + 无 acl_ctx ⇒ 仍是历史那一个字符串（A1 的零漂移不变量）。"""
    r = _stub(monkeypatch, grant=grant)
    assert r._build_permission_filter(None) == '(permission_level="public")'
    assert r._build_permission_filter(None, acl_ctx=None) == '(permission_level="public")'


def test_empty_groups_grant_off_stays_public_only(monkeypatch):
    """GRANT=false ⇒ 空 groups 仍是 public-only（正向通道关闭时不得因 A1 泄漏）。"""
    r = _stub(monkeypatch, grant=False)
    ctx = AclContext(groups=(), ancestor_dept_ids=(34265162,))
    assert r._build_permission_filter(None, acl_ctx=ctx) == '(permission_level="public")'


def test_recheck_skipped_entirely_without_ctx(monkeypatch):
    """未接线的调用点走原 legacy 路径(不碰新代码)——保证分批接线期零漂移。"""
    r = _stub(monkeypatch, phase_d=False)
    same = [NODE_HIT, OWN_HIT]
    assert r._deny_revoked_cross_dept(same, "hr") is same
