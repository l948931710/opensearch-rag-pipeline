# -*- coding: utf-8 -*-
"""doc_update_notify.py — 文档升版可见范围提醒（日调批处理）。

设计稿:`docs/doc_update_notify_design_2026-08-04_DRAFT.md`(rev3 + §11 Sam 拍板)。
三段流水,每段独立幂等,由 DataWorks 日调节点顺序调用(dry-run 默认):

    discover → 扫「版本切换已完成但无事件行」的文档,建 doc_update_event
    resolve  → 按【权威】判定受众,建 doc_update_notice(仅当时已开启的通道)
    send     → 钉钉工作通知按人日摘要投递(console 通道无需投递,行本身即站内信)

── 三条承重不变量(改动前先读)──────────────────────────────────────────────────
1. **通知面 ⊆ 检索面**。受众判定只调 `acl_policy.can_read_doc` + `access_grants.
   resolve_doc_acl(strict=True)`,**绝不另写一套 ACL 逻辑**。判定所需的 flag 姿态取自
   serving 盖的**见证行**(runtime_contract.read_acl_posture),不取本进程 env ——
   DataWorks env 是粘贴时静态注入的,与 SAE 控制台的 serving env 会永久脱钩。
2. **降级一律 HOLD,绝不落终态**。"权威读不到 / 组织快照过期 / 姿态未知"与"这篇确实
   没人可读"是两回事:前者 HOLD(可 --requeue 回放),后者才 SUPPRESSED。把节流条件
   (受众超限/单轮超量)落成终态 = 事件永久静音。
3. **投递必须原子认领**。DataWorks 手动重跑与日调实例会重叠,UNIQUE 键只防插入重复、
   不防双发 ⇒ 出队走 `SELECT ... FOR UPDATE SKIP LOCKED` + 置 SENDING,两个进程拿到
   的行集天然不相交。

摄取管线零改动(唯一例外:`document_version.activated_at` 的两处写入,见 pipeline_nodes /
spot_checker —— 切换时点不能用 `updated_at`,它被 ACL 投影等无关写刷新)。
"""

import argparse
import datetime
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── 状态词表(与 schema/065 注释逐字对应)────────────────────────────────────────
EVENT_PENDING = "PENDING"
EVENT_HOLD = "HOLD"
EVENT_RESOLVED = "RESOLVED"
EVENT_SUPPRESSED = "SUPPRESSED"

NOTICE_PENDING = "PENDING"
NOTICE_SENDING = "SENDING"
NOTICE_SENT = "SENT"
NOTICE_READ = "READ"
NOTICE_FAILED = "FAILED"
NOTICE_SKIPPED = "SKIPPED"

CHANNEL_CONSOLE = "console"
CHANNEL_DINGTALK = "dingtalk"

# HOLD 分词(运维性,可回放)
HOLD_STALE_SNAPSHOT = "stale_snapshot"
HOLD_AUTHORITY_UNAVAILABLE = "authority_unavailable"
HOLD_POSTURE_UNKNOWN = "posture_unknown"
HOLD_POSTURE_MISMATCH = "posture_mismatch"
HOLD_AUDIENCE_CAP = "audience_cap"
HOLD_BULK_GUARD = "bulk_guard"
# SUPPRESSED 分词(语义性,终态)
SUP_UNCHANGED = "unchanged"
SUP_MISSING_SHA = "missing_sha"
SUP_PUBLIC_POLICY = "public_policy"
SUP_AUDIENCE_ZERO = "audience_zero"
SUP_STALE_SWITCH = "stale_switch"
SUP_QUARANTINED = "quarantined_or_retired"
SUP_META_MISSING = "meta_missing"
SUP_BASELINE = "baseline"
SUP_MANUAL = "manual"
# notice SKIPPED 分词
SKIP_ACL_REVOKED = "acl_revoked"
SKIP_QUARANTINED = "quarantined_or_retired"
SKIP_STALE = "stale"
SKIP_ATTEMPTS = "attempts_exhausted"

# 见证行最大年龄:超过即姿态未知(整轮 HOLD)。serving 每小时盖章一次,24h 给足冗余。
POSTURE_MAX_AGE_S = 24 * 3600
# 切换距发现超过它 ⇒ SUPPRESSED(stale_switch):迟到收敛/restore 不冒充"刚更新"。
STALE_SWITCH_DAYS = 7
# notice 落行后超过它仍未发出 ⇒ SKIPPED(stale):防子闸翻转时积压爆发。
STALE_NOTICE_DAYS = 3
_MAX_RECIPIENTS = 100          # asyncsend_v2 单次 userid_list 上限(**分批循环,不截断**)
_SEND_BUDGET_S = 30 * 60       # 每轮发送墙钟预算;超时余量留 PENDING 下轮续投
_CLAIM_BATCH = 500             # 单次认领行数
_SENDING_RECLAIM_MIN = 30      # SENDING 超时回收(分钟)
_MAX_ATTEMPTS = 3
_DIGEST_MAX_TITLES = 10

EXIT_OK, EXIT_GUARD, EXIT_FAIL = 0, 2, 3


def _cfg():
    from opensearch_pipeline.config import get_config
    return get_config()


def _rag():
    """本功能的 flag 全住在 `RAGConfig`(即 `get_config().rag`)。

    ⚠️ 刻意用**直接属性访问**而非 `getattr(..., default)`:字段名写错时应当响亮 AttributeError,
    而不是静默回落成"总闸关/上限 0",那种失败会伪装成"功能正常但没通知任何人"。
    """
    return _cfg().rag


def _kb_db() -> str:
    return _cfg().rds.database


def _op_db() -> str:
    return _cfg().rds.operation_database


def _conn():
    from opensearch_pipeline.db import _get_db_conn
    return _get_db_conn()


def _tables_present(cur) -> bool:
    """065 是否已 apply(缺表 ⇒ 节点 exit 2,不当故障)。"""
    try:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.TABLES"
            " WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN ('doc_update_event','doc_update_notice')",
            (_op_db(),))
        row = cur.fetchone()
        return bool(row and int(row[0]) == 2)
    except Exception as e:   # noqa: BLE001
        logger.warning("065 表探测失败: %s", e)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# 1. discover —— 发现「已生效但未建事件」的版本切换
# ══════════════════════════════════════════════════════════════════════════════

def _fingerprint_verdict(new_row: Sequence, prev_row: Optional[Sequence]) -> Tuple[str, Optional[str]]:
    """双指纹内容门 → (content_changed, suppress_reason)。

    正文轴 `canonical_sha256` 之外必须有字节轴:stage-1 skip-gate 在「正文 sha 相同但
    **资产集有新增**」时会撤销 skip、照常升版(`_asset_additions_block_skip`)——那是管线
    自己认定的"内容变了"。截图型 SOP/ERP 语料里这是最常见的真实更新族,只比正文会把它
    整族静音。字节轴取 `checksum_sha256`,缺失回退 `etag`(prod-RO 实测 checksum 缺 47.9%
    而 etag 仅缺 9.2%,回退把杀伤面小一个量级)。
    """
    new_canon, new_sum, new_etag = new_row[0], new_row[1], new_row[2]
    if not prev_row:
        return "none", SUP_MISSING_SHA
    old_canon, old_sum, old_etag = prev_row[1], prev_row[2], prev_row[3]
    if not new_canon or not old_canon:
        return "none", SUP_MISSING_SHA
    if str(new_canon) != str(old_canon):
        return "text", None
    new_bytes = new_sum or new_etag
    old_bytes = old_sum or old_etag
    if new_bytes and old_bytes and str(new_bytes) != str(old_bytes):
        return "assets", None          # 正文同、文件变 ⇒ 图集/格式更新,要通知
    return "none", SUP_UNCHANGED


def _candidate_rows(cur) -> List[tuple]:
    """主谓词 + 二次谓词,返回 [(doc_id, version_no)]。"""
    from opensearch_pipeline.doc_state import quarantine_exclude_sql
    kb = _kb_db()
    q_dv = quarantine_exclude_sql("dv")
    # 主谓词:current 版本已生效(SUCCESS)、非隔离、且存在被 supersede 的更早版本。
    cur.execute(f"""
        SELECT dm.doc_id, dm.current_version_no
        FROM {kb}.document_meta dm
        JOIN {kb}.document_version dv
          ON dv.doc_id = dm.doc_id AND dv.version_no = dm.current_version_no
        WHERE dm.status = 'active' AND dm.current_version_no > 1
          AND dv.status = 'active' AND dv.index_status = 'SUCCESS'
          AND {q_dv}
          AND EXISTS (SELECT 1 FROM {kb}.document_version o
                      WHERE o.doc_id = dm.doc_id
                        AND o.version_no < dm.current_version_no
                        AND o.status = 'superseded')
    """)
    out = [(r[0], int(r[1])) for r in (cur.fetchall() or [])]
    # 二次谓词:current 指向的版本【未】生效(升版失败/隔离停滞),但更早的某版本确已生效
    # 且有 superseded 兄弟 —— 该次切换同样是真实发生过的,主谓词看不见它。
    cur.execute(f"""
        SELECT dv.doc_id, MAX(dv.version_no)
        FROM {kb}.document_meta dm
        JOIN {kb}.document_version cur_v
          ON cur_v.doc_id = dm.doc_id AND cur_v.version_no = dm.current_version_no
        JOIN {kb}.document_version dv ON dv.doc_id = dm.doc_id
        WHERE dm.status = 'active' AND dm.current_version_no > 1
          AND cur_v.index_status <> 'SUCCESS'
          AND dv.status = 'active' AND dv.index_status = 'SUCCESS'
          AND {q_dv}
          AND EXISTS (SELECT 1 FROM {kb}.document_version o
                      WHERE o.doc_id = dv.doc_id AND o.version_no < dv.version_no
                        AND o.status = 'superseded')
        GROUP BY dv.doc_id
    """)
    out.extend((r[0], int(r[1])) for r in (cur.fetchall() or []))
    return out


def _existing_event_keys(cur, keys: Sequence[tuple]) -> set:
    """跨库反连接在 Python 侧完成(operation×knowledge 直接 JOIN 会因 collation 撞 1267)。"""
    if not keys:
        return set()
    found = set()
    step = 500
    for i in range(0, len(keys), step):
        chunk = keys[i:i + step]
        ph = ",".join(["(%s,%s)"] * len(chunk))
        params = tuple(p for k in chunk for p in k)
        cur.execute(
            f"SELECT doc_id, version_no FROM {_op_db()}.doc_update_event"
            f" WHERE (doc_id, version_no) IN ({ph})", params)
        found.update((r[0], int(r[1])) for r in (cur.fetchall() or []))
    return found


def discover_events(conn, *, commit: bool = False, baseline: bool = False) -> dict:
    """扫描 → 建事件行。返回统计 dict。

    baseline=True:对**全部** current>1 的 (doc, version) 无过滤盖 SUPPRESSED(baseline)。
    这是开闸前一刻的一次性动作 —— 把 retired/restricted/FAILED/隔离等全部存量一次盖住,
    避免开闸首轮把历史升版当新事件群发。
    """
    stats = {"scanned": 0, "created": 0, "suppressed": 0, "skipped_existing": 0}
    kb, op = _kb_db(), _op_db()
    with conn.cursor() as cur:
        if baseline:
            cur.execute(f"""SELECT doc_id, current_version_no FROM {kb}.document_meta
                            WHERE current_version_no > 1""")
            cands = [(r[0], int(r[1])) for r in (cur.fetchall() or [])]
        else:
            cands = _candidate_rows(cur)
        # 去重(主/二次谓词可能同时命中同一 doc 的不同版本;同 doc 取更高版本即可)
        best: Dict[str, int] = {}
        for doc_id, ver in cands:
            if ver > best.get(doc_id, 0):
                best[doc_id] = ver
        keys = sorted(best.items())
        stats["scanned"] = len(keys)
        existing = _existing_event_keys(cur, keys)
        fresh = [(d, v) for d, v in keys if (d, v) not in existing]
        stats["skipped_existing"] = len(keys) - len(fresh)
        cutoff = datetime.datetime.now() - datetime.timedelta(days=STALE_SWITCH_DAYS)
        for doc_id, ver in fresh:
            if baseline:
                row = (None, "none", EVENT_SUPPRESSED, SUP_BASELINE)
            else:
                cur.execute(f"""SELECT canonical_sha256, checksum_sha256, etag, activated_at
                                FROM {kb}.document_version
                                WHERE doc_id=%s AND version_no=%s LIMIT 1""", (doc_id, ver))
                new_row = cur.fetchone()
                if not new_row:
                    continue
                # 前版选取与 stage-1 skip-gate 逐字同款:最新的 canonical_sha256 非 NULL 行
                cur.execute(f"""SELECT version_no, canonical_sha256, checksum_sha256, etag
                                FROM {kb}.document_version
                                WHERE doc_id=%s AND version_no<%s AND canonical_sha256 IS NOT NULL
                                ORDER BY version_no DESC LIMIT 1""", (doc_id, ver))
                prev_row = cur.fetchone()
                changed, reason = _fingerprint_verdict(new_row, prev_row)
                activated = new_row[3]
                if not reason:
                    # 新近度:activated_at 缺失(存量/未经新版收尾)一律保守抑制 —— 无法证明
                    # 这是"刚刚"切换的,宁可不打扰。
                    if activated is None or activated < cutoff:
                        reason = SUP_STALE_SWITCH
                prev_ver = int(prev_row[0]) if prev_row else None
                row = (prev_ver, changed,
                       EVENT_SUPPRESSED if reason else EVENT_PENDING, reason)
            cur.execute(
                f"""INSERT IGNORE INTO {op}.doc_update_event
                    (doc_id, version_no, prev_version_no, content_changed, status, reason,
                     resolved_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (doc_id, ver, row[0], row[1], row[2], row[3],
                 datetime.datetime.now() if row[2] == EVENT_SUPPRESSED else None))
            if row[2] == EVENT_SUPPRESSED:
                stats["suppressed"] += 1
            else:
                stats["created"] += 1
    if commit:
        conn.commit()
    else:
        conn.rollback()
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# 2. resolve —— 权威受众枚举
# ══════════════════════════════════════════════════════════════════════════════

def _load_identity_universe(cur, posture: dict) -> Tuple[List[tuple], bool]:
    """构造全员 (staff_id, AclContext) 列表 + 组织快照是否新鲜。

    与 serving 的 `resolve_acl_context` 同料同序:groups 走 `offline_groups_for_staff`
    (墓碑/seeded/TTL/并集四条优先级),祖先链走 `dept_ancestry.resolve_ancestor_chains`
    的物理父链。**不调钉钉 API**(1000+ 人/日不现实,且在线也只在 cache-miss 才调)。
    """
    from opensearch_pipeline.acl_policy import AclContext
    from opensearch_pipeline.dept_ancestry import resolve_ancestor_chains
    from opensearch_pipeline.dingtalk_identity import (
        _DEPT_SNAPSHOT_MAX_AGE_H, offline_groups_for_staff,
    )
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS
    kb = _kb_db()
    cur.execute(f"""SELECT dept_id, parent_id, name FROM {kb}.dept_dim WHERE is_active=1""")
    dept_rows = cur.fetchall() or []
    parents: Dict[int, int] = {}
    names: Dict[int, str] = {}
    for r in dept_rows:
        parents[int(r[0])] = int(r[1] or 0)
        names[int(r[0])] = r[2] or ""
    # 快照保鲜与 serving 同判据(>48h ⇒ 节点通道 fail-closed)。org_sync 断供时,
    # legacy 组码通道不受影响,但 node 文档判定不可信 ⇒ 调用方整轮 HOLD。
    cur.execute(f"""SELECT TIMESTAMPDIFF(HOUR, MAX(synced_at), NOW()) FROM {kb}.dept_dim""")
    _age_row = cur.fetchone()
    snapshot_age_h = _age_row[0] if _age_row else None
    fresh = bool(dept_rows) and (snapshot_age_h is not None
                                 and snapshot_age_h <= _DEPT_SNAPSHOT_MAX_AGE_H)
    cur.execute(f"SELECT user_id, dept_code, role, "
                f"TIMESTAMPDIFF(SECOND, updated_at, NOW()), is_active FROM {kb}.user_role")
    role_rows = {r[0]: (r[1], r[2], r[3], r[4]) for r in (cur.fetchall() or []) if r and r[0]}
    cur.execute(f"SELECT staff_id, dept_ids FROM {kb}.staff_dim WHERE is_active=1")
    staff_rows = cur.fetchall() or []

    all_groups = set(_VALID_ACL_GROUPS)
    universe = []
    for staff_id, dept_ids_csv in staff_rows:
        direct = []
        for tok in str(dept_ids_csv or "").split(","):
            tok = tok.strip()
            if tok.isdigit():
                direct.append(int(tok))
        groups = offline_groups_for_staff(
            role_rows.get(staff_id), [names[d] for d in direct if d in names],
            ttl_seconds=int(posture.get("ttl") or 0) or None)
        chain, chain_ok = resolve_ancestor_chains(direct, lambda d: parents.get(d))
        universe.append((staff_id, AclContext(
            groups=tuple(groups),
            ancestor_dept_ids=tuple(chain),
            direct_dept_ids=tuple(direct),
            node_channel_ok=bool(fresh and chain_ok),
            org_wide_reader=bool(groups) and all_groups.issubset(set(groups)),
        )))
    return universe, fresh


def resolve_events(conn, *, commit: bool = False) -> dict:
    """PENDING 事件 → 受众 → notice 行。返回统计 dict（含 exit 提示）。"""
    from opensearch_pipeline.access_grants import resolve_doc_acl
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, can_read_doc
    from opensearch_pipeline.runtime_contract import read_acl_posture
    kb, op = _kb_db(), _op_db()
    stats = {"considered": 0, "resolved": 0, "held": 0, "suppressed": 0,
             "notices": 0, "posture_ok": False}
    with conn.cursor() as cur:
        cur.execute(f"""SELECT id, doc_id, version_no FROM {op}.doc_update_event
                        WHERE status=%s ORDER BY id""", (EVENT_PENDING,))
        pend = [(int(r[0]), r[1], int(r[2])) for r in (cur.fetchall() or [])]
        stats["considered"] = len(pend)
        if not pend:
            return stats

        # ── 姿态见证:判定口径的唯一来源 ──────────────────────────────────────
        posture = read_acl_posture(cur)
        if posture is None or posture.get("age_seconds", 1 << 30) > POSTURE_MAX_AGE_S:
            _hold_all(cur, [p[0] for p in pend], HOLD_POSTURE_UNKNOWN)
            stats["held"] = len(pend)
            _finish(conn, commit)
            logger.error("ACL 姿态见证缺失/过期(age=%s)——整轮 HOLD,不做任何受众解析",
                         None if posture is None else posture.get("age_seconds"))
            return stats
        stats["posture_ok"] = True
        grant, enforce = bool(posture["grant"]), bool(posture["enforce"])

        universe, snapshot_fresh = _load_identity_universe(cur, posture)
        if not snapshot_fresh or not universe:
            _hold_all(cur, [p[0] for p in pend], HOLD_STALE_SNAPSHOT)
            stats["held"] = len(pend)
            _finish(conn, commit)
            logger.error("组织快照过期或为空(dept_dim/staff_dim)——整轮 HOLD")
            return stats

        # bulk_guard:计数基准 = 本轮进入 resolve 的事件数(含回放),超出部分整体 HOLD
        limit = int(_rag().doc_notify_max_events_per_run or 50)
        if len(pend) > limit:
            overflow = [p[0] for p in pend[limit:]]
            _hold_all(cur, overflow, HOLD_BULK_GUARD)
            stats["held"] += len(overflow)
            logger.warning("本轮 %d 个事件超过上限 %d —— 超出的 %d 个 HOLD(bulk_guard),"
                           "可 --requeue --all --limit 分批放行", len(pend), limit, len(overflow))
            pend = pend[:limit]

        include_public = bool(_rag().doc_notify_include_public)
        max_aud = int(_rag().doc_notify_max_audience or 1000)
        dingtalk_on = bool(_rag().doc_notify_dingtalk)
        try:
            acls = resolve_doc_acl([p[1] for p in pend], cur, strict=True)
        except Exception as e:   # noqa: BLE001 — 权威不可达 ⇒ 整轮 HOLD,绝不宽松回落
            _hold_all(cur, [p[0] for p in pend], HOLD_AUTHORITY_UNAVAILABLE)
            stats["held"] += len(pend)
            _finish(conn, commit)
            logger.error("node-ACL 权威不可达——整轮 HOLD(绝不按 legacy 宽松解析): %s", e)
            return stats

        now = datetime.datetime.now()
        for ev_id, doc_id, ver in pend:
            acl = acls.get(doc_id)
            if acl is None:
                # strict 下"缺行/未知 mode"都不返回。区分二者:行还在 ⇒ 权威侧问题(HOLD);
                # 行真没了 ⇒ 手工手术删过(生产无此路径),独立分词便于分诊。
                cur.execute(f"SELECT 1 FROM {kb}.document_meta WHERE doc_id=%s LIMIT 1", (doc_id,))
                if cur.fetchone():
                    _set_event(cur, ev_id, EVENT_HOLD, HOLD_AUTHORITY_UNAVAILABLE)
                    stats["held"] += 1
                else:
                    _set_event(cur, ev_id, EVENT_SUPPRESSED, SUP_META_MISSING)
                    stats["suppressed"] += 1
                continue
            perm = (acl.permission_level or "").strip().lower()
            if perm == "public" and not include_public:
                _set_event(cur, ev_id, EVENT_SUPPRESSED, SUP_PUBLIC_POLICY)
                stats["suppressed"] += 1
                continue
            # node 文档 × GRANT 关:线上对它们**无条件 DENY**,受众确实为零 —— 但 GRANT 是
            # 可回滚的运行姿态,把它落成终态会让回滚窗内的所有升版永久静音 ⇒ HOLD。
            if (acl.mode or "").strip().lower() == ACL_MODE_NODE and not grant:
                _set_event(cur, ev_id, EVENT_HOLD, HOLD_POSTURE_MISMATCH)
                stats["held"] += 1
                continue
            audience = [sid for sid, ctx in universe
                        if not ctx.org_wide_reader                 # 总经办不逐篇打扰(Sam 拍板)
                        and can_read_doc(ctx, acl, grant_enabled=grant,
                                         enforce_enabled=enforce)]
            if not audience:
                _set_event(cur, ev_id, EVENT_SUPPRESSED, SUP_AUDIENCE_ZERO, count=0)
                stats["suppressed"] += 1
                continue
            # public 受众=全员是它的定义态,不受范围护栏约束(护栏只管 dept_internal 的异常)
            if perm != "public" and len(audience) > max_aud:
                _set_event(cur, ev_id, EVENT_HOLD, HOLD_AUDIENCE_CAP, count=len(audience))
                stats["held"] += 1
                continue
            channels = [CHANNEL_CONSOLE] + ([CHANNEL_DINGTALK] if dingtalk_on else [])
            for sid in audience:
                for ch in channels:
                    cur.execute(
                        f"""INSERT IGNORE INTO {op}.doc_update_notice
                            (event_id, doc_id, user_id, channel, state)
                            VALUES (%s,%s,%s,%s,%s)""",
                        (ev_id, doc_id, sid, ch, NOTICE_PENDING))
                    stats["notices"] += 1
            # resolved_at 同时是钉钉新近度锚:回放会刷新它 ⇒ HOLD 回放不被 stale 规则误杀
            _set_event(cur, ev_id, EVENT_RESOLVED, None, count=len(audience), ts=now)
            stats["resolved"] += 1
    _finish(conn, commit)
    return stats


def _set_event(cur, ev_id: int, status: str, reason: Optional[str],
               *, count: Optional[int] = None, ts=None) -> None:
    cur.execute(
        f"""UPDATE {_op_db()}.doc_update_event
            SET status=%s, reason=%s, audience_count=COALESCE(%s, audience_count),
                resolved_at=%s
            WHERE id=%s""",
        (status, reason, count, ts or datetime.datetime.now(), ev_id))


def _hold_all(cur, ev_ids: Sequence[int], reason: str) -> None:
    if not ev_ids:
        return
    ph = ",".join(["%s"] * len(ev_ids))
    cur.execute(
        f"""UPDATE {_op_db()}.doc_update_event SET status=%s, reason=%s
            WHERE id IN ({ph})""", (EVENT_HOLD, reason) + tuple(ev_ids))


def _finish(conn, commit: bool) -> None:
    conn.commit() if commit else conn.rollback()


# ══════════════════════════════════════════════════════════════════════════════
# 3. send —— 钉钉工作通知（按人日摘要）
# ══════════════════════════════════════════════════════════════════════════════

def _http_post(url: str, payload: dict) -> dict:
    """HTTP seam(tests monkeypatch 这里;与 admin_notify 同款约定)。"""
    import requests
    resp = requests.post(url, json=payload, timeout=10)
    return resp.json() if resp.status_code == 200 else {"errcode": resp.status_code,
                                                        "errmsg": resp.text[:200]}


def _send_work_notice(user_ids: Sequence[str], text: str) -> Tuple[bool, str]:
    """发一条工作通知给 ≤100 人。返回 (ok, err)。"""
    agent = os.environ.get("RAG_DINGTALK_AGENT_ID", "").strip()
    if not agent:
        return False, "RAG_DINGTALK_AGENT_ID 未配置"
    from opensearch_pipeline.dingtalk_card import _get_access_token
    token = _get_access_token()
    if not token:
        return False, "access_token 获取失败"
    data = _http_post(
        "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2"
        "?access_token=" + token,
        {"agent_id": agent, "userid_list": ",".join(user_ids),
         "msg": {"msgtype": "text", "text": {"content": text}}})
    if data.get("errcode") == 0:
        return True, ""
    return False, "errcode=%s errmsg=%s" % (data.get("errcode"), str(data.get("errmsg"))[:120])


def _claim(cur, limit: int) -> List[tuple]:
    """原子认领待发行:`FOR UPDATE SKIP LOCKED` 保证并发实例拿到的行集不相交。"""
    op = _op_db()
    cur.execute(
        f"""SELECT id, event_id, doc_id, user_id, attempts, created_at
            FROM {op}.doc_update_notice
            WHERE channel=%s AND (
                  state=%s
               OR (state=%s AND attempts < %s)
               OR (state=%s AND claim_ts < DATE_SUB(NOW(), INTERVAL %s MINUTE)))
            ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""",
        (CHANNEL_DINGTALK, NOTICE_PENDING, NOTICE_FAILED, _MAX_ATTEMPTS,
         NOTICE_SENDING, _SENDING_RECLAIM_MIN, limit))
    rows = cur.fetchall() or []
    if rows:
        ids = [int(r[0]) for r in rows]
        ph = ",".join(["%s"] * len(ids))
        cur.execute(
            f"""UPDATE {op}.doc_update_notice
                SET state=%s, attempts=attempts+1, claim_ts=NOW()
                WHERE id IN ({ph})""", (NOTICE_SENDING,) + tuple(ids))
    return rows


def _mark(cur, ids: Sequence[int], state: str, *, reason: Optional[str] = None,
          err: Optional[str] = None) -> None:
    if not ids:
        return
    ph = ",".join(["%s"] * len(ids))
    cur.execute(
        f"""UPDATE {_op_db()}.doc_update_notice
            SET state=%s, reason=%s, last_error=%s,
                sent_at=CASE WHEN %s=%s THEN NOW() ELSE sent_at END
            WHERE id IN ({ph})""",
        (state, reason, err, state, NOTICE_SENT) + tuple(ids))


def _render_digest(items: Sequence[tuple]) -> str:
    """items=[(title, version_no)] → 摘要文案。**不带深链**(Sam 拍板),不带任何 id。"""
    head = "【富岭知识库】你可见的 %d 篇文档有更新：" % len(items)
    shown = items[:_DIGEST_MAX_TITLES]
    body = "、".join("《%s》→v%s" % (t, v) for t, v in shown)
    if len(items) > len(shown):
        body += "等 %d 篇" % len(items)
    return head + body + "。详情见知识库控制台。"


def send_notices(conn, *, commit: bool = False) -> dict:
    """认领 → 发送前复核 → 分批投递 → 同步落状态。"""
    from opensearch_pipeline.access_grants import resolve_doc_acl
    from opensearch_pipeline.acl_policy import can_read_doc
    from opensearch_pipeline.doc_state import version_quarantined
    from opensearch_pipeline.runtime_contract import read_acl_posture
    stats = {"claimed": 0, "sent": 0, "failed": 0, "skipped": 0, "batches": 0}
    if not bool(_rag().doc_notify_dingtalk):
        return stats
    kb, op = _kb_db(), _op_db()
    deadline = time.time() + _SEND_BUDGET_S
    with conn.cursor() as cur:
        posture = read_acl_posture(cur)
        if posture is None or posture.get("age_seconds", 1 << 30) > POSTURE_MAX_AGE_S:
            logger.error("发送轮姿态见证缺失/过期 —— 不发送(行留 PENDING,下轮续投)")
            _finish(conn, commit)
            return stats
        grant, enforce = bool(posture["grant"]), bool(posture["enforce"])
        universe, snapshot_fresh = _load_identity_universe(cur, posture)
        if not snapshot_fresh:
            logger.error("发送轮组织快照过期 —— 不发送(行留 PENDING)")
            _finish(conn, commit)
            return stats
        ctx_by_staff = dict(universe)

        while time.time() < deadline:
            rows = _claim(cur, _CLAIM_BATCH)
            if not rows:
                break
            stats["claimed"] += len(rows)
            if commit:
                conn.commit()                      # 认领尽早落地,缩短并发窗口
            doc_ids = sorted({r[2] for r in rows})
            # 发送前复核所需的三样:文档当前元信息(标题/状态/隔离)、当前 ACL 权威
            cur.execute(
                f"""SELECT dm.doc_id, dm.title, dm.original_filename, dm.status,
                           dv.publish_status, dv.gate_status
                    FROM {kb}.document_meta dm
                    LEFT JOIN {kb}.document_version dv
                      ON dv.doc_id = dm.doc_id AND dv.version_no = dm.current_version_no
                    WHERE dm.doc_id IN ({",".join(["%s"] * len(doc_ids))})""", tuple(doc_ids))
            meta = {}
            for r in (cur.fetchall() or []):
                meta[r[0]] = {"title": r[1] or r[2] or r[0], "status": r[3],
                              "quarantined": version_quarantined(r[4], r[5])}
            try:
                acls = resolve_doc_acl(doc_ids, cur, strict=True)
            except Exception as e:   # noqa: BLE001 — 权威不可达 ⇒ 本批回 PENDING 下轮再来
                _mark(cur, [int(r[0]) for r in rows], NOTICE_PENDING)
                _finish(conn, commit)
                logger.error("发送前权威复核不可达 —— 本批退回 PENDING: %s", e)
                return stats
            ev_ids = sorted({int(r[1]) for r in rows})
            cur.execute(f"""SELECT id, version_no FROM {op}.doc_update_event
                            WHERE id IN ({",".join(["%s"] * len(ev_ids))})""", tuple(ev_ids))
            ver_by_event = {int(r[0]): int(r[1]) for r in (cur.fetchall() or [])}

            per_user: Dict[str, List[tuple]] = {}
            per_user_ids: Dict[str, List[int]] = {}
            skip_ids: Dict[str, List[int]] = {}
            stale_cut = datetime.datetime.now() - datetime.timedelta(days=STALE_NOTICE_DAYS)
            for nid, ev_id, doc_id, user_id, attempts, created in rows:
                nid = int(nid)
                m = meta.get(doc_id)
                acl = acls.get(doc_id)
                ctx = ctx_by_staff.get(user_id)
                if created is not None and created < stale_cut:
                    skip_ids.setdefault(SKIP_STALE, []).append(nid)
                elif int(attempts) + 1 > _MAX_ATTEMPTS:
                    skip_ids.setdefault(SKIP_ATTEMPTS, []).append(nid)
                elif (m is None or str(m["status"] or "").lower() != "active"
                        or m["quarantined"]):
                    # 事件建行与发送隔天,期间可能被隔离/退役 —— 绝不广播已下架文档的标题
                    skip_ids.setdefault(SKIP_QUARANTINED, []).append(nid)
                elif acl is None or ctx is None or not can_read_doc(
                        ctx, acl, grant_enabled=grant, enforce_enabled=enforce):
                    skip_ids.setdefault(SKIP_ACL_REVOKED, []).append(nid)
                else:
                    per_user.setdefault(user_id, []).append(
                        (m["title"], ver_by_event.get(int(ev_id), "")))
                    per_user_ids.setdefault(user_id, []).append(nid)
            for reason, ids in skip_ids.items():
                _mark(cur, ids, NOTICE_SKIPPED, reason=reason)
                stats["skipped"] += len(ids)

            # 同文案合并 → 分批 ≤100 循环(admin_notify 的 [:100] 是**截断**,群发会静默丢人)
            by_text: Dict[str, List[str]] = {}
            for user_id, items in per_user.items():
                by_text.setdefault(_render_digest(items), []).append(user_id)
            for text, users in by_text.items():
                for i in range(0, len(users), _MAX_RECIPIENTS):
                    part = users[i:i + _MAX_RECIPIENTS]
                    ok, err = _send_work_notice(part, text) if commit else (True, "")
                    ids = [n for u in part for n in per_user_ids.get(u, [])]
                    stats["batches"] += 1
                    if ok:
                        _mark(cur, ids, NOTICE_SENT)
                        stats["sent"] += len(ids)
                    else:
                        _mark(cur, ids, NOTICE_FAILED, err=err[:200])
                        stats["failed"] += len(ids)
                        logger.warning("工作通知投递失败(%d 行转 FAILED 待重试): %s",
                                       len(ids), err)
            _finish(conn, commit)
            if len(rows) < _CLAIM_BATCH:
                break
    _finish(conn, commit)
    return stats


# ══════════════════════════════════════════════════════════════════════════════
# 4. CLI
# ══════════════════════════════════════════════════════════════════════════════

def _requeue(conn, *, doc: Optional[str], all_hold: bool, limit: int, commit: bool) -> dict:
    """HOLD → PENDING(仅 HOLD 可回放;SUPPRESSED 是语义终态,不给回放路径)。"""
    op = _op_db()
    stats = {"requeued": 0}
    with conn.cursor() as cur:
        if doc:
            doc_id, _, ver = doc.partition(":")
            if ver:
                cur.execute(f"""UPDATE {op}.doc_update_event SET status=%s, reason=NULL
                                WHERE doc_id=%s AND version_no=%s AND status=%s""",
                            (EVENT_PENDING, doc_id, int(ver), EVENT_HOLD))
            else:
                cur.execute(f"""UPDATE {op}.doc_update_event SET status=%s, reason=NULL
                                WHERE doc_id=%s AND status=%s""",
                            (EVENT_PENDING, doc_id, EVENT_HOLD))
        elif all_hold:
            cur.execute(f"""UPDATE {op}.doc_update_event SET status=%s, reason=NULL
                            WHERE status=%s ORDER BY id LIMIT %s""",
                        (EVENT_PENDING, EVENT_HOLD, limit))
        stats["requeued"] = getattr(cur, "rowcount", 0) or 0
    _finish(conn, commit)
    return stats


def _suppress(conn, *, doc: str, commit: bool) -> dict:
    """人工止损:把 PENDING/HOLD 事件整批压成 SUPPRESSED(manual)。重传波的默认处置。"""
    op = _op_db()
    with conn.cursor() as cur:
        doc_id, _, ver = doc.partition(":")
        if doc_id == "--all-held":
            cur.execute(f"""UPDATE {op}.doc_update_event SET status=%s, reason=%s
                            WHERE status IN (%s,%s)""",
                        (EVENT_SUPPRESSED, SUP_MANUAL, EVENT_HOLD, EVENT_PENDING))
        elif ver:
            cur.execute(f"""UPDATE {op}.doc_update_event SET status=%s, reason=%s
                            WHERE doc_id=%s AND version_no=%s AND status IN (%s,%s)""",
                        (EVENT_SUPPRESSED, SUP_MANUAL, doc_id, int(ver),
                         EVENT_HOLD, EVENT_PENDING))
        else:
            cur.execute(f"""UPDATE {op}.doc_update_event SET status=%s, reason=%s
                            WHERE doc_id=%s AND status IN (%s,%s)""",
                        (EVENT_SUPPRESSED, SUP_MANUAL, doc_id, EVENT_HOLD, EVENT_PENDING))
        n = getattr(cur, "rowcount", 0) or 0
    _finish(conn, commit)
    return {"suppressed": n}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="文档升版可见范围提醒（日调批处理）")
    ap.add_argument("--commit", action="store_true", help="真正落库/投递（缺省 dry-run）")
    ap.add_argument("--baseline", action="store_true",
                    help="开闸前一刻的一次性基线：全部 current>1 盖 SUPPRESSED(baseline)")
    ap.add_argument("--requeue", metavar="DOC[:VER]", help="HOLD → PENDING 回放")
    ap.add_argument("--all", action="store_true", help="配合 --requeue：回放全部 HOLD")
    ap.add_argument("--limit", type=int, default=50, help="配合 --requeue --all 的批量上限")
    ap.add_argument("--suppress", metavar="DOC[:VER]|--all-held",
                    help="人工止损：压成 SUPPRESSED(manual)")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = _cfg()
    # simulate 短路必须在 flag 检查【之前】(retention.py 同款纪律):本地/CI 不碰真库
    if getattr(cfg, "simulate", False) or getattr(cfg, "simulate_db", False):
        logger.info("simulate 模式：doc-update-notify 跳过（不连库、不投递）")
        return EXIT_OK
    if not bool(_rag().doc_notify):
        logger.info("RAG_DOC_NOTIFY 未开启 —— 跳过（总闸默认关）")
        return EXIT_GUARD
    if args.commit:
        from opensearch_pipeline.env_guard import assert_destructive_write_allowed
        assert_destructive_write_allowed("doc_update_notify", cfg.rds.host, kind="rds")

    conn = _conn()
    try:
        with conn.cursor() as cur:
            if not _tables_present(cur):
                logger.error("schema/065 未 apply（doc_update_event/doc_update_notice 缺失）")
                return EXIT_GUARD
        if args.requeue or args.all:
            print(json.dumps(_requeue(conn, doc=args.requeue, all_hold=args.all,
                                      limit=args.limit, commit=args.commit),
                             ensure_ascii=False))
            return EXIT_OK
        if args.suppress:
            print(json.dumps(_suppress(conn, doc=args.suppress, commit=args.commit),
                             ensure_ascii=False))
            return EXIT_OK
        d = discover_events(conn, commit=args.commit, baseline=args.baseline)
        logger.info("discover: %s", d)
        if args.baseline:
            print(json.dumps({"discover": d}, ensure_ascii=False, default=str))
            return EXIT_OK
        r = resolve_events(conn, commit=args.commit)
        logger.info("resolve: %s", r)
        s = send_notices(conn, commit=args.commit)
        logger.info("send: %s", s)
        print(json.dumps({"discover": d, "resolve": r, "send": s},
                         ensure_ascii=False, default=str))
        # 姿态未知/快照过期把整轮 HOLD 了 ⇒ 非零退出,让 DataWorks 标红而不是静默绿
        if r.get("considered") and not r.get("posture_ok"):
            return EXIT_FAIL
        return EXIT_OK
    except Exception as e:   # noqa: BLE001
        logger.error("doc-update-notify 运行失败: %s", e, exc_info=True)
        return EXIT_FAIL
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
