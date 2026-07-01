# -*- coding: utf-8 -*-
"""
db.py — 共享 MySQL 连接池（serving 读路径 + ingest 写路径共用的唯一句柄）

从 pipeline_nodes.py 机械搬移（F-A1 结构债拆分，2026-07-01）：serving 侧
（api/retriever/qa_logger/dingtalk_*/feedback_handler/...）此前每次拿连接都要
`from opensearch_pipeline.pipeline_nodes import _get_db_conn`，把 7000+ 行摄取
代码（连带 chunker）拖进服务进程。现在 serving 直接 import 本模块；
pipeline_nodes 仍 re-export `_get_db_conn` 等名字，摄取节点与既有 tests 的
monkeypatch 目标（`opensearch_pipeline.pipeline_nodes._get_db_conn`）不受影响。

⚠️ 池上的三层守卫（sim→prod 拒建池 / 声明式只读 SESSION READ ONLY /
GuardedDBConnection 连接层拦写）逐字保留——见各函数 docstring。
"""

import os

from opensearch_pipeline.config import get_config


def _get_db_conn(select_db=True):
    """从连接池获取一个数据库连接。

    连接池由 DBUtils.PooledDB 管理，conn.close() 会将连接归还到池中而非真正关闭。
    首次调用时懒初始化池；后续调用直接从池中获取。

    注意: select_db 参数保留用于 API 兼容性。数据库在连接池初始化时已预选。

    返回的不是裸连接，而是 GuardedDBConnection 代理：写语句（INSERT/UPDATE/DELETE/...）
    在连接层过一次 assert_destructive_write_allowed，读语句直通。这让 RAG_READONLY /
    非生产→生产 策略对**任何** cursor 写入生效（含 register_metadata、classify 冻结维护、
    detect_sensitive、redact、publish 等没有显式守卫调用的"裸 cursor"节点），而不拖累
    serving（retriever/api/dingtalk_identity）共用同一池的读路径。
    """
    global _db_pool
    if _db_pool is None:
        _init_db_pool()
    from opensearch_pipeline.env_guard import GuardedDBConnection
    return GuardedDBConnection(_db_pool.connection(), get_config().rds.host)


# ─── 连接池内部实现 ───────────────────────────────────────────────

_db_pool = None  # module-level singleton

def _pool_readonly_declared(full_cfg) -> bool:
    """连接池是否应以 SESSION READ ONLY 建立（声明式只读，物理 MySQL 边界）。

    True 当：
      - RAG_READONLY=true（PROD-RO 会话）——无条件只读，ack 不豁免（PROD-RO 硬边界）；或
      - 非 production 标签却指向生产 RDS 实例，且**没有**当日 RAG_DESTRUCTIVE_PROD_ACK——
        默认只读诊断形态（config 加载期 R1/R3 已强制 RAG_ALLOW_REMOTE_DB=read_only_ack 才放行连接）。
    返回 False（可写）当：environment=production、staging 写 _stg 库、本地非生产目标，或
    非生产标签指向生产 RDS **但带当日 destructive ack**——最后一种与连接层/节点层守卫
    （assert_destructive_write_allowed 的 ack 策略）保持一致：三层同进同出、绝不互相打架，
    同时保留 docs/environment_design.md 文档化的 RAG_DESTRUCTIVE_PROD_ACK 逃生口（写生产
    需当日显式授权）。
    """
    if full_cfg.readonly:
        return True
    env = (full_cfg.environment or "development").lower()
    if env == "production":
        return False
    if env == "staging" and full_cfg.rds.database.endswith("_stg"):
        return False
    from opensearch_pipeline.config import is_prod_target
    if not is_prod_target("rds", full_cfg.rds.host):
        return False
    # 非生产标签 → 生产 RDS：默认只读；唯一放行写的口子 = 当日 destructive ack（与守卫层同源）。
    from opensearch_pipeline.env_guard import _ack_today
    return not _ack_today(os.environ.get("RAG_DESTRUCTIVE_PROD_ACK", ""))


def _init_db_pool():
    """懒初始化 MySQL 连接池。"""
    global _db_pool
    if _db_pool is not None:
        return

    import pymysql
    from dbutils.pooled_db import PooledDB

    full_cfg = get_config()
    cfg = full_cfg.rds

    # 🛡️ Sim→prod leak guard (added 2026-06-14 after the chunk_meta DELETE incident).
    # simulate_db=True 表示全局 config 处于"sim 模式"——调用方应该走 mock 路径而非
    # _get_db_conn。若此时 cfg.host 命中生产 RDS 指纹，几乎一定是 test fixture / sim
    # 入口绕过了 sim 守卫直接连了 prod。拒绝建池避免 DELETE FROM chunk_meta 误打生产。
    from opensearch_pipeline.config import is_prod_target
    if full_cfg.simulate_db and is_prod_target("rds", cfg.host):
        raise RuntimeError(
            f"[DB POOL GUARD] simulate_db=True 但 RDS host {cfg.host!r} 命中生产指纹。"
            f"拒绝建池防误写 prod。\n"
            f"  - 想在 sim/test 流程里 *读* prod RDS："
            f"用 opensearch_pipeline.prod_access.get_prod_readonly_conn() 而非 _get_db_conn。\n"
            f"  - 真要写 prod：export RAG_SIMULATE=false RAG_SIMULATE_DB=false 再来（不推荐）。"
        )

    # 🛡️ P2 generalization (2026-06-16): 此前 _init_db_pool 的守卫**只在 simulate_db=True
    # 时触发**。这个池是 ingest 写路径与 serving 读路径（retriever/api/dingtalk_identity）
    # 共用的句柄，所以**不能**对声明式只读会话拒绝建池（会连读都断）；改为以
    # SESSION TRANSACTION READ ONLY 建池——读照常，任何写在 MySQL 层直接 ERROR 1792。
    # 这让 RAG_READONLY 成为池层的**物理**边界（belt），与连接层 cursor 守卫（suspenders）
    # 互补，并把守卫覆盖面从"simulate_db=True"扩展到声明式只读的真实跑。
    pool_readonly = _pool_readonly_declared(full_cfg)

    pool_kwargs = dict(
        creator=pymysql,
        mincached=2,           # 池中保持的最小空闲连接数
        maxcached=5,           # 池中保持的最大空闲连接数
        maxconnections=10,     # 池允许的最大连接数 (0 = 无限制)
        blocking=True,         # 连接数耗尽时阻塞等待，而非抛异常
        ping=1,                # 每次取连接时 ping 一次，自动重连 (应对 MySQL wait_timeout)
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,  # 预选数据库，所有连接自动使用此库
        charset=cfg.charset,
        connect_timeout=cfg.connect_timeout,
        read_timeout=cfg.read_timeout,
        autocommit=False,
    )
    if pool_readonly:
        # 与 prod_access.get_prod_readonly_conn 同源的会话级只读：对后续所有事务
        # （含 autocommit 隐式事务）生效，PooledDB 复用连接时保持（session-level，非事务级）。
        pool_kwargs["init_command"] = "SET SESSION TRANSACTION READ ONLY"

    _db_pool = PooledDB(**pool_kwargs)
    print(f"    [Pool] MySQL connection pool initialized (min=2, max=10, "
          f"host={cfg.host}:{cfg.port}, db={cfg.database}"
          + ("，🔒 SESSION READ ONLY（声明式只读）" if pool_readonly else "") + ")")


def _reset_db_pool():
    """关闭并重置连接池。用于测试清理或配置变更后重新初始化。"""
    global _db_pool
    if _db_pool is not None:
        _db_pool.close()
        _db_pool = None
