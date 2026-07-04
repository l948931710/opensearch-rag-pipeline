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
        return []

    def fetchone(self):
        if "document_meta" in self._last:
            return self.conn.doc_row
        return None


class _Conn:
    def __init__(self, dept_admins=(), kb_admins=(), doc_row=None):
        self.dept_admins = list(dept_admins)
        self.kb_admins = list(kb_admins)
        self.doc_row = doc_row
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
