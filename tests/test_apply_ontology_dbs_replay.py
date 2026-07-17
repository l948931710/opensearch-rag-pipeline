# -*- coding: utf-8 -*-
"""
test_apply_ontology_dbs_replay.py — 批次4 P1-02：apply_ontology_dbs 真库可重放

对本地真 MySQL 建一次性 scratch 库（fuling_ontology_zztest_*，收尾 DROP）验证：
- **连续执行两次干净**：第二遍全部 SKIP（台账已记 checksum ✓），不再靠「文件恰好幂等」；
- **038a 修订路径**：台账原行 checksum 被改（模拟 staging/prod 已 apply 旧版 038）→
  重放守卫化文件 no-op 并记 `@038a` 修订行，原行不动；
- **--force-replay** 全量重执行仍干净（证明含 038a 在内全家族真幂等——1060 复发即红）；
- **brick 恢复**：038 台账行全删（模拟「DDL 落定但台账写失败 exit 3」）→ 重跑守卫 no-op
  且台账补记，不再 duplicate column 卡死。
无本地 MySQL → 整体 skip；host-pin 本地，绝不写 staging/prod（同 agent 真库族纪律）。
"""
import importlib.util
import os
import uuid

import pytest

from opensearch_pipeline.config import _LOCAL_HOSTS, get_config, is_prod_target

_SCRATCH_DB = f"fuling_ontology_zztest_{uuid.uuid4().hex[:8]}"


def _local_conn():
    import pymysql
    cfg = get_config()
    if cfg.rds.host not in _LOCAL_HOSTS or is_prod_target("rds", cfg.rds.host):
        raise RuntimeError(f"[PROD-GUARD] 仅本地 MySQL；解析到 host {cfg.rds.host!r}，拒绝连接。")
    return pymysql.connect(host=cfg.rds.host, port=cfg.rds.port, user=cfg.rds.user,
                           password=cfg.rds.password, charset="utf8mb4",
                           autocommit=True, connect_timeout=5)


def _mysql_ready():
    try:
        conn = _local_conn()
        conn.close()
        return True
    except Exception:   # noqa: BLE001
        return False


skipif_no_mysql = pytest.mark.skipif(not _mysql_ready(), reason="本地 MySQL 不可用")


def _load_runner():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "apply_ontology_dbs.py")
    spec = importlib.util.spec_from_file_location("aodb_replay_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def scratch_env(monkeypatch):
    """把 runner 指向一次性 scratch 库（config shim），收尾 DROP。"""
    real = get_config()

    class _Rds:
        host, port = real.rds.host, real.rds.port
        user, password = real.rds.user, real.rds.password
        ontology_database = _SCRATCH_DB
        # 34802fe 起 apply_migration._connect_rw 走 cfg.rds.pymysql_ssl_args()（P0-02）——
        # shim 直接借真配置的绑定方法（本地无 ssl_ca → {"ssl_disabled": True}）
        pymysql_ssl_args = real.rds.pymysql_ssl_args

    class _Cfg:
        rds = _Rds()

    import opensearch_pipeline.config as cfgmod
    monkeypatch.setattr(cfgmod, "get_config", lambda: _Cfg())
    yield _SCRATCH_DB
    conn = _local_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{_SCRATCH_DB}`")
    finally:
        conn.close()


def _ledger(db, where=""):
    conn = _local_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT filename, checksum FROM `{db}`.schema_migrations {where}")
            return dict(cur.fetchall())
    finally:
        conn.close()


@skipif_no_mysql
def test_double_apply_revision_and_force_replay(scratch_env, capsys):
    db = scratch_env
    aodb = _load_runner()

    # ① 首灌：全部 apply，038（守卫化后）单遍落定，列/索引在
    assert aodb.main(["--commit"]) == 0
    out1 = capsys.readouterr().out
    assert "⏭️" not in out1                                   # 首灌零跳过
    conn = _local_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='ontology_object' "
                        "AND COLUMN_NAME='normalized_title'", (db,))
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()
    rows = _ledger(db)
    fn038 = next(f for f in rows if f.startswith("038_"))
    assert "@" not in fn038                                   # 首灌记原行

    # ② 重放：全部 SKIP（台账已记 checksum ✓），零重执行
    assert aodb.main(["--commit"]) == 0
    out2 = capsys.readouterr().out
    assert out2.count("⏭️") >= len(rows) and "修订重放" not in out2

    # ③ 修订路径：把 038 原行 checksum 改掉（模拟 staging/prod 记的是旧版裸 ALTER）
    conn = _local_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE `{db}`.schema_migrations SET checksum=%s "
                        "WHERE filename=%s", ("0" * 64, fn038))
    finally:
        conn.close()
    assert aodb.main(["--commit"]) == 0
    out3 = capsys.readouterr().out
    assert "修订重放" in out3
    rows3 = _ledger(db)
    assert f"{fn038}@038a" in rows3                           # 修订行落账
    assert rows3[fn038] == "0" * 64                           # 原行不动（铁律 2）

    # ④ --force-replay：含 038a 在内全家族真幂等（1060 复发即此处红）
    assert aodb.main(["--commit", "--force-replay"]) == 0
    assert "⏭️" not in capsys.readouterr().out

    # ⑤ brick 恢复：038 台账行全删（模拟 DDL 落定但台账写失败）→ 重跑不再卡 1060
    conn = _local_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM `{db}`.schema_migrations WHERE filename LIKE %s",
                        (fn038.split("@")[0] + "%",))
    finally:
        conn.close()
    assert aodb.main(["--commit"]) == 0
    assert fn038 in _ledger(db)                               # 台账补记
