# -*- coding: utf-8 -*-
"""runtime_contract.py — 摄取↔服务嵌入模型契约行（盲区审计 P2-8，schema/018）。

HA3 文档没有模型戳（HA3 schema 加字段须整表重建 —— user-gated 决策，代码侧不动
to_ha3_doc），摄取与服务两平面各自从 env 解析 embedding 模型：v4→v5 这类**同维**升级
一旦两边配置漂移，查询向量与库内向量出自不同模型，相似度是垃圾但没有任何报错。
代码侧能落的最大程度是 RDS 契约行：

  * 写侧（pipeline_nodes.node_generate_embeddings）：每次【真实】嵌入运行成功后
    UPSERT ``embedding_model`` / ``embedding_dimension`` 两行（simulate 跳过；
    fail-open —— 契约行写失败绝不影响摄取主流程）。
  * 读侧（api._lifespan）：serving 启动时读契约行与 get_config().embedding 比对；
    失配 → logger.critical + api._EMBEDDING_CONTRACT_MISMATCH=True → /api/ready 报
    degraded/503。刻意【不 fail-closed 拒绝启动】：本地联调/半配置 staging 会被误伤，
    503 语义由 readiness 探针承载（负载均衡摘流量），失配实例不再吃新流量。
  * 表未 apply（1146）/读失败 → 静默跳过 —— 未迁移环境零影响。

表在 fuling_operation（schema/018；fuling_knowledge 侧另有一份同名 KV 供 ops 心跳，
见 queue_monitor.write_heartbeat）。
"""

import json
import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 契约键（rag_runtime_contract.contract_key）
CONTRACT_EMBEDDING_MODEL = "embedding_model"
CONTRACT_EMBEDDING_DIMENSION = "embedding_dimension"
# ACL 姿态见证（doc-update-notify，2026-08-04）：serving 把自己**当前生效**的 ACL flag
# 姿态盖章到知识库侧 KV，供离线批处理（DataWorks 通知节点）断言"我的判定与线上一致"。
CONTRACT_ACL_POSTURE = "acl_posture"


def _op_db() -> str:
    """运营库名（rag_runtime_contract 契约行所在库；STAGING 自动 _stg）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.operation_database


def _kb_db() -> str:
    """知识库库名（ACL 姿态见证行所在库——与它证明的 ACL 权威表同库；STAGING 自动 _stg）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.database


def upsert_embedding_contract(model: Any, dimension: Any) -> bool:
    """stage-3 真实嵌入运行成功后 UPSERT 契约行（fail-open：失败仅 warning，返回 False）。

    每次真实运行都刷新 —— updated_at 顺带成为"最近一次真实嵌入"的时间锚点。
    """
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {_op_db()}.rag_runtime_contract (contract_key, contract_value)"
                    " VALUES (%s, %s), (%s, %s)"
                    " ON DUPLICATE KEY UPDATE contract_value = VALUES(contract_value)",
                    (CONTRACT_EMBEDDING_MODEL, str(model),
                     CONTRACT_EMBEDDING_DIMENSION, str(dimension)))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — 契约行是辅助治理，绝不阻断摄取
        logger.warning("embedding 契约行 UPSERT 失败（fail-open；schema/018 未 apply?）: %s", e)
        return False


def check_embedding_contract(cfg: Optional[Any] = None) -> Optional[str]:
    """serving 启动时比对契约行与本进程 embedding 配置。

    Returns:
        失配 → 人读描述字符串（调用方 logger.critical + readiness 降级）；
        匹配 / simulate / 无契约行（从未真实摄取）/ 表缺失 / 读失败 → None（零影响）。

    cfg 参数仅供测试注入；缺省走 get_config()。
    """
    try:
        if cfg is None:
            from opensearch_pipeline.config import get_config
            cfg = get_config()
        # simulate（含粒度 simulate_db）：无真实 RDS 可比，跳过
        if getattr(cfg, "simulate", False) or getattr(cfg, "simulate_db", False):
            return None
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT contract_key, contract_value FROM {_op_db()}.rag_runtime_contract"
                    " WHERE contract_key IN (%s, %s)",
                    (CONTRACT_EMBEDDING_MODEL, CONTRACT_EMBEDDING_DIMENSION))
                rows = cur.fetchall() or []
        finally:
            conn.close()
        contract: Dict[str, Any] = {}
        for r in rows:
            if isinstance(r, dict):          # DictCursor（prod_access 形态）兼容
                contract[r.get("contract_key")] = r.get("contract_value")
            else:
                contract[r[0]] = r[1]
        mismatches = []
        want_model = contract.get(CONTRACT_EMBEDDING_MODEL)
        want_dim = contract.get(CONTRACT_EMBEDDING_DIMENSION)
        if want_model and str(want_model) != str(cfg.embedding.model):
            mismatches.append(f"model 索引侧={want_model} vs 服务侧={cfg.embedding.model}")
        if want_dim and str(want_dim) != str(cfg.embedding.dimension):
            mismatches.append(f"dimension 索引侧={want_dim} vs 服务侧={cfg.embedding.dimension}")
        return "; ".join(mismatches) if mismatches else None
    except Exception as e:  # noqa: BLE001 — 表未 apply/读失败：如实缺席，不误报
        logger.debug("embedding 契约比对跳过（表未 apply/读失败，非致命）: %s", e)
        return None


# ── ACL 姿态见证（doc-update-notify）────────────────────────────────────────────
#
# 为什么需要它：离线通知节点要用 `acl_policy.can_read_doc` 枚举受众，而该函数的两个
# **必填**姿态参数（grant/enforce）决定了 node 模式文档是"按授权判定"还是"无条件 DENY"。
# 节点的 env 是 DataWorks 粘贴时静态注入的，serving 的 env 在 SAE 控制台另一面维护 ——
# Sam 在控制台翻一次 flag，两侧就永久脱钩且**无任何检测**：
#   · 节点比 serving 严（节点 GRANT=false）⇒ 受众恒空 ⇒ 整批语料静默零通知；
#   · 节点比 serving 宽（节点 GRANT=true 而线上已回滚）⇒ **通知面 ⊄ 检索面**，
#     把文档标题推给线上根本读不到它的人（钉钉已发不可撤回）。
# 所以"两边 env 填一样"这种约定是愿望不是机制。见证行把它变成机制：serving 盖章自己
# 当前生效的姿态，节点**只用见证值判定**、并对缺失/过期/不符整轮 HOLD。
#
# ⚠️ MySQL 陷阱（这行注释是承重的）：`ON DUPLICATE KEY UPDATE contract_value=VALUES(...)`
#    在新值与旧值**相同**时，MySQL 视该行未变 ⇒ `ON UPDATE CURRENT_TIMESTAMP` 不触发、
#    updated_at 不动。姿态恰恰是长期不变的，若靠隐式 updated_at 做新鲜度，见证会在
#    姿态稳定时"越放越旧"直到被判过期 —— 功能自杀。故必须**显式** `updated_at = NOW()`。
_STAMP_THROTTLE_SECONDS = 3600      # 见证刷新节流：serving 每进程至多每小时盖章一次
_stamp_lock = threading.Lock()
_last_stamp_ts = 0.0


def current_acl_posture(cfg: Optional[Any] = None) -> Dict[str, Any]:
    """本进程**当前生效**的 ACL 姿态（判定口径的单一来源，写侧与读侧共用）。"""
    if cfg is None:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
    from opensearch_pipeline.dingtalk_identity import (
        _acl_ancestry_enabled, _acl_cache_ttl_seconds,
    )
    return {
        "grant": bool(getattr(cfg, "node_acl_grant", False)),
        "enforce": bool(getattr(cfg, "node_acl_enforce", True)),
        "ancestry": bool(_acl_ancestry_enabled()),
        "ttl": int(_acl_cache_ttl_seconds()),
    }


def stamp_acl_posture(cfg: Optional[Any] = None, *, force: bool = False) -> bool:
    """serving 盖章当前 ACL 姿态（fail-open；simulate 跳过）。

    调用点：`api._lifespan` 启动时 force=True；`/api/ready` 探针上节流调用（平台按秒级
    探活 ⇒ 见证行天然保鲜，无需自建定时器）。返回 True=本次真的写了。
    """
    global _last_stamp_ts
    try:
        if cfg is None:
            from opensearch_pipeline.config import get_config
            cfg = get_config()
        if getattr(cfg, "simulate", False) or getattr(cfg, "simulate_db", False):
            return False
        now = time.time()
        with _stamp_lock:
            if not force and (now - _last_stamp_ts) < _STAMP_THROTTLE_SECONDS:
                return False
            _last_stamp_ts = now
        payload = json.dumps(current_acl_posture(cfg), sort_keys=True, separators=(",", ":"))
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {_kb_db()}.rag_runtime_contract (contract_key, contract_value)"
                    " VALUES (%s, %s)"
                    # 显式刷新 updated_at —— 见上方 MySQL 陷阱注释，缺它见证会假性过期
                    " ON DUPLICATE KEY UPDATE contract_value = VALUES(contract_value),"
                    " updated_at = NOW()",
                    (CONTRACT_ACL_POSTURE, payload))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — 见证是治理设施，绝不阻断 serving 启动/探活
        logger.warning("ACL 姿态见证盖章失败（fail-open；schema/018 未 apply?）: %s", e)
        return False


def read_acl_posture(cursor=None) -> Optional[Dict[str, Any]]:
    """读 ACL 姿态见证（离线批处理用）。

    Returns:
        {"grant","enforce","ancestry","ttl","age_seconds"} —— 见证存在且可解析；
        None —— 无行 / 表缺失 / 读失败 / 值不可解析。**调用方必须把 None 当"姿态未知"
        整轮 HOLD 处理，绝不可退回自身 env 猜测**（那正是本机制要消灭的脱钩面）。

    cursor 可传入复用（tuple 游标契约，与 access_grants 同）；缺省自建只读连接。
    """
    def _fetch(cur) -> Optional[tuple]:
        cur.execute(
            f"SELECT contract_value, TIMESTAMPDIFF(SECOND, updated_at, NOW())"
            f" FROM {_kb_db()}.rag_runtime_contract WHERE contract_key = %s",
            (CONTRACT_ACL_POSTURE,))
        return cur.fetchone()

    try:
        if cursor is not None:
            row = _fetch(cursor)
        else:
            from opensearch_pipeline.db import _get_db_conn
            conn = _get_db_conn()
            try:
                with conn.cursor() as cur:
                    row = _fetch(cur)
            finally:
                conn.close()
        if not row:
            return None
        if isinstance(row, dict):            # DictCursor（prod_access 形态）兼容
            raw, age = row.get("contract_value"), list(row.values())[-1]
        else:
            raw, age = row[0], row[1]
        data = json.loads(raw)
        if not isinstance(data, dict) or "grant" not in data or "enforce" not in data:
            logger.warning("ACL 姿态见证格式异常，按姿态未知处理: %r", raw)
            return None
        data["age_seconds"] = int(age if age is not None else 0)
        return data
    except Exception as e:  # noqa: BLE001 — 读不到 ⇒ 姿态未知（调用方 HOLD），绝不猜
        logger.warning("ACL 姿态见证读取失败（按姿态未知处理）: %s", e)
        return None
