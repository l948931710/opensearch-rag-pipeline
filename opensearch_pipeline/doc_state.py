# -*- coding: utf-8 -*-
"""doc_state.py — 文档/版本状态判定的共享谓词（无 FastAPI 依赖，批处理与 serving 共用）。

为什么单列一个模块:`_kb_version_quarantined` 原住在 `routes/kb_console.py`,而 DataWorks
节点(py3.7、只解压 `opensearch_pipeline/`、依赖集仅 PyMySQL/DBUtils/requests)**不能** import
routes —— 那会拖进整个 FastAPI 依赖树。隔离判定又必须是**唯一权威**(OR 语义一旦分叉,
gate-only 隔离件就会在某条路径上被当成正常件放行)⇒ 下沉到这里,kb_console re-export
保持既有 import 面不变。

⚠️ **SQL 侧必须用 NULL 安全形**(评审 R2-A4):`publish_status`/`gate_status` 两列可空,
朴素的 `NOT (publish_status='QUARANTINED' OR gate_status='quarantined')` 在任一列为 NULL 时
整表达式求值为 NULL → WHERE 按非真处理 → **正常行被静默丢出结果集**(漏通知且无留痕)。
故对外只暴露 `QUARANTINE_EXCLUDE_SQL` 这一份带 COALESCE 的片段,调用方不要手写。
"""

# 隔离两轴的字面量(单一来源;spot_checker/cost_breaker 的追溯隔离写 publish_status,
# stage-2 PII 门写 gate_status —— 任一命中即隔离)。
_PUBLISH_QUARANTINED = "QUARANTINED"
_GATE_QUARANTINED = "quarantined"


def version_quarantined(publish_status, gate_status) -> bool:
    """隔离判定唯一权威(codex 共识 2026-08-02):publish_status='QUARANTINED' OR
    gate_status='quarantined',两字段任一命中即隔离。None/空值安全。"""
    return (str(publish_status or "").upper() == _PUBLISH_QUARANTINED
            or str(gate_status or "").lower() == _GATE_QUARANTINED)


def quarantine_exclude_sql(alias: str = "dv") -> str:
    """返回「排除隔离件」的 NULL 安全 WHERE 片段(不含前导 AND)。

    用法:`f"... WHERE x=1 AND {quarantine_exclude_sql('dv_new')}"`。
    COALESCE 是承重的:见模块 docstring —— 少了它,publish/gate 任一为 NULL 的**正常行**
    会被三值逻辑丢掉,而不是被保留。
    """
    return (f"NOT (UPPER(COALESCE({alias}.publish_status, '')) = '{_PUBLISH_QUARANTINED}'"
            f" OR LOWER(COALESCE({alias}.gate_status, '')) = '{_GATE_QUARANTINED}')")


# 便捷常量:默认别名 dv 的片段(绝大多数查询用它)。
QUARANTINE_EXCLUDE_SQL = quarantine_exclude_sql("dv")
