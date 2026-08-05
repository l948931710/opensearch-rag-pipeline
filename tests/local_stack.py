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

import contextlib
import os
import warnings

import pytest

_LOCAL_HOSTS = {"localhost", "127.0.0.1"}

# docker-compose.yml → services.mysql（容器 rag-mysql-local）的默认凭证
_COMPOSE_DB_CREDS = {"user": "root", "password": "your_password", "database": "fuling_knowledge"}

_db_available = None
_db_reason = ""
_os_available = None
_os_reason = ""

# 探测成功那一刻的连接四元组，**冻结**下来给跨进程锁用（见 local_stack_exclusive）。
# 为什么不 lazy 读 config：串行组成员 test_simulate_prod_guard.py 会 monkeypatch
# `cfg.rds.host` 成假生产指纹；锁名与连接参数若在 acquire/release 两侧各算一次，
# 就可能算出不同的名字 → RELEASE 放不掉自己的锁 → 会话级自锁。
_wired_dsn = None


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

    global _wired_dsn
    try:
        _try_connect_mysql(rds.host, rds.port, rds.user, rds.password, rds.database)
        _wired_dsn = (rds.host, int(rds.port), rds.user, rds.password)
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

    _wired_dsn = (rds.host, int(rds.port),
                  _COMPOSE_DB_CREDS["user"], _COMPOSE_DB_CREDS["password"])
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


# ── 跨 pytest 进程互斥（2026-08-05，对抗评审 R1/R2 后定稿）────────────────────
# conftest 的 `_LOCAL_STACK_SERIAL_MODULES` 声明了「这些模块不得并发」，但 xdist 分组
# 只在**单个 pytest 进程内**成立。两个 pytest 同时跑（多 worktree / 多会话），
# test_classification::clean_db 的**无 WHERE** `DELETE FROM document_meta|document_version`
# 会清掉另一进程在飞的 seed 行。实测（同机、噪声进程=循环跑 test_classification）：
# test_ingest_lease_db 25/25 红（9 个不同用例）、test_kb_db_integration 7/30 红；
# 干净单进程则 12/12 全绿。故用 MySQL 命名锁把该组的跨进程并发也串起来。
#
# ⚠️ **这是双边协议**：只有双方都取锁才有效。本机 17 个 worktree 钉在约 12 个不同
# commit 上，老 worktree 不带本 fixture ⇒ 锁对它们无效。因此受害测试**必须保留**
# 自己的兜底判据（见 tests/test_kb_db_integration.py），不能因为有锁就删。
#
# 选 GET_LOCK 而非文件锁：受保护资源就是这台 MySQL 实例，锁与资源同生命周期，
# **连接断开自动释放** ⇒ 进程崩溃不留死锁（实测 8.0.46 确认）。
# 与生产侧 `opensearch_pipeline/dataworks_orchestrator.py:1008 _acquire_stage_lock`
# 同款原语但**刻意相反的失败语义**：那边取不到锁 fail-closed 退出（互斥是生产并发
# 安全边界）；这边 fail-open 降级（测试互斥只是防串数据，取不到锁而让 119 个用例
# 集体 ERROR 是更坏的结果）。锁名前缀也刻意避开它的 `rag_ingest:`，两套锁互不干扰。
_XPROC_LOCK_TIMEOUT_S = 120   # 实测组内最长单测 4.51s；代码级上限约 30s（线程 join）⇒ 约 4× 余量
_XPROC_LOCK_ERROR_GIVEUP = 3  # 连败 N 次（每次 connect_timeout=5s）就不再尝试，避免 125×5s 空等
_xproc_lock_conn = None
_xproc_lock_name_cached = None
_xproc_lock_held = False
# 超时过一次后改用**非阻塞**探测（GET_LOCK(name,0)）而不是彻底放弃：成本≈0，
# 对面一空出来立刻恢复保护。⚠️ 早先写成「一次性永久放弃」是错的——评审实跑证明它会在
# 「锁真的失效」那一刻把保护全程关掉，同时让元测试 skip 自己 ⇒ exit=0 且零痕迹。
_xproc_lock_nonblocking = False
_xproc_lock_errors = 0
# 降级原因：None=没降级。区分「环境」（disabled/error）与「机制没生效」（timeout/null/unwired），
# 元测试据此决定 skip 还是红。计数按原因累计，供元测试做**run 级**判定（不只看自己那一次）。
_xproc_lock_degraded = None
_xproc_lock_degraded_counts = {}


def _xproc_lock_enabled() -> bool:
    """逃生口：RAG_TEST_XPROC_LOCK=false 整体退回加锁前的行为。"""
    return os.environ.get("RAG_TEST_XPROC_LOCK", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def local_stack_lock_stats() -> dict:
    """本 worker 迄今各降级原因的次数（供元测试做 run 级判定）。空 = 全程都握着锁。"""
    return dict(_xproc_lock_degraded_counts)


def _xproc_lock_name() -> str:
    """锁名 = **服务端身份**（`@@server_uuid`），不是客户端对 host 的拼法。

    为什么不用 host:port：`_LOCAL_HOSTS` 同时接受 `localhost` 与 `127.0.0.1`，两个
    worktree 各写一个就会算出**两把不同的锁** ⇒ 零互斥，而两边的元测试都照绿
    （各自持有各自的名字）。评审实测：两种拼法的 `@@server_uuid` 相同、而两个名字
    可被同时持有。用 server_uuid 顺带把「同机多套栈/不同端口」也一并解对。
    每进程只多一次 round-trip（结果缓存）。
    """
    global _xproc_lock_name_cached
    if _xproc_lock_name_cached is None:
        with _xproc_lock_connection().cursor() as cur:
            cur.execute("SELECT @@server_uuid")
            uuid = cur.fetchone()[0]
        name = f"rag_test_xproc:{uuid}"
        if len(name) > 64:   # MySQL 用户锁名上限；静默截断=两边各锁各的，宁可炸
            raise RuntimeError(f"跨进程锁名超 64 字符，会被 MySQL 拒绝：{name!r}")
        _xproc_lock_name_cached = name
    return _xproc_lock_name_cached


def _close_xproc_lock_conn():
    """丢弃专用连接前**显式 close**：否则锁的释放要靠 CPython 引用计数及时归零
    （traceback / warning 栈帧都可能多攥一会儿），旧会话攥着锁 ⇒ 新连接与自己抢锁。"""
    global _xproc_lock_conn
    conn, _xproc_lock_conn = _xproc_lock_conn, None
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.close()


def _xproc_lock_connection():
    """锁专用连接（进程内单例，autocommit）。

    🔴 **绝不能用 `opensearch_pipeline.db._get_db_conn()` 的池连接**——实测过两条独立理由：
      1. 池连接一取即 `_begin_txn()` 开显式事务（db.py:54）。整个测试持有它 = 钉死一个
         REPEATABLE READ 读视图，正好毒化 test_kb_db_integration `_seed_row_counts` 的
         「先 commit 再回查」判据（把「已被别人删掉」读成「还在」，判据反向）；
      2. 锁会**泄漏**：实测在池连接上取锁后 `conn.close()` 归还池、再 `_reset_db_pool()`，
         `IS_USED_LOCK` 仍返回该连接 id（PooledDB.close() 只关 idle 连接，碰不到已借出的）。
         锁活过测试边界，还会随连接被复用给任意其它调用方。
    """
    global _xproc_lock_conn
    if _xproc_lock_conn is None:
        import pymysql
        host, port, user, password = _wired_dsn
        # 本地 dev 栈明文连接（与本文件 _try_connect_mysql 同款；ssl 语义扫描门
        # tests/test_rds_ssl_wiring.py 的 SCAN_ROOTS 不含 tests/）
        _xproc_lock_conn = pymysql.connect(
            host=host, port=port, user=user, password=password,
            connect_timeout=5, autocommit=True,
        )
    return _xproc_lock_conn


_SCRATCH_SCHEMA_PREFIXES = ("pg_probe_pytest_", "badge_parity_test_")
_swept = False


def sweep_stale_scratch_schemas():
    """回收崩溃遗留的探针库（`<前缀><pid>`）。幂等，每进程只跑一次。

    背景：这些库改带 pid 后缀是为了**单边免疫**（不依赖对面进程带锁），代价是丢掉了
    「固定名 + 开跑先 DROP」自带的自愈——被 Ctrl-C/SIGKILL 打断就永远没人清。
    🔴 判据必须是「**pid 已不存在**」而不是「名字对得上」：按前缀一刀切会删掉**正在跑**的
    另一个 pytest 进程的库，等于把刚修掉的跨进程互踩原样造回来。
    """
    global _swept
    if _swept or _wired_dsn is None:
        return
    _swept = True
    try:
        with _xproc_lock_connection().cursor() as cur:
            cur.execute("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")
            names = [r[0] for r in cur.fetchall()]
            for name in names:
                for pfx in _SCRATCH_SCHEMA_PREFIXES:
                    if not name.startswith(pfx):
                        continue
                    tail = name[len(pfx):]
                    if not tail.isdigit():
                        continue
                    try:
                        os.kill(int(tail), 0)      # 进程还在 ⇒ 别碰（可能正是对面在跑）
                    except ProcessLookupError:
                        cur.execute(f"DROP DATABASE IF EXISTS `{name}`")
                    except PermissionError:
                        pass                        # 存在但不属本用户 ⇒ 同样别碰
    except Exception as e:   # noqa: BLE001 — 清理是尽力而为，绝不阻断测试
        warnings.warn(f"[local_stack] 陈旧探针库清理失败（不阻断）：{type(e).__name__}: {e}",
                      stacklevel=2)


def local_stack_lock_held() -> bool:
    """当前测试是否**真的**握着跨进程锁——供测试在失败/skip 文案里如实交代
    「刚才有没有锁」。降级是静默的，不写进文案就没人能发现锁从未生效。"""
    return _xproc_lock_held


def local_stack_lock_degraded_reason():
    """本次降级的原因（None=没降级）。'timeout'/'error' 属环境争用，'disabled' 是显式关闭。"""
    return _xproc_lock_degraded


@contextlib.contextmanager
def local_stack_exclusive():
    """把一段真库操作与**其它 pytest 进程**的同组操作互斥开。取不到锁则降级放行。

    失败形态按实测处理：`GET_LOCK` 超时返回 **0**（不是异常）；而连接失效/用户锁死锁
    走的是 pymysql **异常**（OperationalError 2013/2006/3058）——所以整段必须 try 起来，
    只判返回值会让 autouse fixture 在 setup 期抛未捕获异常 ⇒ 组内 119 个用例集体 ERROR。
    """
    global _xproc_lock_held, _xproc_lock_degraded
    global _xproc_lock_nonblocking, _xproc_lock_errors

    def _degrade(reason, msg=None):
        global _xproc_lock_held, _xproc_lock_degraded
        _xproc_lock_held = False
        _xproc_lock_degraded = reason
        _xproc_lock_degraded_counts[reason] = _xproc_lock_degraded_counts.get(reason, 0) + 1
        if msg:
            warnings.warn(f"[local_stack] {msg}", stacklevel=3)

    if not _xproc_lock_enabled():
        _degrade("disabled")
        yield
        return
    if _wired_dsn is None:
        _degrade("unwired")
        yield
        return
    if _xproc_lock_errors >= _XPROC_LOCK_ERROR_GIVEUP:
        _degrade("error")          # 已连败到上限，不再每个用例白付 connect_timeout
        yield
        return

    conn = name = None
    got = None
    try:
        conn = _xproc_lock_connection()
        name = _xproc_lock_name()
        wait = 0 if _xproc_lock_nonblocking else _XPROC_LOCK_TIMEOUT_S
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, %s)", (name, wait))
            got = cur.fetchone()[0]
        _xproc_lock_errors = 0
    except Exception as e:   # noqa: BLE001 — 连接失效等；降级放行，绝不把整组打成 ERROR
        _xproc_lock_errors += 1
        _close_xproc_lock_conn()
        _degrade("error", f"跨进程锁取锁异常（连败第 {_xproc_lock_errors} 次），"
                          f"本次降级无保护：{type(e).__name__}: {e}")
        yield
        return

    if got == 0:
        if not _xproc_lock_nonblocking:
            _xproc_lock_nonblocking = True
            _degrade("timeout",
                     f"跨进程锁等待 {_XPROC_LOCK_TIMEOUT_S}s 超时（另一个 pytest 进程占着，"
                     "或它是不带本 fixture 的老 worktree）——后续改用非阻塞探测，"
                     "对面一空出来即恢复保护。本次无保护。")
        else:
            _degrade("timeout")
        yield
        return
    if got != 1:
        # SQL NULL 等：MySQL 侧出错。不补这一档就会「没持锁 + 没降级原因 + 没告警」，
        # 元测试反而会把它误判成「接线坏了」并红在下一条用例上。
        _degrade("null", f"GET_LOCK 返回 {got!r}（既非 1 也非 0，MySQL 侧发生错误）——本次无保护")
        yield
        return

    _xproc_lock_held = True
    _xproc_lock_degraded = None
    try:
        yield
    finally:
        _xproc_lock_held = False
        try:
            with conn.cursor() as cur:      # 复用 acquire 那条连接，不重新解析全局
                cur.execute("SELECT RELEASE_LOCK(%s)", (name,))
                released = cur.fetchone()[0]
            if released != 1:
                # GET_LOCK 是**可重入计数**锁：漏放一次，计数永不归零 ⇒ 本进程后续
                # 照常拿到锁（看不出异常），而对面每个测试都要等满超时。必须响亮。
                warnings.warn(f"[local_stack] 跨进程锁 RELEASE_LOCK 返回 {released!r}"
                              "（应为 1）——锁可能没放干净，另一进程会被拖住。",
                              stacklevel=2)
        except Exception as e:   # noqa: BLE001
            _close_xproc_lock_conn()
            warnings.warn(f"[local_stack] 跨进程锁释放失败：{type(e).__name__}: {e}",
                          stacklevel=2)


requires_local_db = pytest.mark.skipif(
    not ensure_local_db_wired(), reason=local_db_unavailable_reason()
)

requires_local_opensearch = pytest.mark.skipif(
    not ensure_local_opensearch_wired(), reason=local_opensearch_unavailable_reason()
)
