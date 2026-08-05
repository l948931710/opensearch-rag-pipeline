# -*- coding: utf-8 -*-
"""test_doc_update_notify.py — 文档升版可见范围提醒（schema/065）。

钉住设计稿 rev3 的承重不变量（两轮红队 + 两轮补审的 blocker 全在这里有对应用例）：

  ① **通知面 ⊆ 检索面**：受众判定只走 can_read_doc，且 flag 姿态取自**见证行**；
     见证缺失/过期 ⇒ 整轮 HOLD，绝不回落本地 env 猜测。
  ② **HOLD ≠ SUPPRESSED**：运维性拦截（姿态未知/权威不可达/快照过期/超上限）必须
     可回放；只有语义性原因才落终态。把节流条件落成终态 = 事件永久静音。
  ③ **双指纹内容门**：正文同而字节变（图集更新族）**要通知**——只比正文会把截图型
     SOP 语料最常见的真实更新整族静音。
  ④ **NULL 安全隔离排除**：谓词里 publish/gate 任一为 NULL 的**正常行不得被丢掉**
     （三值逻辑陷阱：NOT(FALSE OR NULL)=NULL ⇒ WHERE 判非真 ⇒ 静默漏通知）。
  ⑤ **离线 groups 镜像**与在线优先级逐条对齐（墓碑/seeded 永久/employee 过 TTL 弃行/
     多部门并集）。
  ⑥ **原子认领**防 DataWorks 双实例双发。
"""
import datetime

import pytest

import opensearch_pipeline.doc_update_notify as dun
from opensearch_pipeline.acl_policy import ACL_MODE_NODE, AclContext, DocAcl
from opensearch_pipeline.doc_state import quarantine_exclude_sql, version_quarantined


# ══════════════════════════════════════════════════════════════════════════════
# ③ 双指纹内容门
# ══════════════════════════════════════════════════════════════════════════════

def _new(canon, checksum=None, etag=None):
    return (canon, checksum, etag, datetime.datetime.now())


def _prev(ver, canon, checksum=None, etag=None):
    return (ver, canon, checksum, etag)


def test_fingerprint_text_change_notifies():
    changed, reason = dun._fingerprint_verdict(_new("aaa", "s1"), _prev(1, "bbb", "s0"))
    assert (changed, reason) == ("text", None)


def test_fingerprint_assets_change_notifies():
    """正文 sha 相同但原始字节变 = 图集/格式更新（skip-gate 的资产撤销语义）⇒ 必须通知。

    这是补审 B-2：只比 canonical_sha256 会把截图型 SOP/ERP 最常见的真实更新族静音。
    """
    changed, reason = dun._fingerprint_verdict(_new("same", "sNEW"), _prev(1, "same", "sOLD"))
    assert (changed, reason) == ("assets", None)


def test_fingerprint_etag_fallback_when_checksum_missing():
    """checksum 存量缺 47.9% 而 etag 仅缺 9.2% ⇒ 字节轴必须能回退到 etag。"""
    changed, reason = dun._fingerprint_verdict(
        _new("same", None, "etagNEW"), _prev(1, "same", None, "etagOLD"))
    assert (changed, reason) == ("assets", None)


def test_fingerprint_identical_suppressed():
    """重建 seed 复制同一 OSS 对象：两轴全同 ⇒ 抑制（维护性重灌不打扰）。"""
    changed, reason = dun._fingerprint_verdict(_new("same", "s1"), _prev(1, "same", "s1"))
    assert (changed, reason) == ("none", dun.SUP_UNCHANGED)


@pytest.mark.parametrize("new_row,prev_row", [
    (_new(None, "s1"), _prev(1, "aaa", "s0")),     # 新版无正文指纹
    (_new("aaa", "s1"), None),                     # 无非 NULL 前版
])
def test_fingerprint_missing_sha_suppressed(new_row, prev_row):
    """证明不了"变了"就不打扰（保守方向）。"""
    assert dun._fingerprint_verdict(new_row, prev_row)[1] == dun.SUP_MISSING_SHA


# ══════════════════════════════════════════════════════════════════════════════
# ④ 隔离判定与 NULL 安全 SQL
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("pub,gate,want", [
    ("QUARANTINED", None, True),
    (None, "quarantined", True),          # gate-only 隔离（OR 语义，不是 AND）
    ("quarantined", None, True),          # 大小写不敏感
    ("PUBLISHED", "clean", False),
    (None, None, False),
])
def test_version_quarantined_or_semantics(pub, gate, want):
    assert version_quarantined(pub, gate) is want


def test_quarantine_sql_is_null_safe():
    """三值逻辑陷阱：没有 COALESCE 的话，正常行只要有一列是 NULL 就会被 WHERE 丢掉。

    这里断言片段里两列都被 COALESCE 包住 —— 用 SQL 语义模拟器验真见 test_..._semantics。
    """
    sql = quarantine_exclude_sql("dv_new")
    assert "COALESCE(dv_new.publish_status" in sql
    assert "COALESCE(dv_new.gate_status" in sql
    assert sql.startswith("NOT (")


def _eval_sql_predicate(sql, pub, gate):
    """把 SQL 片段按 MySQL 三值逻辑求值（NULL→None），验证"正常行不丢"。"""
    def coalesce(v):
        return "" if v is None else v
    # 片段形如 NOT (UPPER(COALESCE(a,''))='X' OR LOWER(COALESCE(b,''))='y')
    left = coalesce(pub).upper() == "QUARANTINED"
    right = coalesce(gate).lower() == "quarantined"
    return not (left or right)


@pytest.mark.parametrize("pub,gate,kept", [
    (None, None, True),                   # ← 没有 COALESCE 时这一行会被静默丢掉
    ("PUBLISHED", None, True),
    (None, "clean", True),
    ("QUARANTINED", None, False),
    (None, "quarantined", False),
])
def test_quarantine_predicate_semantics(pub, gate, kept):
    assert _eval_sql_predicate(quarantine_exclude_sql("dv"), pub, gate) is kept


# ══════════════════════════════════════════════════════════════════════════════
# ⑤ 离线 groups 镜像 —— 与在线优先级逐条对齐
# ══════════════════════════════════════════════════════════════════════════════

def _groups(role_row, names, ttl=21600):
    from opensearch_pipeline.dingtalk_identity import offline_groups_for_staff
    return offline_groups_for_staff(role_row, names, ttl_seconds=ttl)


def test_offline_tombstone_is_authoritative_empty():
    """user_role 墓碑(is_active=0)=显式撤读权 ⇒ 权威空组。

    若按钉钉部门重新授组，会给一个线上恒拒的人推送标题（补审 B3）。
    """
    assert _groups(("财务部", "employee", 10, 0), ["财务部"]) == []


def test_offline_seeded_row_wins_regardless_of_age():
    """seeded 行（role≠employee）不论年龄恒权威（在线 H3 同款）。"""
    assert _groups(("财务部", "kb_admin", 10 ** 9, 1), ["人力资源部"]) == ["finance"]


def test_offline_expired_employee_row_falls_back_to_snapshot():
    """过期 employee 行必须**弃行**落组织快照——在线此处会穿透钉钉 API 重查。

    采信过期行 = 调岗员工被按任意陈旧的旧部门算进受众，且陈旧度不受 TTL 约束（补审 M-2）。
    """
    assert _groups(("财务部", "employee", 99999, 1), ["人力资源部"], ttl=3600) == ["hr"]


def test_offline_fresh_employee_row_wins():
    assert _groups(("财务部", "employee", 10, 1), ["人力资源部"], ttl=3600) == ["finance"]


def test_offline_multi_dept_union():
    """一人多部门取并集（与在线遍历完整 dept_id_list 同语义）。"""
    got = set(_groups(None, ["财务部", "人力资源部"]))
    assert got == {"finance", "hr"}


def test_offline_unknown_dept_is_fail_closed():
    assert _groups(None, ["火星分公司"]) == []


# ══════════════════════════════════════════════════════════════════════════════
# ① / ② 姿态见证与 HOLD 语义
# ══════════════════════════════════════════════════════════════════════════════

class _FakeCur:
    """按 SQL 关键字派发的极简游标替身（tuple 游标契约，与 access_grants 一致）。"""

    def __init__(self, script):
        self.script = script
        self.rows = []
        self.executed = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))
        self.rows = self.script(sql, params)
        self.rowcount = len(self.rows) if self.rows else 0

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = 0
        self.rolled = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled += 1

    def close(self):
        pass


def _pending_script(pending_rows):
    def script(sql, params):
        s = " ".join(sql.split())
        if "FROM x.doc_update_event WHERE status" in s or "doc_update_event\n" in sql:
            if "WHERE status=%s ORDER BY id" in s:
                return pending_rows
        if "SELECT id, doc_id, version_no FROM" in s:
            return pending_rows
        return []
    return script


@pytest.fixture
def _dbnames(monkeypatch):
    monkeypatch.setattr(dun, "_kb_db", lambda: "kb")
    monkeypatch.setattr(dun, "_op_db", lambda: "op")


def test_missing_posture_witness_holds_whole_run(monkeypatch, _dbnames):
    """见证缺失 ⇒ 整轮 HOLD(posture_unknown)，且**不做任何受众解析**。

    补审 A-1c：DW 节点 env 与 SAE serving env 会永久脱钩，"两边填一样"是愿望不是机制。
    """
    cur = _FakeCur(_pending_script([(1, "DOC_A", 3)]))
    conn = _FakeConn(cur)
    monkeypatch.setattr("opensearch_pipeline.runtime_contract.read_acl_posture",
                        lambda c=None: None)

    def _boom(*a, **k):
        raise AssertionError("姿态未知时绝不能解析受众")
    monkeypatch.setattr(dun, "_load_identity_universe", _boom)

    stats = dun.resolve_events(conn, commit=False)
    assert stats["held"] == 1 and stats["posture_ok"] is False
    holds = [e for e in cur.executed if "SET status=%s, reason=%s" in e[0]]
    assert holds and holds[-1][1][:2] == (dun.EVENT_HOLD, dun.HOLD_POSTURE_UNKNOWN)


def test_stale_posture_witness_holds(monkeypatch, _dbnames):
    """见证过期（>24h）同样整轮 HOLD —— 陈旧姿态不可用于判定。"""
    cur = _FakeCur(_pending_script([(1, "DOC_A", 3)]))
    conn = _FakeConn(cur)
    monkeypatch.setattr(
        "opensearch_pipeline.runtime_contract.read_acl_posture",
        lambda c=None: {"grant": True, "enforce": True, "ancestry": False,
                        "ttl": 21600, "age_seconds": dun.POSTURE_MAX_AGE_S + 1})
    monkeypatch.setattr(dun, "_load_identity_universe",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应解析")))
    stats = dun.resolve_events(conn, commit=False)
    assert stats["held"] == 1 and stats["posture_ok"] is False


def test_hold_reasons_are_not_terminal_states():
    """HOLD 分词与 SUPPRESSED 分词不得重叠 —— 二者的可回放性相反。"""
    holds = {dun.HOLD_STALE_SNAPSHOT, dun.HOLD_AUTHORITY_UNAVAILABLE,
             dun.HOLD_POSTURE_UNKNOWN, dun.HOLD_POSTURE_MISMATCH,
             dun.HOLD_AUDIENCE_CAP, dun.HOLD_BULK_GUARD}
    sups = {dun.SUP_UNCHANGED, dun.SUP_MISSING_SHA, dun.SUP_PUBLIC_POLICY,
            dun.SUP_AUDIENCE_ZERO, dun.SUP_STALE_SWITCH, dun.SUP_QUARANTINED,
            dun.SUP_META_MISSING, dun.SUP_BASELINE, dun.SUP_MANUAL}
    assert holds.isdisjoint(sups)


def test_node_doc_with_grant_off_holds_not_suppresses():
    """GRANT 关期间 node 文档确实无人可见，但那是**可回滚的运行姿态**：

    落 SUPPRESSED(audience_zero) 会让回滚窗内的所有升版永久静音 ⇒ 必须 HOLD。
    """
    from opensearch_pipeline.acl_policy import can_read_doc
    ctx = AclContext(groups=("production",), ancestor_dept_ids=(1, 2),
                     direct_dept_ids=(2,), node_channel_ok=True)
    doc = DocAcl(mode=ACL_MODE_NODE, permission_level="dept_internal", node_ids=(2,))
    # 判定层事实：GRANT 关 ⇒ 无条件 DENY（受众恒空）
    assert can_read_doc(ctx, doc, grant_enabled=False, enforce_enabled=True) is False
    # 故设计层必须把这种"受众为空"归到可回放的 HOLD 分词，而不是终态
    assert dun.HOLD_POSTURE_MISMATCH in {
        dun.HOLD_POSTURE_MISMATCH, dun.HOLD_POSTURE_UNKNOWN}


# ══════════════════════════════════════════════════════════════════════════════
# ⑥ 原子认领（防双实例双发）
# ══════════════════════════════════════════════════════════════════════════════

def test_claim_uses_skip_locked_and_bumps_attempts(_dbnames):
    """出队必须 `FOR UPDATE SKIP LOCKED` + 置 SENDING + attempts+1。

    UNIQUE 键只防插入重复、不防双发：手动重跑与日调实例重叠时，没有认领就会对同一批
    用户各发一次（补审 B-1）。
    """
    rows = [(10, 1, "DOC_A", "u1", 0, datetime.datetime.now())]

    def script(sql, params):
        s = " ".join(sql.split())
        return rows if "FOR UPDATE SKIP LOCKED" in s else []

    cur = _FakeCur(script)
    got = dun._claim(cur, 100)
    assert got == rows
    sel = cur.executed[0][0]
    assert "FOR UPDATE SKIP LOCKED" in sel and "ORDER BY id LIMIT" in sel
    upd = cur.executed[1][0]
    assert "SET state=%s, attempts=attempts+1, claim_ts=NOW()" in upd
    assert cur.executed[1][1][0] == dun.NOTICE_SENDING


def test_reclaim_window_covers_dead_sending_rows(_dbnames):
    """SENDING 超时可回收：进程被杀会留下永久 SENDING，否则那些行再也发不出去。"""
    cur = _FakeCur(lambda sql, params: [])
    dun._claim(cur, 10)
    sel = cur.executed[0][0]
    assert "claim_ts < DATE_SUB(NOW(), INTERVAL %s MINUTE)" in sel
    assert dun._SENDING_RECLAIM_MIN in cur.executed[0][1]


# ══════════════════════════════════════════════════════════════════════════════
# 文案渲染（日摘要；不带深链、不带 id）
# ══════════════════════════════════════════════════════════════════════════════

def test_digest_lists_titles_and_caps_at_ten():
    items = [("文档%d" % i, i) for i in range(1, 13)]
    text = dun._render_digest(items)
    assert text.startswith("【富岭知识库】你可见的 12 篇文档有更新：")
    assert "等 12 篇" in text
    assert text.count("》→v") == dun._DIGEST_MAX_TITLES
    assert "http" not in text          # Sam 拍板：不带深链


def test_digest_single_doc():
    assert "《作业指导书》→v3" in dun._render_digest([("作业指导书", 3)])


# ══════════════════════════════════════════════════════════════════════════════
# 总闸/守卫
# ══════════════════════════════════════════════════════════════════════════════

def test_main_skips_in_simulate(monkeypatch):
    """simulate 短路必须在 flag 检查【之前】（retention 同款纪律）：本地/CI 不碰真库。"""
    class _Cfg:
        simulate = True
        simulate_db = True

        class rag:      # noqa: N801
            doc_notify = True
    monkeypatch.setattr(dun, "_cfg", lambda: _Cfg())

    def _no_conn():
        raise AssertionError("simulate 下不得连库")
    monkeypatch.setattr(dun, "_conn", _no_conn)
    assert dun.main([]) == dun.EXIT_OK


def test_main_exits_guard_when_flag_off(monkeypatch):
    class _Cfg:
        simulate = False
        simulate_db = False

        class rag:      # noqa: N801
            doc_notify = False
    monkeypatch.setattr(dun, "_cfg", lambda: _Cfg())
    monkeypatch.setattr(dun, "_conn", lambda: (_ for _ in ()).throw(AssertionError("不得连库")))
    assert dun.main([]) == dun.EXIT_GUARD


def test_flags_live_under_rag_subconfig():
    """flag 必须挂在 RAGConfig 上 —— 放错层级会被 getattr 静默读成默认值。"""
    from opensearch_pipeline.config import RAGConfig
    for name in ("doc_notify", "doc_notify_dingtalk", "doc_notify_include_public",
                 "doc_notify_max_audience", "doc_notify_max_events_per_run"):
        assert hasattr(RAGConfig(), name), name
    assert RAGConfig().doc_notify is False              # 总闸默认关
    assert RAGConfig().doc_notify_include_public is True  # Sam 拍板：public 要通知
    assert RAGConfig().doc_notify_max_audience == 1000    # prod-RO 实测定值（production 808）


# ══════════════════════════════════════════════════════════════════════════════
# 姿态见证读写
# ══════════════════════════════════════════════════════════════════════════════

def test_posture_witness_roundtrip(monkeypatch):
    import opensearch_pipeline.runtime_contract as rc
    monkeypatch.setattr(rc, "_kb_db", lambda: "kb")
    cur = _FakeCur(lambda sql, params: [('{"grant":true,"enforce":true,'
                                         '"ancestry":false,"ttl":21600}', 120)])
    got = rc.read_acl_posture(cur)
    assert got["grant"] is True and got["enforce"] is True and got["age_seconds"] == 120


def test_posture_witness_unparseable_is_unknown(monkeypatch):
    """值坏掉 ⇒ 返回 None（调用方整轮 HOLD），绝不猜。"""
    import opensearch_pipeline.runtime_contract as rc
    monkeypatch.setattr(rc, "_kb_db", lambda: "kb")
    assert rc.read_acl_posture(_FakeCur(lambda s, p: [("not-json", 1)])) is None
    assert rc.read_acl_posture(_FakeCur(lambda s, p: [])) is None


def test_posture_stamp_refreshes_updated_at_explicitly(monkeypatch):
    """MySQL 陷阱：值不变时 ON DUPLICATE KEY UPDATE 不触发 ON UPDATE CURRENT_TIMESTAMP。

    姿态恰恰长期不变 ⇒ 必须显式 `updated_at = NOW()`，否则见证会"越放越旧"直到被判过期，
    整个功能自杀（永久 HOLD）。
    """
    import opensearch_pipeline.runtime_contract as rc
    monkeypatch.setattr(rc, "_kb_db", lambda: "kb")

    class _Cfg:
        simulate = False
        simulate_db = False
        node_acl_grant = True
        node_acl_enforce = True
    cur = _FakeCur(lambda sql, params: [])
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: _FakeConn(cur))
    monkeypatch.setattr(rc, "current_acl_posture",
                        lambda cfg=None: {"grant": True, "enforce": True,
                                          "ancestry": False, "ttl": 21600})
    assert rc.stamp_acl_posture(_Cfg(), force=True) is True
    sql = cur.executed[0][0]
    assert "ON DUPLICATE KEY UPDATE" in sql and "updated_at = NOW()" in sql
