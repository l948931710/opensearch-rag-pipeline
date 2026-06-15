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


def _ack_matches(ack: str, op: str) -> bool:
    """ack 格式 '<op>:<YYYY-MM-DD>' 或 '*:<YYYY-MM-DD>'；日期必须为今天。"""
    if not ack or ":" not in ack:
        return False
    ack_op, _, ack_date = ack.partition(":")
    if ack_op not in (op, "*"):
        return False
    return ack_date == date.today().isoformat()


def assert_destructive_write_allowed(op: str, target: str, *, kind: str) -> None:
    """在不可逆/污染性写操作前调用。

    Args:
        op:     操作名（进 ack 与报错信息），如 'deactivate_old_chunks'、'search_delete'
        target: 物理目标标识（host / endpoint / bucket 名）
        kind:   指纹类别，∈ {'rds', 'search', 'oss'}
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
    if _ack_matches(ack, op):
        print(f"    !! [ENV GUARD OVERRIDE] {op} -> {target} 已被 RAG_DESTRUCTIVE_PROD_ACK={ack} 显式放行")
        return
    raise DestructiveOpBlocked(
        f"[ENV GUARD] 拒绝在 environment={cfg.environment} 下对生产目标 {target!r} 执行 {op}。"
        f"确需操作（你清楚自己在做什么）：export RAG_DESTRUCTIVE_PROD_ACK={op}:{date.today().isoformat()}")


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
