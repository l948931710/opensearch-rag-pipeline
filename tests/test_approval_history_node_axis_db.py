# -*- coding: utf-8 -*-
"""审批历史 `/api/kb/approval-history` 的 node 轴 × 真实 MySQL（codex 双盲评审 C2，2026-08-07）。

缺陷有**方向相反**的两半，本模块两半都钉：

  (a) fail-closed 半边 —— 旧实现在 `owners`（组码 grant 展开集）为空时直接
      `return items=[]`。只持 `dept_admin_node_grant` 的管理员一条组码 grant 都没有
      ⇒ **整页**审批历史消失，连他自己部门的 `kb_access_request` 记录也一起没了。
      prod-ro 实测：持 node 管辖根且不在 `dept_admin_grant` 里的用户 = 27 人（= 全部）。

  (b) 泄露半边 —— contribution 段只按 `category_dept` 收窄，完全不读 `category_dept_id`。
      M8 迁移的 4 行**有意保留** `category_dept='hr'` 作审计留痕（同时 category_dept_id
      非空），于是任何持 hr 组码 grant 的人都能在历史页看到它们，而审核队列
      （`_contrib_can_manage`）对同一个人是拒绝的 —— 正是 node 轴要消灭的「组码残值命中」。

## 为什么必须是真库

桩游标不跑 SQL 谓词：`_FakeCur` 里 `category_dept_id` / `acl_mode` 只是返回值里的一个
位置，「node 行不被组码腿命中」这条不变量在桩上**恒真**，而这恰恰是本次要防的越权面
（同族教训：`stubs-that-flatten-interfaces`、066 的现网 NOT NULL）。故作用域一律落真库。

真实 DML：全部按本模块专属固定主键 seed / 精确清理，开跑先预清一次故可重入。
本模块在 `conftest._LOCAL_STACK_SERIAL_MODULES` 里（与 test_pipeline 的无 WHERE 整表
DML 同库同表族，不进组会被连坐清掉种子，症状是「断言看到 0 行」而非报错）。
"""
import pytest

from tests.local_stack import requires_local_db

# ── 本模块专属固定主键（AHX 前缀，绝不触碰库里其他行）────────────────────────
_ADMIN_NODE = "AHX-admin-node"      # 只持 node 管辖根、**零组码 grant** —— 现网 27 人的形态
_ADMIN_LEGACY = "AHX-admin-legacy"  # 只持 hr 组码授权、零 node 授权
_KB_ADMIN = "AHX-kbadmin"

# 组织树：911001(depth1) → 921001(depth2，管辖根) → 931001(depth3，归属节点)
_D1, _D2, _D3 = 911001, 921001, 931001
_ROOT_PARENT = 1                    # 钉钉虚拟根

# 文档：一篇已迁 node（owner_dept=NULL），一篇仍组码 hr
_DOC_NODE, _DOC_LEGACY = "AHX-DOC-NODE", "AHX-DOC-LEGACY"
_T_NODE, _T_LEGACY = "AHX 节点归属文档", "AHX 组码归属文档"
# 申请人展示名 = 本模块识别 access 行的键（`subject` 不脱敏，见端点 DTO 注释）
_REQ_ON_NODE, _REQ_ON_LEGACY = "AHXREQNODE", "AHXREQLEGACY"

# 贡献：三种形态。作者名 = 识别键（`subject` 同样不脱敏）
_C_NODE, _A_NODE = "AHX_C_NODE", "AHXCNODE"              # 纯 node 轴（category_dept 空串哨兵）
_C_MIGRATED, _A_MIGRATED = "AHX_C_MIGRATED", "AHXCMIG"   # 🔴 M8 形态：node 归属 + 组码残值 'hr'
_C_LEGACY, _A_LEGACY = "AHX_C_LEGACY", "AHXCLEGACY"      # 纯组码轴（category_dept_id IS NULL）
_ALL_CIDS = (_C_NODE, _C_MIGRATED, _C_LEGACY)


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


def _dbs():
    from opensearch_pipeline.api import _kb_db
    from opensearch_pipeline.qa_logger import _op_db
    return _kb_db(), _op_db()


def _skip_if_axes_missing(conn):
    """060 / 067 任一未 apply 的库直接 skip —— 缺列时两段都回落组码语义，
    在那种库上跑本模块只会得到「全部回落」的假绿。"""
    kb_db, op_db = _dbs()
    with conn.cursor() as cur:
        for schema, table, col, mig in (
            (kb_db, "document_meta", "acl_mode", "060"),
            (op_db, "kb_contribution", "category_dept_id", "067"),
        ):
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s"
                " AND TABLE_NAME=%s AND COLUMN_NAME=%s", (schema, table, col))
            if not (cur.fetchone() or (0,))[0]:
                pytest.skip(f"{schema}.{table}.{col} 缺失（schema/{mig} 未 apply）")


def _cleanup(conn):
    kb_db, op_db = _dbs()
    with conn.cursor() as cur:
        ph = ",".join(["%s"] * len(_ALL_CIDS))
        cur.execute(f"DELETE FROM {op_db}.kb_contribution WHERE contribution_id IN ({ph})",
                    _ALL_CIDS)
        cur.execute(f"DELETE FROM {kb_db}.kb_access_request WHERE doc_id LIKE 'AHX-DOC%%'")
        cur.execute(f"DELETE FROM {kb_db}.document_meta WHERE doc_id LIKE 'AHX-DOC%%'")
        for uid in (_ADMIN_NODE, _ADMIN_LEGACY, _KB_ADMIN):
            cur.execute(f"DELETE FROM {kb_db}.user_role WHERE user_id=%s", (uid,))
            cur.execute(f"DELETE FROM {kb_db}.dept_admin_grant WHERE user_id=%s", (uid,))
            cur.execute(f"DELETE FROM {kb_db}.dept_admin_node_grant WHERE user_id=%s", (uid,))
        cur.execute(f"DELETE FROM {kb_db}.dept_dim WHERE dept_id IN (%s,%s,%s)", (_D1, _D2, _D3))
    conn.commit()
    _clear_caches()


def _clear_caches():
    """组织快照/后代集/两轴 capability 都是**进程内缓存**，跨用例必须清（本模块每条用例
    都在改 dept_dim / grant）。node capability 的正向缓存由 conftest 的 autouse fixture 清。"""
    from opensearch_pipeline import org_sync
    from opensearch_pipeline.dingtalk_identity import _org_snapshot_cache
    from opensearch_pipeline.routes import contribution as CT
    org_sync._children_cache_clear()
    _org_snapshot_cache.clear()
    CT._CONTRIB_AXIS_PRESENT.clear()


def _seed(conn):
    """组织树 + 三个管理员 + 两篇文档 + 两条已决申请 + 三条已决贡献。

    ⚠️ 决策时间全部取 `NOW() + 1 天`：端点按 decided_at DESC 取前 200 条，本地共享库里
    存量行不受控，不钉死排序位就会随库内容变化间歇性被截掉（那是环境噪声不是产品行为）。
    """
    kb_db, op_db = _dbs()
    with conn.cursor() as cur:
        for did, pid, depth, name in (
            (_D1, _ROOT_PARENT, 1, "AHX 一级中心"),
            (_D2, _D1, 2, "AHX 二级部门"),
            (_D3, _D2, 3, "AHX 三级班组"),
        ):
            cur.execute(
                f"INSERT INTO {kb_db}.dept_dim (dept_id, parent_id, name, depth, is_active,"
                " snapshot_rev, synced_at) VALUES (%s,%s,%s,%s,1,1,NOW())"
                " ON DUPLICATE KEY UPDATE parent_id=VALUES(parent_id), name=VALUES(name),"
                " depth=VALUES(depth), is_active=1, synced_at=NOW()",
                (did, pid, name, depth))
        for uid, role in ((_ADMIN_NODE, "dept_admin"), (_ADMIN_LEGACY, "dept_admin"),
                          (_KB_ADMIN, "kb_admin")):
            cur.execute(f"INSERT INTO {kb_db}.user_role (user_id, user_name, role, is_active)"
                        " VALUES (%s,%s,%s,1)", (uid, uid, role))
        cur.execute(f"INSERT INTO {kb_db}.dept_admin_node_grant"
                    " (user_id, managed_dept_id, source, is_active) VALUES (%s,%s,'manual',1)",
                    (_ADMIN_NODE, _D2))
        cur.execute(f"INSERT INTO {kb_db}.dept_admin_grant"
                    " (user_id, managed_owner_dept, is_active) VALUES (%s,'hr',1)",
                    (_ADMIN_LEGACY,))

        # 文档：node 篇 owner_dept=NULL（060 契约），legacy 篇 owner_dept='hr'
        cur.execute(
            f"INSERT INTO {kb_db}.document_meta (doc_id, title, owner_dept, acl_mode,"
            " owner_dept_id, permission_level, status, current_version_no)"
            " VALUES (%s,%s,NULL,'node',%s,'dept_internal','active',1)",
            (_DOC_NODE, _T_NODE, _D3))
        cur.execute(
            f"INSERT INTO {kb_db}.document_meta (doc_id, title, owner_dept, acl_mode,"
            " owner_dept_id, permission_level, status, current_version_no)"
            " VALUES (%s,%s,'hr','legacy',NULL,'dept_internal','active',1)",
            (_DOC_LEGACY, _T_LEGACY))
        # 🔴 node 文档上那条申请的 owner_dept **仍是 'hr'** —— 申请落库时该文档还是组码轴，
        # 迁 node 之后这个快照就成了残值。它命中不命中旧组码管理员，正是本模块的判据。
        for doc_id, req_name, owner in ((_DOC_NODE, _REQ_ON_NODE, "hr"),
                                        (_DOC_LEGACY, _REQ_ON_LEGACY, "hr")):
            cur.execute(
                f"INSERT INTO {kb_db}.kb_access_request (doc_id, owner_dept, requester_id,"
                " requester_name, requester_depts, reason, status, decided_by, decided_at)"
                " VALUES (%s,%s,%s,%s,'quality','借阅参考','approved',%s,"
                " DATE_ADD(NOW(), INTERVAL 1 DAY))",
                (doc_id, owner, f"AHX-{req_name}", req_name, _KB_ADMIN))

        for cid, author, dept, dept_id, status in (
            (_C_NODE, _A_NODE, "", _D3, "accepted"),
            (_C_MIGRATED, _A_MIGRATED, "hr", _D3, "accepted"),
            (_C_LEGACY, _A_LEGACY, "hr", None, "rejected"),
        ):
            cur.execute(
                f"INSERT INTO {op_db}.kb_contribution (contribution_id, question, content,"
                " category_dept, category_dept_id, author_id, author_name, review_status,"
                " ingestion_status, reviewed_by, reviewed_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'none',%s, DATE_ADD(NOW(), INTERVAL 1 DAY))",
                (cid, f"{author} 的问题正文？", f"{author} 的答案正文，足够长以过校验。" * 3,
                 dept, dept_id, f"AHX-{author}", author, status, _KB_ADMIN))
    conn.commit()
    _clear_caches()


# simulate 下 resolve_kb_identity 从 env 构造身份（dingtalk_identity.py:883-895）。
# _ADMIN_NODE 的组码位**必须**留空 —— 那正是现网 27 人的形态，也是缺陷 (a) 的触发条件。
_PERSONA = {
    _ADMIN_NODE: ("dept_admin", "", str(_D2)),
    _ADMIN_LEGACY: ("dept_admin", "hr", ""),
    _KB_ADMIN: ("kb_admin", "", ""),
}


def _become(monkeypatch, uid):
    role, owners, roots = _PERSONA[uid]
    monkeypatch.setenv("RAG_SIM_USER_ROLE", role)
    monkeypatch.setenv("RAG_SIM_MANAGED_OWNER_DEPTS", owners)
    monkeypatch.setenv("RAG_SIM_MANAGED_NODE_ROOTS", roots)
    from opensearch_pipeline.api import Identity
    return Identity(user_id=uid, acl_groups=["hr"], name=uid)


def _no_rate_limit(monkeypatch):
    """⚠️ 必须打在 **routes.kb_access** 上：该模块顶层是 `from ...api import
    _enforce_rate_limit`（拷贝绑定），patch `api._enforce_rate_limit` 对它无效。"""
    from opensearch_pipeline.routes import kb_access as KA
    monkeypatch.setattr(KA, "_enforce_rate_limit", lambda *a, **k: None)


def _history(uid, monkeypatch):
    from opensearch_pipeline import api
    _no_rate_limit(monkeypatch)
    return api.kb_approval_history(request=None, identity=_become(monkeypatch, uid)).items


def _subjects(items, kind):
    return {i.subject for i in items if i.kind == kind}


def _require_access_join(conn):
    """access 段的 `kb_access_request ⋈ document_meta` 在**本地库 collation 漂移**时会
    1267 打挂整段 —— 那是环境缺陷，不是产品行为，不能记成红。

    漂移形态：库默认 collation 是 MySQL 8 的 `utf8mb4_0900_ai_ci`，凡**建表时没写显式
    COLLATE 的旧表**都拿了它；而带显式 `COLLATE=utf8mb4_unicode_ci` 的新表
    （kb_access_request 等）是对的。schema/001:91 与 schema/008 两份权威 DDL 都写的是
    utf8mb4_unicode_ci ⇒ 从零建库（`scripts/ci_load_schema.sh`）的环境没有这条漂移。
    同一根因在 2026-06-27 生产上炸过三个 JOIN 端点（见 schema/008 头注）。

    ⚠️ 本守卫**不是**为某一台机器写的，别因为「现在没人命中」就删：开发机 2026-08-07
    实测漂了 22 张表（已按权威 DDL 全量转回 unicode_ci，本 skip 在该机上不再命中），
    但任何不是从 ci_load_schema.sh 建出来的库——快照恢复、老 worktree、手工建的
    第二套库——都可能再漂回来，届时这里给的是一句能直接照做的修法，不是一条看不懂的红。
    """
    from opensearch_pipeline.api import _kb_db
    with conn.cursor() as cur:
        try:
            cur.execute(f"SELECT 1 FROM {_kb_db()}.kb_access_request r"
                        f" JOIN {_kb_db()}.document_meta m ON m.doc_id = r.doc_id LIMIT 1")
            cur.fetchall()
        except Exception as e:   # noqa: BLE001
            pytest.skip(f"本地库 collation 漂移，access 段 JOIN 不可用（环境缺陷，非产品）：{e}。"
                        " 修法：ALTER TABLE document_meta CONVERT TO CHARACTER SET utf8mb4"
                        " COLLATE utf8mb4_unicode_ci（对齐 schema/001 权威 DDL）")


@pytest.fixture
def db(monkeypatch):
    _skip_if_not_sim()
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    _skip_if_axes_missing(conn)
    _cleanup(conn)
    try:
        yield conn
    finally:
        _cleanup(conn)
        conn.close()


# ⚠️ 用例按段拆开（contribution / access 各自成条），不是啰嗦：access 段依赖
#    `kb_access_request ⋈ document_meta`，本机库有 collation 漂移会整段不可用。
#    混写会让**两段一起** skip，contribution 侧的覆盖凭空消失且没人看得出来。

# ── (a) fail-closed 半边 · contribution 段：零组码 grant 的 node 管理员不该拿到空页 ──
@requires_local_db
def test_node_admin_sees_contribution_history_instead_of_empty_page(db, monkeypatch):
    """旧实现对这个身份返回 `items=[]`（整页空），现网 27 人全员命中。"""
    _seed(db)
    items = _history(_ADMIN_NODE, monkeypatch)

    assert items, "零组码 grant 的 node 管理员拿到空页 —— 缺陷 (a) 复发"
    assert {_A_NODE, _A_MIGRATED} <= _subjects(items, "contribution"), \
        "管辖后代上的贡献历史必须可见（contribution 段 node 腿）"
    assert _A_LEGACY not in _subjects(items, "contribution"), \
        "他没有 hr 组码，组码轴的行一条都不该看到（轴隔离的另一半）"


# ── (a) fail-closed 半边 · access 段：被旧实现一起吞掉的那一半 ─────────────────
@requires_local_db
def test_node_admin_sees_access_history_on_node_doc(db, monkeypatch):
    """缺陷 (a) 吞的不只是贡献那一段 —— 早退发生在**取数之前**，`kb_access_request`
    的历史一并消失。这条单独钉住 access 段，否则 access 腿漂了 contribution 那条照样绿。"""
    _seed(db)
    _require_access_join(db)
    items = _history(_ADMIN_NODE, monkeypatch)

    assert _REQ_ON_NODE in _subjects(items, "access"), \
        "node 文档上的历史申请必须对其 node 管理员可见（access 段 node 腿）"
    assert _REQ_ON_LEGACY not in _subjects(items, "access"), \
        "组码轴文档的申请历史不该被 node 管理员看到（轴隔离）"


# ── 反证锚：把 node 腿打成不可得 ⇒ 上面两条的可见性必须消失 ────────────────────
@requires_local_db
def test_counterproof_node_leg_off_hides_node_rows_and_not_500(db, monkeypatch):
    """没有这条，上面的绿可能来自「作用域根本没生效、谁都看得见」——最坏的假绿。

    同时钉住降级形态：两腿都没有时该段只是 `AND 1=0`（空结果），**不是**子查询失败。
    子查询若抛异常，两段全失败会触发端点的「诚实 500」——那就把 fail-closed 变成了故障。
    """
    from opensearch_pipeline.routes import contribution as CT
    from opensearch_pipeline.routes import kb_access as KA
    _seed(db)
    monkeypatch.setattr(KA, "_kb_managed_descendants", lambda _kb: None)
    monkeypatch.setattr(CT, "_kb_managed_descendants", lambda _kb: None)

    items = _history(_ADMIN_NODE, monkeypatch)      # 不抛 500
    assert not ({_A_NODE, _A_MIGRATED} & _subjects(items, "contribution")), \
        "后代集不可得时 contribution 段 node 腿必须整腿去掉"
    assert _REQ_ON_NODE not in _subjects(items, "access"), \
        "后代集不可得时 access 段 node 腿必须整腿去掉（绝不回落 r.owner_dept 残值）"


# ── (b) 泄露半边 · contribution 段：M8 留痕的组码残值不得命中 hr 组码管理员 ──────
@requires_local_db
def test_legacy_admin_never_hit_by_contribution_residual(db, monkeypatch):
    """🔴 「两支互斥、不 OR 交叉」的判据。M8 迁移行保留 `category_dept='hr'`
    （`category_dept_id` 非空）——若判定对 node 行还回落一次组码腿（哪怕只是"再试一次"），
    持 hr 组码、**零 node 授权**的管理员就能在历史页看到它，而审核队列
    （`_contrib_can_manage`）对同一个人是拒绝的，两处口径当场分裂。"""
    _seed(db)
    items = _history(_ADMIN_LEGACY, monkeypatch)

    assert _A_MIGRATED not in _subjects(items, "contribution"), \
        "组码管理员不得因 category_dept 残值看到 node 贡献行（缺陷 (b)）"
    assert _A_LEGACY in _subjects(items, "contribution"), \
        "组码轴本身逐字节不变：他原本看得见的那条不能少"


# ── (b) 泄露半边 · access 段：迁 node 之前落的 r.owner_dept 快照同样是残值 ───────
@requires_local_db
def test_legacy_admin_never_hit_by_access_residual(db, monkeypatch):
    _seed(db)
    _require_access_join(db)
    items = _history(_ADMIN_LEGACY, monkeypatch)

    assert _REQ_ON_NODE not in _subjects(items, "access"), \
        "组码管理员不得因 r.owner_dept 残值看到 node 文档的申请历史"
    assert _REQ_ON_LEGACY in _subjects(items, "access"), \
        "组码轴文档的申请历史必须仍然可见（组码腿谓词未变一字节）"


# ── 响应口径：node 贡献行带 category_dept_id + 节点现名，不再只回组码残值 ────────
@requires_local_db
def test_node_contribution_rows_carry_node_owner_dto(db, monkeypatch):
    """M8 迁移行的 `owner_dept` 是 'hr'（留痕），照它渲染等于把一个**已经无权审**的部门
    写在历史里。展示口径必须走 owner_key/owner_label（前端 docOwnerText 优先读 key）。"""
    _seed(db)
    items = _history(_KB_ADMIN, monkeypatch)
    by_subject = {i.subject: i for i in items if i.kind == "contribution"}

    mig = by_subject.get(_A_MIGRATED)
    assert mig is not None, "kb_admin 应看得到全部贡献历史"
    assert mig.category_dept_id == _D3
    assert mig.owner_key == f"node:{_D3}" and mig.owner_label == "AHX 三级班组", \
        f"node 贡献行必须带节点现名，实为 key={mig.owner_key!r} label={mig.owner_label!r}"
    assert mig.owner_dept == "hr", "组码残值本身如实回传（审计留痕），只是不再是展示口径"

    leg = by_subject.get(_A_LEGACY)
    assert leg is not None
    assert leg.category_dept_id is None and leg.owner_key == "", \
        "组码轴贡献行的字段形态与 067 之前逐字节一致"


# ── kb_admin 全量口径不受影响（两段都不收窄）─────────────────────────────────
@requires_local_db
def test_kb_admin_still_sees_all_contributions(db, monkeypatch):
    _seed(db)
    assert {_A_NODE, _A_MIGRATED, _A_LEGACY} <= _subjects(
        _history(_KB_ADMIN, monkeypatch), "contribution")


@requires_local_db
def test_kb_admin_still_sees_all_access_rows(db, monkeypatch):
    _seed(db)
    _require_access_join(db)
    items = _history(_KB_ADMIN, monkeypatch)
    assert {_REQ_ON_NODE, _REQ_ON_LEGACY} <= _subjects(items, "access")
    acc = next(i for i in items if i.kind == "access" and i.subject == _REQ_ON_NODE)
    assert acc.owner_key == f"node:{_D3}" and acc.owner_label == "AHX 三级班组", \
        "node 文档的 access 历史必须带节点现名（owner_dept 恒空，只回它=整段「归属」消失）"
