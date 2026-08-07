# -*- coding: utf-8 -*-
"""贡献域 node 轴（schema/067）× 真实 MySQL —— 方案「测试」章的 7 条 + 反证锚 + 一致性用例。

方案：docs/contribution_node_axis_migration_2026-08-06.md

## 为什么必须是真库

桩游标不校验列名也不校验轴：`_FakeCur` 里 `category_dept_id` 和 `category_dept` 都只是
返回值里的一个位置，**「node 行不被组码腿命中」这条不变量在桩上恒真**（因为桩根本不跑
SQL 谓词）。而这正是本方案要防的越权面。同族教训：066（现网 NOT NULL 严于权威 DDL、
桩抓不到）、`stubs-that-flatten-interfaces`。所以队列作用域、孤儿判定、落库三字段、
默认 subtree grant 这几条一律落真库。

真实 DML：全部按本模块专属固定主键 seed / 精确清理，开跑先预清一次故可重入。
本模块在 `conftest._LOCAL_STACK_SERIAL_MODULES` 里（与 test_pipeline 的无 WHERE 整表
DML 同库同表族，不进组会被连坐清掉种子）。
"""
import pytest

from tests.local_stack import requires_local_db

# ── 本模块专属固定主键（都带 NAX 前缀，绝不触碰库里其他行）──────────────────
_ADMIN_NODE = "NAX-admin-node"      # 只持 node 管辖根的 dept_admin
_ADMIN_LEGACY = "NAX-admin-legacy"  # 只持组码授权的 dept_admin
_KB_ADMIN = "NAX-kbadmin"
_AUTHOR = "NAX-author"

# 组织树：910(depth1) → 920(depth2，管辖根) → 930(depth3)；940 = 无人管辖的 depth1
_D1, _D2, _D3, _ORPHAN = 910001, 920001, 930001, 940001
_ROOT_PARENT = 1                    # 钉钉虚拟根

_C_NODE = "NAX_CONTRIB_NODE"        # 挂 930（管辖根后代）
_C_ORPHAN = "NAX_CONTRIB_ORPHAN"    # 挂 940（无人管辖）
_C_LEGACY = "NAX_CONTRIB_LEGACY"    # 组码轴 hr
# 🔴 M8 迁移行的形态：**同时**有 node 归属和组码残值（category_dept='hr' 留痕不清）。
# 这是全库唯一带残值的 node 形态，也是「轴隔离」这条不变量真正被考验的地方——
# 不带这一条，任何"node 行也回落组码判定"的实现变异都测不出来（node 行组码为空串时
# 组码腿恒不命中，越权不显形）。
_C_MIGRATED = "NAX_CONTRIB_MIGRATED"
_ALL_CIDS = (_C_NODE, _C_ORPHAN, _C_LEGACY, _C_MIGRATED)


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


def _dbs():
    from opensearch_pipeline.api import _kb_db
    from opensearch_pipeline.qa_logger import _op_db
    return _kb_db(), _op_db()


def _skip_if_axis_missing(conn):
    """067 未 apply 的库直接 skip —— 本模块测的就是 067 之后的行为，
    在缺列的库上跑只会得到「全部回落组码」的假绿。"""
    kb_db, op_db = _dbs()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s"
            " AND TABLE_NAME='kb_contribution' AND COLUMN_NAME='category_dept_id'", (op_db,))
        if not (cur.fetchone() or (0,))[0]:
            pytest.skip(f"{op_db}.kb_contribution.category_dept_id 缺失（schema/067 未 apply）")


def _cleanup(conn):
    kb_db, op_db = _dbs()
    with conn.cursor() as cur:
        ph = ",".join(["%s"] * len(_ALL_CIDS))
        cur.execute(f"DELETE FROM {op_db}.kb_contribution WHERE contribution_id IN ({ph})",
                    _ALL_CIDS)
        # 端点自己生成的 contribution_id（提交用例）不在固定清单里 —— 按作者兜底清，
        # 否则一次失败的跑就会给后续跑留下"上一次的行"（实测踩过）。作者 id 是本模块专属。
        cur.execute(f"DELETE FROM {op_db}.kb_contribution WHERE author_id LIKE 'NAX-%%'")
        cur.execute(f"DELETE FROM {kb_db}.kb_doc_node_grant WHERE granted_by LIKE 'NAX-%%'")
        cur.execute(f"DELETE FROM {kb_db}.document_version WHERE doc_id LIKE 'NAX-DOC%%'")
        cur.execute(f"DELETE FROM {kb_db}.document_meta WHERE doc_id LIKE 'NAX-DOC%%'")
        for uid in (_ADMIN_NODE, _ADMIN_LEGACY, _KB_ADMIN, _AUTHOR):
            cur.execute(f"DELETE FROM {kb_db}.user_role WHERE user_id=%s", (uid,))
            cur.execute(f"DELETE FROM {kb_db}.dept_admin_grant WHERE user_id=%s", (uid,))
            cur.execute(f"DELETE FROM {kb_db}.dept_admin_node_grant WHERE user_id=%s", (uid,))
        cur.execute(f"DELETE FROM {kb_db}.staff_dim WHERE staff_id LIKE 'NAX-%%'")
        cur.execute(f"DELETE FROM {kb_db}.dept_dim WHERE dept_id IN (%s,%s,%s,%s)",
                    (_D1, _D2, _D3, _ORPHAN))
    conn.commit()
    _clear_caches()


def _clear_caches():
    """组织快照/后代集/067 capability 都是**进程内缓存**，跨用例必须清，否则上一条用例的
    树会漏进下一条（本模块每条用例都在改 dept_dim / grant）。"""
    from opensearch_pipeline import org_sync
    from opensearch_pipeline.dingtalk_identity import _org_snapshot_cache
    from opensearch_pipeline.routes import contribution as CT
    org_sync._children_cache_clear()
    _org_snapshot_cache.clear()
    CT._CONTRIB_AXIS_PRESENT.clear()


def _seed(conn, *, node_admin_root=_D2, orphan_active=True):
    """组织树 + 两个 dept_admin + kb_admin + 三条贡献行。dept_dim.synced_at 取 NOW()
    （快照 >48h 会让整条 node 通道 fail-closed，那是用例 4 才要的状态）。"""
    kb_db, op_db = _dbs()
    with conn.cursor() as cur:
        for did, pid, depth, name, active in (
            (_D1, _ROOT_PARENT, 1, "NAX 一级中心", 1),
            (_D2, _D1, 2, "NAX 二级部门", 1),
            (_D3, _D2, 3, "NAX 三级班组", 1),
            (_ORPHAN, _ROOT_PARENT, 1, "NAX 无人管辖中心", 1 if orphan_active else 0),
        ):
            cur.execute(
                f"INSERT INTO {kb_db}.dept_dim (dept_id, parent_id, name, depth, is_active,"
                " snapshot_rev, synced_at) VALUES (%s,%s,%s,%s,%s,1,NOW())"
                " ON DUPLICATE KEY UPDATE parent_id=VALUES(parent_id), name=VALUES(name),"
                " depth=VALUES(depth), is_active=VALUES(is_active), synced_at=NOW()",
                (did, pid, name, depth, active))
        for uid, role in ((_ADMIN_NODE, "dept_admin"), (_ADMIN_LEGACY, "dept_admin"),
                          (_KB_ADMIN, "kb_admin"), (_AUTHOR, "employee")):
            cur.execute(f"INSERT INTO {kb_db}.user_role (user_id, user_name, role, is_active)"
                        " VALUES (%s,%s,%s,1)", (uid, uid, role))
        cur.execute(f"INSERT INTO {kb_db}.dept_admin_node_grant"
                    " (user_id, managed_dept_id, source, is_active) VALUES (%s,%s,'manual',1)",
                    (_ADMIN_NODE, node_admin_root))
        cur.execute(f"INSERT INTO {kb_db}.dept_admin_grant"
                    " (user_id, managed_owner_dept, is_active) VALUES (%s,'hr',1)",
                    (_ADMIN_LEGACY,))
        cur.execute(f"INSERT INTO {kb_db}.staff_dim (staff_id, dept_ids, primary_dept, is_active,"
                    " snapshot_rev, synced_at) VALUES (%s,%s,%s,1,1,NOW())",
                    (_AUTHOR, str(_D2), _D2))
        for cid, dept, dept_id in ((_C_NODE, "", _D3), (_C_ORPHAN, "", _ORPHAN),
                                   (_C_LEGACY, "hr", None), (_C_MIGRATED, "hr", _D3)):
            cur.execute(
                f"INSERT INTO {op_db}.kb_contribution (contribution_id, question, content,"
                " category_dept, category_dept_id, author_id, author_name, review_status,"
                " ingestion_status) VALUES (%s,%s,%s,%s,%s,%s,'NAX 作者','pending','none')",
                (cid, f"{cid} 的问题？", f"{cid} 的答案正文，足够长以过校验。" * 3,
                 dept, dept_id, _AUTHOR))
    conn.commit()
    _clear_caches()


# simulate 下 resolve_kb_identity 从 env 构造身份（dingtalk_identity.py:883-895），
# 故"换个身份"= 换这三个环境变量。node 管辖根走 RAG_SIM_MANAGED_NODE_ROOTS。
_PERSONA = {
    _ADMIN_NODE: ("dept_admin", "", str(_D2)),
    _ADMIN_LEGACY: ("dept_admin", "hr", ""),
    _KB_ADMIN: ("kb_admin", "", ""),
    _AUTHOR: ("employee", "", ""),
}


def _become(monkeypatch, uid):
    role, owners, roots = _PERSONA[uid]
    monkeypatch.setenv("RAG_SIM_USER_ROLE", role)
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", owners)
    monkeypatch.setenv("RAG_SIM_MANAGED_NODE_ROOTS", roots)
    from opensearch_pipeline.api import Identity
    return Identity(user_id=uid, acl_groups=["hr"], name=uid)


def _no_rate_limit(monkeypatch):
    """⚠️ 必须打在 **routes.contribution** 上：该模块顶层是 `from ...api import
    _enforce_rate_limit`（拷贝绑定），patch `api._enforce_rate_limit` 对它无效。"""
    from opensearch_pipeline.routes import contribution as CT
    monkeypatch.setattr(CT, "_enforce_rate_limit", lambda *a, **k: None)


def _accept_ok(cid, uid, monkeypatch, *, attempts=4):
    """采纳并确保物化成功；**只**对 1213 死锁走既有恢复路径（retry-ingestion 同键续跑）。

    为什么需要它：本地/CI 是**共享的**一台 MySQL，别的 pytest 进程（老 worktree、并行的
    第二个 `make test`）会对 `document_meta` 跑无 WHERE 的整表 DML —— 与本用例的
    `INSERT ... ON DUPLICATE KEY UPDATE` 互为死锁对手，实测 ~1/5 全量跑复现一次。
    这是**环境并发**，不是产品缺陷：物化本就不假设跨系统原子，失败记 `ingestion_error`、
    固定键留存、由 retry 同键续跑 —— 组码版 `_materialize_contribution` 自始就是这个契约。
    所以这里按契约恢复，而**不是**放宽断言：非 1213 的失败一律当场红，连续 attempts 次
    都死锁才 skip（并说清是谁在撞），绝不把环境噪声记成绿、也不记成产品红。
    """
    from opensearch_pipeline import api
    r = api.kb_contribution_accept(cid=cid, req=api.KbContributionAcceptRequest(),
                                   request=None, identity=_become(monkeypatch, uid))
    for _ in range(attempts - 1):
        if r.ok:
            return r
        if "1213" not in (r.error or "") and "Deadlock" not in (r.error or ""):
            break
        r = api.kb_contribution_retry(cid=cid, request=None,
                                      identity=_become(monkeypatch, uid))
    if not r.ok and ("1213" in (r.error or "") or "Deadlock" in (r.error or "")):
        pytest.skip(f"共享本地 MySQL 上连续 {attempts} 次死锁（另一进程在对 document_meta "
                    f"跑无 WHERE DML）——本 run 无法证伪产品行为：{r.error}")
    assert r.ok, f"accept 应成功：ing={r.ingestion_status} err={r.error}"
    return r


def _pending_ids(uid, monkeypatch):
    from opensearch_pipeline import api
    _no_rate_limit(monkeypatch)
    ident = _become(monkeypatch, uid)
    resp = api.kb_contributions_pending(request=None, limit=50, offset=0, identity=ident)
    return {i.contribution_id for i in resp.items}


@pytest.fixture
def db(monkeypatch):
    _skip_if_not_sim()
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    _skip_if_axis_missing(conn)
    _cleanup(conn)
    # 通知挂点默认关（RAG_ADMIN_NOTIFY 未设），此处再显式钉死：本模块不测通知
    monkeypatch.delenv("RAG_ADMIN_NOTIFY", raising=False)
    try:
        yield conn
    finally:
        _cleanup(conn)
        conn.close()


# ── 用例 1：管辖根 depth2 + 贡献挂 depth3 后代 ⇒ 可见 + accept 成功 + 落库正确 ──
@requires_local_db
def test_node_admin_sees_and_adopts_descendant_contribution(db, monkeypatch):
    """方案测试 1。四条断言缺一不可：可见 / accept 成功 / node 文档三字段 / 默认 subtree grant。

    第四条是 Codex C7 BLOCKER 的活体守卫：缺了 `kb_doc_node_grant` 这行，文档
    **谁都搜不到**，而前三条断言全绿——「采纳成功」在界面上完全看不出这个洞。"""
    kb_db, _ = _dbs()
    _seed(db)
    _no_rate_limit(monkeypatch)
    monkeypatch.setattr("opensearch_pipeline.env_guard.assert_metadata_write_allowed",
                        lambda *a, **k: None)

    assert _C_NODE in _pending_ids(_ADMIN_NODE, monkeypatch), "管辖根后代的贡献必须可见"

    resp = _accept_ok(_C_NODE, _ADMIN_NODE, monkeypatch)
    assert resp.doc_id

    db.commit()
    with db.cursor() as cur:
        cur.execute(f"SELECT owner_dept, acl_mode, owner_dept_id, status, current_version_no"
                    f" FROM {kb_db}.document_meta WHERE doc_id=%s", (resp.doc_id,))
        meta = cur.fetchone()
        assert meta, "node 文档未落 document_meta"
        assert meta[0] is None, f"node 文档 owner_dept 必须 NULL（组码残值=越权面），实为 {meta[0]!r}"
        assert meta[1] == "node" and int(meta[2]) == _D3 and meta[3] == "active"
        assert int(meta[4]) == 1
        cur.execute(f"SELECT dept_id, scope FROM {kb_db}.kb_doc_node_grant"
                    " WHERE doc_id=%s AND revoked_at IS NULL", (resp.doc_id,))
        grants = cur.fetchall()
    assert [(int(g[0]), g[1]) for g in grants] == [(_D3, "subtree")], \
        f"默认可见范围必须是归属节点 subtree（裁决 D），实为 {grants}"


# ── 反证锚：强制关掉 node 腿，用例 1 必须回到全落兜底 ──────────────────────
@requires_local_db
def test_counterproof_node_leg_off_falls_back_to_kb_admin(db, monkeypatch):
    """反证锚（方案「测试」章）：把后代集打成不可得（快照失效的等价注入）后，
    用例 1 的可见性必须**消失**并落到 kb_admin 兜底。

    没有这条，用例 1 的绿可能来自「作用域根本没生效、谁都看得见」——那是最坏的假绿。"""
    from opensearch_pipeline.routes import contribution as CT
    _seed(db)
    _no_rate_limit(monkeypatch)
    monkeypatch.setattr(CT, "_kb_managed_descendants", lambda _kb: None)

    assert _C_NODE not in _pending_ids(_ADMIN_NODE, monkeypatch), \
        "后代集不可得时 node 腿必须整腿去掉（fail-closed）"
    # kb_admin 侧：全集不可得 ⇒ node 支 fail-open，宁多看不丢件
    monkeypatch.setattr(CT, "_all_managed_node_descendants", lambda _cur: None)
    assert {_C_NODE, _C_ORPHAN} <= _pending_ids(_KB_ADMIN, monkeypatch), \
        "node 管辖全集不可得时 kb_admin 必须看得到全部 node 行（Codex C9 fail-open）"


# ── 用例 2：贡献挂未授权 depth1 ⇒ 不可见 + accept 403 + 落 kb_admin 兜底 ────
@requires_local_db
def test_unmanaged_node_contribution_is_invisible_and_falls_back(db, monkeypatch):
    from fastapi import HTTPException

    from opensearch_pipeline import api
    _seed(db)
    _no_rate_limit(monkeypatch)

    assert _C_ORPHAN not in _pending_ids(_ADMIN_NODE, monkeypatch)
    with pytest.raises(HTTPException) as ei:
        api.kb_contribution_accept(cid=_C_ORPHAN, req=api.KbContributionAcceptRequest(),
                                   request=None, identity=_become(monkeypatch, _ADMIN_NODE))
    assert ei.value.status_code == 403
    assert _C_ORPHAN in _pending_ids(_KB_ADMIN, monkeypatch), "无人管辖的 node 行必须落 kb_admin 兜底"


# ── 用例 3：category_dept_id IS NULL ⇒ 与改动前逐字节一致 ────────────────
@requires_local_db
def test_legacy_rows_keep_group_code_semantics(db, monkeypatch):
    """组码轴行：hr 管理员看得见、node 管理员看不见、kb_admin 孤儿规则不变（hr 有管理员 ⇒ 不在兜底队列）。"""
    _seed(db)
    assert _C_LEGACY in _pending_ids(_ADMIN_LEGACY, monkeypatch)
    assert _C_LEGACY not in _pending_ids(_ADMIN_NODE, monkeypatch), \
        "node 管理员绝不能靠组码残值看到 legacy 行（轴隔离）"
    assert _C_LEGACY not in _pending_ids(_KB_ADMIN, monkeypatch), \
        "hr 有 active dept_admin ⇒ legacy 孤儿规则下不进 kb_admin 兜底（改动前语义）"


# ── 轴隔离的**反向**证据：M8 迁移行（node 归属 + 组码残值）不得被组码管理员命中 ──
@requires_local_db
def test_migrated_row_with_residual_group_code_is_node_only(db, monkeypatch):
    """🔴 这条是「两支互斥、不 OR 交叉」的真正判据（方案 M2 / Codex C3）。

    M8 迁移把 4 行的 `category_dept_id` 填上，**保留** `category_dept='hr'` 留痕。若判定
    对 node 行还回落一次组码腿（哪怕只是"再试一次"），持 hr 组码授权、**零 node 授权**的
    管理员就能审这些行 —— 与 060 阶段 A 的 `owner_dept` 残值越权同型。
    队列侧与鉴权侧都要证：只测其中一处，另一处漂了照样绿。"""
    from fastapi import HTTPException

    from opensearch_pipeline import api
    _seed(db)
    _no_rate_limit(monkeypatch)

    assert _C_MIGRATED not in _pending_ids(_ADMIN_LEGACY, monkeypatch), \
        "组码管理员不得因 category_dept 残值在队列里看到 node 行"
    assert _C_MIGRATED in _pending_ids(_ADMIN_NODE, monkeypatch), \
        "迁移后该行应归 node 管辖者"

    with pytest.raises(HTTPException) as ei:
        api.kb_contribution_reject(cid=_C_MIGRATED, req=api.KbContributionRejectRequest(note="越权探针"),
                                   request=None, identity=_become(monkeypatch, _ADMIN_LEGACY))
    assert ei.value.status_code == 403, "鉴权侧同样不得回落组码腿"


# ── 用例 4：快照失效 ⇒ dept_admin 全 False；kb_admin 仅 node 全集，legacy 孤儿规则不变 ──
@requires_local_db
def test_stale_snapshot_fail_closed_for_dept_admin_only(db, monkeypatch):
    from opensearch_pipeline.routes import contribution as CT
    _seed(db)
    monkeypatch.setattr(CT, "_kb_managed_descendants", lambda _kb: None)
    monkeypatch.setattr(CT, "_all_managed_node_descendants", lambda _cur: None)

    assert _pending_ids(_ADMIN_NODE, monkeypatch) == set(), "node 管理员必须 fail-closed"
    assert _C_LEGACY in _pending_ids(_ADMIN_LEGACY, monkeypatch), \
        "组码管理员不受 node 快照影响（两轴独立）"
    kb_seen = _pending_ids(_KB_ADMIN, monkeypatch)
    assert {_C_NODE, _C_ORPHAN} <= kb_seen
    assert _C_LEGACY not in kb_seen, "fail-open **只**作用于 node 支，legacy 孤儿规则分毫不动"


# ── 用例 5：retry 与 accept 结论一致 ────────────────────────────────────
@requires_local_db
def test_retry_matches_accept_verdict(db, monkeypatch):
    """采纳后由**无权**的 node 管理员点重试 ⇒ 403；有权者 ⇒ 通过。
    Codex C2：retry 整条路径原本遗漏 node 轴，会与 accept 得出相反结论。"""
    from fastapi import HTTPException

    from opensearch_pipeline import api
    _seed(db)
    _no_rate_limit(monkeypatch)
    monkeypatch.setattr("opensearch_pipeline.env_guard.assert_metadata_write_allowed",
                        lambda *a, **k: None)
    _accept_ok(_C_NODE, _ADMIN_NODE, monkeypatch)
    db.commit()

    with pytest.raises(HTTPException) as ei:
        api.kb_contribution_retry(cid=_C_NODE, request=None, identity=_become(monkeypatch, _ADMIN_LEGACY))
    assert ei.value.status_code == 403, "组码管理员绝不能靠组码残值重试 node 行"
    r2 = api.kb_contribution_retry(cid=_C_NODE, request=None, identity=_become(monkeypatch, _ADMIN_NODE))
    assert r2.ok, f"有管辖权者重试应成功：{r2.error}"


# ── 用例 6：节点提交后被停用 ⇒ accept / retry 403 ──────────────────────
@requires_local_db
def test_deactivated_node_blocks_accept_and_retry(db, monkeypatch):
    """⚠️ 身份**必须**用 kb_admin，否则这条测的不是 M5：
    `load_children_index` 只读 `is_active=1`，节点一停用就自动掉出 dept_admin 的后代集，
    于是 403 来自「管辖判定」而非「active 现查」—— 把 M5 三处删光这条照绿（实测变异存活）。
    kb_admin 在 `_contrib_can_manage` 里直接短路 True，只有独立的 active 现查能拦住它，
    所以它才是这条不变量的唯一有效探针。"""
    from fastapi import HTTPException

    from opensearch_pipeline import api
    kb_db, _ = _dbs()
    _seed(db)
    _no_rate_limit(monkeypatch)
    monkeypatch.setattr("opensearch_pipeline.env_guard.assert_metadata_write_allowed",
                        lambda *a, **k: None)

    # (a) M5 第 3 处：先正常 accept 造出可 retry 的行，再停用节点
    _accept_ok(_C_NODE, _KB_ADMIN, monkeypatch)
    with db.cursor() as cur:
        cur.execute(f"UPDATE {kb_db}.dept_dim SET is_active=0 WHERE dept_id=%s", (_D3,))
    db.commit()
    _clear_caches()
    with pytest.raises(HTTPException) as ei:
        api.kb_contribution_retry(cid=_C_NODE, request=None,
                                  identity=_become(monkeypatch, _KB_ADMIN))
    assert ei.value.status_code == 403, "节点停用后 retry 必须 403（M5 第 3 处）"

    # (b) M5 第 2 处：另一条仍 pending 的行，节点已停用 ⇒ accept 直接 403
    with db.cursor() as cur:
        cur.execute(f"UPDATE {kb_db}.dept_dim SET is_active=0 WHERE dept_id=%s", (_ORPHAN,))
    db.commit()
    _clear_caches()
    with pytest.raises(HTTPException) as ei2:
        api.kb_contribution_accept(cid=_C_ORPHAN, req=api.KbContributionAcceptRequest(),
                                   request=None, identity=_become(monkeypatch, _KB_ADMIN))
    assert ei2.value.status_code == 403, "节点停用后 accept 必须 403（M5 第 2 处）"


# ── M5 第 1 处：提交入口的 active 现查（服务端不信客户端传值）──────────────
@requires_local_db
def test_submit_rejects_inactive_node(db, monkeypatch):
    """提交入口挡的是**脏数据落库**：没有它，指向死节点的 pending 行会一直躺到 accept 才失败。"""
    from fastapi import HTTPException

    from opensearch_pipeline import api
    from opensearch_pipeline.routes import contribution as CT
    kb_db, op_db = _dbs()
    _seed(db)
    _no_rate_limit(monkeypatch)
    monkeypatch.setattr(CT, "_node_acl_grant_on", lambda: True)
    with db.cursor() as cur:
        cur.execute(f"UPDATE {kb_db}.dept_dim SET is_active=0 WHERE dept_id=%s", (_D3,))
    db.commit()
    _clear_caches()

    with pytest.raises(HTTPException) as ei:
        api.kb_contribution_submit(
            req=api.KbContributionSubmitRequest(
                question="死节点提交测试问题？", content="正文正文正文正文正文正文正文正文。",
                category_dept_id=_D3),
            request=None, identity=_become(monkeypatch, _AUTHOR))
    assert ei.value.status_code == 400
    db.commit()
    with db.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {op_db}.kb_contribution"
                    " WHERE category_dept_id=%s AND question LIKE '死节点提交测试%%'", (_D3,))
        assert cur.fetchone()[0] == 0, "被拒的提交绝不能留下 pending 行"


# ── 评审 C1：提交端的**归属越界**校验（服务端不信客户端传值，Sam 2026-08-07 裁决）──
@requires_local_db
def test_submit_rejects_node_outside_own_depts(db, monkeypatch):
    """`my-depts` 的收窄若只做在前端，构造 POST 就能把贡献投进任意部门的待审队列。

    _AUTHOR 直属 _D2，而 _D2 有活跃子节点 _D3 ⇒ 按裁决 G 他的可选集恰为 {_D3}。
    _D1 同样 active、且是他的**上级**——正因为「active」和「跟我有关系」是两回事，
    只查 active 的老实现会放行它。
    """
    from fastapi import HTTPException

    from opensearch_pipeline import api
    from opensearch_pipeline.routes import contribution as CT
    _kb_db, op_db = _dbs()
    _seed(db)
    _no_rate_limit(monkeypatch)
    monkeypatch.setattr(CT, "_node_acl_grant_on", lambda: True)
    _clear_caches()

    for target, why in ((_D1, "上级中心（active 但不是我的可选集）"),
                        (_ORPHAN, "无关的另一个一级中心")):
        with pytest.raises(HTTPException) as ei:
            api.kb_contribution_submit(
                req=api.KbContributionSubmitRequest(
                    question=f"越界提交测试问题 {target}？",
                    content="正文正文正文正文正文正文正文正文。",
                    category_dept_id=target),
                request=None, identity=_become(monkeypatch, _AUTHOR))
        assert ei.value.status_code == 403, why
    db.commit()
    with db.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {op_db}.kb_contribution"
                    " WHERE question LIKE '越界提交测试%%'")
        assert cur.fetchone()[0] == 0, "被拒的越界提交绝不能留下 pending 行"

    # 反证锚：同一条路径上，**自己可选集内**的节点必须照常放行——否则上面的 403
    # 可能只是「node 提交整条挂了」，而不是「越界被挡住」。
    _clear_caches()
    api.kb_contribution_submit(
        req=api.KbContributionSubmitRequest(
            question="可选集内提交应当成功？", content="正文正文正文正文正文正文正文正文。",
            category_dept_id=_D3),
        request=None, identity=_become(monkeypatch, _AUTHOR))
    db.commit()
    with db.cursor() as cur:
        cur.execute(f"SELECT category_dept_id FROM {op_db}.kb_contribution"
                    " WHERE question LIKE '可选集内提交应当成功%%'")
        assert [r[0] for r in cur.fetchall()] == [_D3]


# ── 用例 7：flag 关 ⇒ 提交 node 贡献 400（裁决 E，不静默回落组码轴）────────
@requires_local_db
def test_submit_node_contribution_rejected_when_flag_off(db, monkeypatch):
    from fastapi import HTTPException

    from opensearch_pipeline import api
    from opensearch_pipeline.routes import contribution as CT
    _seed(db)
    _no_rate_limit(monkeypatch)
    monkeypatch.setattr(CT, "_node_acl_grant_on", lambda: False)
    with pytest.raises(HTTPException) as ei:
        api.kb_contribution_submit(
            req=api.KbContributionSubmitRequest(
                question="停用闸测试问题？", content="正文正文正文正文正文正文正文正文。",
                category_dept_id=_D3),
            request=None, identity=_become(monkeypatch, _AUTHOR))
    assert ei.value.status_code == 400
    assert "RAG_NODE_ACL_GRANT" in str(ei.value.detail)


# ── /api/kb/my-depts：裁决 G 的「叶子返回自己 / 非叶子返回直接子节点集」──────
@requires_local_db
def test_my_depts_returns_children_for_non_leaf_and_self_for_leaf(db, monkeypatch):
    """作者直挂 _D2（有活跃子节点 _D3）⇒ 返回 **_D3**、不含 _D2 —— 这条就是裁决 G 消解
    L1 盲区的机制：直挂上级节点的人得以选一个有管理员的下级，而不是被迫挂在无人管辖的
    中心节点上。_D3 停用后 _D2 变叶子 ⇒ 返回 _D2 自己。"""
    from opensearch_pipeline import api
    from opensearch_pipeline.routes import contribution as CT
    kb_db, _ = _dbs()
    _seed(db)
    _no_rate_limit(monkeypatch)
    monkeypatch.setattr(CT, "_node_acl_grant_on", lambda: True)

    r = api.kb_my_depts(request=None, identity=_become(monkeypatch, _AUTHOR))
    assert r.available is True
    assert [i.dept_id for i in r.items] == [_D3], \
        f"非叶子应返回直接子节点集（不含自己），实为 {[i.dept_id for i in r.items]}"

    with db.cursor() as cur:
        cur.execute(f"UPDATE {kb_db}.dept_dim SET is_active=0 WHERE dept_id=%s", (_D3,))
    db.commit()
    _clear_caches()
    r2 = api.kb_my_depts(request=None, identity=_become(monkeypatch, _AUTHOR))
    assert [i.dept_id for i in r2.items] == [_D2], "叶子应返回自己"

    # flag 关 ⇒ available=False（「算不出」≠「你没有可选部门」，前端据此回落组码轴）
    monkeypatch.setattr(CT, "_node_acl_grant_on", lambda: False)
    r3 = api.kb_my_depts(request=None, identity=_become(monkeypatch, _AUTHOR))
    assert r3.available is False and r3.items == []


# ── 一致性用例：列表可见但 accept 403 —— 单一入口成立则必红 ───────────────
@requires_local_db
def test_queue_visibility_and_accept_never_disagree(db, monkeypatch):
    """把「队列作用域」与「accept 鉴权」这两处判定对同一批行交叉验证。

    方案要求的一致性用例：单一判定入口（`_contrib_can_manage` / `_contrib_scope_sql`
    同源）成立时本用例恒绿；一旦有人在其中一处加了不对称谓词（历史上八个消费点各写各的
    就是这么裂开的），这里当场红。"""
    from fastapi import HTTPException

    from opensearch_pipeline import api
    _seed(db)
    _no_rate_limit(monkeypatch)
    monkeypatch.setattr("opensearch_pipeline.env_guard.assert_metadata_write_allowed",
                        lambda *a, **k: None)
    for uid in (_ADMIN_NODE, _ADMIN_LEGACY):
        visible = _pending_ids(uid, monkeypatch)
        for cid in _ALL_CIDS:
            try:
                api.kb_contribution_reject(cid=cid, req=api.KbContributionRejectRequest(note="一致性探针"),
                                           request=None, identity=_become(monkeypatch, uid))
                allowed = True
            except HTTPException as e:
                if e.status_code != 403:
                    raise
                allowed = False
            assert allowed == (cid in visible), (
                f"{uid} 对 {cid}：队列可见={cid in visible} 但鉴权放行={allowed} —— "
                "两处判定口径分裂（应共用 _contrib_can_manage / _contrib_scope_sql）")
            db.commit()
            # 驳回是终态，复位回 pending 供下一个身份复用同一批行
            with db.cursor() as cur:
                cur.execute(f"UPDATE {_dbs()[1]}.kb_contribution SET review_status='pending',"
                            " reviewed_by=NULL, reviewed_at=NULL WHERE contribution_id=%s", (cid,))
            db.commit()
