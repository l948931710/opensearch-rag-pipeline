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

from opensearch_pipeline.config import get_config, is_prod_target

__all__ = ["DestructiveOpBlocked", "assert_destructive_write_allowed", "GuardedBucket"]


class DestructiveOpBlocked(RuntimeError):
    """非生产环境对生产目标的破坏性写操作被守卫拦截。"""


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
        if kind == "search" and cfg.alibaba_vector.table_name.endswith("_stg"):
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
