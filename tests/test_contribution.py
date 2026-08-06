# -*- coding: utf-8 -*-
"""test_contribution.py — 员工知识贡献：纯函数层 + 端点状态机/授权/缺口口径，全程 simulate。

覆盖方案 v2「测试必覆盖」清单（点10）：
  并发 accept 不出第二篇 · OSS 成功但 DB 失败后 retry 恢复 · retry 幂等 · 跨部门只读不能审核 ·
  改目标部门按新部门重授权 · 合成正文不含提交人姓名 · PII 不泄露 · 一问多 chunk 不重复计数 ·
  accepted 但未 searchable 缺口不消失（searchable 才关闭）· hash 归一化。

桩 DB 按 SQL 关键字回放 fetchone/fetchall（仿 test_kb_register._FakeConn）。
"""
import pytest


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


# ── 桩 DB ────────────────────────────────────────────────────────────────
class _FakeCur:
    def __init__(self, conn):
        self.conn = conn
        self._last = ""
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        self._last = sql
        self.rowcount = 1
        # 注入：document_version INSERT 失败（非 1062）→ 物化失败路径
        if ("INSERT INTO" in sql and "document_version" in sql
                and self.conn.fail_version_insert > 0):
            self.conn.fail_version_insert -= 1
            raise Exception("simulated DB failure on version insert")
        # 注入：retry 的 FAILED 文档重新排队 UPDATE 失败（best-effort 断言用）
        if (self.conn.fail_requeue and "document_version" in sql
                and "content_process_status='NOT_STARTED'" in sql):
            raise Exception("simulated requeue failure")
        # 条件认领 UPDATE：rowcount = claim_rowcount（0=竞态被抢）
        if ("UPDATE" in sql and "kb_contribution" in sql
                and "review_status='accepted'" in sql
                and "AND review_status='pending'" in sql):
            self.rowcount = self.conn.claim_rowcount
        return 1

    def fetchone(self):
        s, c = self._last, self.conn
        if "kb_contribution" in s and "FOR UPDATE" in s:
            return c.contrib_row
        if "document_version" in s and "WHERE raw_key=" in s:
            return c.dv_exists
        if ("review_status, ingestion_status, doc_id" in s
                and "WHERE contribution_id=" in s and "FOR UPDATE" not in s):
            return c.reread_row
        if "ingestion_status='searchable'" in s and "COUNT(*)" in s:
            return (c.summary.get("answered", 0),)
        if "YEAR(created_at)=YEAR(NOW())" in s:
            return (c.summary.get("this_month", 0),)
        if "COUNT(DISTINCT author_id)" in s:
            return (c.summary.get("contributors", 0),)
        if "TIMESTAMPDIFF(MINUTE, created_at, reviewed_at)" in s:   # ε-3 R3 审核漏斗
            if c.boom_funnel:
                raise Exception("simulated funnel failure")
            return c.funnel_row
        if "SELECT group_id" in s and "qa_gap_semantic_group" in s:   # 忽略联动：查组头
            return c.gap_group_head_row
        if "qa_gap_dismissal" in s and "LIMIT 1" in s:   # 批次9 裁权：幂等豁免探针
            return (1,) if c.gap_dismissal_rows else None
        if "WHERE message_id=" in s and "session_id" in s:   # 缺口上下文：单 message 行
            return c.ctx_msg_row
        if "hit_mine" in s and "WHERE q.message_id=" in s:   # 缺口上下文：REFUSAL 可见性
            return c.ctx_vis_row
        if "staff_dim" in s and "staff_id=%s" in s:          # 三榜：请求者本部门
            return c.staff_me_row
        return None

    def fetchall(self):
        s, c = self._last, self.conn
        if "MIN(id)" in s and "GROUP BY session_id" in s:   # 会话上下文批查（2026-07-18）
            if c.boom_session_min:
                raise Exception("simulated session-min failure")
            return c.session_min_rows
        if "ORDER BY created_at DESC, id DESC" in s:        # 缺口上下文：前序行游标查询
            return c.ctx_prior_rows
        if "answer_status='NO_RESULT'" in s:
            return c.no_result_rows
        if "t.hit_mine=1 OR t.all_public=1" in s:
            return c.refusal_rows
        if "kb_contribution WHERE question_hash IN" in s:
            return c.coverage_rows
        if "GROUP BY c.author_id" in s:        # ε-2 R2 heroes 引用数聚合（先于泛 author_id 分支）
            return c.hero_hits_rows
        if "staff_dim" in s and "IN (" in s:            # 三榜：积分池组织归属
            return c.staff_pool_rows
        if "FROM" in s and "dept_dim" in s:             # 三榜：组织树（一级上溯）
            return c.dept_dim_rows
        if "handled_status='RESOLVED'" in s and "GROUP_CONCAT" in s:   # 总积分榜：有效反馈聚合
            if c.boom_feedback:
                raise Exception("simulated feedback aggregation failure")
            return c.hero_feedback_rows
        if "FROM" in s and "user_role" in s and "user_id IN" in s:     # 展示名回填
            return c.user_role_rows
        if "GROUP BY jt.doc_id" in s:          # ε-2 R2 mine 逐文档引用数
            if c.boom_hits:
                raise Exception("simulated hits aggregation failure")
            return c.doc_hits_rows
        if "qa_gap_dismissal" in s:            # 忽略台账（schema/041）
            if c.boom_dismissal:
                raise Exception("simulated dismissal lookup failure")
            return c.gap_dismissal_rows
        if "qa_gap_semantic_group" in s and "WHERE group_id=" in s:   # 联动成员展开
            return c.gap_group_member_rows
        if "qa_gap_semantic_group" in s:       # 语义组映射（schema/040）
            if c.boom_gap_groups:
                raise Exception("simulated gap group lookup failure")
            return c.gap_group_rows
        # ε-3 R1 管线徽章派生（标记=document_meta dm——reconcile 的 UPDATE 也含 document_version dv）
        if "document_version dv" in s and "document_meta dm" in s:
            if c.boom_badges:
                raise Exception("simulated badge derivation failure")
            return c.dv_badge_rows
        if "COALESCE(gap_query_hash, question_hash)" in s:   # ε-3 R2 asks：hash 映射
            if c.boom_asks:
                raise Exception("simulated asks hash lookup failure")
            return c.hash_rows
        if "answer_status='REFUSAL'" in s and "hit_mine" not in s:   # ε-3 R2 asks：REFUSAL 平查
            return c.refusal_plain_rows
        if "GROUP BY author_id" in s:
            return c.hero_rows
        if "kb_contribution" in s and "ORDER BY" in s:
            return c.list_rows
        return []


class _FakeConn:
    def __init__(self, **kw):
        self.contrib_row = kw.get("contrib_row")
        self.dv_exists = kw.get("dv_exists")
        self.reread_row = kw.get("reread_row")
        self.claim_rowcount = kw.get("claim_rowcount", 1)
        self.fail_version_insert = kw.get("fail_version_insert", 0)
        self.fail_requeue = kw.get("fail_requeue", False)
        self.no_result_rows = kw.get("no_result_rows", [])
        self.refusal_rows = kw.get("refusal_rows", [])
        self.coverage_rows = kw.get("coverage_rows", [])
        self.hero_rows = kw.get("hero_rows", [])
        self.hero_hits_rows = kw.get("hero_hits_rows", [])
        self.hero_feedback_rows = kw.get("hero_feedback_rows", [])
        self.staff_pool_rows = kw.get("staff_pool_rows", [])
        self.dept_dim_rows = kw.get("dept_dim_rows", [])
        self.staff_me_row = kw.get("staff_me_row")
        self.boom_feedback = kw.get("boom_feedback", False)
        self.user_role_rows = kw.get("user_role_rows", [])
        self.doc_hits_rows = kw.get("doc_hits_rows", [])
        self.boom_hits = kw.get("boom_hits", False)
        self.dv_badge_rows = kw.get("dv_badge_rows", [])
        self.boom_badges = kw.get("boom_badges", False)
        self.hash_rows = kw.get("hash_rows", [])
        self.refusal_plain_rows = kw.get("refusal_plain_rows", [])
        self.boom_asks = kw.get("boom_asks", False)
        self.funnel_row = kw.get("funnel_row", (None, None, None))
        self.boom_funnel = kw.get("boom_funnel", False)
        self.gap_group_rows = kw.get("gap_group_rows", [])
        self.boom_gap_groups = kw.get("boom_gap_groups", False)
        self.gap_dismissal_rows = kw.get("gap_dismissal_rows", [])
        self.boom_dismissal = kw.get("boom_dismissal", False)
        self.gap_group_head_row = kw.get("gap_group_head_row")
        self.gap_group_member_rows = kw.get("gap_group_member_rows", [])
        self.session_min_rows = kw.get("session_min_rows", [])     # (session_id, min_id)
        self.boom_session_min = kw.get("boom_session_min", False)
        self.ctx_msg_row = kw.get("ctx_msg_row")                   # 上下文端点：单 message 行
        self.ctx_vis_row = kw.get("ctx_vis_row")                   # (hit_mine, all_public)
        self.ctx_prior_rows = kw.get("ctx_prior_rows", [])         # 前序行（DESC 序）
        self.list_rows = kw.get("list_rows", [])
        self.summary = kw.get("summary", {})
        self.calls = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return _FakeCur(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _install_conn(monkeypatch, conn):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)
    return conn


def _capture_put(monkeypatch, ok=True, sink=None):
    import opensearch_pipeline.oss_url as ou

    def _put(key, data, content_type="text/markdown; charset=utf-8"):
        if sink is not None:
            sink.append((key, data.decode("utf-8") if isinstance(data, bytes) else data))
        return ok
    monkeypatch.setattr(ou, "put_object", _put)


def _dept_admin(monkeypatch, managed="marketing"):
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", managed)


def _kb_admin(monkeypatch):
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")


def _employee(monkeypatch):
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")


def _ident(uid="u1", groups=("marketing",), name="张三"):
    from opensearch_pipeline import api
    return api.Identity(user_id=uid, acl_groups=list(groups), name=name)


# ── 1) 纯函数层 ──────────────────────────────────────────────────────────
def test_normalize_and_hash_equivalence():
    from opensearch_pipeline import contribution as C
    assert C.normalize_question("如何  申请生产环境密钥？") == "如何申请生产环境密钥"
    # 全角/大小写/空白/标点差异 → 同 hash
    assert C.question_hash("How to APPLY  key?") == C.question_hash("how　to　apply key")
    assert C.question_hash("如何申请密钥") == C.question_hash("如何 申请密钥！")
    assert C.question_hash("a") != C.question_hash("b")


def test_synthesize_markdown_faq_shape_excludes_author_name():
    from opensearch_pipeline import contribution as C
    md = C.synthesize_markdown("如何报销?", "走 OA 流程提交。")
    # FAQ 形态：问：/答： 段落（chunker._chunk_faq 可配成 1 个 faq_chunk）；不用 # 标题（会打断配对）
    assert md == "问：如何报销?\n\n答：走 OA 流程提交。\n"
    assert not md.lstrip().startswith("#")              # 无 H1 heading
    assert "张三" not in md and "提交人" not in md      # 姓名/审计绝不进正文


def test_contribution_state_folding():
    from opensearch_pipeline import contribution as C
    assert C.contribution_state("pending", "none") == "pending"
    assert C.contribution_state("rejected", "none") == "rejected"
    assert C.contribution_state("accepted", "none") == "registering"
    assert C.contribution_state("accepted", "registered") == "registering"
    assert C.contribution_state("accepted", "searchable") == "searchable"
    assert C.contribution_state("accepted", "failed") == "failed"


def test_redact_query_text_masks_pii():
    from opensearch_pipeline import contribution as C
    out = C.redact_query_text("我的手机号13800138000怎么报销")
    assert "13800138000" not in out and "已脱敏" in out


def test_validate_contribution_text():
    from opensearch_pipeline import contribution as C
    assert C.validate_contribution_text("", "a") is not None
    assert C.validate_contribution_text("q", "") is not None
    assert C.validate_contribution_text("q", "a") is None


# ── 2) 提交 ──────────────────────────────────────────────────────────────
def test_submit_happy(monkeypatch):
    _skip_if_not_sim()
    _employee(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn())
    from opensearch_pipeline import api
    resp = api.kb_contribution_submit(
        req=api.KbContributionSubmitRequest(question="如何申请密钥?", content="走 OA。", category_dept="marketing"),
        request=None, identity=_ident())
    assert resp.review_status == "pending" and resp.state == "pending"
    assert resp.category_dept == "marketing"
    assert conn.committed is True
    # INSERT 落了 question_hash（去重对齐）
    assert any("INSERT INTO" in s and "kb_contribution" in s for s, _ in conn.calls)


def test_submit_invalid_dept_400(monkeypatch):
    _skip_if_not_sim()
    _employee(monkeypatch)
    _install_conn(monkeypatch, _FakeConn())
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_contribution_submit(
            req=api.KbContributionSubmitRequest(question="q", content="a", category_dept="不存在的部门"),
            request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 400


def test_submit_requires_login_401(monkeypatch):
    _skip_if_not_sim()
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_contribution_submit(
            req=api.KbContributionSubmitRequest(question="q", content="a", category_dept="marketing"),
            request=None, identity=None)
    assert getattr(ei.value, "status_code", None) == 401


# ── 3) 采纳：happy / 物化 / 正文洁净 ─────────────────────────────────────
def test_accept_happy_materializes_clean_doc(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "如何申请密钥?", "走 OA。", "marketing"),
        claim_rowcount=1, dv_exists=None))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="CONTRIB_X", req=api.KbContributionAcceptRequest(),
                                      request=None, identity=_ident())
    assert resp.review_status == "accepted"
    assert resp.ingestion_status == "registered"   # 物化登记完成
    assert resp.state == "registering"             # 徽章=已采纳·待入库（未 searchable）
    assert resp.ok is True
    # 写了 document_meta + document_version（NOT_STARTED）
    assert any("INSERT INTO" in s and "document_meta" in s for s, _ in conn.calls)
    assert any("document_version" in s and "NOT_STARTED" in s for s, _ in conn.calls)
    # 合成正文洁净（FAQ 形态：问：/答：，不含提交人姓名）
    assert sink and "张三" not in sink[0][1]
    assert "问：如何申请密钥?" in sink[0][1] and "答：" in sink[0][1]


# ── 3b) 部门领导定可见范围 + 权限编码进路径（防 stage-2 升/降权）──
def test_accept_default_is_dept_internal_internal_path(monkeypatch):
    """默认=部门公开：raw_key 含 /internal/ 段（管线解析回 dept_internal，不升公开），meta 写 dept_internal/private。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"), claim_rowcount=1, dv_exists=None))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(), request=None, identity=_ident())
    assert resp.ok is True
    assert sink and "/internal/" in sink[0][0]   # 路径编码 dept_internal
    meta = [p for s, p in conn.calls if "INSERT INTO" in s and "document_meta" in s][0]
    assert "dept_internal" in meta and "private" in meta


def test_accept_slash_injected_dept_cannot_escalate_to_public(monkeypatch):
    """P0 回归(2026-07-17 ultra 评审)：category_dept='marketing/' 曾通过 authorize_upload
    (校验净化副本)却把原值编进 raw_key,权限段错位 → perm_from_raw_key 读回 public,
    dept_admin 绕过 kb_admin 审批直发全员。修后:净值贯穿授权/raw_key/落库,物化 dept_internal。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"),
        claim_rowcount=1, dv_exists=None))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(
        cid="C_SLASH", req=api.KbContributionAcceptRequest(category_dept="marketing/"),
        request=None, identity=_ident())
    assert resp.ok is True
    # raw_key 无双斜杠、权限段(第 3 段)完好
    assert sink and "//" not in sink[0][0] and "raw/marketing/internal/" in sink[0][0]
    # 物化 permission_level=dept_internal,绝非 public
    meta = [p for s, p in conn.calls if "INSERT INTO" in s and "document_meta" in s][0]
    assert "dept_internal" in meta and "public" not in meta
    # kb_contribution 落库的 category_dept 是净值 'marketing'
    upd = [p for s, p in conn.calls if "SET review_status='accepted'" in s][0]
    assert "marketing" in upd and "marketing/" not in upd


def test_accept_public_by_dept_admin_gated_pending_approval(monkeypatch):
    """P2-16（2026-07-04 拍板「kb_admin 只管入库」）：dept_admin 选「全员公开」不再直通——
    物化登记为 PENDING_APPROVAL/PENDING 进 kb_admin 既有待审批队列；raw_key 仍扁平、meta 仍 public。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"), claim_rowcount=1, dv_exists=None))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(permission_level="public"),
                                      request=None, identity=_ident())
    assert resp.ok is True and resp.ingestion_status == "registered"
    assert resp.requires_kb_admin_approval is True     # 前端提示"放行后才入库"
    assert sink and "/internal/" not in sink[0][0]
    meta = [p for s, p in conn.calls if "INSERT INTO" in s and "document_meta" in s][0]
    assert "public" in meta
    # document_version 登记为待审批（scanner 不认领），而非 NOT_STARTED 直通
    dv_sqls = [s for s, _ in conn.calls if "INSERT INTO" in s and "document_version" in s]
    assert dv_sqls and "PENDING_APPROVAL" in dv_sqls[0] and "'PENDING'" in dv_sqls[0]
    assert "NOT_STARTED" not in dv_sqls[0]


def test_accept_public_by_kb_admin_stays_direct(monkeypatch):
    """kb_admin 自己采纳 public → 照旧直通（其即终审）：NOT_STARTED/APPROVED，无待审批。"""
    _skip_if_not_sim()
    _kb_admin(monkeypatch)
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"), claim_rowcount=1, dv_exists=None))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(permission_level="public"),
                                      request=None, identity=_ident())
    assert resp.ok is True and resp.ingestion_status == "registered"
    assert resp.requires_kb_admin_approval is False
    dv_sqls = [s for s, _ in conn.calls if "INSERT INTO" in s and "document_version" in s]
    assert dv_sqls and "NOT_STARTED" in dv_sqls[0] and "PENDING_APPROVAL" not in dv_sqls[0]


def test_accept_dept_internal_by_dept_admin_unchanged(monkeypatch):
    """P2-16 回归护栏：dept_admin 采纳 dept_internal（默认）完全不变——NOT_STARTED 直通。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"), claim_rowcount=1, dv_exists=None))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(),
                                      request=None, identity=_ident())
    assert resp.ok is True and resp.requires_kb_admin_approval is False
    dv_sqls = [s for s, _ in conn.calls if "INSERT INTO" in s and "document_version" in s]
    assert dv_sqls and "NOT_STARTED" in dv_sqls[0] and "PENDING_APPROVAL" not in dv_sqls[0]


def test_accept_invalid_permission_400(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"), claim_rowcount=1))
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(permission_level="restricted"),
                                   request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 400


# ── 4) 授权矩阵 ──────────────────────────────────────────────────────────
def test_accept_employee_forbidden(monkeypatch):
    _skip_if_not_sim()
    _employee(monkeypatch)
    _install_conn(monkeypatch, _FakeConn(contrib_row=("pending", "none", None, None, None, "q", "a", "marketing")))
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(),
                                   request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 403


def test_accept_cross_dept_readonly_cannot_review(monkeypatch):
    """跨部门只读授权者（dept_admin 管 marketing）不能采纳 finance 的贡献。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "finance"), claim_rowcount=1))
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(),
                                   request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 403


def test_accept_dept_change_reauthz(monkeypatch):
    """锁定原部门（用户裁决 2026-06-29）：accept 先校验 _kb_can_manage(原 category_dept)。
    - 仅管 marketing 的管理员，原 finance → 改投 marketing：禁止跨部门接管 → 403。
    - 同时管 finance+marketing 的管理员，原 finance → 改投 marketing：合法改投 → 放行。
    - 改成未管理的目标部门 → 403。
    """
    _skip_if_not_sim()
    _capture_put(monkeypatch, ok=True)
    from opensearch_pipeline import api
    # (1) 仅管 marketing，原 finance → 改投 marketing：管不了原部门 finance → 403（防抢稿）
    _dept_admin(monkeypatch, managed="marketing")
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "finance"),
        claim_rowcount=1, dv_exists=None))
    with pytest.raises(Exception) as ei0:
        api.kb_contribution_accept(
            cid="C1", req=api.KbContributionAcceptRequest(category_dept="marketing"),
            request=None, identity=_ident())
    assert getattr(ei0.value, "status_code", None) == 403
    # (2) 同时管 finance+marketing，原 finance → 改投 marketing：两端皆可管 → 放行
    _dept_admin(monkeypatch, managed="finance,marketing")
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "finance"),
        claim_rowcount=1, dv_exists=None))
    resp = api.kb_contribution_accept(
        cid="C2", req=api.KbContributionAcceptRequest(category_dept="marketing"),
        request=None, identity=_ident())
    assert resp.review_status == "accepted"
    # (3) 仅管 marketing，原 marketing → 改投未管理的 finance → 403（目标部门写权限）
    _dept_admin(monkeypatch, managed="marketing")
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"), claim_rowcount=1))
    with pytest.raises(Exception) as ei:
        api.kb_contribution_accept(
            cid="C3", req=api.KbContributionAcceptRequest(category_dept="finance"),
            request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 403


def test_accept_foreign_dept_poach_blocked(monkeypatch):
    """回归：仅管 marketing 的管理员凭 cid 直接采纳 finance 队列的贡献（不改 final_dept），
    也必须 403——原部门校验先于目标部门，杜绝跨部门接管。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "finance"), claim_rowcount=1))
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_contribution_accept(cid="C9", req=api.KbContributionAcceptRequest(),
                                   request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 403


# ── 5) 幂等 / 并发：不出第二篇文档 ───────────────────────────────────────
def test_accept_already_accepted_idempotent_no_second_doc(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("accepted", "registered", "DOC_PRIOR", "UP1", "raw/marketing/DOC_PRIOR/UP1/x.md",
                     "q", "a", "marketing")))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(),
                                      request=None, identity=_ident())
    assert resp.idempotent is True
    assert resp.doc_id == "DOC_PRIOR"
    # 已采纳 → 不再物化（无 OSS 写入、无 document_version INSERT）
    assert sink == []
    assert not any("INSERT INTO" in s and "document_version" in s for s, _ in conn.calls)


def test_accept_concurrent_claim_lost_returns_idempotent(monkeypatch):
    """条件认领 rowcount=0（他人抢先）→ 重读返回幂等，不二次物化。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"),
        claim_rowcount=0,  # 认领落空
        reread_row=("accepted", "registering", "DOC_WINNER")))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(),
                                      request=None, identity=_ident())
    assert resp.idempotent is True and resp.doc_id == "DOC_WINNER"
    assert sink == []  # 输家不物化


# ── 6) OSS 成功但 DB 失败 → failed → retry 恢复（不重复出文档）──────────
def test_accept_db_fail_then_retry_recovers(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    # document_version INSERT 第一次失败 → 物化 failed
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"),
        claim_rowcount=1, dv_exists=None, fail_version_insert=1))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(),
                                      request=None, identity=_ident())
    assert resp.ingestion_status == "failed" and resp.ok is False and resp.error
    # 取本次固定下来的 doc_id（从 accept 写库的 UPDATE 参数里拿）
    doc_id = None
    for s, p in conn.calls:
        if "UPDATE" in s and "review_status='accepted'" in s and p:
            doc_id = p[2]   # (reviewer, note, doc_id, upload_id, raw_key, ...)
    assert doc_id and doc_id.startswith("DOC_")

    # retry：用固定键续跑；这次 version INSERT 不再失败 → registered，doc_id 不变
    # retry SELECT 列序：review_status, ingestion_status, doc_id, raw_key, category_dept, question, content
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("accepted", "failed", doc_id,
                     f"raw/marketing/{doc_id}/UP1/contribution-C1.md", "marketing", "q", "a"),
        dv_exists=None, fail_version_insert=0))
    resp2 = api.kb_contribution_retry(cid="C1", request=None, identity=_ident())
    assert resp2.ingestion_status == "registered" and resp2.ok is True
    assert resp2.doc_id == doc_id  # 同一篇，绝不新建


def test_retry_searchable_idempotent(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("accepted", "searchable", "DOC_OK", "raw/marketing/DOC_OK/UP1/x.md",
                     "marketing", "q", "a")))
    from opensearch_pipeline import api
    resp = api.kb_contribution_retry(cid="C1", request=None, identity=_ident())
    assert resp.idempotent is True and resp.ingestion_status == "searchable"
    assert sink == []  # 已 searchable 不再物化


# ── 7) 驳回 ──────────────────────────────────────────────────────────────
def test_reject_pending(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, "marketing")))  # reject 读 4 列
    from opensearch_pipeline import api
    resp = api.kb_contribution_reject(cid="C1", req=api.KbContributionRejectRequest(note="重复"),
                                      request=None, identity=_ident())
    assert resp.review_status == "rejected" and resp.state == "rejected"
    assert conn.committed is True


def test_reject_cross_dept_forbidden(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _install_conn(monkeypatch, _FakeConn(contrib_row=("pending", "none", None, "finance")))
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_contribution_reject(cid="C1", req=api.KbContributionRejectRequest(),
                                   request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 403


def test_reject_accepted_conflict(monkeypatch):
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _install_conn(monkeypatch, _FakeConn(contrib_row=("accepted", "registered", "DOC_A", "marketing")))
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_contribution_reject(cid="C1", req=api.KbContributionRejectRequest(),
                                   request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 409


# ── 8) 缺口口径：脱敏 / 去重 / searchable 关闭 / pending 标记 ─────────────
def test_gaps_redaction_and_distinct_and_coverage(monkeypatch):
    _skip_if_not_sim()
    _employee(monkeypatch)
    from opensearch_pipeline import api, contribution as C
    q_phone = "我手机号13800138000能报销吗"
    q_dup = "如何申请密钥"
    # REFUSAL：同一归一化问题出现在两个不同 message_id → asks 应=2（去 message 扇出）
    refusal_rows = [
        (q_dup, "m1", 3, "marketing"),
        (q_dup + "！", "m2", 5, "marketing"),   # 归一化后同 hash
        (q_phone, "m3", 1, "marketing"),
    ]
    # NO_RESULT：同 message 重复行 → 去重后 asks 不重复计
    no_result_rows = [
        ("怎么开发票", "n1", 2, "marketing"),
        ("怎么开发票", "n1", 2, "marketing"),
    ]
    h_closed = C.question_hash(q_phone)        # 该缺口已 searchable → 关闭
    h_pending = C.question_hash(q_dup)         # 该缺口 accepted 未 searchable → 标等待入库
    coverage_rows = [
        (h_closed, "accepted", "searchable"),
        (h_pending, "accepted", "registered"),
    ]
    _install_conn(monkeypatch, _FakeConn(
        refusal_rows=refusal_rows, no_result_rows=no_result_rows, coverage_rows=coverage_rows,
        summary={"answered": 7, "this_month": 3, "contributors": 2}))
    resp = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    items = {it.question_hash: it for it in resp.items}
    # 已 searchable 覆盖的缺口被关闭（不在列表）
    assert h_closed not in items
    # q_dup 缺口：两不同 message → asks=2，且标 has_pending_contribution
    assert h_pending in items
    assert items[h_pending].asks == 2
    assert items[h_pending].has_pending_contribution is True
    # NO_RESULT 同 message 去重 → asks=1
    h_invoice = C.question_hash("怎么开发票")
    assert items[h_invoice].asks == 1
    # summary 透传
    assert resp.summary.answered == 7 and resp.summary.this_month == 3
    # 脱敏：手机号那条已关闭，但确保任何展示 question 不含明文手机号
    assert all("13800138000" not in it.question for it in resp.items)


def test_gaps_semantic_merge_flag_on(monkeypatch):
    """语义组归并（schema/040，RAG_QA_GAP_SEMANTIC 开）：相似问法并为一张卡——
    asks 求和/days 取 min/refusal 优先/phrasings=成员数；卡片 hash=组代表（真实成员，
    贡献关闭语义不变）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_GAP_SEMANTIC", "true")
    from opensearch_pipeline import api, contribution as C
    h1 = C.question_hash("怎么开发票")
    h2 = C.question_hash("发票怎么开")
    conn = _install_conn(monkeypatch, _FakeConn(
        no_result_rows=[("怎么开发票", "n1", 5, "marketing")],
        refusal_rows=[("发票怎么开", "m1", 2, "marketing")],
        gap_group_rows=[(h1, h1), (h2, h1)]))
    resp = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    assert len(resp.items) == 1
    it = resp.items[0]
    assert it.question_hash == h1                 # 组代表=真实成员 hash
    assert it.asks == 2 and it.last_days == 2     # 求和 / 取 min
    assert it.kind == "refusal"                   # refusal 优先
    assert it.phrasings == 2
    assert resp.summary.unanswered == 1           # 计数按归并后
    assert any("qa_gap_semantic_group" in s for s, _ in conn.calls)


def test_gaps_semantic_off_by_default(monkeypatch):
    """flag 缺省关：零行为变化——不发映射查询、不归并（phrasings 恒 1）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.delenv("RAG_QA_GAP_SEMANTIC", raising=False)
    from opensearch_pipeline import api, contribution as C
    h1 = C.question_hash("怎么开发票")
    h2 = C.question_hash("发票怎么开")
    conn = _install_conn(monkeypatch, _FakeConn(
        no_result_rows=[("怎么开发票", "n1", 5, "marketing"),
                        ("发票怎么开", "n2", 2, "marketing")],
        gap_group_rows=[(h1, h1), (h2, h1)]))   # 有映射也不该被查
    resp = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    assert len(resp.items) == 2
    assert all(it.phrasings == 1 for it in resp.items)
    assert not any("qa_gap_semantic_group" in s for s, _ in conn.calls)


def test_gaps_semantic_lookup_failure_fails_open(monkeypatch):
    """映射查询失败（表未 apply/040 缺失）→ 不归并原样展示，绝不 500（漏斗同款独立降级）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_GAP_SEMANTIC", "true")
    from opensearch_pipeline import api
    _install_conn(monkeypatch, _FakeConn(
        no_result_rows=[("怎么开发票", "n1", 5, "marketing"),
                        ("发票怎么开", "n2", 2, "marketing")],
        boom_gap_groups=True))
    resp = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    assert len(resp.items) == 2   # 未归并、未 500


def test_gap_dismiss_employee_403_and_bad_hash_400(monkeypatch):
    """「忽略此缺口」权限=console 管理员（2026-07-15 拍板交 dept_admin）：员工恒 403；
    非法 hash 400（不落库）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    from opensearch_pipeline import api
    _install_conn(monkeypatch, _FakeConn())
    with pytest.raises(Exception) as ei:
        api.kb_gap_dismiss(req=api.KbGapDismissRequest(question_hash="a" * 64),
                           request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 403
    _dept_admin(monkeypatch, managed="marketing")
    with pytest.raises(Exception) as ei:
        api.kb_gap_dismiss(req=api.KbGapDismissRequest(question_hash="不是hash"),
                           request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 400


def test_gap_dismiss_then_gaps_excluded(monkeypatch):
    """dept_admin 忽略：落台账（可撤销 upsert）+ kb_gaps 排除 active 行。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    monkeypatch.delenv("RAG_QA_GAP_SEMANTIC", raising=False)
    from opensearch_pipeline import api, contribution as C
    h1 = C.question_hash("怎么开发票")
    # 批次9 裁权：dept_admin 只能忽略可见集内的 hash——夹具给可见的 NO_RESULT 行
    conn = _install_conn(monkeypatch, _FakeConn(
        no_result_rows=[("怎么开发票", "n1", 2, "marketing")]))
    resp = api.kb_gap_dismiss(
        req=api.KbGapDismissRequest(question_hash=h1, question="怎么开发票", reason="闲聊噪音"),
        request=None, identity=_ident())
    assert resp.ok is True and resp.affected == 1
    ins = [(s, p) for s, p in conn.calls if "qa_gap_dismissal" in s and "INSERT" in s]
    assert len(ins) == 1
    s, p = ins[0]
    assert "ON DUPLICATE KEY UPDATE revoked_at=NULL" in s   # 重复忽略=复活同一行
    assert p[0] == h1 and p[3] == "u1"                      # hash + 操作人
    # 读侧排除：忽略台账返回该 hash → 不再出现在待回答
    _employee(monkeypatch)
    _install_conn(monkeypatch, _FakeConn(
        no_result_rows=[("怎么开发票", "n1", 2, "marketing"),
                        ("食堂几点开门", "n2", 1, "marketing")],
        gap_dismissal_rows=[(h1,)]))
    gaps = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    hashes = {it.question_hash for it in gaps.items}
    assert h1 not in hashes and len(gaps.items) == 1
    assert gaps.summary.unanswered == 1


def test_gap_dismiss_semantic_expansion(monkeypatch):
    """语义组开：忽略扩展到同组全部成员（否则归并卡摘头后换头复现=打地鼠）；
    restore 对称扩展。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    monkeypatch.setenv("RAG_QA_GAP_SEMANTIC", "true")
    from opensearch_pipeline import api
    h1, h2, h3 = "1" * 64, "2" * 64, "3" * 64
    # 批次9 裁权：合成 hash 不可见——走「已处 active 忽略态」的幂等豁免路径过闸
    conn = _install_conn(monkeypatch, _FakeConn(
        gap_group_head_row=(h1,), gap_group_member_rows=[(h1,), (h2,), (h3,)],
        gap_dismissal_rows=[(h1,)]))
    resp = api.kb_gap_dismiss(req=api.KbGapDismissRequest(question_hash=h1),
                              request=None, identity=_ident())
    assert resp.affected == 3
    ins = [s for s, _ in conn.calls if "qa_gap_dismissal" in s and "INSERT" in s]
    assert len(ins) == 3
    conn2 = _install_conn(monkeypatch, _FakeConn(
        gap_group_head_row=(h1,), gap_group_member_rows=[(h1,), (h2,), (h3,)]))
    api.kb_gap_restore(req=api.KbGapDismissRequest(question_hash=h1),
                       request=None, identity=_ident())
    upd = [(s, p) for s, p in conn2.calls if "qa_gap_dismissal" in s and "revoked_at=NOW()" in s]
    assert len(upd) == 1
    assert set(upd[0][1][1:]) == {h1, h2, h3}   # IN 列表覆盖全组


def test_gaps_dismissed_list_admin_only_and_rows(monkeypatch):
    """「已忽略」折叠区数据源：console 管理员可列 active 行（员工 403）；
    041 未 apply → 诚实空列表不 500。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    from opensearch_pipeline import api
    _install_conn(monkeypatch, _FakeConn())
    with pytest.raises(Exception) as ei:
        api.kb_gaps_dismissed(request=None, identity=_ident())
    assert getattr(ei.value, "status_code", None) == 403
    _dept_admin(monkeypatch, managed="marketing")
    import datetime
    at = datetime.datetime(2026, 7, 15, 10, 0, 0)
    _install_conn(monkeypatch, _FakeConn(
        gap_dismissal_rows=[("a" * 64, "怎么开发票", "闲聊噪音", "生产部管理员", at)]))
    resp = api.kb_gaps_dismissed(request=None, identity=_ident())
    assert len(resp.items) == 1
    it = resp.items[0]
    assert it.question_hash == "a" * 64 and it.question_preview == "怎么开发票"
    assert it.reason == "闲聊噪音" and it.dismissed_by_name == "生产部管理员"
    assert it.dismissed_at.startswith("2026-07-15")
    # 041 未 apply / 查询失败 → 空列表（fail-open）
    _install_conn(monkeypatch, _FakeConn(boom_dismissal=True))
    resp2 = api.kb_gaps_dismissed(request=None, identity=_ident())
    assert resp2.items == []


def test_gaps_dismissal_lookup_fails_open(monkeypatch):
    """041 未 apply/查询失败 → 不排除原样展示（宁噪音回来不丢真缺口），绝不 500。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    from opensearch_pipeline import api
    _install_conn(monkeypatch, _FakeConn(
        no_result_rows=[("怎么开发票", "n1", 2, "marketing")], boom_dismissal=True))
    resp = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    assert len(resp.items) == 1


def test_gaps_requires_login_401(monkeypatch):
    _skip_if_not_sim()
    from opensearch_pipeline import api
    with pytest.raises(Exception) as ei:
        api.kb_gaps(request=None, limit=20, offset=0, identity=None)
    assert getattr(ei.value, "status_code", None) == 401


# ── 9) reconcile 跨库 JOIN 必须 collation-cast（staging _stg 实测 1267 静默吞 → 永不 searchable）──
def test_reconcile_join_collation_safe(monkeypatch):
    """staging 端到端逮到的真 bug 回归：kb_contribution(unicode_ci) ⋈ document_version(_0900) 不
    cast 会 1267 被 try/except 吞掉 → reconcile 永不 flip searchable。锁死 COLLATE cast 在 SQL 里。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn(list_rows=[]))
    from opensearch_pipeline import api
    api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    recon = [s for s, _ in conn.calls if "kb_contribution c" in s and "document_version dv" in s]
    assert recon, "reconcile UPDATE 未发出"
    assert all("COLLATE utf8mb4_unicode_ci" in s for s in recon), "reconcile 跨库 doc_id JOIN 必须 collation-cast"


def test_reconcile_flips_kb_admin_rejected_public_to_failed(monkeypatch):
    """P2-16 闭环：PENDING_APPROVAL 的贡献文档被 kb_admin 在既有审批入口驳回
    （content_process_status='REJECTED'）→ reconcile 把 registered 翻 failed，
    而非永远停在「等待入库」（REJECTED 的版本 index_status 永不写入）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn(list_rows=[]))
    from opensearch_pipeline import api
    api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    recon = [s for s, _ in conn.calls if "kb_contribution c" in s and "document_version dv" in s]
    rejected = [s for s in recon if "content_process_status='REJECTED'" in s]
    assert rejected, "缺少 kb_admin 驳回 → 贡献翻 failed 的 reconcile 分支"
    assert all("ingestion_status='failed'" in s and "ingestion_status='registered'" in s
               for s in rejected)


def test_reconcile_flips_cs_failed_exhausted_to_failed(monkeypatch):
    """ε-5 审计 P0 根治（reconcile cs=FAILED 谓词对齐）：内容阶段 FAILED 且自动重试用尽
    → registered 翻显式 failed。守卫=orchestrator stage-2 认领谓词（retry_count<3 会被
    自动续跑）的镜像补集：>=3 或 NULL 才判死——无守卫会把明天能自愈的行提前判死
    （提前翻 failed 后 DAG 自愈也不会再翻 searchable）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn(list_rows=[]))
    from opensearch_pipeline import api
    api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    recon = [s for s, _ in conn.calls if "kb_contribution c" in s and "document_version dv" in s]
    failed = [s for s in recon if "content_process_status='FAILED'" in s]
    assert failed, "缺少 cs=FAILED 死链 → 贡献翻 failed 的 reconcile 分支"
    for s in failed:
        assert "ingestion_status='failed'" in s and "ingestion_status='registered'" in s
        assert "dv.retry_count >= 3" in s and "dv.retry_count IS NULL" in s, \
            "cs=FAILED 谓词必须带 retry_count 用尽守卫（<3 的行 DAG 还会自动续跑，不是死链）"


def test_retry_requeues_cs_failed_doc(monkeypatch):
    """ε-5 审计 P0 出路侧：对已登记文档（物化幂等早退、绝不碰 document_version）点「重试」
    必须把内容阶段 FAILED 的死链行重新排队（NOT_STARTED + retry_count=0，谓词锁死 FAILED）
    ——否则重试只是 registered↔failed 空转，管理员按钮形同虚设。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("accepted", "failed", "DOC_F", "raw/marketing/DOC_F/UP1/contribution-C1.md",
                     "marketing", "q", "a"),
        dv_exists=("DOC_F", 1)))   # raw_key 已登记 → 物化幂等早退
    from opensearch_pipeline import api
    resp = api.kb_contribution_retry(cid="C1", request=None, identity=_ident())
    assert resp.ingestion_status == "registered" and resp.ok is True
    req = [(s, p) for s, p in conn.calls
           if "document_version" in s and "content_process_status='NOT_STARTED'" in s]
    assert req, "retry 未重新排队 FAILED 文档"
    s, p = req[0]
    assert "retry_count=0" in s
    assert "content_process_status='FAILED'" in s, "排队谓词必须锁死 FAILED（其余状态零触碰）"
    assert p == ("DOC_F",)


def test_retry_requeue_failure_is_nonfatal(monkeypatch):
    """重新排队是 best-effort：失败不拖垮重试主流程（贡献停在 registered，下次 reconcile
    诚实翻回 failed——自洽，无假终态，故无需回滚）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("accepted", "failed", "DOC_F", "raw/marketing/DOC_F/UP1/contribution-C1.md",
                     "marketing", "q", "a"),
        dv_exists=("DOC_F", 1), fail_requeue=True))
    from opensearch_pipeline import api
    resp = api.kb_contribution_retry(cid="C1", request=None, identity=_ident())
    assert resp.ingestion_status == "registered" and resp.ok is True


# ── P2-17) 图片占位符注入防护（<<IMG:N>> 视觉引用伪造）──────────────────────
def test_strip_img_markers_covers_renderer_pattern_superset():
    """剥离面 ⊇ content_blocks_builder 的标准 + lenient 两个解析 regex：
    单/双尖括号、全角【】《》、全/半角冒号、空白、N.M 子下标；正常文本不受伤。"""
    from opensearch_pipeline import contribution as C
    for evil in ("<<IMG:1>>", "<IMG:2>", "<<IMG:3.2>>", "【IMG:4】", "《IMG:5》",
                 "<<IMG：6>>", "<< IMG : 7 >>", "<<img:8>>"):
        assert "IMG" not in C.strip_img_markers(f"点开{evil}按钮").upper(), evil
        assert C.strip_img_markers(f"点开{evil}按钮") == "点开按钮", evil
    # 正常内容原样保留（含 <b> 这类非标记尖括号与 IMG 单词本身）
    keep = "在 U8 里点 <确定>，IMG 格式说明见附件（image:3 不是标记）"
    assert C.strip_img_markers(keep) == keep
    assert C.strip_img_markers(None) == "" and C.strip_img_markers("") == ""


def test_synthesize_markdown_strips_literal_img_markers():
    """合成 .md 是入库前最终关口：存量行（修复前已采纳）经 retry 续跑也不得带字面标记。"""
    from opensearch_pipeline import contribution as C
    md = C.synthesize_markdown("怎么开发票<<IMG:1>>？", "按【IMG:2】所示，点开票<IMG:3>入口。")
    assert "IMG" not in md.upper()
    assert md.startswith("问：怎么开发票？")
    assert "答：按所示，点开票入口。" in md


def test_submit_strips_img_markers_before_persist(monkeypatch):
    """submit 侧剥离：入表的 question/content 与 question_hash 均基于剥离后的文本。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn())
    from opensearch_pipeline import api
    from opensearch_pipeline import contribution as C
    resp = api.kb_contribution_submit(
        req=api.KbContributionSubmitRequest(
            question="如何开发票<<IMG:1>>?", content="点开票入口<IMG:2>。", category_dept="marketing"),
        request=None, identity=_ident())
    assert "<<IMG" not in resp.question and "<IMG" not in resp.content
    ins = [p for s, p in conn.calls if "INSERT INTO" in s and "kb_contribution" in s][0]
    assert all("IMG:" not in str(v) for v in ins)
    # hash 基于剥离后的问题（与缺口去重口径一致）
    assert C.question_hash("如何开发票?") in ins


def test_accept_strips_img_markers_even_if_added_after_submit(monkeypatch):
    """accept 侧剥离（防「先提交干净、采纳前改回」）：修订文本带标记 → 合成 .md 干净。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"),
        claim_rowcount=1, dv_exists=None))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(
        cid="C1",
        req=api.KbContributionAcceptRequest(question="怎么改密码<<IMG:9>>？",
                                            content="见《IMG:2》与<<IMG:1.2>>步骤。"),
        request=None, identity=_ident())
    assert resp.ok is True
    assert sink and "IMG" not in sink[0][1].upper()


# ── 批次δ-3：贡献审批权归属 dept_admin（kb_admin 队列=仅孤儿部门兜底）───────────────
def test_pending_scope_dept_admin_unchanged(monkeypatch):
    """dept_admin 的审核队列作用域不变：本部门 category_dept ∈ managed。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    conn = _install_conn(monkeypatch, _FakeConn())
    from opensearch_pipeline import api
    api.kb_contributions_pending(request=None, identity=_ident())
    sql = next(s for s, _ in conn.calls if "review_status='pending'" in s)
    assert "category_dept IN" in sql
    assert "dept_admin_grant" not in sql               # 孤儿子查询只属于 kb_admin 分支


def test_pending_scope_kb_admin_orphan_only(monkeypatch):
    """kb_admin 的审核队列=仅孤儿部门（无任何 active dept_admin 管辖的 category_dept）——
    业务采纳/驳回的日常动线移交 dept_admin（业务标准在部门）；accept/reject 权限面不动
    （救急兜底通道保留，test_accept_public_by_kb_admin_stays_direct 锁定），与 2026-07-04
    access-request「kb_admin 只管入库、后端留救急通道」拍板同构。"""
    _skip_if_not_sim()
    _kb_admin(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn())
    from opensearch_pipeline import api
    api.kb_contributions_pending(request=None, identity=_ident())
    sql = next(s for s, _ in conn.calls if "review_status='pending'" in s)
    assert "NOT IN" in sql
    assert "dept_admin_grant" in sql and "is_active=1" in sql
    assert "managed_owner_dept" in sql


# ── 批次ε-1：缺口溯源字段透出（source_message_id/gap_query 写侧一直在存，读侧此前不透出）──
def test_contrib_list_exposes_gap_provenance(monkeypatch):
    """mine/pending 行带缺口溯源两列：SELECT 列面含 source_message_id/gap_query，
    响应项透出（前端审核队列据此显「来自缺口」徽标）；空值归一为 None。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    rows = [
        ("CONTRIB_1", "如何开发票", "按流程", "finance", "u1", "张三",
         "pending", "none", None, None, None, None, "msg_9", "开发票的抬头怎么填", None),
        ("CONTRIB_2", "自发贡献", "内容", "finance", "u1", "张三",
         "pending", "none", None, None, None, None, None, "", None),
    ]
    conn = _install_conn(monkeypatch, _FakeConn(list_rows=rows))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    sql = next(s for s, _ in conn.calls if "SELECT" in s and "author_id=" in s)
    assert "source_message_id" in sql and "gap_query" in sql
    assert resp.items[0].source_message_id == "msg_9"
    assert resp.items[0].gap_query == "开发票的抬头怎么填"
    assert resp.items[1].source_message_id is None      # 空值归一 None（前端 v-if 判空自隐）
    assert resp.items[1].gap_query is None


# ── 批次ε-2 Round1：审核结果通知提交人（accept/reject/retry 三挂点，commit 之后）─────────
def _spy_result_notify(monkeypatch, conn=None, boom=False):
    """替换 notify_contribution_result；记录 (cid, outcome, note, error, actor, committed_at_call)。"""
    calls = []

    def _fake(cid, outcome, note="", error="", actor_id=""):
        if boom:
            raise RuntimeError("notify down")
        calls.append((cid, outcome, note, error, actor_id,
                      bool(getattr(conn, "committed", False)) if conn is not None else None))
    monkeypatch.setattr("opensearch_pipeline.admin_notify.notify_contribution_result", _fake)
    return calls


def test_accept_fires_author_result_notify_after_commit(monkeypatch):
    """采纳直通 → outcome=accepted、actor=审核人；调用时 conn 已 commit（挂点在事务外）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"),
        claim_rowcount=1, dv_exists=None))
    calls = _spy_result_notify(monkeypatch, conn=conn)
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(),
                                      request=None, identity=_ident())
    assert resp.ok is True
    assert len(calls) == 1
    cid, outcome, _n, _e, actor, committed = calls[0]
    assert (cid, outcome, actor) == ("C1", "accepted", "u1")
    assert committed is True, "通知必须发生在业务 commit 之后"


def test_accept_public_pending_approval_notifies_author_distinctly(monkeypatch):
    """P2-16 待放行 ≠ 直通：outcome=pending_approval（两种「采纳成功」文案必须可区分）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"),
        claim_rowcount=1, dv_exists=None))
    calls = _spy_result_notify(monkeypatch, conn=conn)
    from opensearch_pipeline import api
    api.kb_contribution_accept(cid="C1",
                               req=api.KbContributionAcceptRequest(permission_level="public"),
                               request=None, identity=_ident())
    assert [c[1] for c in calls] == ["pending_approval"]


def test_reject_fires_author_result_notify_with_note(monkeypatch):
    """驳回 → outcome=rejected + 理由透传（空理由的兜底句在 admin_notify 侧验证）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    conn = _install_conn(monkeypatch, _FakeConn(contrib_row=("pending", "none", None, "marketing")))
    calls = _spy_result_notify(monkeypatch, conn=conn)
    from opensearch_pipeline import api
    resp = api.kb_contribution_reject(cid="C2", req=api.KbContributionRejectRequest(note="与现行制度冲突"),
                                      request=None, identity=_ident())
    assert resp.review_status == "rejected"
    assert calls == [("C2", "rejected", "与现行制度冲突", "", "u1", True)]


def test_reject_idempotent_does_not_renotify(monkeypatch):
    """已驳回的幂等重驳 → 无状态变化，不重复通知（防重放骚扰作者）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    conn = _install_conn(monkeypatch, _FakeConn(contrib_row=("rejected", "none", None, "marketing")))
    calls = _spy_result_notify(monkeypatch, conn=conn)
    from opensearch_pipeline import api
    resp = api.kb_contribution_reject(cid="C2", req=api.KbContributionRejectRequest(),
                                      request=None, identity=_ident())
    assert resp.idempotent is True
    assert calls == []


def test_notify_boom_never_breaks_review_flow(monkeypatch):
    """真实现 no-raise 端到端：flag 开 + 模块内部（作者查询）炸 → accept 主流程照常返回。
    挂点裸调不包 try/except 是有意的——best-effort 兜底在 admin_notify 模块内部，此测试
    走【真】notify_contribution_result 验证该兜底而非 mock 自证。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")

    def _boom(_cid):
        raise RuntimeError("db down")
    monkeypatch.setattr("opensearch_pipeline.admin_notify._contrib_author_question", _boom)
    _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"),
        claim_rowcount=1, dv_exists=None))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(),
                                      request=None, identity=_ident())
    assert resp.ok is True   # 通知内部炸光，审核结果不受影响


def test_retry_fires_author_result_notify(monkeypatch):
    """重试成功 → outcome=accepted（从 failed 恢复也要告知作者）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _capture_put(monkeypatch, ok=True)
    doc_id = "DOC_R"
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("accepted", "failed", doc_id,
                     f"raw/marketing/{doc_id}/UP1/contribution-C3.md", "marketing", "q", "a"),
        dv_exists=None))
    calls = _spy_result_notify(monkeypatch, conn=conn)
    from opensearch_pipeline import api
    resp = api.kb_contribution_retry(cid="C3", request=None, identity=_ident())
    assert resp.ok is True
    assert len(calls) == 1 and calls[0][1] in ("accepted", "pending_approval")


# ── 批次ε-2 R2：被引用数（cited=True 口径、全期窗口、事实表路径、诚实 None）──────────
def test_heroes_hits_cited_alltime_fact_path(monkeypatch):
    """榜上作者回填 hits：cited=1 口径 + COUNT(DISTINCT message_id) + 无时间窗（全期）+
    同库直连事实表（不经 document_meta 零 collation cast）；无引用行的作者=真 0；
    排名仍按入库篇数（hits 不改序）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "true")
    conn = _install_conn(monkeypatch, _FakeConn(
        hero_rows=[("u1", "李娜", 3), ("u2", "王伟", 2)],
        hero_hits_rows=[("u1", 6)]))
    from opensearch_pipeline import api
    resp = api.kb_contribution_heroes(request=None, identity=_ident())
    assert [(i.author_id, i.count, i.hits) for i in resp.items] == [("u1", 3, 6), ("u2", 2, 0)]
    sql = next(s for s, _ in conn.calls if "GROUP BY c.author_id" in s)
    assert "jt.cited=1" in sql and "COUNT(DISTINCT jt.message_id)" in sql
    assert "qa_retrieved_doc" in sql and "document_meta" not in sql   # 同库直连，零 cast
    assert "created_at" not in sql                                    # 全期窗口（同榜同口径）


def test_heroes_hits_none_when_fact_disabled(monkeypatch):
    """事实表路径关（flag off / 未迁移环境）→ hits=None（诚实「算不出」，绝不 0 顶替），
    且不发任何 JSON_TABLE 兜底查询（有意不做：无界表全解析不值得为激励信号冒险）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "false")
    conn = _install_conn(monkeypatch, _FakeConn(hero_rows=[("u1", "李娜", 3)]))
    from opensearch_pipeline import api
    resp = api.kb_contribution_heroes(request=None, identity=_ident())
    assert resp.items[0].hits is None
    assert not any("JSON_TABLE" in s or "qa_retrieved_doc" in s for s, _ in conn.calls)


def test_mine_hits_backfill_only_searchable(monkeypatch):
    """mine：仅已入库(searchable 且有 doc_id)行回填 hits；命中缺席=真 0；pending 行保持 None。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "true")
    rows = [
        ("C_A", "q1", "a1", "finance", "u1", "张三", "accepted", "searchable",
         "DOC_1", None, None, None, None, None, None),
        ("C_B", "q2", "a2", "finance", "u1", "张三", "accepted", "searchable",
         "DOC_2", None, None, None, None, None, None),
        ("C_C", "q3", "a3", "finance", "u1", "张三", "pending", "none",
         None, None, None, None, None, None, None),
    ]
    _install_conn(monkeypatch, _FakeConn(list_rows=rows, doc_hits_rows=[("DOC_1", 4)]))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    assert [(i.contribution_id, i.hits) for i in resp.items] == [("C_A", 4), ("C_B", 0), ("C_C", None)]


def test_mine_hits_failure_honest_none_list_intact(monkeypatch):
    """引用数聚合炸 → 全部 hits=None、主列表照常返回（激励信号绝不拖垮主列表）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "true")
    rows = [("C_A", "q1", "a1", "finance", "u1", "张三", "accepted", "searchable",
             "DOC_1", None, None, None, None, None, None)]
    _install_conn(monkeypatch, _FakeConn(list_rows=rows, boom_hits=True))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    assert len(resp.items) == 1 and resp.items[0].hits is None


# ── 批次ε-3 R1：registering 行管线徽章派生（待放行/死链/正常排队 三分可辨）─────────────
def _reg_row(cid, doc_id):
    return (cid, f"q-{cid}", "a", "finance", "u1", "张三", "accepted", "registered",
            doc_id, None, None, None, None, None, None)


def test_contrib_badges_never_silently_all_none(monkeypatch):
    """★ 防吞守卫（codex 2026-08-06 点名）：`_contrib_doc_badges` 的调用**整个**裹在
    「徽章派生绝不拖垮主列表」的 `except → return None` 里 —— 任何签名/列数不匹配都不会报错，
    只会让**整片 doc_badge 变成 None**，看起来像"这批就是算不出"。

    本条正是那个失效形态的钉子：正常输入下 doc_badge 必须**全部非空**。
    （实证：2026-08-06 给 helper 加 version_status 列时，桩少喂一列即触发本形态。）
    """
    rows = [_reg_row("C1", "D1")]
    _install_conn(monkeypatch, _FakeConn(list_rows=rows, dv_badge_rows=[
        ("D1", "DONE", "SUCCESS", None, None, "active", None, "active"),
    ]))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    got = [i.doc_badge for i in resp.items]
    assert all(got), f"徽章派生被 fail-open 吞成 None：{got}"
    assert got == ["已上线"]


def test_contrib_v1_superseded_shows_history_badge(monkeypatch):
    """本函数固定读 v1；文档升版后 v1 会变 superseded ⇒「历史版本」在此**可达**，
    不是理论分支（codex 明确指出这一点）。"""
    rows = [_reg_row("C1", "D1")]
    _install_conn(monkeypatch, _FakeConn(list_rows=rows, dv_badge_rows=[
        ("D1", "DONE", "SUCCESS", None, None, "active", None, "superseded"),
    ]))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    assert [i.doc_badge for i in resp.items] == ["历史版本"]


def test_mine_doc_badge_three_way_distinguishable(monkeypatch):
    """同为 registering：PENDING_APPROVAL→待审核（卡 kb_admin 放行）、QUARANTINED→已隔离、
    EMPTY/SKIPPED→未入索引（死链）、NOT_STARTED→排队中（正常）——四者徽章互异（复用
    _kb_status_badge 台账词表，勿重造窄谓词；此前全部折叠成同一个「已采纳·待入库」）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    rows = [_reg_row("C1", "D1"), _reg_row("C2", "D2"), _reg_row("C3", "D3"),
            _reg_row("C4", "D4"), _reg_row("C5", "D5")]
    # 第 7 列 = dv.gate_status（渲染侧 gate 轴，2026-08-04 独立核验 B2）；D5 = gate-only 隔离
    # 第 8 列 = dv.status（版本级，2026-08-06）——本函数固定读 v1，升版后 v1 会变 superseded。
    # ⚠️ 少喂这一列时表现是**整片 doc_badge=None**（IndexError 被「徽章派生绝不拖垮主列表」
    # 的 except 吞掉），不是报错——下面 test_contrib_badges_never_silently_all_none 即其守卫。
    conn = _install_conn(monkeypatch, _FakeConn(list_rows=rows, dv_badge_rows=[
        ("D1", "PENDING_APPROVAL", "NOT_INDEXED", None, None, "active", None, "active"),
        ("D2", "DONE", "SUCCESS", "QUARANTINED", None, "active", None, "active"),
        ("D3", "DONE", "NOT_INDEXED", "SKIPPED_EXPLOSION", "EMPTY", "active", None, "active"),
        ("D4", "NOT_STARTED", "NOT_INDEXED", None, None, "active", None, "active"),
        ("D5", "DONE", "SUCCESS", None, None, "active", "quarantined", "active"),
    ]))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    assert [i.doc_badge for i in resp.items] == ["待审核", "已隔离", "未入索引", "排队中", "已隔离"]
    sql = next(s for s, _ in conn.calls if "document_version dv" in s and "document_meta dm" in s)
    assert "IN (" in sql and "dv.version_no=1" in sql
    assert "COLLATE" not in sql   # IN 绑定参数，非跨库列对列 JOIN，无 1267 面


def test_mine_doc_badge_missing_row_none_no_500(monkeypatch):
    """P2-16 时序竞态：registering 行还没有 document_version 行 → doc_badge=None 不 500。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    _install_conn(monkeypatch, _FakeConn(list_rows=[_reg_row("C1", "D_GONE")], dv_badge_rows=[]))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    assert resp.items[0].doc_badge is None


def test_mine_doc_badge_failure_honest_none_list_intact(monkeypatch):
    """徽章派生炸 → doc_badge=None、主列表照常（派生信号绝不拖垮主列表）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    _install_conn(monkeypatch, _FakeConn(list_rows=[_reg_row("C1", "D1")], boom_badges=True))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    assert len(resp.items) == 1 and resp.items[0].doc_badge is None


def test_mine_doc_badge_only_queried_for_registering(monkeypatch):
    """searchable/pending/rejected 行不触发徽章查询（零成本原则）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    rows = [
        ("C_S", "q", "a", "finance", "u1", "张三", "accepted", "searchable",
         "D_S", None, None, None, None, None, None),
        ("C_P", "q2", "a2", "finance", "u1", "张三", "pending", "none",
         None, None, None, None, None, None, None),
    ]
    conn = _install_conn(monkeypatch, _FakeConn(list_rows=rows))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    assert all(i.doc_badge is None for i in resp.items)
    assert not any("document_version dv" in s and "document_meta dm" in s for s, _ in conn.calls)


# ── 批次ε-3 R2：审核队列被问次数（asks 口径归并——不再依赖 gaps 的 acl 口径）─────────────
def _pending_row(cid):
    return (cid, f"q-{cid}", "a", "marketing", "u9", "王伟", "pending", "none",
            None, None, None, None, None, None, None)


def test_pending_asks_two_sources_merged_dedup(monkeypatch):
    """asks=近 30 天 NO_RESULT+REFUSAL 两源按 COALESCE(gap_query_hash, question_hash) 归并的
    去重 message 数；同 message 重复出现只计一次；无匹配=真 0；SQL 面锁 COALESCE（gap 优先，
    审核人修订 question 后 hash 变了仍指向原始提问）与 30 天/400 cap 参数（asks 专属口径——
    ε-4 起与 kb_gaps 的 365 天缺口窗解耦）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    from opensearch_pipeline import contribution as C
    h_gap = C.question_hash("生产环境密钥在哪申请")
    conn = _install_conn(monkeypatch, _FakeConn(
        list_rows=[_pending_row("C_A"), _pending_row("C_B")],
        hash_rows=[("C_A", h_gap), ("C_B", C.question_hash("完全没人问过的问题"))],
        no_result_rows=[("生产环境密钥在哪申请", "m1"), ("生产环境密钥在哪申请？", "m2"),
                        ("生产环境密钥在哪申请", "m2")],       # m2 重复 → 去重
        refusal_plain_rows=[("生产环境密钥在哪申请！", "m3"), ("别的问题", "m9")]))
    from opensearch_pipeline import api
    resp = api.kb_contributions_pending(request=None, identity=_ident())
    assert [(i.contribution_id, i.asks) for i in resp.items] == [("C_A", 3), ("C_B", 0)]
    sql = next(s for s, _ in conn.calls if "COALESCE(gap_query_hash, question_hash)" in s)
    assert "kb_contribution" in sql
    qa_calls = [(s, p) for s, p in conn.calls if "qa_session_log" in s and "INTERVAL" in s]
    assert len(qa_calls) == 2                                   # NO_RESULT + REFUSAL 两源
    assert all(p == (30, 400) for _, p in qa_calls)             # 同窗同 cap
    assert all("hit_mine" not in s for s, _ in qa_calls)        # REFUSAL 平查，不拖 dept JOIN


def test_pending_asks_failure_honest_none_queue_intact(monkeypatch):
    """asks 聚合炸 → 全部 None、审核队列照常返回（热度信号绝不拖垮队列）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    _install_conn(monkeypatch, _FakeConn(list_rows=[_pending_row("C_A")], boom_asks=True))
    from opensearch_pipeline import api
    resp = api.kb_contributions_pending(request=None, identity=_ident())
    assert len(resp.items) == 1 and resp.items[0].asks is None


def test_mine_has_no_asks_aggregation(monkeypatch):
    """asks 只属于审核队列：mine 端点不触发 hash/qa 聚合（作者侧无此信号，零成本）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn(list_rows=[_pending_row("C_A")]))
    from opensearch_pipeline import api
    resp = api.kb_contributions_mine(request=None, limit=20, offset=0, identity=_ident())
    assert resp.items[0].asks is None
    assert not any("COALESCE(gap_query_hash" in s for s, _ in conn.calls)


# ── 批次ε-3 R3：summary 审核漏斗 + 缺口窗口下发（统计卡口径修正的后端半）─────────────
def test_gaps_summary_funnel_and_window_days(monkeypatch):
    """近 30 天（按 reviewed_at）采纳率与平均审核时长（分钟→小时）；window_days 下发
    =_CONTRIB_WINDOW_DAYS（前端卡头/空态标注据此渲染，防文案与后端漂移）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    conn = _install_conn(monkeypatch, _FakeConn(funnel_row=(8, 2, 1584.0)))
    from opensearch_pipeline import api
    resp = api.kb_gaps(request=None, identity=_ident())
    assert resp.window_days == 365
    assert resp.summary.review_accept_rate_30d == 0.8
    assert resp.summary.review_avg_hours_30d == 26.4
    sql = next(s for s, _ in conn.calls if "TIMESTAMPDIFF" in s)
    assert "reviewed_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)" in sql   # 按审核时刻窗


def test_gaps_summary_funnel_no_samples_none(monkeypatch):
    """近 30 天无任何审核样本（分母 0）→ 两字段 None 自隐，绝不显 0%/0 小时误导。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    _install_conn(monkeypatch, _FakeConn(funnel_row=(0, 0, None)))
    from opensearch_pipeline import api
    resp = api.kb_gaps(request=None, identity=_ident())
    assert resp.summary.review_accept_rate_30d is None
    assert resp.summary.review_avg_hours_30d is None


def test_gaps_summary_funnel_failure_not_counted_to_500(monkeypatch):
    """漏斗聚合炸 → 字段 None、端点照常 200（辅助信号不计入 fails>=4 的 500 阈值）。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch)
    _install_conn(monkeypatch, _FakeConn(boom_funnel=True))
    from opensearch_pipeline import api
    resp = api.kb_gaps(request=None, identity=_ident())
    assert resp.summary.review_accept_rate_30d is None and resp.summary.review_avg_hours_30d is None
    assert resp.window_days == 365


def test_gaps_funnel_admin_only_and_never_cached(monkeypatch):
    """2026-07-15 拍板：漏斗字段 API 层只给管理员。员工响应恒 None 且不触发聚合查询；
    共享缓存（按 depts 键，同部门管理员/员工同条目）绝不携带漏斗——按请求身份补注：
    员工先填缓存 → 管理员命中同一缓存仍拿到漏斗；管理员之后员工再读 → 仍 None。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_KB_GAPS_CACHE_TTL", "60")
    conn = _install_conn(monkeypatch, _FakeConn(funnel_row=(8, 2, 1584.0)))
    from opensearch_pipeline import api
    # ① 员工请求（填缓存）：无漏斗、无聚合查询
    _employee(monkeypatch)
    r1 = api.kb_gaps(request=None, identity=_ident())
    assert r1.summary.review_accept_rate_30d is None
    assert not any("TIMESTAMPDIFF" in s for s, _ in conn.calls)
    # ② 管理员命中同一缓存条目（同 depts 键）→ 漏斗按请求身份补注
    _dept_admin(monkeypatch, managed="marketing")
    r2 = api.kb_gaps(request=None, identity=_ident())
    assert r2.summary.review_accept_rate_30d == 0.8
    assert r2.summary.review_avg_hours_30d == 26.4
    # ③ 员工再读（缓存已被管理员访问过）→ 仍 None（缓存对象未被污染）
    _employee(monkeypatch)
    r3 = api.kb_gaps(request=None, identity=_ident())
    assert r3.summary.review_accept_rate_30d is None and r3.summary.review_avg_hours_30d is None


# ── 批次ε-4：缺口窗 365 天与 asks 30 天口径解耦 + Top 30 排行（后端半）─────────────
def test_gap_window_constants_decoupled():
    """常量解耦锚点：缺口窗=365/2000、asks=30/400。若有人图省事原地改 _CONTRIB_*，
    test_pending_asks_two_sources_merged_dedup 的 (30,400) 断言会红——那是真实功能回归
    （asks 近期热度被拉成 365 天），不是测试该改。"""
    from opensearch_pipeline.routes import contribution as ctr
    assert (ctr._GAP_WINDOW_DAYS, ctr._GAP_CANDIDATE_CAP) == (365, 2000)
    assert (ctr._CONTRIB_WINDOW_DAYS, ctr._CONTRIB_CANDIDATE_CAP) == (30, 400)


def test_gaps_window_days_correct_on_cache_hit(monkeypatch):
    """缓存命中路径 window_days 仍=365（响应字段取自常量默认值而非缓存对象——审计点名的
    「查询改了、响应还报旧窗」失真面在缓存两路都要锁死）。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_KB_GAPS_CACHE_TTL", "60")
    _employee(monkeypatch)
    _install_conn(monkeypatch, _FakeConn())
    from opensearch_pipeline import api
    r1 = api.kb_gaps(request=None, identity=_ident())
    r2 = api.kb_gaps(request=None, identity=_ident())   # 第二次=命中缓存
    assert r1.window_days == r2.window_days == 365


def test_gaps_coverage_in_placeholders_match_params(monkeypatch):
    """覆盖子查询（question_hash IN）拼装自洽：占位符数=实参数=去重 hash 数
    （窗口放大后 IN 上界从 ≤800 涨到 ≤4000，拼装错位会静默失真）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    rows = [(f"问题变体{i}号怎么办", f"m{i}", 1, "marketing") for i in range(60)]
    conn = _install_conn(monkeypatch, _FakeConn(no_result_rows=rows))
    from opensearch_pipeline import api
    api.kb_gaps(request=None, identity=_ident())
    cov = [(s, p) for s, p in conn.calls if "kb_contribution WHERE question_hash IN" in s]
    assert len(cov) == 1
    sql, params = cov[0]
    assert sql.count("%s") == len(params) == 60


def test_gap_dismiss_scope_denied_outside_visibility(monkeypatch):
    """批次9（ultra P3 contribution:1422）：dept_admin 不能忽略可见范围外的 hash——
    忽略台账是全局排除，此前任何 dept_admin 可离线算 hash 全局压掉别部门缺口。"""
    import pytest
    from fastapi import HTTPException
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")
    monkeypatch.delenv("RAG_QA_GAP_SEMANTIC", raising=False)
    from opensearch_pipeline import api
    _install_conn(monkeypatch, _FakeConn())    # 可见集为空 + 无既往忽略
    with pytest.raises(HTTPException) as ei:
        api.kb_gap_dismiss(req=api.KbGapDismissRequest(question_hash="a" * 64),
                           request=None, identity=_ident())
    assert ei.value.status_code == 403


def test_gap_dismiss_kb_admin_unrestricted(monkeypatch):
    """kb_admin 不受可见范围限制（全局治理权保留）。"""
    _skip_if_not_sim()
    _kb_admin(monkeypatch)
    monkeypatch.delenv("RAG_QA_GAP_SEMANTIC", raising=False)
    from opensearch_pipeline import api
    conn = _install_conn(monkeypatch, _FakeConn())
    resp = api.kb_gap_dismiss(req=api.KbGapDismissRequest(question_hash="b" * 64),
                              request=None, identity=_ident())
    assert resp.ok is True
    assert any("qa_gap_dismissal" in s and "INSERT" in s for s, _ in conn.calls)


# ── 10) 无效问题分层过滤 + 缺口上下文（2026-07-18）──────────────────────────
def test_junk_and_incomplete_pure_functions():
    """分层判定纯函数边界：junk 只认确定性垃圾；incomplete 只认短缺主体；
    「怎么请假/如何报销/库存多少」两者都不命中（绝不误杀有效短问题）。"""
    from opensearch_pipeline.contribution import is_incomplete_question, is_junk_question
    # 确定性垃圾
    for q in ("", "   ", "？？？", "。。。", "!!!", "😀😀", "5", "12345", "假"):
        assert is_junk_question(q), q
        assert not is_incomplete_question(q), q   # 两类互斥：junk 不再算 incomplete
    # 短缺主体（追问形态）
    for q in ("那第二步呢", "然后呢", "上面那个", "还有吗", "这个呢", "继续", "第3步呢"):
        assert not is_junk_question(q), q
        assert is_incomplete_question(q), q
    # 有效短问题：两者都不命中
    for q in ("怎么请假", "如何报销", "库存多少", "访客WiFi密码", "请假流程", "怎么开发票"):
        assert not is_junk_question(q), q
        assert not is_incomplete_question(q), q
    # 长句即便含指代词也不算 incomplete（内容自足）
    assert not is_incomplete_question("那台注塑机的换模流程第二步的审批人是谁")


def test_gaps_junk_filtered_and_flag_off_restores(monkeypatch):
    """确定性垃圾（纯标点/单字/纯数字）默认不进缺口；RAG_QA_GAP_JUNK_FILTER=false 逃生阀恢复。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    from opensearch_pipeline import api, contribution as C
    rows = [("？？？", "j1", 1, "marketing"),
            ("7", "j2", 1, "marketing"),
            ("怎么开发票", "n1", 2, "marketing")]
    _install_conn(monkeypatch, _FakeConn(no_result_rows=rows))
    resp = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    assert [it.question_hash for it in resp.items] == [C.question_hash("怎么开发票")]
    # 逃生阀：关掉过滤 → 垃圾行回来（现状行为）
    monkeypatch.setenv("RAG_QA_GAP_JUNK_FILTER", "false")
    from opensearch_pipeline.routes import contribution as RC
    RC._gaps_cache_clear()
    _install_conn(monkeypatch, _FakeConn(no_result_rows=rows))
    resp2 = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    assert len(resp2.items) == 3


def test_gaps_incomplete_layering(monkeypatch):
    """分层第 2/3 层：短缺主体+有会话上文 → 保留（has_context=True）；
    短缺主体+无上文 → 低质量隐藏；RAG_QA_GAP_HIDE_INCOMPLETE=false 恢复。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    from opensearch_pipeline import api, contribution as C
    # 6 列行：(query_text, message_id, days, dept, session_id, id)
    rows = [("然后呢", "m1", 1, "marketing", "S1", 30),        # S1 min_id=10 → 有上文
            ("上面那个", "m2", 1, "marketing", "S2", 5),       # S2 min_id=5 → 自己是首行=无上文
            ("怎么开发票", "m3", 2, "marketing", "S3", 8)]     # 完整问题不受分层影响
    conn_kw = dict(no_result_rows=rows, session_min_rows=[("S1", 10), ("S2", 5), ("S3", 8)])
    _install_conn(monkeypatch, _FakeConn(**conn_kw))
    resp = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    by_hash = {it.question_hash: it for it in resp.items}
    assert C.question_hash("然后呢") in by_hash            # 有上文：保留
    assert by_hash[C.question_hash("然后呢")].has_context is True
    assert C.question_hash("上面那个") not in by_hash      # 无上文：低质量隐藏
    assert C.question_hash("怎么开发票") in by_hash        # 有效问题不受影响
    # has_context 收窄（2026-07-18）：独立完整问题不给上下文按钮（前几轮多为无关主题）
    assert by_hash[C.question_hash("怎么开发票")].has_context is False
    # 独立 flag 关闭 → 无上文的短缺主体问题也回来
    monkeypatch.setenv("RAG_QA_GAP_HIDE_INCOMPLETE", "false")
    from opensearch_pipeline.routes import contribution as RC
    RC._gaps_cache_clear()
    _install_conn(monkeypatch, _FakeConn(**conn_kw))
    resp2 = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    assert C.question_hash("上面那个") in {it.question_hash for it in resp2.items}


def test_gaps_incomplete_session_probe_fails_open(monkeypatch):
    """会话批查失败 → fail-open：不隐藏任何缺口（宁噪音不丢真缺口），has_context=False。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    from opensearch_pipeline import api, contribution as C
    rows = [("然后呢", "m1", 1, "marketing", "S1", 30)]
    _install_conn(monkeypatch, _FakeConn(no_result_rows=rows, boom_session_min=True))
    resp = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    assert [it.question_hash for it in resp.items] == [C.question_hash("然后呢")]
    assert resp.items[0].has_context is False


def test_gaps_rewritten_query_display_and_hash(monkeypatch):
    """追问改写行（第 7 列 rewritten_query 非空）：展示与归并都按改写后的独立问题。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    from opensearch_pipeline import api, contribution as C
    rows = [("那第二步呢", "m1", 1, "marketing", "S1", 30, "U8 采购入库的第二步怎么操作"),
            ("U8 采购入库的第二步怎么操作", "m2", 3, "marketing", "S2", 7, None)]
    _install_conn(monkeypatch, _FakeConn(no_result_rows=rows))
    resp = api.kb_gaps(request=None, limit=20, offset=0, identity=_ident(groups=("marketing",)))
    assert len(resp.items) == 1                       # 改写后与独立问法同 hash → 归并
    it = resp.items[0]
    assert it.question_hash == C.question_hash("U8 采购入库的第二步怎么操作")
    assert "第二步怎么操作" in it.question and it.asks == 2


def test_gap_context_endpoint_visibility_and_cursor(monkeypatch):
    """上下文端点：不存在/非缺口行 404；REFUSAL 不可见 403；可见时前序行按
    (created_at,id) 游标返回、时间正序、SUCCESS 轮才带节选。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    from fastapi import HTTPException
    from opensearch_pipeline import api
    # 404：无此 message
    _install_conn(monkeypatch, _FakeConn(ctx_msg_row=None))
    with pytest.raises(HTTPException) as ei:
        api.kb_gap_context(request=None, message_id="MX", identity=_ident(groups=("marketing",)))
    assert ei.value.status_code == 404
    # 404：存在但非缺口行（SUCCESS）
    _install_conn(monkeypatch, _FakeConn(
        ctx_msg_row=(30, "S1", "2026-07-18 10:00:00", "SUCCESS", "marketing", "M1")))
    with pytest.raises(HTTPException) as ei:
        api.kb_gap_context(request=None, message_id="M1", identity=_ident(groups=("marketing",)))
    assert ei.value.status_code == 404
    # 403：REFUSAL 且命中文档不在我部门、非全公开
    _install_conn(monkeypatch, _FakeConn(
        ctx_msg_row=(30, "S1", "2026-07-18 10:00:00", "REFUSAL", "finance", "M1"),
        ctx_vis_row=(0, 0)))
    with pytest.raises(HTTPException) as ei:
        api.kb_gap_context(request=None, message_id="M1", identity=_ident(groups=("marketing",)))
    assert ei.value.status_code == 403
    # 200：NO_RESULT 本部门可见；前序行（DESC 给桩）→ 响应时间正序；SUCCESS 才带节选；
    # 噪音轮（「5」「hi」垃圾/寒暄）被过滤不展示（2026-07-18）；超长问题截 120 字
    conn = _install_conn(monkeypatch, _FakeConn(
        ctx_msg_row=(30, "S1", "2026-07-18 10:00:00", "NO_RESULT", "marketing", "M1"),
        ctx_prior_rows=[
            ("5", "REFUSAL", None, "2026-07-18 09:40:00", 25),          # 垃圾轮：滤
            ("hi", "SUCCESS", "您好！我是助手", "2026-07-18 09:35:00", 22),  # 寒暄轮：滤
            ("第二个问题", "NO_RESULT", None, "2026-07-18 09:30:00", 20),
            ("长" * 300, "SUCCESS", "答案正文", "2026-07-18 09:00:00", 10),
        ]))
    resp = api.kb_gap_context(request=None, message_id="M1", identity=_ident(groups=("marketing",)))
    assert len(resp.items) == 2                                          # 噪音轮不出现
    assert all("hi" != t.question and "5" != t.question for t in resp.items)
    assert len(resp.items[0].question) == 120                            # 超长问题截断
    assert resp.items[0].answer_excerpt == "答案正文"                     # SUCCESS 带节选
    assert resp.items[1].answer_excerpt == "" and resp.items[1].answer_status == "NO_RESULT"
    # (created_at,id) 复合游标进了 SQL（同时间戳不漏不乱）
    prior_sql = next(s for s, _ in conn.calls if "ORDER BY created_at DESC, id DESC" in s)
    assert "created_at < %s OR (created_at = %s AND id < %s)" in prior_sql


# ── 总积分榜（2026-08-01，Sam 拍板预览权重 10/1/3）────────────────────────────
def test_heroes_total_score_composition_and_ranking(monkeypatch):
    """score = 10×采纳 + 1×引用 + 3×有效反馈；排名按总分。
    u1: 3 采纳+6 引用+0 反馈=36；u2: 2 采纳+0 引用+8 反馈=44 → u2 反超。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "true")
    _install_conn(monkeypatch, _FakeConn(
        hero_rows=[("u1", "李娜", 3), ("u2", "王伟", 2)],
        hero_hits_rows=[("u1", 6)],
        hero_feedback_rows=[("u2", 8)]))
    from opensearch_pipeline import api
    resp = api.kb_contribution_heroes(request=None, identity=_ident())
    got = [(i.author_id, i.score, i.adopted, i.hits, i.feedback) for i in resp.items]
    assert got == [("u2", 44, 2, 0, 8), ("u1", 36, 3, 6, 0)]
    assert [i.rank for i in resp.items] == [1, 2]
    assert resp.items[0].count == 2      # 旧字段 count=adopted 兼容保留


def test_heroes_feedback_only_user_enters_board(monkeypatch):
    """只反馈没贡献的人也上榜（反馈是办法认可的参与项）；展示名从 user_role 回填。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "false")
    _install_conn(monkeypatch, _FakeConn(
        hero_rows=[("u1", "李娜", 1)],
        hero_feedback_rows=[("fb9", 5)],
        user_role_rows=[("fb9", "赵反馈")]))
    from opensearch_pipeline import api
    resp = api.kb_contribution_heroes(request=None, identity=_ident())
    got = {i.author_id: (i.score, i.author_name) for i in resp.items}
    assert got["fb9"] == (15, "赵反馈") and got["u1"] == (10, "李娜")
    assert resp.items[0].author_id == "fb9"                      # 15 > 10


def test_heroes_feedback_sql_invariants(monkeypatch):
    """有效反馈 SQL 必须带齐三道闸：RESOLVED（④人工确认）、首报去重（⑤GROUP_CONCAT
    按 created_at,id 最早）、渠道①（conversation_type IS NULL）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "false")
    conn = _install_conn(monkeypatch, _FakeConn(hero_rows=[("u1", "李娜", 1)]))
    from opensearch_pipeline import api
    api.kb_contribution_heroes(request=None, identity=_ident())
    sql = next(s for s, _ in conn.calls if "handled_status='RESOLVED'" in s)
    assert "feedback_type='downvote'" in sql
    assert "GROUP_CONCAT(f.user_id ORDER BY f.created_at ASC, f.id ASC)" in sql
    assert "conversation_type IS NULL" in sql and "GROUP BY f.message_id" in sql


def test_heroes_feedback_failure_degrades_not_breaks(monkeypatch):
    """反馈聚合失败 → 该项按 0，榜单照出（单项失败绝不拖垮）。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "false")
    _install_conn(monkeypatch, _FakeConn(
        hero_rows=[("u1", "李娜", 2)], boom_feedback=True))
    from opensearch_pipeline import api
    resp = api.kb_contribution_heroes(request=None, identity=_ident())
    assert [(i.author_id, i.score, i.feedback) for i in resp.items] == [("u1", 20, 0)]


def test_heroes_fact_off_score_counts_hits_as_zero_honest_none(monkeypatch):
    """fact 路径关：hits=None（诚实算不出）、分数按 0 如实少算——不伪造引用分。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "false")
    _install_conn(monkeypatch, _FakeConn(hero_rows=[("u1", "李娜", 3)]))
    from opensearch_pipeline import api
    resp = api.kb_contribution_heroes(request=None, identity=_ident())
    assert resp.items[0].hits is None and resp.items[0].score == 30


# ── 三榜切换（2026-08-01）：部门榜 = 同分按一级部门求和；本部门榜 = 归属过滤 ────
_DEPT_TREE = [
    (100, 1, "生产中心", 1), (110, 100, "注塑事业部", 2), (111, 110, "注塑制程检", 3),
    (200, 1, "综合管理中心", 1), (210, 200, "人力资源部", 2),
]


def test_heroes_dept_board_sums_by_l1(monkeypatch):
    """u1(注塑制程检,深3)与 u2(注塑事业部,深2)都上溯到生产中心求和；u3 归综合管理中心。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "false")
    _install_conn(monkeypatch, _FakeConn(
        hero_rows=[("u1", "李娜", 3), ("u2", "王伟", 2), ("u3", "张三", 1)],
        staff_pool_rows=[("u1", 111), ("u2", 110), ("u3", 210)],
        dept_dim_rows=_DEPT_TREE))
    from opensearch_pipeline import api
    resp = api.kb_contribution_heroes(request=None, identity=_ident())
    got = [(d.dept_name, d.score, d.members) for d in resp.dept_items]
    assert got == [("生产中心", 50, 2), ("综合管理中心", 10, 1)]
    assert [d.rank for d in resp.dept_items] == [1, 2]


def test_heroes_my_dept_board_filters_by_requester_l1(monkeypatch):
    """请求者挂注塑事业部 → 本部门榜只含生产中心成员（u3 不在）；部门内重排名次。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "false")
    _install_conn(monkeypatch, _FakeConn(
        hero_rows=[("u1", "李娜", 3), ("u2", "王伟", 2), ("u3", "张三", 5)],
        staff_pool_rows=[("u1", 111), ("u2", 110), ("u3", 210)],
        dept_dim_rows=_DEPT_TREE,
        staff_me_row=(110,)))
    from opensearch_pipeline import api
    resp = api.kb_contribution_heroes(request=None, identity=_ident())
    assert resp.my_dept_name == "生产中心"
    assert [(i.author_id, i.rank, i.score) for i in resp.my_dept_items] == [
        ("u1", 1, 30), ("u2", 2, 20)]
    # 全公司榜不受影响（u3 50 分居首）
    assert resp.items[0].author_id == "u3"


def test_heroes_unattributed_user_skips_dept_board_keeps_company(monkeypatch):
    """未在册（不在 staff_dim）的人：全公司/本部门个人榜照常，只是不计入部门榜。"""
    _skip_if_not_sim()
    _employee(monkeypatch)
    monkeypatch.setenv("RAG_QA_FACT_JOIN", "false")
    _install_conn(monkeypatch, _FakeConn(
        hero_rows=[("u1", "李娜", 3), ("ghost", "外部号", 9)],
        staff_pool_rows=[("u1", 111)],
        dept_dim_rows=_DEPT_TREE))
    from opensearch_pipeline import api
    resp = api.kb_contribution_heroes(request=None, identity=_ident())
    assert resp.items[0].author_id == "ghost"                       # 全公司榜照常
    assert [(d.dept_name, d.score) for d in resp.dept_items] == [("生产中心", 30)]
    assert resp.my_dept_items == [] and resp.my_dept_name == ""     # 请求者未在册 → 诚实空
