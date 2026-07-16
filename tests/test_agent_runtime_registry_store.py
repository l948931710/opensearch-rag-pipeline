# -*- coding: utf-8 -*-
"""
test_agent_runtime_registry_store.py — tool_registry DB 治理（DB 驱动 kill switch）真库测试

RDSToolRegistryStore 对本地真 MySQL（fuling_operation.tool_registry，schema/022）验：
sync upsert（status 不覆盖）、set_status/disabled_names（TTL=0 即时）、drift、
registry 挂 DB 源后 resolve 真被拦。无本地库/无表 → skip；host-pin 本地。
测试工具名带 `zztest_` 前缀，收尾清理，不碰真实注册行。
"""
import json
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
                        "WHERE table_schema=DATABASE() AND table_name='tool_registry'")
            ok = cur.fetchone()[0] == 1
        conn.close()
        return ok
    except Exception:
        return False


_AGENT_DB_OK = _db_ready()

# 批次6（unknown-unknowns P1-06）：RAG_AGENT_TESTS_REQUIRE_RDS=1 → 探测断线收集期硬红
# （与 ontology 家族 REQUIRE_RDS 同纪律，CI db-integration 的零跳过机器强制）。
if not _AGENT_DB_OK and os.environ.get("RAG_AGENT_TESTS_REQUIRE_RDS", "").strip() == "1":
    raise RuntimeError(
        "RAG_AGENT_TESTS_REQUIRE_RDS=1 但本地 MySQL/tool_registry 不可用——"
        "真库契约族不许静默 skip（P1-06）；检查 ci_load_schema 与连接配置")

skipif_no_db = pytest.mark.skipif(not _AGENT_DB_OK,
                                  reason="本地 MySQL 无 tool_registry（先 apply 022）")


def _registry(*names):
    from opensearch_pipeline.agent_runtime.registry import ToolRegistry
    from opensearch_pipeline.agent_runtime.tool import RiskLevel, ToolResult, ToolSpec

    class _T:
        def __init__(self, name):
            self.spec = ToolSpec(
                name=name, version="1.0", description="zztest",
                input_schema={"type": "object"}, output_schema={"type": "object"},
                risk_level=RiskLevel.READ_ONLY, permission_scope="kb.search",
                data_classification="internal")

        def run(self, ctx, args, idempotency_key=None):
            return ToolResult.text_ok("ok")

    reg = ToolRegistry()
    for n in names:
        reg.register(_T(n))
    return reg


def _cleanup(*names):
    conn = _local_operation_conn()
    with conn.cursor() as cur:
        for n in names:
            cur.execute("DELETE FROM tool_registry WHERE tool_name=%s", (n,))
    conn.close()


@skipif_no_db
def test_sync_upserts_and_preserves_disabled_status():
    from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore
    name = f"zztest_{uuid.uuid4().hex[:8]}"
    reg = _registry(name)
    store = RDSToolRegistryStore(ttl_s=0)
    try:
        assert store.sync_specs(reg) == 1
        rows = [r for r in store.list_rows() if r["tool_name"] == name]
        assert rows and rows[0]["status"] == "active" and rows[0]["risk_level"] == "read_only"
        # 管理员停用 → 重启同步（re-sync）**绝不覆盖** status
        assert store.set_status(name, "disabled") == 1
        assert store.sync_specs(reg) == 1
        rows = [r for r in store.list_rows() if r["tool_name"] == name]
        assert rows[0]["status"] == "disabled", "sync 覆盖了管理员的停用决定（发布即复活=事故）"
        assert name in store.disabled_names()
    finally:
        _cleanup(name)


@skipif_no_db
def test_set_status_unknown_tool_returns_zero():
    from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore
    assert RDSToolRegistryStore(ttl_s=0).set_status(f"zztest_nope_{uuid.uuid4().hex[:6]}",
                                                    "disabled") == 0


@skipif_no_db
def test_disabled_names_ttl_cache_and_invalidate():
    from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore
    name = f"zztest_{uuid.uuid4().hex[:8]}"
    reg = _registry(name)
    slow = RDSToolRegistryStore(ttl_s=3600)                    # 长 TTL：验证缓存与失效
    try:
        slow.sync_specs(reg)
        assert name not in slow.disabled_names()               # 建立缓存（active）
        slow.set_status(name, "disabled")                      # set_status 自失效 → 即时可见
        assert name in slow.disabled_names()
    finally:
        _cleanup(name)


@skipif_no_db
def test_registry_gate_blocks_via_db():
    """端到端：DB 置 disabled → registry.resolve 真被拦（多实例拉闸的读侧）。"""
    from opensearch_pipeline.agent_runtime.registry import ToolDisabled
    from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore
    name = f"zztest_{uuid.uuid4().hex[:8]}"
    reg = _registry(name)
    store = RDSToolRegistryStore(ttl_s=0)
    try:
        store.sync_specs(reg)
        reg.attach_disabled_source(store.disabled_names)
        assert reg.resolve(name) is not None
        store.set_status(name, "disabled")
        with pytest.raises(ToolDisabled):
            reg.resolve(name)
        store.set_status(name, "active")
        assert reg.resolve(name) is not None
    finally:
        _cleanup(name)


@skipif_no_db
def test_drift_warnings_detect_spec_tamper():
    from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore
    name = f"zztest_{uuid.uuid4().hex[:8]}"
    reg = _registry(name)
    store = RDSToolRegistryStore(ttl_s=0)
    try:
        store.sync_specs(reg)
        assert not [w for w in store.drift_warnings(reg) if name in w]   # 刚同步：本工具无漂移
        conn = _local_operation_conn()                                   # 表被手改 → spec 漂移
        with conn.cursor() as cur:
            cur.execute("UPDATE tool_registry SET spec_json=%s WHERE tool_name=%s",
                        (json.dumps({"tampered": True}), name))
        conn.close()
        assert any("spec 漂移" in w and name in w for w in store.drift_warnings(reg))
    finally:
        _cleanup(name)
