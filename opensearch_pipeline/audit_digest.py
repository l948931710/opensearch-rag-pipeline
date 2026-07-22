# -*- coding: utf-8 -*-
"""audit_digest.py — agent_audit_log 篡改检测摘要作业（Majors γ6/M7-lite，codex 共识 2026-07-21）。

**M7 状态=OPEN（本模块是检测层缓解，不是防篡改）**：行内哈希链已被共识否决
（读前行哈希=head 行锁串行化全部治理事务，且与 HIGH_WRITE 写前审计 fail-closed 事务
耦合）；RAG_AUDIT_DB_DSN 独立凭证构想同撤（同事务审计用调用方游标，换 DSN=失去
原子性）。本作业的边界：**事后可检测**「已封存完整日的删改」，不能阻止篡改本身，
也检测不了「当日内写入→当日内改掉」的窗口（如实记录的残余）；根治（独立只写账号/
OSS WORM/KMS 托管密钥）= ε 批 user-gated 清单。

机制：每个北京业务完整日产一份 manifest 写 OSS 独立前缀（OSS 前缀本身就是台账，
无额外水位状态）：
  - 行序 (created_at, audit_id)；created_at 由 SQL DATE_FORMAT 固定
    'YYYY-MM-DDTHH:MM:SS.ffffff'（DATETIME(3) 本不受会话时区影响，仍 SET 会话
    time_zone='+08:00' 钉死防未来 TIMESTAMP 列混入）；
  - 每行 canonical JSON（sort_keys + separators(",",":") + ensure_ascii）→ SHA-256
    行哈希 → **Merkle 根**（逐层 sha256(hex_l+hex_r)，奇数复制末叶；空日=
    sha256("empty:{table}:{date}")，有意区分于「无 manifest」）；
  - manifest = {schema,table,date,count,merkle_root,prev_digest,key_id,generated_at}
    + HMAC-SHA256(RAG_AUDIT_HMAC_KEY) 签名（签 canonical manifest 无 hmac 字段），
    canonical 落盘；
  - **成链**：prev_digest = 前一日 manifest 文件字节的 SHA-256（第一日=None）——改
    历史任何一份都会断链；
  - **key 轮换**：key_id = SHA-256(key) 前 16 hex。换 key ⇒ 新 key_id，旧链用旧 key
    验（verify 脚本可带多把 key，按 manifest.key_id 匹配）；
  - **水位追赶**：列 OSS 前缀取最大已封日，从其次日追到最近可结算日；无任何 manifest
    → 从表内最早行的北京日起。单轮封存日数有上限（防首跑无界），**剩余积压如实进
    report.pending_days，绝不静默截断**；
  - **午夜结算宽限窗**（RAG_AUDIT_DIGEST_GRACE_MIN，默认 30 分钟）：北京 00:00-00:30
    间不结算昨日——给迟到的 NOW(3) 写入落定，避免封了又来行。
  - **correction manifest 规格**（本轮只定规格不产出）：已封日事后合法补写（如法证
    回填）时，运维人工核准后产 `{date}.correction-N.json`——内容同 manifest 外加
    {"corrects": 原文件 sha256, "reason": 人工说明}，同 HMAC 签名；verify 脚本视其为
    该日新权威并继续按原链验 prev（correction 不改后续日的 prev_digest——链的锚点
    永远是首版文件字节，防「用 correction 洗白链条」）。

flag：RAG_AUDIT_DIGEST_ENABLE 默认 off（skipped/exit 0）；on 而 RAG_AUDIT_HMAC_KEY
缺失 → error（exit 3，fail-closed：没有签名密钥的摘要=无证据力，绝不产未签名产物）。
retention（F-36）归档 agent_audit_log 前应先确认对应日已封存（归档面清单 ε 批）。
核验：scripts/verify_audit_digest.py（只读）。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import logging
import os
import re
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TABLE = "agent_audit_log"
_SCHEMA_VERSION = 1
_MANIFEST_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.json$")
_AUDIT_COLS = ("audit_id", "run_id", "step_no", "request_id", "event_type", "action",
               "risk_level", "decision", "policy_id", "user_id", "roles",
               "acl_groups_snapshot", "channel", "args_digest", "detail_json")
_MAX_DAYS_PER_RUN = 200          # 单轮封存上限（首跑追赶防无界；剩余进 pending_days）


def enabled() -> bool:
    return os.environ.get("RAG_AUDIT_DIGEST_ENABLE",
                          "").strip().lower() in ("1", "true", "yes", "on")


def _hmac_key() -> str:
    return (os.environ.get("RAG_AUDIT_HMAC_KEY") or "").strip()


def key_id_of(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _oss_prefix() -> str:
    p = (os.environ.get("RAG_AUDIT_DIGEST_OSS_PREFIX") or "audit_digest/").strip()
    return p if p.endswith("/") else p + "/"


def _manifest_key(day: str) -> str:
    return f"{_oss_prefix()}{TABLE}/{day}.json"


# ── canonical 形态（verify 脚本共用同一实现，绝不各写一份漂移）────────────────


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def row_hash(row: Dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(row)).hexdigest()


def merkle_root(hashes: List[str], *, table: str, day: str) -> str:
    if not hashes:
        return hashlib.sha256(f"empty:{table}:{day}".encode("utf-8")).hexdigest()
    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])              # 奇数复制末叶（比特币式约定）
        level = [hashlib.sha256((level[i] + level[i + 1]).encode("ascii")).hexdigest()
                 for i in range(0, len(level), 2)]
    return level[0]


def sign_manifest(manifest: Dict[str, Any], key: str) -> Dict[str, Any]:
    payload = {k: v for k, v in manifest.items() if k != "hmac"}
    sig = hmac_mod.new(key.encode("utf-8"), canonical_bytes(payload),
                       hashlib.sha256).hexdigest()
    return {**payload, "hmac": sig}


def verify_manifest_signature(manifest: Dict[str, Any], key: str) -> bool:
    sig = manifest.get("hmac") or ""
    payload = {k: v for k, v in manifest.items() if k != "hmac"}
    want = hmac_mod.new(key.encode("utf-8"), canonical_bytes(payload),
                        hashlib.sha256).hexdigest()
    return hmac_mod.compare_digest(sig, want)


# ── 日界（qa_rollup 同约定：存储=太平洋墙钟，业务日=北京）─────────────────────


def _day_predicate(col: str, day: str) -> Tuple[str, tuple]:
    from opensearch_pipeline.qa_rollup import _beijing_day_pacific_bounds
    bounds = _beijing_day_pacific_bounds(day)
    if bounds is not None:
        return f"{col} >= %s AND {col} < %s", (bounds[0], bounds[1])
    return (f"DATE(CONVERT_TZ({col}, 'America/Los_Angeles', 'Asia/Shanghai')) = %s", (day,))


def _beijing_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:   # noqa: BLE001
        return datetime.now()


def _storage_dt_to_beijing_day(dt: datetime) -> str:
    """存储墙钟（naive，≈太平洋）→ 北京业务日（水位起点推导用；失败退原日期）。"""
    try:
        from zoneinfo import ZoneInfo
        aware = dt.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        return aware.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:   # noqa: BLE001
        return dt.strftime("%Y-%m-%d")


def latest_settleable_day(now_bj: Optional[datetime] = None) -> Optional[str]:
    """最近可结算完整日：默认=北京昨日；宽限窗内（00:00–00:MM）退一天。"""
    now_bj = now_bj or _beijing_now()
    grace_min = int(os.environ.get("RAG_AUDIT_DIGEST_GRACE_MIN", "30") or 30)
    day = (now_bj.date() - timedelta(days=1))
    if now_bj.hour == 0 and now_bj.minute < grace_min:
        day = day - timedelta(days=1)
    return day.strftime("%Y-%m-%d")


# ── 采集 + 封存 ──────────────────────────────────────────────────────────────


def _fetch_day_rows(conn, db: str, day: str) -> List[Dict[str, Any]]:
    pred, params = _day_predicate("created_at", day)
    cols = ", ".join(_AUDIT_COLS)
    with conn.cursor() as cur:
        cur.execute("SET SESSION time_zone='+08:00'")     # 钉死（见模块注）
        cur.execute(
            f"SELECT {cols}, DATE_FORMAT(created_at, '%%Y-%%m-%%dT%%H:%%i:%%s.%%f') "
            f"FROM {db}.{TABLE} WHERE {pred} ORDER BY created_at, audit_id", params)
        rows = cur.fetchall() or []
    out = []
    for r in rows:
        d = {c: r[i] for i, c in enumerate(_AUDIT_COLS)}
        d["created_at"] = r[len(_AUDIT_COLS)]
        for k, v in list(d.items()):
            if isinstance(v, bytes):
                d[k] = v.decode("utf-8", "replace")
            elif v is not None and not isinstance(v, (str, int, float, bool)):
                d[k] = str(v)
        out.append(d)
    return out


def build_day_manifest(rows: List[Dict[str, Any]], *, day: str,
                       prev_digest: Optional[str], key: str) -> Dict[str, Any]:
    hashes = [row_hash(r) for r in rows]
    manifest = {
        "schema": _SCHEMA_VERSION, "table": TABLE, "date": day,
        "count": len(rows), "merkle_root": merkle_root(hashes, table=TABLE, day=day),
        "prev_digest": prev_digest, "key_id": key_id_of(key),
        "generated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return sign_manifest(manifest, key)


def _list_sealed_days(bucket) -> List[str]:
    """列 OSS 前缀下已封存日（手动分页走 bucket.list_objects——不依赖 oss2.ObjectIterator，
    GuardedBucket/测试桶同型可用）。"""
    days: List[str] = []
    marker = ""
    prefix = f"{_oss_prefix()}{TABLE}/"
    while True:
        result = bucket.list_objects(prefix=prefix, marker=marker, max_keys=1000)
        for obj in (getattr(result, "object_list", None) or []):
            m = _MANIFEST_RE.search(obj.key)
            if m:
                days.append(m.group(1))
        if not getattr(result, "is_truncated", False):
            break
        marker = getattr(result, "next_marker", "") or ""
        if not marker:
            break
    return sorted(days)


def run_audit_digest(*, alert: bool = True) -> dict:
    """作业入口（ops_monitor 契约）：flag off / simulate → skipped；key 缺失/OSS 不可用/
    采集失败 → error（exit 3）；正常 → ok=True + sealed/pending_days。"""
    if not enabled():
        return {"skipped": True, "reason": "RAG_AUDIT_DIGEST_ENABLE off"}
    try:
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        if cfg.simulate or cfg.simulate_db or cfg.simulate_oss:
            return {"skipped": True, "reason": "simulate"}
    except Exception as e:   # noqa: BLE001
        return {"error": f"config: {e}"}
    key = _hmac_key()
    if not key:
        # fail-closed：未签名的摘要无证据力，绝不产出（配好 key 或关 flag）
        logger.error("audit_digest: RAG_AUDIT_DIGEST_ENABLE=on 但 RAG_AUDIT_HMAC_KEY 缺失"
                     "——拒绝产未签名 manifest（exit 3）")
        return {"error": "RAG_AUDIT_HMAC_KEY missing (fail-closed)"}
    try:
        from opensearch_pipeline.clients import _get_oss_bucket
        bucket, simulated = _get_oss_bucket()
        if simulated or bucket is None:
            return {"error": "OSS 不可用（真实 bucket 是 manifest 的落点，无处可写）"}
        return _catch_up(bucket, key)
    except Exception as e:   # noqa: BLE001
        logger.error("audit_digest 作业失败: %s", e, exc_info=True)
        return {"error": str(e)}


def _catch_up(bucket, key: str) -> dict:
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.db import _get_db_conn
    db = get_config().rds.operation_database
    sealed_days = _list_sealed_days(bucket)
    settle_end = latest_settleable_day()
    conn = _get_db_conn()
    try:
        if sealed_days:
            start = (datetime.strptime(sealed_days[-1], "%Y-%m-%d").date()
                     + timedelta(days=1))
            prev_bytes = bucket.get_object(_manifest_key(sealed_days[-1])).read()
            prev_digest = hashlib.sha256(prev_bytes).hexdigest()
        else:
            with conn.cursor() as cur:
                cur.execute(f"SELECT MIN(created_at) FROM {db}.{TABLE}")
                row = cur.fetchone()
            if not row or row[0] is None:
                return {"ok": True, "sealed": [], "pending_days": 0,
                        "note": "audit 表为空，无可封存日"}
            first = row[0] if isinstance(row[0], datetime) else datetime.fromisoformat(str(row[0]))
            start = datetime.strptime(_storage_dt_to_beijing_day(first), "%Y-%m-%d").date()
            prev_digest = None
        end = datetime.strptime(settle_end, "%Y-%m-%d").date()
        if start > end:
            return {"ok": True, "sealed": [], "pending_days": 0, "watermark": str(start)}
        all_days: List[date_cls] = []
        d = start
        while d <= end:
            all_days.append(d)
            d += timedelta(days=1)
        todo, overflow = all_days[:_MAX_DAYS_PER_RUN], all_days[_MAX_DAYS_PER_RUN:]
        sealed: List[str] = []
        for day_d in todo:
            day = day_d.strftime("%Y-%m-%d")
            rows = _fetch_day_rows(conn, db, day)
            manifest = build_day_manifest(rows, day=day, prev_digest=prev_digest, key=key)
            body = canonical_bytes(manifest)
            bucket.put_object(_manifest_key(day), body)
            prev_digest = hashlib.sha256(body).hexdigest()
            sealed.append(day)
            logger.info("audit_digest: 封存 %s（rows=%s root=%s…）",
                        day, manifest["count"], manifest["merkle_root"][:12])
        report = {"ok": True, "sealed": sealed, "pending_days": len(overflow)}
        if overflow:
            # 无静默截断：单轮上限外的积压如实喊出（下一轮从新水位续追）
            logger.warning("audit_digest: 本轮封存 %s 日达上限，剩余 %s 日待下轮追赶"
                           "（%s → %s）", len(sealed), len(overflow),
                           overflow[0], overflow[-1])
        return report
    finally:
        conn.close()
