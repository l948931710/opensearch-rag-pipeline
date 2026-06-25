# -*- coding: utf-8 -*-
"""
env_guard.py — 危险操作运行时守卫（环境防御纵深的第二道防线）

第一道防线是 config.py 加载期的环境标签↔物理目标交叉校验（fail-fast）；
本模块在**真正执行不可逆/污染性写操作之前**再拦一次，覆盖加载期看不到的形态
（例如 ack 放行的 PROD-RO 会话里误触写路径、ctx 级 simulate 覆盖后的真实分支）。

判定规则（assert_destructive_write_allowed）：
  1. RAG_READONLY=true（PROD-RO 会话声明）→ 一律拒绝，无豁免；
  2. environment=production → 放行（DataWorks/SAE 是合法写方）；
  3. 目标未命中生产指纹（本地 docker / locale2e_* / staging 桶表）→ 放行；
  4. 非生产环境 → 生产目标：需要**当日** ack：
       export RAG_DESTRUCTIVE_PROD_ACK=<op>:<YYYY-MM-DD>   （或 *:<date> 放行全部 op）
     日期必须是今天——防止陈年 export 残留长期放行。

守卫只做字符串比较 + 一次 get_config()，无 I/O，可在热路径调用。
失败行为：抛 DestructiveOpBlocked（RuntimeError）——发生在首个网络调用之前，
不存在半删状态；DAG 行级失效锁（2h takeover）天然兜底重入。
"""

import os
from datetime import date

from opensearch_pipeline.config import _STAGING_HA3_SUFFIXES, get_config, is_prod_target

__all__ = ["DestructiveOpBlocked", "EvalModeError",
           "assert_destructive_write_allowed", "GuardedBucket",
           "GuardedDBConnection", "GuardedDBCursor", "is_write_sql",
           "assert_eval_mode", "assert_staging_eval_mode", "is_eval_mode"]


class DestructiveOpBlocked(RuntimeError):
    """非生产环境对生产目标的破坏性写操作被守卫拦截。"""


class EvalModeError(RuntimeError):
    """评测模式 (chunker A/B framework) 前置校验失败。"""


# ── 评测模式标志位(v3.1) ──
# session_store / qa_logger / feedback_handler 等"serving 副作用"模块,在评测
# 期间应该完全短路(不写真 RDS / 不发钉钉 / 不记 audit 日志),否则双 serving 跑
# A/B 时会污染生产 qa_session_log 表 + 触发 webhook 等不可逆操作.
# 评测启动前由 chunker_ab.py / Makefile 设置 RAG_EVAL_MODE=1.

def is_eval_mode() -> bool:
    """评测模式探测(供 session_store/qa_logger/feedback_handler 短路)."""
    return os.environ.get("RAG_EVAL_MODE", "").lower() in ("1", "true", "yes")


# 索引/doc_id 前缀强约束 — 评测灌入只能走这些前缀,生产命名直接 fail-loud
_LOCAL_INDEX_PREFIX_RE = None
_STAGING_INDEX_PREFIX_RE = None

def _compile_prefix_regexes():
    global _LOCAL_INDEX_PREFIX_RE, _STAGING_INDEX_PREFIX_RE
    import re
    _LOCAL_INDEX_PREFIX_RE = re.compile(r"^locale2e_chunkab_[a-z0-9_]+_\d{8}_\d{4}$")
    _STAGING_INDEX_PREFIX_RE = re.compile(r"^staging_chunkab_[a-z0-9_]+_\d{8}_\d{4}$")


def assert_eval_mode(*, index_name: str = None, allow_localhost_only: bool = True) -> None:
    """本地评测模式启动前置守门(chunker A/B Tier 2 用).

    Args:
        index_name: OS 索引名(若提供,强校验 locale2e_chunkab_* 前缀)
        allow_localhost_only: 是否强制 cfg.opensearch.host ∈ {localhost, 127.0.0.1}

    Raises:
        EvalModeError 任一守门失败.

    校验项:
        1. RAG_EVAL_MODE=1 已设(让 session_store/qa_logger 短路)
        2. cfg.environment 不是 production
        3. OS host 是 localhost(防误指生产 HA3)
        4. index_name 匹配 `^locale2e_chunkab_[a-z0-9_]+_\\d{8}_\\d{4}$`(全小写)
    """
    if not is_eval_mode():
        raise EvalModeError(
            "[EVAL GUARD] 评测模式需要 RAG_EVAL_MODE=1 已设."
            "session_store/qa_logger/feedback_handler 会写真 RDS 污染生产数据.")
    cfg = get_config()
    if cfg.environment == "production":
        raise EvalModeError(
            f"[EVAL GUARD] environment={cfg.environment} 不允许跑评测(必须本地/staging).")
    if allow_localhost_only:
        host = cfg.opensearch.host
        if host not in ("localhost", "127.0.0.1"):
            raise EvalModeError(
                f"[EVAL GUARD] OS host={host} 不是 localhost,Tier 2 评测必须本地 docker.")
    if index_name is not None:
        if _LOCAL_INDEX_PREFIX_RE is None:
            _compile_prefix_regexes()
        if not _LOCAL_INDEX_PREFIX_RE.match(index_name):
            raise EvalModeError(
                f"[EVAL GUARD] 索引名 {index_name!r} 不符合 locale2e_chunkab_<arm>_<YYYYMMDD>_<HHMM> 全小写前缀."
                f"评测索引必须严格前缀隔离生产命名空间.")


def assert_staging_eval_mode(*, index_name: str = None) -> None:
    """Staging 评测模式启动前置守门(chunker A/B Tier 3 staging 用).

    Args:
        index_name: HA3/OS 索引名(若提供,强校验 staging_chunkab_* 前缀)

    Raises:
        EvalModeError 任一守门失败.

    校验项:
        1. RAG_EVAL_MODE=1 + RAG_ENV=staging
        2. cfg.environment == 'staging'
        3. 写入目标资源都带 staging 后缀/前缀(RDS _stg / OSS -staging / HA3 _stg)
        4. index_name 匹配 `^staging_chunkab_[a-z0-9_]+_\\d{8}_\\d{4}$`(全小写)
    """
    if not is_eval_mode():
        raise EvalModeError("[EVAL GUARD STAGING] 需要 RAG_EVAL_MODE=1 已设.")
    if os.environ.get("RAG_ENV") != "staging":
        raise EvalModeError("[EVAL GUARD STAGING] 需要 RAG_ENV=staging 已设.")
    cfg = get_config()
    if cfg.environment != "staging":
        raise EvalModeError(
            f"[EVAL GUARD STAGING] environment={cfg.environment} 不是 staging.")
    # 校验 staging 资源后缀(复用 assert_destructive_write_allowed 的 staging 判定)
    if not cfg.rds.database.endswith("_stg"):
        raise EvalModeError(
            f"[EVAL GUARD STAGING] RDS database={cfg.rds.database} 不带 _stg 后缀.")
    if not cfg.oss.bucket_name.endswith("-staging"):
        raise EvalModeError(
            f"[EVAL GUARD STAGING] OSS bucket={cfg.oss.bucket_name} 不带 -staging 后缀.")
    # HA3 table_name 接受 _stg / _s 后缀（_stg 建表失败后改用 fuling_kb_chunks_s,
    # 与 config._STAGING_HA3_SUFFIXES 对齐）或 staging_ 前缀
    if cfg.alibaba_vector.table_name and not (
            cfg.alibaba_vector.table_name.endswith(_STAGING_HA3_SUFFIXES)
            or cfg.alibaba_vector.table_name.startswith("staging_")):
        raise EvalModeError(
            f"[EVAL GUARD STAGING] HA3 table={cfg.alibaba_vector.table_name} 不带 _stg/_s 后缀/staging_ 前缀.")
    if index_name is not None:
        if _STAGING_INDEX_PREFIX_RE is None:
            _compile_prefix_regexes()
        if not _STAGING_INDEX_PREFIX_RE.match(index_name):
            raise EvalModeError(
                f"[EVAL GUARD STAGING] 索引名 {index_name!r} 不符合 staging_chunkab_<arm>_<YYYYMMDD>_<HHMM> 全小写前缀.")


def _ack_today(ack: str) -> bool:
    """ack 的日期部分是否为今天（忽略具体 op，但 op 段必须非空——拒 ':2026-..' 这类
    退化/typo ack 静默放行）。连接层兜底守卫与 _pool_readonly_declared 共用：它们拿不到
    语义 op，任意**合法** op 的当日 ack 即视为"今天已显式授权对生产写"。"""
    if not ack or ":" not in ack:
        return False
    ack_op, _, ack_date = ack.partition(":")
    if not ack_op.strip():
        return False
    return ack_date == date.today().isoformat()


def _ack_matches(ack: str, op: str) -> bool:
    """ack 格式 '<op>:<YYYY-MM-DD>' 或 '*:<YYYY-MM-DD>'；日期必须为今天。"""
    if not ack or ":" not in ack:
        return False
    ack_op, _, ack_date = ack.partition(":")
    if ack_op not in (op, "*"):
        return False
    return ack_date == date.today().isoformat()


def assert_destructive_write_allowed(op: str, target: str, *, kind: str,
                                     quiet: bool = False, any_ack: bool = False) -> None:
    """在不可逆/污染性写操作前调用。

    Args:
        op:      操作名（进 ack 与报错信息），如 'deactivate_old_chunks'、'search_delete'
        target:  物理目标标识（host / endpoint / bucket 名）
        kind:    指纹类别，∈ {'rds', 'search', 'oss'}
        quiet:   True 时抑制 ack 放行的 stdout 打印（连接层兜底守卫每条写语句都会过，
                 用 quiet 避免刷屏；节点级显式调用保持 quiet=False 以保留一条可见放行记录）。
        any_ack: True 时 ack 按"当日任意 op"匹配（连接层兜底守卫用：它只看到裸 SQL，
                 拿不到语义 op，故接受任何当日 ack；节点级显式调用保持 False = 按精确 op）。
    """
    cfg = get_config()
    if cfg.readonly:
        raise DestructiveOpBlocked(
            f"[ENV GUARD] RAG_READONLY=true（PROD-RO 会话）下拒绝写操作 {op} -> {target}。"
            f"该会话被声明为只读，无豁免；写操作请使用对应环境的 RAG_ENV 启动。")
    if cfg.environment == "production":
        return
    if cfg.environment == "staging":
        # STAGING 层共享生产实例但写 _stg 后缀资源——这是合法形态
        # （后缀约束已在 config 加载期被 RAG_ENV=staging 强校验）
        if kind == "rds" and cfg.rds.database.endswith("_stg"):
            return
        # HA3 staging 表接受 _stg 或 _s 后缀（_stg 建表失败后改用 _s,
        # 与 config._STAGING_HA3_SUFFIXES 单一来源对齐）
        if kind == "search" and cfg.alibaba_vector.table_name.endswith(_STAGING_HA3_SUFFIXES):
            return
        if kind == "oss" and cfg.oss.bucket_name.endswith("-staging"):
            return
    if not is_prod_target(kind, target):
        return
    ack = os.environ.get("RAG_DESTRUCTIVE_PROD_ACK", "")
    if _ack_today(ack) if any_ack else _ack_matches(ack, op):
        if not quiet:
            print(f"    !! [ENV GUARD OVERRIDE] {op} -> {target} 已被 RAG_DESTRUCTIVE_PROD_ACK={ack} 显式放行")
        return
    raise DestructiveOpBlocked(
        f"[ENV GUARD] 拒绝在 environment={cfg.environment} 下对生产目标 {target!r} 执行 {op}。"
        f"确需操作（你清楚自己在做什么）：export RAG_DESTRUCTIVE_PROD_ACK={op}:{date.today().isoformat()}")


def assert_metadata_write_allowed(op: str, target: str, *, kind: str = "rds") -> None:
    """【轻量】元数据写守卫——用于 kb 自助上传的 register（写 document_meta/version 行）等
    **可逆、非污染性**的写，与不可逆 HA3 删除级别的 assert_destructive_write_allowed【分级隔离】。

    ⚠️ 故意使用【独立】的放行开关 RAG_METADATA_PROD_ACK（≠ RAG_DESTRUCTIVE_PROD_ACK）：
    一个元数据写的临时放行，绝不能顺带授权一次 HA3 删除，反之亦然。

    规则与 destructive 一致的两点：RAG_READONLY=true（PROD-RO）一律拒绝、无豁免；
    生产/预发环境的正常写直接放行。差异：非生产环境写生产目标时，认 RAG_METADATA_PROD_ACK。
    """
    cfg = get_config()
    if cfg.readonly:
        raise DestructiveOpBlocked(
            f"[ENV GUARD] RAG_READONLY=true（PROD-RO 会话）下拒绝写操作 {op} -> {target}。")
    if cfg.environment in ("production", "staging"):
        return
    if not is_prod_target(kind, target):
        return
    ack = os.environ.get("RAG_METADATA_PROD_ACK", "")
    if _ack_matches(ack, op) or _ack_today(ack):
        print(f"    !! [ENV GUARD OVERRIDE] {op} -> {target} 已被 RAG_METADATA_PROD_ACK={ack} 放行")
        return
    raise DestructiveOpBlocked(
        f"[ENV GUARD] 拒绝在 environment={cfg.environment} 下对生产目标 {target!r} 执行元数据写 {op}。"
        f"确需操作：export RAG_METADATA_PROD_ACK={op}:{date.today().isoformat()}")


class GuardedBucket:
    """OSS Bucket 写守卫代理：拦截 put_*/delete_*，读与签名透传。

    正常的本地形态是 simulate_oss=true（根本不会构造真实 bucket）——
    本代理只防"本地误设 simulate_oss=false + 指向生产桶"的配置漂移。
    staging 桶（-staging 后缀）与其他非指纹桶不受影响。
    """

    _WRITE_METHODS = ("put_object", "put_object_from_file", "delete_object",
                      "batch_delete_objects", "append_object")

    def __init__(self, bucket, bucket_name: str):
        self._bucket = bucket
        self._bucket_name = bucket_name

    def __getattr__(self, name):
        attr = getattr(self._bucket, name)
        if name in self._WRITE_METHODS:
            def _guarded(*args, **kwargs):
                key = str(args[0]) if args else ""
                assert_destructive_write_allowed(
                    f"oss_write:{key.split('/', 1)[0] or 'root'}",
                    self._bucket_name, kind="oss")
                return attr(*args, **kwargs)
            return _guarded
        return attr


# ── 裸 cursor 写守卫（P4：让 RAG_READONLY / 非生产→生产策略对**任何** cursor.execute 生效）──
# 背景：assert_destructive_write_allowed 此前只在少数节点显式调用（write_chunk_meta /
# acquire_index_lock / deactivate / push / update_index_status）。register_metadata、
# classify 冻结维护 UPDATE、detect_sensitive、redact、publish_to_rag_ready 等仍是"裸
# cursor"——RAG_READONLY=true 的 PROD-RO 会话照样能从这些路径写穿。本守卫在**连接层**
# 兜底：_get_db_conn 返回 GuardedDBConnection，其 cursor 的 execute/executemany 只要识别到
# 写语句就过同一道 assert_destructive_write_allowed（单一事实源，无策略漂移）。
#
# 读语句（SELECT/SHOW/...）一律直通——serving 的 retriever/api/dingtalk_identity 读路径
# 与 ingest 写路径**共用**这个连接池，连接层守卫绝不能拦读。判定按"首关键字白名单"：
# 只有动词命中写/DDL 集才过守卫，未知/读动词放行（对可用性 fail-open，对写 fail-loud）。

_SQL_WRITE_VERBS = frozenset({
    "INSERT", "UPDATE", "DELETE", "REPLACE", "TRUNCATE",
    "DROP", "ALTER", "CREATE", "RENAME", "GRANT", "REVOKE",
    "LOAD", "MERGE",
})


def _leading_sql_keyword(sql: str) -> str:
    """取 SQL 首个关键字（大写）。剥离前导空白/注释，并穿透几类退化前缀拿到真实动词：
      - `-- 行注释` / `/* 惰性块注释 */`：剥离（MySQL 不执行）。
      - `/*!nnnnn ... */` **可执行**注释：MySQL 真的会执行——剥掉 `/*!` 与可选版本号，
        继续看里面的真实动词（否则 `/*!40001 DELETE ... */` 会被当成空关键字=读而漏守卫）。
      - 前导 `(` / `;`：跳过（`(SELECT ...)` 是读、`(INSERT ...)` 是写——穿透到真实动词，
        既不把括号读误判为写，也不把括号写漏成读）。
    注意：CTE 前缀 DML（`WITH cte AS (...) DELETE ...`，MySQL 8）仍返回 'WITH'→判为读——
    这是 leading-keyword 模型的已知盲点，但本库写路径无 CTE-DML（且 PROD-RO/dev→prod 还有
    池层 SESSION READ ONLY 兜底），故仅作注释留存，不做易误伤读的全句扫描。
    """
    s = (sql or "").lstrip()
    while s:
        if s.startswith("--"):
            nl = s.find("\n")
            s = s[nl + 1:].lstrip() if nl != -1 else ""
        elif s.startswith("/*!"):
            s = s[3:].lstrip()
            j = 0
            while j < len(s) and s[j].isdigit():  # 可选的版本号 /*!40001
                j += 1
            s = s[j:].lstrip()
        elif s.startswith("/*"):
            end = s.find("*/")
            s = s[end + 2:].lstrip() if end != -1 else ""
        elif s[0] in "(;":
            s = s[1:].lstrip()
        else:
            break
    i = 0
    while i < len(s) and (s[i].isalpha() or s[i] == "_"):
        i += 1
    return s[:i].upper()


def is_write_sql(sql: str) -> bool:
    """首关键字是否属于写/DDL 动词（决定是否过破坏性写守卫）。读语句返回 False。"""
    return _leading_sql_keyword(sql) in _SQL_WRITE_VERBS


class GuardedDBCursor:
    """包裹真实 DB cursor：写语句过 assert_destructive_write_allowed，读语句直通。

    同时支持上下文管理（`with conn.cursor() as c:`）与裸用（`c = conn.cursor(DictCursor)`）。
    execute/executemany/callproc 三个会写库的入口都过守卫；其余（fetch*/rowcount/...）直通。
    """

    def __init__(self, cursor, conn: "GuardedDBConnection"):
        self._cursor = cursor
        self._conn = conn  # 反向引用：守卫逻辑与"已放行"记忆化都集中在连接上（单点）

    def execute(self, query, *args, **kwargs):
        self._conn._guard_sql(query)
        return self._cursor.execute(query, *args, **kwargs)

    def executemany(self, query, *args, **kwargs):
        self._conn._guard_sql(query)
        return self._cursor.executemany(query, *args, **kwargs)

    def callproc(self, procname, *args, **kwargs):
        # 存储过程可能写库，且无法从调用静态判定——保守按"写"过守卫
        #（当前代码库无 callproc 调用，纯属堵 __getattr__ 直通的兜底口子）。
        self._conn._guard_callproc(procname)
        return self._cursor.callproc(procname, *args, **kwargs)

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._cursor.__exit__(exc_type, exc, tb)

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        # fetchone/fetchall/fetchmany/rowcount/lastrowid/description/close/...
        return getattr(self._cursor, name)


class GuardedDBConnection:
    """包裹连接池连接：cursor() 返回 GuardedDBCursor，其余（commit/rollback/close/ping/...）直通。

    只防"裸 cursor 写穿"——读直通，真实写入语义不变。target 是 RDS host，供守卫做指纹比对与报错。
    写守卫逻辑集中在这里（_guard_sql/_guard_callproc），被 cursor 的 execute/executemany/callproc
    与连接自身的 query()（pymysql 底层 COM_QUERY 写口子）共用，确保所有写入口走同一道策略。
    """

    def __init__(self, conn, target: str):
        # 直接写 __dict__，避免触发下面的 __getattr__（它会读 self._conn）。
        self.__dict__["_conn"] = conn
        self.__dict__["_target"] = target
        self.__dict__["_write_guard_passed"] = False

    def _guard_sql(self, query):
        # 记忆化：一个连接内策略决策恒定（config 缓存、date 进程内稳定、ack env 稳定），
        # 首条写放行后即不再重复评估——把每语句开销与（非 quiet 场景下的）打印都收敛到一次。
        if self._write_guard_passed:
            return
        if is_write_sql(str(query)):
            assert_destructive_write_allowed(
                "rds_write", self._target, kind="rds", quiet=True, any_ack=True)
            self.__dict__["_write_guard_passed"] = True

    def _guard_callproc(self, procname):
        if self._write_guard_passed:
            return
        assert_destructive_write_allowed(
            f"rds_callproc:{procname}", self._target, kind="rds", quiet=True, any_ack=True)
        self.__dict__["_write_guard_passed"] = True

    def cursor(self, *args, **kwargs):
        return GuardedDBCursor(self._conn.cursor(*args, **kwargs), self)

    def query(self, sql, *args, **kwargs):
        # pymysql.Connection.query —— cursor.execute 内部也走它；显式 conn.query() 也要过守卫。
        self._guard_sql(sql)
        return self._conn.query(sql, *args, **kwargs)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._conn.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self.__dict__["_conn"], name)
