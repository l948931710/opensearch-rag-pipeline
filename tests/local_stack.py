# -*- coding: utf-8 -*-
"""本地 dev 栈（docker-compose：rag-mysql-local / rag-opensearch-local）测试接线与可用性探测。

DB/检索集成测试（test_classification / test_image_funnel 的 RDS 用例、test_concurrency、
test_pipeline 的 bulk 切分用例与 reset_db_state fixture）设计上跑在本地 dev 栈上。
pytest 默认不带 RAG_ENV（保持全局 simulate，避免 .env.local 的 RAG_SIMULATE=false 把
整个套件切到真实 API），因此进程内的存储凭证是 dataclass 默认值，可能连不上本地容器。

接线规则（conftest.py 在收集任何测试前调用）：
  1. 仅当目标指向本机（localhost/127.0.0.1，或 OpenSearch host 为空默认值）时才探测——
     远程 host（含生产 RDS rm-bp15... / 生产 HA3）一律不探测、不回退，集成测试只允许连本机；
  2. MySQL：先用配置内凭证探测；失败则回退 docker-compose.yml 的默认凭证
     （root / your_password / fuling_knowledge），成功后写回 RAG_RDS_* 环境变量
     并同步已实例化的 config 单例（写回环境变量是为了挺过 test_config_loading
     等用例对 config 单例的重建）；
  3. OpenSearch：仅当未配置任何检索后端（HA3 endpoint 与 opensearch.host 均空）时，
     探测 http://localhost:9200（compose 栈 DISABLE_SECURITY_PLUGIN=true 免认证），
     可达则写回 RAG_OPENSEARCH_HOST/USE_SSL/VERIFY_CERTS；
  4. 连不上 → 标记不可用，带 `requires_local_db` / `requires_local_opensearch`
     的用例整体 skip。

⚠️ 接线成功后，这些集成测试会对本地 fuling_knowledge 执行真实 DML
（test_pipeline 的 reset fixture 会清空 chunk_meta 等表），并向本地 OpenSearch
写入测试索引。本地库如导入过需要保留的镜像数据，请先备份或换库再跑 `make test`。
"""

import os

import pytest

_LOCAL_HOSTS = {"localhost", "127.0.0.1"}

# docker-compose.yml → services.mysql（容器 rag-mysql-local）的默认凭证
_COMPOSE_DB_CREDS = {"user": "root", "password": "your_password", "database": "fuling_knowledge"}

_db_available = None
_db_reason = ""
_os_available = None
_os_reason = ""


def _try_connect_mysql(host, port, user, password, database):
    import pymysql

    pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=database, connect_timeout=2,
    ).close()


def ensure_local_db_wired() -> bool:
    """探测本地 MySQL；必要时把 compose 默认凭证写回 RAG_RDS_*。幂等。"""
    global _db_available, _db_reason
    if _db_available is not None:
        return _db_available

    from opensearch_pipeline.config import get_config

    rds = get_config().rds
    if rds.host not in _LOCAL_HOSTS:
        _db_available = False
        _db_reason = (
            f"RDS host '{rds.host}' is not local — "
            "DB integration tests only run against the local dev stack"
        )
        return False

    try:
        _try_connect_mysql(rds.host, rds.port, rds.user, rds.password, rds.database)
        _db_available = True
        return True
    except Exception:
        pass

    try:
        _try_connect_mysql(rds.host, rds.port, **_COMPOSE_DB_CREDS)
    except Exception as e:
        _db_available = False
        _db_reason = f"Local MySQL not available: {e}"
        return False

    os.environ["RAG_RDS_USER"] = _COMPOSE_DB_CREDS["user"]
    os.environ["RAG_RDS_PASSWORD"] = _COMPOSE_DB_CREDS["password"]
    os.environ["RAG_RDS_DATABASE"] = _COMPOSE_DB_CREDS["database"]
    rds.user = _COMPOSE_DB_CREDS["user"]
    rds.password = _COMPOSE_DB_CREDS["password"]
    rds.database = _COMPOSE_DB_CREDS["database"]
    print("    [tests] Local MySQL wired with docker-compose dev credentials "
          f"({rds.host}:{rds.port}/{rds.database})")
    _db_available = True
    return True


def ensure_local_opensearch_wired() -> bool:
    """探测本地 OpenSearch（compose 免认证栈）；可达则写回 RAG_OPENSEARCH_*。幂等。"""
    global _os_available, _os_reason
    if _os_available is not None:
        return _os_available

    from opensearch_pipeline.config import get_config

    config = get_config()
    # 已显式配置检索后端（HA3 或远程 OpenSearch）时不做任何接线
    if config.alibaba_vector.endpoint:
        _os_available = False
        _os_reason = "HA3 endpoint configured — local OpenSearch wiring skipped"
        return False
    if config.opensearch.host and config.opensearch.host not in _LOCAL_HOSTS:
        _os_available = False
        _os_reason = (
            f"OpenSearch host '{config.opensearch.host}' is not local — "
            "search integration tests only run against the local dev stack"
        )
        return False

    host = config.opensearch.host or "localhost"
    port = config.opensearch.port or 9200
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://{host}:{port}", timeout=2) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
    except Exception as e:
        _os_available = False
        _os_reason = f"Local OpenSearch not available: {e}"
        return False

    os.environ["RAG_OPENSEARCH_HOST"] = host
    os.environ["RAG_OPENSEARCH_USE_SSL"] = "false"
    os.environ["RAG_OPENSEARCH_VERIFY_CERTS"] = "false"
    config.opensearch.host = host
    config.opensearch.use_ssl = False
    config.opensearch.verify_certs = False
    print(f"    [tests] Local OpenSearch wired ({host}:{port}, security plugin disabled)")
    _os_available = True
    return True


def local_db_unavailable_reason() -> str:
    return _db_reason or "Local MySQL not available"


def local_opensearch_unavailable_reason() -> str:
    return _os_reason or "Local OpenSearch not available"


requires_local_db = pytest.mark.skipif(
    not ensure_local_db_wired(), reason=local_db_unavailable_reason()
)

requires_local_opensearch = pytest.mark.skipif(
    not ensure_local_opensearch_wired(), reason=local_opensearch_unavailable_reason()
)
