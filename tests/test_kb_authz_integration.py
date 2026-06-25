# -*- coding: utf-8 -*-
"""
test_kb_authz_integration.py — 写授权身份贯通（令牌 role 往返 + DB 现查 resolver）

覆盖：
  - issue/verify_session_token 携带 role（向后兼容：不传 role → 令牌无该键，消费端兜底 employee）。
  - dingtalk_identity.resolve_kb_identity 在 simulate 下从 env 构造身份，且【读组 ≠ 写授权】：
    国际贸易部 acl_groups=[marketing,production]，但 managed_owner_depts 仅 {marketing}。
"""

from opensearch_pipeline.auth_token import issue_session_token, verify_session_token


# ── 令牌 role 往返 + 向后兼容 ─────────────────────────────────────
def test_token_role_roundtrip(monkeypatch):
    monkeypatch.setenv("RAG_SESSION_SIGNING_KEY", "test-signing-key-32bytes-minimum-xx")

    tok = issue_session_token("u1", dept=["marketing"], name="张三", role="dept_admin")
    payload = verify_session_token(tok)
    assert payload and payload["uid"] == "u1"
    assert payload["role"] == "dept_admin"
    assert payload["acl_groups"] == ["marketing"]

    # 不传 role → 令牌不写该键（旧消费端兼容；新消费端 .get('role') 兜底 employee）
    tok2 = issue_session_token("u2", dept=["finance"])
    p2 = verify_session_token(tok2)
    assert p2 and "role" not in p2
    assert (p2.get("role") or "employee") == "employee"


# ── resolver（simulate）：读组 ≠ 写授权 的端到端证明 ─────────────────
def test_resolve_kb_identity_simulate_read_ne_write(monkeypatch):
    # 必须在 simulate 下跑（pytest 以 RAG_SIMULATE=true 启动）
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        import pytest
        pytest.skip("需 RAG_SIMULATE=true")

    monkeypatch.setenv("RAG_SIM_USER_ROLE", "dept_admin")
    monkeypatch.setenv("RAG_SIM_USER_DEPT", "国际贸易部")          # 读组 → [marketing, production]
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", "marketing")  # 写授权 → 仅 marketing

    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline import kb_authz as ka

    ident = resolve_kb_identity("trade_admin_1")
    assert ident.role == "dept_admin"
    # 读组含 production（来自 _DEPT_NAME_TO_GROUPS["国际贸易部"]）
    assert "production" in ident.acl_groups and "marketing" in ident.acl_groups
    # 但写授权仅 marketing —— 关键不变量
    assert ka.managed_owner_depts(ident) == ["marketing"]
    # 据此裁决：能写 marketing，不能写 production
    assert ka.authorize_upload(ident, "marketing", "dept_internal").allowed
    assert ka.authorize_upload(ident, "production", "dept_internal").reason == "owner_dept_not_managed"


def test_resolve_kb_identity_unmapped_fail_closed(monkeypatch):
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        import pytest
        pytest.skip("需 RAG_SIMULATE=true")

    # 未配置角色/授权 → employee、无写权、无入口（fail-closed）
    monkeypatch.delenv("RAG_SIM_USER_ROLE", raising=False)
    monkeypatch.delenv("RAG_SIM_MANAGED_OWNER_DEPTS", raising=False)
    monkeypatch.setenv("RAG_SIM_USER_DEPT", "审计部")  # 未映射部门

    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline import kb_authz as ka

    ident = resolve_kb_identity("auditor_1")
    assert ident.role == "employee"
    assert not ka.can_access_console(ident)
    assert ka.managed_owner_depts(ident) == []
    assert ka.authorize_upload(ident, "finance", "dept_internal").reason == "not_admin"
