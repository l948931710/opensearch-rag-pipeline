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
# P1-09（外审核查 2026-07-16）补 llm_call_log（023，记账/成本看板）与 agent_audit_log
# （024——HIGH_WRITE write-ahead 审计 fail-closed：表缺失时写工具一开即全阻断）。
_AGENT_TABLES = ["agent_run", "agent_checkpoint", "agent_step", "tool_invocation",
                 "approval_request", "approval_decision", "tool_registry",
                 "llm_call_log", "agent_audit_log"]
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


def _load_manifest(schema_dir: Path) -> Optional[Dict[str, str]]:
    """P1-09（外审核查 2026-07-16）：schema/MIGRATION_MANIFEST.tsv（file→目标）机器可读
    单一来源（与 scripts/ci_load_schema.sh 共用）。读不到/为空 → None（回退全局并集
    语义——旧镜像无此文件不误红）。"""
    try:
        p = schema_dir / "MIGRATION_MANIFEST.tsv"
        if not p.is_file():
            return None
        out: Dict[str, str] = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
        return out or None
    except Exception as e:   # noqa: BLE001
        logger.warning("readiness: MIGRATION_MANIFEST.tsv 读取失败（回退全局并集）: %s", e)
        return None


def _schema_drift_once() -> str:
    """三库台账 checksum vs 本地 schema/ 文件 sha256。
    - 台账里有该文件名 且 本地存在 且 sha256 不同 → drift（同名内容漂移，032 语义）；
    - **本地存在但目标库台账未记 → unapplied:N**（批次5 P0-06c + P1-09 逐库收紧，
      外审核查 2026-07-16：旧语义把三库台账并成一个集合——迁移记错库也算「全局已
      应用」；现按 MIGRATION_MANIFEST.tsv 逐库判定，记错库=unapplied 并响亮点名；
      manifest 缺失/未列出的文件回退全局并集（旧镜像/新文件窗口不误红）；
      修订行 `NNN_xxx.sql@NNNa` 计入其基名的已应用证据）；
    - 台账无 checksum 列/表不存在（未迁移环境）→ unavailable；
    - 本地无 schema/ 目录（旧镜像）→ no_local_files。
    优先级 drift > unapplied > ok；strict（RAG_READY_SCHEMA_STRICT）下非 ok 即不就绪。"""
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
    manifest = _load_manifest(d)
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        dbs = [cfg.rds.database, cfg.rds.operation_database, cfg.rds.ontology_database]
        # manifest 目标词 → 运行环境实际库名（staging/local 库名可与生产不同，
        # 必须经 config 映射，绝不硬编码 fuling_*）
        target_dbs = {"knowledge": [cfg.rds.database],
                      "operation": [cfg.rds.operation_database],
                      "ontology": [cfg.rds.ontology_database],
                      "both": list(dbs)}
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        drifted: List[str] = []
        applied_by_db: Dict[str, set] = {}
        checked = False
        try:
            with conn.cursor() as cur:
                for dbname in dbs:
                    try:
                        cur.execute(
                            f"SELECT filename, checksum FROM {dbname}.schema_migrations")
                        rows = cur.fetchall() or []
                    except Exception:   # noqa: BLE001 — 缺 checksum 列（pre-032）：退只取文件名
                        try:
                            cur.execute(
                                f"SELECT filename FROM {dbname}.schema_migrations")
                            rows = [(r["filename"] if isinstance(r, dict) else r[0], None)
                                    for r in (cur.fetchall() or [])]
                        except Exception:   # noqa: BLE001 — 库/表不存在：跳过该库
                            continue
                    checked = True
                    bag = applied_by_db.setdefault(dbname, set())
                    for r in rows:
                        fn = r["filename"] if isinstance(r, dict) else r[0]
                        cs = (r.get("checksum") if isinstance(r, dict)
                              else (r[1] if len(r) > 1 else None))
                        # 台账 filename 可能带修订标记 `NNN_xxx.sql@NNNa`——取 @ 前基名
                        base = str(fn).split("@", 1)[0]
                        bag.add(base)
                        if base in local and cs and local[base] != cs:
                            drifted.append(f"{dbname}:{base}")
        finally:
            conn.close()
        if drifted:
            logger.error("readiness: schema_migrations checksum 漂移：%s", drifted[:10])
            return "drift"
        if not checked:
            return "unavailable"
        applied_union = set().union(*applied_by_db.values()) if applied_by_db else set()
        unapplied: List[str] = []
        misplaced: List[str] = []
        for base in sorted(local):
            target = (manifest or {}).get(base)
            expected = target_dbs.get(target or "")
            if expected:
                # 逐库判定：目标库全部记账才算已应用（both=三库都要）
                missing_dbs = [db for db in expected
                               if base not in applied_by_db.get(db, set())]
                if missing_dbs:
                    unapplied.append(base)
                    if base in applied_union:
                        misplaced.append(f"{base}→缺{','.join(missing_dbs)}")
            else:
                # split/未列清单/无 manifest：回退全局并集语义（不误红）
                if base not in applied_union:
                    unapplied.append(base)
        if unapplied:
            logger.warning("readiness: 迁移未按目标库记账（unapplied）：%s%s%s",
                           unapplied[:10], "…" if len(unapplied) > 10 else "",
                           f"；其中记错库（misplaced）：{misplaced[:5]}" if misplaced else "")
            return f"unapplied:{len(unapplied)}"
        return "ok"
    except Exception as e:   # noqa: BLE001
        logger.warning("readiness: schema_migrations 校验失败: %s", e)
        return "unavailable"


def schema_migrations_status() -> str:
    ttl = float(os.environ.get("RAG_READY_SCHEMA_TTL_S", "300"))
    return _cached("schema_migrations", ttl, _schema_drift_once)


def schema_strict() -> bool:
    """schema 台账健康是否计入关键就绪（默认 off——台账抖动不该全量摘流量；staging
    验证后再开）。批次5（P0-06d）收紧 strict 语义：开着时要求状态为 **ok**——
    drift / unapplied / unavailable / no_local_files 全部不就绪（此前只拒 drift，
    「缺迁移仍绿」正是外审复现的假健康）。"""
    return _flag_on("RAG_READY_SCHEMA_STRICT")


# ── 批次5（unknown-unknowns P0-06）：列契约 / kill-switch / 038 回填探针 ────────────
# 「表在、列不在」是蓝绿/回滚窗口的典型假健康：下列列缺失时对应功能首次触库即
# 500/静默降级，但表存在性探针全绿。
_AGENT_CONTRACT_COLUMNS = [   # (table, column, 来源迁移)
    ("agent_run", "message_id", "036"),
    ("agent_run", "active_thread", "037"),
    ("approval_decision", "final_args_digest", "031"),
    ("agent_checkpoint", "state_digest", "022"),
]
_ONTOLOGY_CONTRACT_COLUMNS = [
    ("ontology_object", "normalized_title", "038"),
]
# P1-09（外审核查 2026-07-16）：关键唯一索引契约——uk_thread_active 缺失时列 probe 仍绿
# 但 per-thread 串行化静默失效（并发双 submit 双双入库，409 兜底不触发）。
_AGENT_CONTRACT_INDEXES = [   # (table, index_name, 来源迁移)
    ("agent_run", "uk_thread_active", "037"),
]


def _indexes_exist(dbname: str, idxs) -> str:
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        missing: List[str] = []
        try:
            with conn.cursor() as cur:
                for table, index_name, mig in idxs:
                    cur.execute(
                        "SELECT COUNT(*) FROM information_schema.statistics "
                        "WHERE table_schema=%s AND table_name=%s AND index_name=%s",
                        (dbname, table, index_name))
                    row = cur.fetchone()
                    n = row[0] if not isinstance(row, dict) else list(row.values())[0]
                    if not int(n):
                        missing.append(f"{table}#{index_name}(schema/{mig})")
        finally:
            conn.close()
        if missing:
            logger.error("readiness: 关键索引契约缺失（%s）：%s", dbname, missing)
            return "missing:" + ",".join(missing)
        return "ok"
    except Exception as e:   # noqa: BLE001
        logger.warning("readiness: 索引契约探针失败（%s）: %s", dbname, e)
        return "error"


def _columns_exist(dbname: str, cols) -> str:
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        missing: List[str] = []
        try:
            with conn.cursor() as cur:
                for table, col, mig in cols:
                    cur.execute(
                        "SELECT COUNT(*) FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
                        (dbname, table, col))
                    row = cur.fetchone()
                    n = row[0] if not isinstance(row, dict) else list(row.values())[0]
                    if not int(n):
                        missing.append(f"{table}.{col}(schema/{mig})")
        finally:
            conn.close()
        if missing:
            logger.error("readiness: 关键列契约缺失（%s）：%s", dbname, missing)
            return "missing:" + ",".join(missing)
        return "ok"
    except Exception as e:   # noqa: BLE001
        logger.warning("readiness: 列契约探针失败（%s）: %s", dbname, e)
        return "error"


def schema_contract_status() -> str:
    """flag-on 面的关键列契约（表存在 ≠ 契约在位；critical 与 agent_tables 同级）。
    对应 flag 全 off → skipped。"""
    if not (_flag_on("RAG_AGENT_ENABLE") or _flag_on("RAG_ONTOLOGY_ENABLE")):
        return "skipped"

    def _compute() -> str:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        parts: List[str] = []
        if _flag_on("RAG_AGENT_ENABLE"):
            parts.append(_columns_exist(cfg.rds.operation_database, _AGENT_CONTRACT_COLUMNS))
            # P1-09：uk_thread_active 唯一索引探针（列在、索引不在 = 串行化静默失效）
            parts.append(_indexes_exist(cfg.rds.operation_database, _AGENT_CONTRACT_INDEXES))
        if _flag_on("RAG_ONTOLOGY_ENABLE"):
            parts.append(_columns_exist(cfg.rds.ontology_database, _ONTOLOGY_CONTRACT_COLUMNS))
        bad = [p for p in parts if p != "ok"]
        if not bad:
            return "ok"
        miss = [p for p in bad if p.startswith("missing:")]
        return miss[0] if miss else "error"

    return _cached("schema_contract", 60, _compute)


def kill_switch_status() -> str:
    """DB 驱动 kill switch 的读路径健康（批次5 P0-06b）：绕缓存直读一次——
    「表在但读退化」（权限被收/行损坏）此前只活在 warning 日志，readiness 恒绿。
    agent off → skipped；error 是否摘流量由 kill_switch_critical 决定。"""
    if not _flag_on("RAG_AGENT_ENABLE"):
        return "skipped"

    def _compute() -> str:
        try:
            from opensearch_pipeline.agent_runtime.registry_store import RDSToolRegistryStore
            return RDSToolRegistryStore().probe_health()
        except Exception as e:   # noqa: BLE001
            logger.warning("readiness: kill switch 探针构建失败: %s", e)
            return "error"

    return _cached("kill_switch", 60, _compute)


def kill_switch_critical() -> bool:
    """kill switch error 是否摘流量：HIGH_WRITE 工具启用时 critical（写工具的治理
    控制面失联不该继续接流量）；只读窗口 error 仅报告——readiness 红把全部实例
    摘空比「读工具照跑+Policy/审批兜底」更糟。"""
    try:
        from opensearch_pipeline.agent_tools import ontology_write_tools_enabled
        return ontology_write_tools_enabled()
    except Exception:   # noqa: BLE001
        return False


def ontology_backfill_status() -> str:
    """038 normalized_title 回填缺口（批次5 P0-06e）：active 对象 NULL 计数——NULL 行
    不参与等值召回（同名漏归并→误铸重复品）。**report-only**：治理指标而非摘流量判据
    （真实播种开闸前置=回填清零，由 gate 流程核对本状态词）。"""
    if not _flag_on("RAG_ONTOLOGY_ENABLE"):
        return "skipped"

    def _compute() -> str:
        try:
            from opensearch_pipeline.config import get_config
            from opensearch_pipeline.db import _get_db_conn
            db = get_config().rds.ontology_database
            conn = _get_db_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {db}.ontology_object "
                        "WHERE status='active' AND normalized_title IS NULL")
                    row = cur.fetchone()
                    n = row[0] if not isinstance(row, dict) else list(row.values())[0]
            finally:
                conn.close()
            return "ok" if not int(n) else f"pending({int(n)})"
        except Exception as e:   # noqa: BLE001
            logger.warning("readiness: normalized_title 回填探针失败: %s", e)
            return "error"

    return _cached("ontology_backfill", 300, _compute)


def legacy_open_posture_status() -> str:
    """P1-14（外审核查 2026-07-16）：LEGACY-OPEN 逃生口可见性——设了
    RAG_ALLOW_LEGACY_OPEN_PROD 的实例在 /api/ready 里持续自报（report-only，不摘流量：
    config 启动断言已把无效/过期 ack 挡在启动前，这里只保证「姿态缺口不被遗忘」）。"""
    ack = os.environ.get("RAG_ALLOW_LEGACY_OPEN_PROD", "").strip().lower()
    return f"legacy_open({ack})" if ack else "off"


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
