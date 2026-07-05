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

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 契约键（rag_runtime_contract.contract_key）
CONTRACT_EMBEDDING_MODEL = "embedding_model"
CONTRACT_EMBEDDING_DIMENSION = "embedding_dimension"


def _op_db() -> str:
    """运营库名（rag_runtime_contract 契约行所在库；STAGING 自动 _stg）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.operation_database


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
