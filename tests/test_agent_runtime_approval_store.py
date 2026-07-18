# -*- coding: utf-8 -*-
"""
test_agent_runtime_approval_store.py — 审批闭环持久化（schema/025）真库测试 + scope 单测

RDSApprovalStore 对**本地真 MySQL**（fuling_operation）验：create_request（脱敏入库）、
decide 状态机（pending CAS / 幂等重放 / 迟到拒绝）、expire_stale（过期=拒绝）、
get_latest_by_run（resume 崩溃重放锚点）。无本地库/无 025 表 → skip（同 e2e 惯例）；
host-pin 本地，绝不写 staging/prod。
"""
import os
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


_AGENT_DB_OK = _db_ready()

# 批次6（unknown-unknowns P1-06）：RAG_AGENT_TESTS_REQUIRE_RDS=1 → 探测断线收集期硬红
# （与 ontology 家族 REQUIRE_RDS 同纪律，CI db-integration 的零跳过机器强制）。
if not _AGENT_DB_OK and os.environ.get("RAG_AGENT_TESTS_REQUIRE_RDS", "").strip() == "1":
    raise RuntimeError(
        "RAG_AGENT_TESTS_REQUIRE_RDS=1 但本地 MySQL/审批表不可用——"
        "真库契约族不许静默 skip（P1-06）；检查 ci_load_schema 与连接配置")

skipif_no_approval_db = pytest.mark.skipif(
    not _AGENT_DB_OK, reason="本地 MySQL 无审批表（先 apply schema 025）")


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


@skipif_no_approval_db
def test_b4_list_decided_unresumed_only_latest_request():
    """批次4（ultra P1 approval_store:488）：对账扫描只捞 run 的**最新**请求——多审批周期
    run（request1 已批、resume、又挂起 request2）里旧决定不再永久满足扫描（此前会被
    B6 对账重放到新挂起调用：同工具同参 = 批一次、同参调用永放行）。"""
    import json as _json
    import time as _time

    from opensearch_pipeline.agent_runtime.approval_store import (
        DECIDE_ACCEPTED, RDSApprovalStore)
    store = RDSApprovalStore()
    run_id = uuid.uuid4().hex
    conn = _local_operation_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_run (run_id, thread_id, user_id, channel, agent_profile, "
            "status, acl_groups_snapshot, started_at) "
            "VALUES (%s,%s,%s,'console','default','suspended',%s,NOW(3))",
            (run_id, f"t-{run_id[:8]}", "appr-u1", _json.dumps(["production"])))
    conn.close()
    rid1 = store.create_request(run_id, _ctx(), _pending_call())
    rid2 = None
    try:
        assert store.decide(rid1, decision="approved", decided_by="admin-1",
                            idempotency_key="b4k1") == DECIDE_ACCEPTED
        _time.sleep(0.02)
        hits = store.list_decided_unresumed(grace_s=0, limit=100)
        assert any(h["request_id"] == rid1 for h in hits), "最新且已决：应命中（重发 resume）"

        _time.sleep(0.02)                                    # created_at(3) 拉开毫秒差
        pc2 = dict(_pending_call())
        pc2["call_id"] = "c2"
        rid2 = store.create_request(run_id, _ctx(), pc2)     # 更新的 pending 请求
        hits2 = store.list_decided_unresumed(grace_s=0, limit=100)
        assert not any(h["request_id"] == rid1 for h in hits2), \
            "存在更新请求：旧决定必须退出扫描（不得重放到新挂起调用）"

        assert store.decide(rid2, decision="rejected_feedback", decided_by="admin-1",
                            idempotency_key="b4k2") == DECIDE_ACCEPTED
        _time.sleep(0.02)
        hits3 = store.list_decided_unresumed(grace_s=0, limit=100)
        assert any(h["request_id"] == rid2 for h in hits3)   # 最新已决 → 照常命中
        assert not any(h["request_id"] == rid1 for h in hits3)
    finally:
        _cleanup(rid1)
        if rid2:
            _cleanup(rid2)
        conn2 = _local_operation_conn()
        with conn2.cursor() as cur:
            cur.execute("DELETE FROM agent_run WHERE run_id=%s", (run_id,))
        conn2.close()
