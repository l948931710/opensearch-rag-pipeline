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
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.setattr(pn, "_get_db_conn", lambda *a, **k: conn)
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


def test_accept_public_choice_flat_path_direct(monkeypatch):
    """部门领导选「全员公开」→ 直接放行（不转 kb_admin）；raw_key 扁平（无 /internal/），meta 写 public。"""
    _skip_if_not_sim()
    _dept_admin(monkeypatch, managed="marketing")   # dept_admin 直接定 public（用户裁决 A）
    sink = []
    _capture_put(monkeypatch, ok=True, sink=sink)
    conn = _install_conn(monkeypatch, _FakeConn(
        contrib_row=("pending", "none", None, None, None, "q", "a", "marketing"), claim_rowcount=1, dv_exists=None))
    from opensearch_pipeline import api
    resp = api.kb_contribution_accept(cid="C1", req=api.KbContributionAcceptRequest(permission_level="public"),
                                      request=None, identity=_ident())
    assert resp.ok is True and resp.ingestion_status == "registered"   # 直接入库，无 PENDING_APPROVAL
    assert sink and "/internal/" not in sink[0][0]
    meta = [p for s, p in conn.calls if "INSERT INTO" in s and "document_meta" in s][0]
    assert "public" in meta


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
