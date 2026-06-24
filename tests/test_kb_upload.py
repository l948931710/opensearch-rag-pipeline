# -*- coding: utf-8 -*-
"""
test_kb_upload.py — Phase 1 上传后端：纯 helper + 签名 upload token + 端点授权先行。
"""
import pytest

from opensearch_pipeline import kb_upload as ku


# ── ULID / doc_id ────────────────────────────────────────────────
def test_ulid_and_doc_id():
    a, b = ku.new_ulid(), ku.new_ulid()
    assert len(a) == 26 and a != b
    assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in a)
    doc = ku.new_doc_id()
    assert doc.startswith("DOC_") and len(doc) == 30


# ── 文件名校验 ────────────────────────────────────────────────────
def test_validate_filename():
    assert ku.validate_upload_filename("a.pdf") == (True, "pdf", "ok")
    assert ku.validate_upload_filename("报告.DOCX")[0] is True
    assert ku.validate_upload_filename("x.doc") == (False, "doc", "legacy_format")
    assert ku.validate_upload_filename("x.xls")[2] == "legacy_format"
    assert ku.validate_upload_filename("x.zip")[2] == "unsupported_format"
    assert ku.validate_upload_filename("noext")[2] == "no_extension"


def test_safe_filename_strips_traversal():
    assert ku.safe_filename("../../etc/passwd") == "passwd"
    assert ku.safe_filename("a\\b\\c.pdf") == "c.pdf"
    assert ku.safe_filename("dir/报告 v2.pdf") == "报告 v2.pdf"


# ── raw_key 与 _dept_from_raw_key 契约（owner_dept 必须是第 2 段）─────
def test_raw_key_owner_is_second_segment():
    from opensearch_pipeline.pipeline_nodes import _dept_from_raw_key
    key = ku.build_raw_key("marketing", "DOC_ABC", ku.new_ulid(), "报告.pdf")
    assert key.startswith("raw/marketing/DOC_ABC/")
    assert key.endswith("/报告.pdf")
    assert _dept_from_raw_key(key) == "marketing"   # 管线据此解析 owner_dept


# ── 签名 upload token：往返 / 篡改 / 过期 ──────────────────────────
def test_upload_token_roundtrip(monkeypatch):
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "k" * 40)
    tok = ku.sign_upload_token({"uid": "u1", "doc_id": "DOC_X", "owner_dept": "finance",
                                "raw_key": "raw/finance/DOC_X/u/abc.pdf"})
    p = ku.verify_upload_token(tok)
    assert p and p["uid"] == "u1" and p["owner_dept"] == "finance" and p["typ"] == "kb_upload"

    # 篡改 payload → 验签失败
    head, sig = tok.split(".", 1)
    bad = ("A" + head[1:]) + "." + sig if head[0] != "A" else ("B" + head[1:]) + "." + sig
    assert ku.verify_upload_token(bad) is None

    # 过期
    expired = ku.sign_upload_token({"uid": "u1"}, ttl=-10)
    assert ku.verify_upload_token(expired) is None

    # 非 kb_upload 类型的通用 token 不被 verify_upload_token 接受
    from opensearch_pipeline.auth_token import sign_payload
    other = sign_payload({"uid": "u1", "typ": "something_else"}, ttl=300)
    assert ku.verify_upload_token(other) is None


# ── 端点：授权先行（不依赖 OSS/DB）──────────────────────────────────
def _sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


def test_upload_url_employee_forbidden(monkeypatch):
    _sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "k" * 40)
    from opensearch_pipeline import api
    req = api.KbUploadUrlRequest(action="new", filename="a.pdf", owner_dept="finance")
    with pytest.raises(Exception) as ei:
        api.kb_upload_url(req=req, request=None, identity=api.Identity(user_id="emp1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_upload_url_legacy_format_rejected(monkeypatch):
    _sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "k" * 40)
    from opensearch_pipeline import api
    req = api.KbUploadUrlRequest(action="new", filename="old.doc", owner_dept="finance")
    with pytest.raises(Exception) as ei:
        api.kb_upload_url(req=req, request=None, identity=api.Identity(user_id="dev1"))
    assert getattr(ei.value, "status_code", None) == 400


def test_upload_url_kb_admin_new_ok(monkeypatch):
    _sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "k" * 40)
    from opensearch_pipeline import api
    req = api.KbUploadUrlRequest(action="new", filename="报告.pdf", owner_dept="production",
                                 permission_level="dept_internal")
    resp = api.kb_upload_url(req=req, request=None, identity=api.Identity(user_id="dev1"))
    assert resp.doc_id.startswith("DOC_")
    assert resp.raw_key.startswith("raw/production/")
    assert not resp.requires_kb_admin_approval     # kb_admin dept_internal 直接发布
    # token 解析回来，owner/raw_key 与响应一致（客户端不可改）
    p = ku.verify_upload_token(resp.upload_token)
    assert p["owner_dept"] == "production" and p["raw_key"] == resp.raw_key


def test_upload_url_dept_admin_public_needs_approval(monkeypatch):
    _sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "finance")
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "k" * 40)
    from opensearch_pipeline import api
    # 公开 → 需 kb_admin 审批
    req = api.KbUploadUrlRequest(action="new", filename="a.pdf", owner_dept="finance",
                                 permission_level="public")
    resp = api.kb_upload_url(req=req, request=None, identity=api.Identity(user_id="da1"))
    assert resp.requires_kb_admin_approval is True
    # 越权 owner（不在 managed）→ 403
    bad = api.KbUploadUrlRequest(action="new", filename="a.pdf", owner_dept="production")
    with pytest.raises(Exception) as ei:
        api.kb_upload_url(req=bad, request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


# ── 升版（action=version）：可见范围不可被客户端篡改 + 越权部门拒绝 ───────────────
class _FakeCur:
    """桩游标：with conn.cursor() as cur → execute/fetchone 返回固定 (owner, perm)。"""
    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _FakeCur(self._row)

    def close(self):
        pass


def _stub_doc(monkeypatch, owner, perm):
    """让 upload-url 的 version 分支读到 (owner, perm) 作为「原文档」元数据。"""
    import opensearch_pipeline.pipeline_nodes as pn
    monkeypatch.setattr(pn, "_get_db_conn", lambda: _FakeConn((owner, perm)))


def test_upload_url_version_forces_original_permission(monkeypatch):
    """升版强制继承原文档 permission_level —— 客户端伪造 public 必须被忽略（防偷偷转公开）。"""
    _sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "k" * 40)
    _stub_doc(monkeypatch, owner="production", perm="dept_internal")  # 原文档=部门内
    from opensearch_pipeline import api
    # 客户端伪造 permission_level=public，试图借升版把 dept_internal 文档转公开
    req = api.KbUploadUrlRequest(action="version", doc_id="DOC_X", filename="v2.pdf",
                                 owner_dept="production", permission_level="public")
    resp = api.kb_upload_url(req=req, request=None, identity=api.Identity(user_id="dev1"))
    p = ku.verify_upload_token(resp.upload_token)
    assert p["permission_level"] == "dept_internal"   # 强制继承，忽略伪造的 public
    assert p["action"] == "version" and p["doc_id"] == "DOC_X"
    assert resp.requires_kb_admin_approval is False    # 若伪造转 public 成功会要求审批


def test_upload_url_version_other_dept_forbidden(monkeypatch):
    """dept_admin 不能升版其管理范围外部门的文档（owner 不在 managed → 403）。"""
    _sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "finance")
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "k" * 40)
    _stub_doc(monkeypatch, owner="production", perm="dept_internal")
    from opensearch_pipeline import api
    req = api.KbUploadUrlRequest(action="version", doc_id="DOC_P", filename="v2.pdf",
                                 owner_dept="production", permission_level="dept_internal")
    with pytest.raises(Exception) as ei:
        api.kb_upload_url(req=req, request=None, identity=api.Identity(user_id="da1"))
    assert getattr(ei.value, "status_code", None) == 403


def test_register_bad_token(monkeypatch):
    _sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "k" * 40)
    from opensearch_pipeline import api
    req = api.KbRegisterRequest(upload_token="garbage.token")
    with pytest.raises(Exception) as ei:
        api.kb_register(req=req, request=None, identity=api.Identity(user_id="dev1"))
    assert getattr(ei.value, "status_code", None) == 400


def test_register_employee_forbidden(monkeypatch):
    _sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "employee")
    from opensearch_pipeline import api
    req = api.KbRegisterRequest(upload_token="whatever")
    with pytest.raises(Exception) as ei:
        api.kb_register(req=req, request=None, identity=api.Identity(user_id="emp1"))
    assert getattr(ei.value, "status_code", None) == 403
