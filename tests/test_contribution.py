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
        return None

    def fetchall(self):
        s, c = self._last, self.conn
        if "answer_status='NO_RESULT'" in s:
            return c.no_result_rows
        if "t.hit_mine=1 OR t.all_public=1" in s:
            return c.refusal_rows
        if "kb_contribution WHERE question_hash IN" in s:
            return c.coverage_rows
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
        self.no_result_rows = kw.get("no_result_rows", [])
        self.refusal_rows = kw.get("refusal_rows", [])
        self.coverage_rows = kw.get("coverage_rows", [])
        self.hero_rows = kw.get("hero_rows", [])
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
