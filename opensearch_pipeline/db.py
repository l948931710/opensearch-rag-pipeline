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
import time

from opensearch_pipeline.config import get_config


class DBPoolExhausted(RuntimeError):
    """连接池在 RAG_DB_ACQUIRE_TIMEOUT 内拿不到连接（OBS/P1-07）。

    此前池以 blocking=True 无超时建立：并发 > 池上限时，多余请求**无限期静默等连接**
    （『问答莫名卡住』尾延迟，无日志线索），AnyIO 线程池令牌被逐一等死→假健康。
    现在获取连接有上限等待，超时抛本异常；serving 端 api.py 注册了异常处理器把它映射成
    503 DB_POOL_EXHAUSTED（而非 500/挂起），让调用方快速失败、释放线程池令牌。
    """


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

    池耗尽时最多等待 RAG_DB_ACQUIRE_TIMEOUT 秒（默认 5s），超时抛 DBPoolExhausted
    （P1-07），而非无限期阻塞。设 ≤0 恢复旧的无限阻塞语义。
    """
    global _db_pool
    if _db_pool is None:
        _init_db_pool()
    from opensearch_pipeline.env_guard import GuardedDBConnection
    conn = GuardedDBConnection(_acquire_pool_connection(), get_config().rds.host)
    _begin_txn(conn)   # ultra P1：显式事务边界，关闭 SteadyDB 单句重放（见 _begin_txn）
    return conn


def _txn_begin_enabled() -> bool:
    """取连接是否显式 begin()（默认 on=修复生效）。RAG_DB_TXN_BEGIN=false 恢复旧无-begin 语义（逃生口）。"""
    return os.environ.get("RAG_DB_TXN_BEGIN", "true").strip().lower() not in ("0", "false", "no", "off")


def _begin_txn(conn) -> None:
    """取连接即开启显式事务边界（ultra P1，2026-07-17）。

    池以 autocommit=False 建立却从不 begin() 时，DBUtils SteadyDB 视每条语句可独立重放：
    多语句事务中途断连（pymysql 把死锁 1213 映射 OperationalError、1205 落 InternalError，
    均在 SteadyDB 默认失败类）被 tough cursor 透明重连 + **只重执行失败那一句**，而服务端
    已回滚在途事务的所有前置语句——调用方随后 commit() 只落下尾句，账本被撕成两半
    （如 document_meta 更新了但 document_version 行丢失）。begin() 置 _transaction 位后
    tough 重放关闭，断连如实抛错走回滚路径，原子性恢复。对齐 agent_runtime.run_store._begin
    与 spot_checker/cost_breaker 的同款前置。

    读路径代价：单句 SELECT 中途断连不再静默重放，改为抛错（serving 侧映射 503 快速失败，
    优于静默错误答案）；取连接时的 ping=1 重连不受影响（发生在 begin 之前）。桩连接无 begin
    则跳过；begin 本身抛错不阻断取连接（退化为旧无-begin 语义，保读路径可用性）。
    RAG_DB_TXN_BEGIN=false 整体恢复旧行为。"""
    if not _txn_begin_enabled():
        return
    fn = getattr(conn, "begin", None)
    if fn is None:
        return
    try:
        fn()
        try:
            # PERF-1（2026-07-19）：标记「本连接已 begin」——run_store 等下游多语句方法
            # 的 _begin 探到即跳过（双 BEGIN 归一：每借出恰一次往返；flag off 时标记
            # 缺席，下游照旧自 begin，两种姿态下都恰好一次）。
            conn._rag_txn_begun = True
        except Exception:   # noqa: BLE001 — 桩连接禁 setattr：退双 begin 旧行为（无害）
            pass
    except Exception as e:  # noqa: BLE001 — begin 失败极罕见（连接刚取、健康）；绝不阻断读路径
        print(f"    ⚠️ [Pool] conn.begin() 失败（退化为无-begin 语义，不阻断）: {e}")


def _db_acquire_timeout() -> float:
    """池获取连接的上限等待秒数（P1-07）。默认 5.0；≤0 恢复无限阻塞（旧语义）。"""
    try:
        return float(os.environ.get("RAG_DB_ACQUIRE_TIMEOUT", "5") or 5)
    except ValueError:
        return 5.0


def _acquire_pool_connection():
    """从池取一个裸连接，池耗尽时有上限地等待（P1-07）。

    timeout ≤ 0 → 直接走 PooledDB 的阻塞取连接（旧的无限等待）。
    timeout > 0 → 池以 blocking=False 建立（见 _init_db_pool），耗尽时 .connection() 抛
    TooManyConnections；此处按指数退避轮询到 deadline，仍拿不到就抛 DBPoolExhausted。
    轮询而非独立信号量：连接归还由池自身记账，绝不会因未 close 的连接泄漏许可导致更严重死锁。
    """
    timeout = _db_acquire_timeout()
    if timeout <= 0:
        return _db_pool.connection()
    try:
        from dbutils.pooled_db import TooManyConnections
    except Exception:  # pragma: no cover - 依赖缺失时退回阻塞语义
        return _db_pool.connection()
    deadline = time.monotonic() + timeout
    delay = 0.02
    while True:
        try:
            return _db_pool.connection()
        except TooManyConnections:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DBPoolExhausted(
                    f"连接池耗尽：{timeout}s 内未取得连接"
                    f"（上限 {_pool_max_connections()}，调大 RAG_DB_POOL_MAX 或排查慢查询/连接泄漏）"
                ) from None
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.2)


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
    # #F-staging-opdb 与 assert_destructive_write_allowed 同步：staging 只有主库与运营库
    # 都切 _stg 才判可写；运营库仍是生产 fuling_operation 时不 fast-return，落到下面按
    # 生产 RDS 默认只读（三层同进同出，不与写守卫打架）。
    if env == "staging" and full_cfg.rds.database.endswith("_stg") \
            and full_cfg.rds.operation_database.endswith("_stg"):
        return False
    from opensearch_pipeline.config import is_prod_target
    if not is_prod_target("rds", full_cfg.rds.host):
        return False
    # 非生产标签 → 生产 RDS：默认只读；唯一放行写的口子 = 当日 destructive ack（与守卫层同源）。
    from opensearch_pipeline.env_guard import _ack_today
    return not _ack_today(os.environ.get("RAG_DESTRUCTIVE_PROD_ACK", ""))


def _pool_max_connections() -> int:
    """池上限（perf#13/#14）。默认 max(20, RAG_CLASSIFY_CONCURRENCY+4)，RAG_DB_POOL_MAX 显式覆盖。

    背景：固定 10 时（blocking=True 无超时），serving 侧看板类长查询 + AnyIO 线程池（默认 120 令牌）
    并发 >10 即让其余请求无限期静默等连接（『问答莫名卡住』尾延迟，无日志线索）；摄取侧
    classify 并发（默认 8）逼近上限后，调大 RAG_CLASSIFY_CONCURRENCY 会被池上限静默串行化。
    20 只是上限不是常驻——mincached=2 不变；maxcached 批次7 起默认对齐池上限
    （见 _pool_max_cached：此前 5 的空闲帽让 >5 并发振荡时归还连接被硬 close，
    每个超额请求付整套 TCP+认证+TLS 握手），空闲连接的 RDS 侧成本仍≈0（内存级）。"""
    try:
        classify_conc = int(os.environ.get("RAG_CLASSIFY_CONCURRENCY", "8") or 8)
    except ValueError:
        classify_conc = 8
    default_max = max(20, classify_conc + 4)
    try:
        v = int(os.environ.get("RAG_DB_POOL_MAX", str(default_max)))
        maxconn = v if v > 0 else default_max
    except ValueError:
        maxconn = default_max
    if classify_conc > maxconn:
        print(f"    ⚠️ [Pool] RAG_CLASSIFY_CONCURRENCY={classify_conc} > 池上限 {maxconn}——"
              f"blocking=True 下超出部分将串行等连接，调参无效果；请同步调大 RAG_DB_POOL_MAX。")
    return maxconn


def _pool_max_cached(max_conn: int) -> int:
    """空闲连接帽（批次7，ultra db:195）：默认对齐池上限。dbutils PooledDB.cache() 在空闲数
    已达 maxcached 时对归还连接直接 close——并发在旧帽 5 以上振荡时，每个超额请求都重付
    整套 TCP+MySQL 认证握手（P0-02 开 RDS TLS 后再加一次 TLS 握手），serving 热路径重连
    抖动放大。空闲连接的 RDS 侧成本≈0（内存级），缓存到上限是纯收益。
    RAG_DB_POOL_MAXCACHED 可显式收小（逃生口；≤0/非法值回默认）。"""
    try:
        v = int(os.environ.get("RAG_DB_POOL_MAXCACHED", "") or max_conn)
        return v if v > 0 else max_conn
    except ValueError:
        return max_conn


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

    _max_conn = _pool_max_connections()
    _max_cached = _pool_max_cached(_max_conn)
    # P1-07：timeout>0 时以 blocking=False 建池，由 _acquire_pool_connection 做「有上限的等待」；
    # ≤0 恢复旧的 blocking=True 无限等待（逃生口）。
    _blocking = _db_acquire_timeout() <= 0
    pool_kwargs = dict(
        creator=pymysql,
        mincached=2,           # 池中保持的最小空闲连接数
        maxcached=_max_cached,  # 空闲帽默认=池上限（批次7，见 _pool_max_cached）
        maxconnections=_max_conn,  # 池上限（perf#13/#14 可配+联动，见 _pool_max_connections）
        blocking=_blocking,    # P1-07：默认 False（有超时轮询）；RAG_DB_ACQUIRE_TIMEOUT≤0 恢复无限阻塞
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
        **cfg.pymysql_ssl_args(),   # P0-02：RDS_SSL_CA 配了才传 ssl（默认空=现网不变）
    )
    if pool_readonly:
        # 与 prod_access.get_prod_readonly_conn 同源的会话级只读：对后续所有事务
        # （含 autocommit 隐式事务）生效，PooledDB 复用连接时保持（session-level，非事务级）。
        pool_kwargs["init_command"] = "SET SESSION TRANSACTION READ ONLY"

    _db_pool = PooledDB(**pool_kwargs)
    print(f"    [Pool] MySQL connection pool initialized (min=2, max={_max_conn}, "
          f"classify_concurrency={os.environ.get('RAG_CLASSIFY_CONCURRENCY', '8')}, "
          f"host={cfg.host}:{cfg.port}, db={cfg.database}"
          + ("，🔒 SESSION READ ONLY（声明式只读）" if pool_readonly else "") + ")")


def _reset_db_pool():
    """关闭并重置连接池。用于测试清理或配置变更后重新初始化。"""
    global _db_pool
    if _db_pool is not None:
        _db_pool.close()
        _db_pool = None
