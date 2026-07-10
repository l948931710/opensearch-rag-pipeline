# -*- coding: utf-8 -*-
"""PR-B（P0-02）：fuling_ontology 独立库隔离的 DB 层证明。

外审结论：`fuling_ro` 持库级 `GRANT SELECT ON fuling_operation.*`，ontology 表族
留在运营库时「sem_* 不授 fuling_ro」只是注释——任何持该账号的脚本/NL2SQL 可绕过
服务层行过滤。修复=表族整体移入 fuling_ontology 且不授 fuling_ro。

本文件在真库上机械验证这道隔离：建一个模拟 fuling_ro 授权面的探针账号
（GRANT SELECT ON fuling_operation.*），断言它 ①SELECT ontology 表/sem 视图
得 1142/1044 ②SHOW GRANTS 不含 fuling_ontology。host-pin 本地 MySQL（同
test_ontology_store 模板），生产指纹拒连。
"""
import uuid

import pytest

# ── RDS 可用性探测（host-pin；需要 root 建探针账号）──────────────────────────────


def _rds_ready() -> bool:
    try:
        import pymysql

        from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target
        cfg = get_config()
        if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
            return False
        conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                               password=cfg.rds.password,
                               database=cfg.rds.ontology_database,
                               autocommit=True, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                        "WHERE table_schema=DATABASE() AND table_name='ontology_object'")
            has_table = cur.fetchone()[0] == 1
            # 需要能建探针账号（本地 compose root / CI service root 均可）
            cur.execute("SELECT CURRENT_USER()")
            is_root = cur.fetchone()[0].startswith("root@")
        conn.close()
        return has_table and is_root
    except Exception:   # noqa: BLE001
        return False


_RDS_OK = _rds_ready()
rds_only = pytest.mark.skipif(
    not _RDS_OK, reason="本地 MySQL 无 fuling_ontology.ontology_object 或非 root")


@rds_only
def test_operation_wildcard_grant_cannot_reach_ontology_db():
    import pymysql

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    probe_user = f"ont_probe_{uuid.uuid4().hex[:8]}"
    probe_pwd = uuid.uuid4().hex
    admin = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                            password=cfg.rds.password, autocommit=True, connect_timeout=3)
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE USER '{probe_user}'@'%' IDENTIFIED BY '{probe_pwd}'")
            # 复刻生产 fuling_ro 的授权面（docs/environment_design.md §6.2）
            cur.execute(f"GRANT SELECT ON {cfg.rds.operation_database}.* TO '{probe_user}'@'%'")
            cur.execute(f"GRANT SELECT ON {cfg.rds.database}.* TO '{probe_user}'@'%'")

        probe = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=probe_user,
                                password=probe_pwd, autocommit=True, connect_timeout=3)
        try:
            with probe.cursor() as cur:
                # ① 运营库照常可读（授权面没缩水——隔离不是靠废掉 fuling_ro）
                cur.execute(f"SELECT COUNT(*) FROM {cfg.rds.operation_database}.qa_session_log")
                # ② ontology 基表 / sem 视图：一律权限拒绝（1142 表级 / 1044 库级）
                for target in ("ontology_object", "ontology_identifier", "sem_packing"):
                    with pytest.raises(pymysql.err.OperationalError) as exc:
                        cur.execute(
                            f"SELECT * FROM {cfg.rds.ontology_database}.{target} LIMIT 1")
                    assert exc.value.args[0] in (1044, 1142), (target, exc.value.args)
                # ③ SHOW GRANTS 里不含 ontology 库（授权面审计口径）
                cur.execute("SHOW GRANTS")
                grants = " ".join(str(r[0]) for r in cur.fetchall())
                assert cfg.rds.ontology_database not in grants
        finally:
            probe.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f"DROP USER IF EXISTS '{probe_user}'@'%'")
        admin.close()


@rds_only
def test_ontology_db_collation_pinned():
    """独立库必须显式 utf8mb4_unicode_ci（README 铁律 4，防 MySQL8 _0900_ai_ci 漂移）。"""
    import pymysql

    from opensearch_pipeline.config import get_config
    cfg = get_config()
    conn = pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, autocommit=True, connect_timeout=3)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
                        "WHERE SCHEMA_NAME=%s", (cfg.rds.ontology_database,))
            row = cur.fetchone()
            assert row and row[0] == "utf8mb4_unicode_ci"
            # 键列的列级 bin 覆盖（PR-A P0-04）也一并钉住
            cur.execute(
                "SELECT COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='ontology_identifier' "
                "AND COLUMN_NAME IN ('namespace','norm_value')",
                (cfg.rds.ontology_database,))
            assert {r[0] for r in cur.fetchall()} == {"utf8mb4_bin"}
    finally:
        conn.close()
