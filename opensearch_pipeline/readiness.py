# -*- coding: utf-8 -*-
"""
readiness.py — /api/ready 的扩展探针（P0-6 重评审计四项）。

审计点名 readiness 缺的四项在此补齐，api.py 只做接线：
① agent 表族存在（RAG_AGENT_ENABLE 开时判定；off → skipped——不依赖就不判）；
② ontology 表族存在（RAG_ONTOLOGY_ENABLE 同理）；
③ schema_migrations checksum 漂移（台账 vs 本地 schema/ 文件 sha256——镜像里带
   schema/，部署了"改过内容的同名迁移"当场可见）；
④ DashScope 模型在线（RAG_READY_DASHSCOPE_LIVE 开时才发真探针——models 列表轻量
   免费；默认仍 config-only，不烧配额）。

探针纪律：全部 TTL 缓存（默认 60s/300s，防 SAE 每秒探活放大成 DB/外网风暴）；
任何异常报状态词不抛出、不外泄原文（与 /api/ready 既有 P2-05 纪律一致）。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# TTL 缓存：key → (expires_epoch, value)
_cache: Dict[str, Tuple[float, str]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl_s: float, compute) -> str:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    val = compute()
    with _cache_lock:
        _cache[key] = (now + ttl_s, val)
    return val


def _reset_cache() -> None:
    """测试钩子。"""
    with _cache_lock:
        _cache.clear()


def _flag_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _tables_exist(dbname: str, tables: List[str]) -> str:
    """information_schema 表存在性 → ok / missing / error。"""
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                marks = ",".join(["%s"] * len(tables))
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    f"WHERE table_schema=%s AND table_name IN ({marks})",
                    (dbname, *tables))
                row = cur.fetchone()
                n = row[0] if not isinstance(row, dict) else list(row.values())[0]
        finally:
            conn.close()
        return "ok" if int(n) == len(tables) else "missing"
    except Exception as e:   # noqa: BLE001 — 探针报告不抛出
        logger.warning("readiness: 表存在性探针失败（%s）: %s", dbname, e)
        return "error"


# agent/ontology 表族的判定集：端点/运行时首次触库就要的最小集合——缺任何一张，
# flag-on 实例的对应功能必然 500，就绪判定应把它摘出。
_AGENT_TABLES = ["agent_run", "agent_checkpoint", "agent_step", "tool_invocation",
                 "approval_request", "approval_decision", "tool_registry"]
_ONTOLOGY_TABLES = ["ontology_object", "ontology_identifier", "ontology_link",
                    "ontology_resolution_case", "ontology_stewardship"]


def agent_tables_status() -> str:
    if not _flag_on("RAG_AGENT_ENABLE"):
        return "skipped"
    from opensearch_pipeline.config import get_config
    db = get_config().rds.operation_database
    return _cached("agent_tables", 60, lambda: _tables_exist(db, _AGENT_TABLES))


def ontology_tables_status() -> str:
    if not _flag_on("RAG_ONTOLOGY_ENABLE"):
        return "skipped"
    from opensearch_pipeline.config import get_config
    db = get_config().rds.ontology_database
    return _cached("ontology_tables", 60, lambda: _tables_exist(db, _ONTOLOGY_TABLES))


def tool_registry_status() -> str:
    """工具注册表可构建（agent flag 开时）：代码内声明注册若 import/构建即炸，
    agent 首个请求必 500——就绪期暴露。ok(N)=可用工具数。"""
    if not _flag_on("RAG_AGENT_ENABLE"):
        return "skipped"

    def _compute() -> str:
        try:
            from opensearch_pipeline.agent_tools import build_default_registry
            reg = build_default_registry()
            n = len(getattr(reg, "_latest", {}) or {})
            return f"ok({n})" if n else "empty"
        except Exception as e:   # noqa: BLE001
            logger.warning("readiness: tool registry 构建失败: %s", e)
            return "error"

    return _cached("tool_registry", 300, _compute)


# ── schema_migrations checksum ────────────────────────────────────────────────
_MIG_RE = re.compile(r"^\d+[a-z]?_.+\.sql$")


def _schema_dir() -> Optional[Path]:
    """定位 schema/：仓库根（开发机）或镜像内与包同级（Dockerfile COPY schema/）。"""
    for base in (Path(__file__).resolve().parent.parent,):
        d = base / "schema"
        if d.is_dir():
            return d
    return None


def _schema_drift_once() -> str:
    """三库台账 checksum vs 本地 schema/ 文件 sha256。
    - 台账里有该文件名 且 本地存在 且 sha256 不同 → drift（同名内容漂移，032 语义）；
    - 台账无 checksum 列/表不存在（未迁移环境）→ unavailable；
    - 本地无 schema/ 目录（旧镜像）→ no_local_files。
    只比【台账里记过的】文件——本地新增未 apply 的迁移不是漂移。"""
    d = _schema_dir()
    if d is None:
        return "no_local_files"
    local: Dict[str, str] = {}
    try:
        for f in d.iterdir():
            if f.is_file() and _MIG_RE.match(f.name):
                local[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
    except Exception as e:   # noqa: BLE001
        logger.warning("readiness: 本地 schema 目录读取失败: %s", e)
        return "unavailable"
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        dbs = [cfg.rds.database, cfg.rds.operation_database, cfg.rds.ontology_database]
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        drifted: List[str] = []
        checked = False
        try:
            with conn.cursor() as cur:
                for dbname in dbs:
                    try:
                        cur.execute(
                            f"SELECT filename, checksum FROM {dbname}.schema_migrations "
                            "WHERE checksum IS NOT NULL")
                        rows = cur.fetchall() or []
                    except Exception:   # noqa: BLE001 — 库/表/列不存在：跳过该库
                        continue
                    checked = True
                    for r in rows:
                        fn = r["filename"] if isinstance(r, dict) else r[0]
                        cs = r["checksum"] if isinstance(r, dict) else r[1]
                        # 台账 filename 可能带修订标记 `NNN_xxx.sql@NNNa`——取 @ 前基名
                        base = str(fn).split("@", 1)[0]
                        if base in local and cs and local[base] != cs:
                            drifted.append(f"{dbname}:{base}")
        finally:
            conn.close()
        if drifted:
            logger.error("readiness: schema_migrations checksum 漂移：%s", drifted[:10])
            return "drift"
        return "ok" if checked else "unavailable"
    except Exception as e:   # noqa: BLE001
        logger.warning("readiness: schema_migrations 校验失败: %s", e)
        return "unavailable"


def schema_migrations_status() -> str:
    ttl = float(os.environ.get("RAG_READY_SCHEMA_TTL_S", "300"))
    return _cached("schema_migrations", ttl, _schema_drift_once)


def schema_strict() -> bool:
    """drift 是否计入关键就绪（默认 off——台账抖动不该全量摘流量；staging 验证后再开）。"""
    return _flag_on("RAG_READY_SCHEMA_STRICT")


# ── DashScope 模型在线 ─────────────────────────────────────────────────────────
def dashscope_status(api_key: Optional[str], model: Optional[str]) -> str:
    """默认 config-only（configured/unconfigured，零外呼零配额——历史行为）；
    RAG_READY_DASHSCOPE_LIVE=true → TTL 内一次 GET /compatible-mode/v1/models
    （轻量、免费），校验密钥有效 + 服务可达 + 配置的模型在列 → ok / model_missing /
    error。live 结果**只报告不摘流量**：DashScope 全局故障时把所有实例摘出只会雪上加霜。"""
    if not api_key:
        return "unconfigured"
    if not _flag_on("RAG_READY_DASHSCOPE_LIVE"):
        return "configured"

    def _probe() -> str:
        try:
            from opensearch_pipeline.http_session import get_session
            resp = get_session().get(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
                headers={"Authorization": f"Bearer {api_key}"}, timeout=5)
            if resp.status_code != 200:
                logger.warning("readiness: DashScope models 探针 HTTP %s", resp.status_code)
                return "error"
            ids = {m.get("id") for m in (resp.json().get("data") or [])}
            if model and ids and model not in ids:
                return "model_missing"
            return "ok"
        except Exception as e:   # noqa: BLE001
            logger.warning("readiness: DashScope 探针失败: %s", e)
            return "error"

    ttl = float(os.environ.get("RAG_READY_DASHSCOPE_TTL_S", "300"))
    return _cached("dashscope_live", ttl, _probe)
