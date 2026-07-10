# -*- coding: utf-8 -*-
"""
test_agent_runtime_approval_store.py — 审批闭环持久化（schema/025）真库测试 + scope 单测

RDSApprovalStore 对**本地真 MySQL**（fuling_operation）验：create_request（脱敏入库）、
decide 状态机（pending CAS / 幂等重放 / 迟到拒绝）、expire_stale（过期=拒绝）、
get_latest_by_run（resume 崩溃重放锚点）。无本地库/无 025 表 → skip（同 e2e 惯例）；
host-pin 本地，绝不写 staging/prod。
"""
import uuid

import pytest

from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target


def _local_operation_conn():
    import pymysql
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        raise RuntimeError(f"[PROD-GUARD] 仅本地 MySQL；解析到 host {cfg.rds.host!r}，拒绝连接。")
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, database=cfg.rds.operation_database,
                           autocommit=True, connect_timeout=5)


def _db_ready():
    try:
        conn = _local_operation_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema=DATABASE() AND table_name IN "
                        "('approval_request','approval_decision')")
            ok = cur.fetchone()[0] == 2
        conn.close()
        return ok
    except Exception:
        return False


skipif_no_approval_db = pytest.mark.skipif(
    not _db_ready(), reason="本地 MySQL 无审批表（先 apply schema 025）")


def _ctx(user="appr-u1", groups=("production", "marketing")):
    from opensearch_pipeline.agent_runtime import ExecutionContext
    return ExecutionContext.create(request_id="t", user_id=user, acl_groups=list(groups),
                                   roles=["employee"], channel="console", thread_id="t1")


def _pending_call():
    return {"call_id": "c1", "tool_name": "u8_writeback",
            "arguments": {"qty": 7, "note": "联系电话 13812345678"}}


def _cleanup(request_id):
    conn = _local_operation_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM approval_decision WHERE request_id=%s", (request_id,))
        cur.execute("DELETE FROM approval_request WHERE request_id=%s", (request_id,))
    conn.close()


# ── derive_approver_scope（纯单测，无库）─────────────────────────────────────
def test_scope_is_first_non_public_group():
    from opensearch_pipeline.agent_runtime.approval_store import derive_approver_scope
    assert derive_approver_scope(_ctx(groups=("public", "production"))) == "production"
    assert derive_approver_scope(_ctx(groups=("hr",))) == "hr"
    assert derive_approver_scope(_ctx(groups=("public",))) == ""     # 仅 public → 只有 kb_admin 可审
    assert derive_approver_scope(_ctx(groups=())) == ""


# ── RDS 真库 ────────────────────────────────────────────────────────────────
@skipif_no_approval_db
def test_create_request_sanitizes_args_and_sets_scope():
    from opensearch_pipeline.agent_runtime.approval_store import RDSApprovalStore
    store = RDSApprovalStore()
    run_id = uuid.uuid4().hex
    rid = store.create_request(run_id, _ctx(), _pending_call())
    try:
        areq = store.get_request(rid)
        assert areq["status"] == "pending" and areq["run_id"] == run_id
        assert areq["approver_scope"] == "production"
        assert areq["requested_by"] == "appr-u1"
        assert areq["tool_name"] == "u8_writeback" and areq["call_id"] == "c1"
        # 脱敏入库：手机号原文绝不落 proposed_args_json
        import json
        assert "13812345678" not in json.dumps(areq["proposed_args"], ensure_ascii=False)
        assert areq["proposed_args"]["qty"] == 7
        assert areq["expires_at"] is not None
        # get_pending_by_run / get_latest_by_run 都能锚定
        assert store.get_pending_by_run(run_id)["request_id"] == rid
        assert store.get_latest_by_run(run_id)["request_id"] == rid
    finally:
        _cleanup(rid)


@skipif_no_approval_db
def test_decide_cas_first_valid_wins_and_idempotent_replay():
    from opensearch_pipeline.agent_runtime.approval_store import (
        DECIDE_ACCEPTED, DECIDE_ALREADY_DECIDED, DECIDE_DUPLICATE, RDSApprovalStore)
    store = RDSApprovalStore()
    run_id = uuid.uuid4().hex
    rid = store.create_request(run_id, _ctx(), _pending_call())
    try:
        assert store.decide(rid, decision="approved", decided_by="admin-1",
                            idempotency_key="k1") == DECIDE_ACCEPTED
        # 同键同向重放 → 幂等
        assert store.decide(rid, decision="approved", decided_by="admin-1",
                            idempotency_key="k1") == DECIDE_DUPLICATE
        # 异键/异向迟到决策 → 拒绝（first-valid-wins）
        assert store.decide(rid, decision="rejected_terminate", decided_by="admin-2",
                            idempotency_key="k2") == DECIDE_ALREADY_DECIDED
        areq = store.get_request(rid)
        assert areq["status"] == "approved" and areq["decided_at"] is not None
        # decision 行恰一条（uk_req_idem 防重复）
        conn = _local_operation_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), MAX(decided_by) FROM approval_decision "
                        "WHERE request_id=%s", (rid,))
            n, by = cur.fetchone()
        conn.close()
        assert n == 1 and by == "admin-1"
    finally:
        _cleanup(rid)


@skipif_no_approval_db
def test_expire_stale_marks_pending_expired():
    from opensearch_pipeline.agent_runtime.approval_store import RDSApprovalStore
    store = RDSApprovalStore()
    run_id = uuid.uuid4().hex
    rid = store.create_request(run_id, _ctx(), _pending_call(), ttl_s=-5)   # 立即过期
    try:
        assert store.expire_stale() >= 1
        assert store.get_request(rid)["status"] == "expired"
        # 过期后迟到决策被拒（沉默不是同意）
        from opensearch_pipeline.agent_runtime.approval_store import DECIDE_ALREADY_DECIDED
        assert store.decide(rid, decision="approved",
                            decided_by="admin-1") == DECIDE_ALREADY_DECIDED
    finally:
        _cleanup(rid)


@skipif_no_approval_db
def test_list_pending_scoping():
    from opensearch_pipeline.agent_runtime.approval_store import RDSApprovalStore
    store = RDSApprovalStore()
    run_id = uuid.uuid4().hex
    rid = store.create_request(run_id, _ctx(groups=("hr",)), _pending_call())
    try:
        assert any(a["request_id"] == rid for a in store.list_pending(["hr"]))
        assert not any(a["request_id"] == rid for a in store.list_pending(["production"]))
        assert any(a["request_id"] == rid for a in store.list_pending(None))          # kb_admin 全量
        assert any(a["request_id"] == rid
                   for a in store.list_pending(None, requested_by="appr-u1"))         # mine 视图
        assert store.list_pending([]) == []                                           # 空 scope 空结果
    finally:
        _cleanup(rid)
