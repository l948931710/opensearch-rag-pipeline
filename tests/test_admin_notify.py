# -*- coding: utf-8 -*-
"""test_admin_notify.py — 管理员钉钉工作通知（RAG_ADMIN_NOTIFY，默认关）。

铁律验证：flag 关=零副作用；开=收件人与授权体系同源（dept_admin_grant / user_role）、
asyncsend_v2 载荷正确；任何失败（HTTP 异常/无收件人/缺 agent_id）绝不外抛。
全程 sync（_SEND_ASYNC=False）+ monkeypatch _http_post / db._get_db_conn。
"""
import pytest

import opensearch_pipeline.admin_notify as an


@pytest.fixture(autouse=True)
def _sync_send(monkeypatch):
    monkeypatch.setattr(an, "_SEND_ASYNC", False)
    monkeypatch.setattr("opensearch_pipeline.dingtalk_card._get_access_token", lambda: "tok")
    monkeypatch.setenv("RAG_DINGTALK_AGENT_ID", "1234567")


class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        self._last = sql

    def fetchall(self):
        if "dept_admin_grant" in self._last:
            return self.conn.dept_admins
        if "user_role" in self._last:
            return self.conn.kb_admins
        if "qa_session_log" in self._last:
            return self.conn.cited_depts
        return []

    def fetchone(self):
        if "document_meta" in self._last:
            return self.conn.doc_row
        return None


class _Conn:
    def __init__(self, dept_admins=(), kb_admins=(), doc_row=None, cited_depts=()):
        self.dept_admins = list(dept_admins)
        self.kb_admins = list(kb_admins)
        self.doc_row = doc_row
        self.cited_depts = list(cited_depts)
        self.calls = []

    def cursor(self):
        return _Cur(self)

    def close(self):
        pass


def _wire(monkeypatch, conn):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda *a, **k: conn)


def _spy_http(monkeypatch, result=None):
    sent = []
    def _fake(url, payload):
        sent.append((url, payload))
        return result if result is not None else {"errcode": 0}
    monkeypatch.setattr(an, "_http_post", _fake)
    return sent


def test_flag_off_is_total_noop(monkeypatch):
    """默认关：不查库、不发 HTTP（挂点可无脑常驻在业务代码里）。"""
    monkeypatch.delenv("RAG_ADMIN_NOTIFY", raising=False)
    sent = _spy_http(monkeypatch)
    conn = _Conn(dept_admins=[("u1",)])
    _wire(monkeypatch, conn)
    an.notify_access_request("marketing", "D1", "hr")
    an.notify_contribution("marketing", "q?")
    an.notify_upload_approval("marketing", "t")
    an.notify_escalation("M1", "q?")
    assert sent == [] and conn.calls == []


def test_access_request_notifies_dept_admins(monkeypatch):
    """开 flag：收件人=dept_admin_grant(owner_dept)；文案含标题/申请部门；载荷=asyncsend_v2。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _Conn(dept_admins=[("mgr1",), ("mgr2",), ("mgr1",)],   # 重复→去重
                             doc_row=("营销物料规范", "g.pdf")))
    an.notify_access_request("marketing", "D1", "hr,rd")
    assert len(sent) == 1
    url, payload = sent[0]
    assert "corpconversation/asyncsend_v2" in url and "access_token=tok" in url
    assert payload["agent_id"] == "1234567"
    assert payload["userid_list"] == "mgr1,mgr2"
    text = payload["msg"]["text"]["content"]
    assert "营销物料规范" in text and "跨部门检索申请" in text


def test_contribution_and_upload_channels(monkeypatch):
    """贡献→部门管理员（问题截断 40 字）；上传审批→kb_admin 名单。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _Conn(dept_admins=[("mgr1",)], kb_admins=[("adm1",), ("adm2",)]))
    an.notify_contribution("production", "问" * 60)
    an.notify_upload_approval("hr", "员工手册")
    assert len(sent) == 2
    contrib_text = sent[0][1]["msg"]["text"]["content"]
    assert "知识贡献" in contrib_text and ("问" * 40 + "…") in contrib_text
    assert sent[1][1]["userid_list"] == "adm1,adm2"
    assert "待审批" in sent[1][1]["msg"]["text"]["content"] and "员工手册" in sent[1][1]["msg"]["text"]["content"]


def test_no_recipients_or_no_agent_skips_http(monkeypatch):
    """收件人为空 / 缺 agent_id：静默跳过，不发 HTTP。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _Conn(dept_admins=[]))
    an.notify_contribution("marketing", "q?")
    assert sent == []
    monkeypatch.setenv("RAG_DINGTALK_AGENT_ID", "")
    _wire(monkeypatch, _Conn(dept_admins=[("mgr1",)]))
    an.notify_contribution("marketing", "q?")
    assert sent == []


def test_failures_never_raise(monkeypatch):
    """DB 炸 / HTTP 炸 / errcode 非 0：一律吞掉（挂点在 commit 之后，绝不能反噬主流程）。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")

    def _db_boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", _db_boom)
    an.notify_access_request("marketing", "D1", "hr")      # 不抛即过

    _wire(monkeypatch, _Conn(dept_admins=[("mgr1",)]))
    def _http_boom(url, payload):
        raise RuntimeError("dingtalk down")
    monkeypatch.setattr(an, "_http_post", _http_boom)
    an.notify_contribution("marketing", "q?")              # 不抛即过

    _spy_http(monkeypatch, result={"errcode": 88, "errmsg": "limited"})
    an.notify_upload_approval("hr", "t")                   # errcode≠0 仅告警


def test_submit_endpoint_fires_hook(monkeypatch):
    """端点挂点：授权申请提交成功后调用 notify_access_request（flag 语义由模块自管）。"""
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "production")
    monkeypatch.setenv("RAG_SIM_USER_DEPT", "生产部")
    calls = []
    monkeypatch.setattr("opensearch_pipeline.admin_notify.notify_access_request",
                        lambda **kw: calls.append(kw))

    class _SCur(_Cur):
        lastrowid = 7
        def fetchone(self):
            if "document_meta" in self._last:
                return ("marketing", "dept_internal", "active")   # 他部门 dept_internal 文档
            return None
    class _SConn(_Conn):
        lastrowid = 7
        def cursor(self):
            c = _SCur(self); return c
        def commit(self):
            pass
    _wire(monkeypatch, _SConn())
    from opensearch_pipeline import api
    resp = api.kb_access_request_submit(
        api.KbAccessRequestSubmit(doc_id="D9", reason="需要引用"),
        request=None, identity=api.Identity(user_id="da1"))
    assert resp.status == "pending"
    assert len(calls) == 1 and calls[0]["owner_dept"] == "marketing" and calls[0]["doc_id"] == "D9"


# ── 转人工工单通知（盲区审计 P1-2：工单不再只写不读）────────────────────────────

def test_escalation_notifies_cited_dept_admins(monkeypatch):
    """收件人=被引用文档 owner_dept 的 dept_admin（与控制台工单队列可见性同源）；
    文案含提问节选（40 字截断）。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _Conn(dept_admins=[("mgr1",), ("mgr2",)],
                             cited_depts=[("production",)]))
    an.notify_escalation("M1", "注" * 60)
    assert len(sent) == 1
    assert sent[0][1]["userid_list"] == "mgr1,mgr2"
    text = sent[0][1]["msg"]["text"]["content"]
    assert "转人工工单" in text and ("注" * 40 + "…") in text


def test_escalation_no_cited_docs_falls_back_to_kb_admin(monkeypatch):
    """无引用文档（NO_RESULT 转人工 = 语料缺口）→ kb_admin 兜底，工单不落进虚空。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _Conn(kb_admins=[("adm1",)], cited_depts=[]))
    an.notify_escalation("M2", "出口欧盟要哪些认证？")
    assert len(sent) == 1 and sent[0][1]["userid_list"] == "adm1"


def test_create_escalation_fires_notify_hook(monkeypatch):
    """feedback_handler._create_escalation：INSERT+commit 成功后调用 notify_escalation
    （best-effort：通知炸了不影响返回 True）。"""
    calls = []
    monkeypatch.setattr("opensearch_pipeline.admin_notify.notify_escalation",
                        lambda mid, q: calls.append((mid, q)))

    class _ECur(_Cur):
        def fetchone(self):
            if "qa_session_log" in self._last:
                return ("S1", "怎么冲销入库单？", "AI 的回答", "财务部")
            return None
    class _EConn(_Conn):
        def cursor(self):
            return _ECur(self)
        def commit(self):
            self.committed = True
        def rollback(self):
            pass
    conn = _EConn()
    _wire(monkeypatch, conn)
    from opensearch_pipeline.feedback_handler import _create_escalation
    assert _create_escalation(message_id="M9", user_id="u1", user_name="张三") is True
    assert calls == [("M9", "怎么冲销入库单？")]
    assert getattr(conn, "committed", False)

    # 通知炸了 → 主流程不受影响（工单已 commit，仍返回 True）
    def _boom(mid, q):
        raise RuntimeError("notify down")
    monkeypatch.setattr("opensearch_pipeline.admin_notify.notify_escalation", _boom)
    conn2 = _EConn()
    _wire(monkeypatch, conn2)
    assert _create_escalation(message_id="M10", user_id="u1", user_name=None) is True


def test_contribution_orphan_dept_falls_back_to_kb_admin(monkeypatch):
    """批次δ-3：孤儿部门（无 dept_admin）的新贡献 → kb_admin 兜底通知（含兜底提示语），
    不再静默落进虚空——与 notify_escalation 兜底同款；兜底队列归 kb_admin，必须有人被叫到。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _Conn(dept_admins=[], kb_admins=[("kbadm1",)]))
    an.notify_contribution("legal", "海外仓合同模板在哪？")
    assert len(sent) == 1 and sent[0][1]["userid_list"] == "kbadm1"
    text = sent[0][1]["msg"]["text"]["content"]
    assert "兜底" in text and "知识贡献" in text


def test_contribution_orphan_and_no_kb_admin_still_silent(monkeypatch):
    """孤儿部门且连 kb_admin 都查不到（种子缺失/DB 空）→ 仍静默跳过不炸（收件人空的原语义保留）。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _Conn(dept_admins=[], kb_admins=[]))
    an.notify_contribution("legal", "q?")
    assert sent == []


# ── 批次ε-2 Round1：审核结果 → 通知提交人（notify_contribution_result）──────────────
def _author_conn(author=("emp7", "如何申请生产环境密钥？")):
    class _RCur(_Cur):
        def fetchone(self):
            if "kb_contribution" in self._last:
                return author
            return super().fetchone()
    class _RConn(_Conn):
        def cursor(self):
            return _RCur(self)
    return _RConn()


def test_contribution_result_flag_off_noop(monkeypatch):
    monkeypatch.delenv("RAG_ADMIN_NOTIFY", raising=False)
    sent = _spy_http(monkeypatch)
    conn = _author_conn()
    _wire(monkeypatch, conn)
    an.notify_contribution_result("C1", "accepted")
    assert sent == [] and conn.calls == []


def test_contribution_result_recipient_is_author_four_wordings_distinct(monkeypatch):
    """收件人=提交人本人（非管理员名单）；四种 outcome 文案互斥可区分；
    驳回空理由/失败空原因均有兜底句（绝不拼空串）。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _author_conn())
    an.notify_contribution_result("C1", "accepted")
    an.notify_contribution_result("C1", "pending_approval")
    an.notify_contribution_result("C1", "failed", error="")            # 空原因 → 兜底
    an.notify_contribution_result("C1", "rejected", note="")           # 空理由 → 兜底
    an.notify_contribution_result("C1", "rejected", note="与制度冲突")
    assert len(sent) == 5
    assert all(p["userid_list"] == "emp7" for _, p in sent)            # 全部发给作者本人
    texts = [p["msg"]["text"]["content"] for _, p in sent]
    assert "正在入库" in texts[0] and "放行" not in texts[0]
    assert "需知识库管理员放行" in texts[1]
    assert "入库失败" in texts[2] and "系统原因" in texts[2]
    assert "未被采纳" in texts[3] and "未填写理由" in texts[3] and "重新提交" in texts[3]
    assert "与制度冲突" in texts[4]
    assert all("如何申请生产环境密钥" in t for t in texts)


def test_contribution_result_self_action_skipped(monkeypatch):
    """审核人=作者（自采/自驳/作者自己点重试）→ 跳过，自己的操作不通知自己。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _author_conn())
    an.notify_contribution_result("C1", "accepted", actor_id="emp7")
    assert sent == []


def test_contribution_result_unknown_outcome_or_missing_row_silent(monkeypatch):
    """未知 outcome / 贡献行不存在（author 查空）→ 静默不发、不炸。"""
    monkeypatch.setenv("RAG_ADMIN_NOTIFY", "1")
    sent = _spy_http(monkeypatch)
    _wire(monkeypatch, _author_conn())
    an.notify_contribution_result("C1", "whatever")
    _wire(monkeypatch, _Conn())          # fetchone 走不到 kb_contribution 分支 → None
    an.notify_contribution_result("C_MISSING", "accepted")
    assert sent == []
