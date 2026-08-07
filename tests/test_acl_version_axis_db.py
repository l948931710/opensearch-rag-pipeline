# -*- coding: utf-8 -*-
"""C3′ ACL 投影版本轴 · 真库验证（2026-08-07）。

方案：`docs/ops/c3prime_version_axis_plan_2026-08-04_DRAFT.md`（含 §0.5 生产实测）。
Codex 三轮共识 + Sam 拍板政策1 / §8.1 方案 A。

## 为什么必须真库

被测的全是 **SQL 谓词**与 **rowcount 语义**，桩游标一律证不了：

  · 版本集来自 `SELECT DISTINCT version_no … WHERE is_active=1`；
  · 每条投影写都带 `AND version_no=%s AND is_active=1` —— "写不到任何行"是
    rowcount=0 而不是报错；
  · 预筛的 gate / epoch 门是一条聚合查询在内存里复算，行形状一变判定就变；
  · `_version_processing_gate` 的三态（ok/locked/missing）由 `document_version`
    行的**存在性**与 `NOW() - INTERVAL 2 HOUR` 决定。

依赖本地 MySQL + schema/062（`acl_epoch` 两列）。缺任一 → skip，不假绿。

## 反证锚（每条断言都要能被改坏改红）

`test_flag_off_leaves_old_version_untouched` 是 T1/T2 的**反证锚**：把版本集退回
`[current]`（= flag off 的形态）时旧版本必须**纹丝不动**。没有它，T1 在
"reconcile 其实什么都没做、而值本来就对" 时照样绿。
"""
import json

import pytest

from tests.local_stack import requires_local_db

_DOC = "DOC_ITEST_VERSION_AXIS"
_GRANTEE = "rd"          # 必须在 retriever._VALID_ACL_GROUPS 白名单内
_OTHER = "finance"


def _epoch_columns_present(cur) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='chunk_meta' AND COLUMN_NAME='acl_epoch'")
    return bool(cur.fetchone()[0])


def _cleanup(cur, doc=None):
    for t in ("kb_acl_projection_outbox", "kb_access_request", "chunk_meta",
              "document_version", "document_meta"):
        cur.execute(f"DELETE FROM {t} WHERE doc_id LIKE %s", ((doc or _DOC) + "%",))


def _seed(cur, *, versions, approved=_GRANTEE, perm=None, doc_epoch=5,
          dv_versions=None, index_status="INDEXED", doc=None):
    """种一篇 legacy 文档，`versions` = {version_no: (allowed_depts_json|None, chunk_epoch)}。

    `perm` = {version_no: permission_level}（缺省全 dept_internal）。
    `dv_versions` = 要建 `document_version` 行的版本集（缺省 = versions 全集）；
    刻意留空某版本用来构造 §4-E1「无 dv 行」。
    """
    perm = perm or {}
    _DOC = doc or globals()['_DOC']
    dv_versions = versions.keys() if dv_versions is None else dv_versions
    cur.execute(
        "INSERT INTO document_meta (doc_id,title,original_filename,owner_dept,status,"
        "current_version_no,permission_level,acl_epoch) "
        "VALUES (%s,'版本轴用例','axis.docx',%s,'active',%s,'dept_internal',%s)",
        (_DOC, _OTHER, max(versions), doc_epoch))
    for v in dv_versions:
        cur.execute(
            "INSERT INTO document_version (doc_id,version_no,status,content_process_status,"
            "index_status,canonical_sha256) VALUES (%s,%s,'active','DONE',%s,%s)",
            (_DOC, v, index_status, f"itest-axis-{v}"))
    for v, (allowed, cep) in versions.items():
        cur.execute(
            "INSERT INTO chunk_meta (chunk_id,doc_id,version_no,chunk_index,chunk_text,"
            "permission_level,owner_dept,allowed_depts,is_active,index_status,acl_epoch) "
            "VALUES (%s,%s,%s,0,'itest',%s,%s,%s,1,'INDEXED',%s)",
            (f"{_DOC}_v{v}_c0", _DOC, v, perm.get(v, "dept_internal"),
             _OTHER, allowed, cep))
    if approved:
        # ⚠️ `kb_access_request.owner_dept` 是 NOT NULL 无默认（本地库实测）——必须显式给。
        cur.execute(
            "INSERT INTO kb_access_request (doc_id,owner_dept,requester_id,requester_depts,status) "
            "VALUES (%s,%s,'itest-user',%s,'approved')", (_DOC, _OTHER, approved))


def _state(cur, doc=None):
    """{version_no: (allowed_depts, acl_epoch, index_status)}"""
    cur.execute("SELECT version_no, allowed_depts, acl_epoch, index_status FROM chunk_meta "
                "WHERE doc_id=%s AND is_active=1 ORDER BY version_no", (doc or _DOC,))
    return {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}


@pytest.fixture
def _env(monkeypatch):
    """真 DB（被测就是 SQL 谓词）。`acl_version_axis` 由各用例自己设。"""
    monkeypatch.setenv("RAG_SIMULATE_DB", "false")
    monkeypatch.setenv("RAG_ALLOWED_DEPTS_ACL", "true")
    from opensearch_pipeline import config as _cfg
    _cfg._config = None
    yield
    _cfg._config = None


def _axis(monkeypatch, on: bool):
    from opensearch_pipeline.config import get_config
    monkeypatch.setattr(get_config().rag, "acl_version_axis", on, raising=False)


def _with_db(fn):
    """开局清场 + 收尾清场；`fn(cur, conn)`。"""
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            if not _epoch_columns_present(cur):
                pytest.skip("schema/062 未 apply（chunk_meta.acl_epoch 缺列）")
            _cleanup(cur)
            conn.commit()
            fn(cur, conn)
        with conn.cursor() as cur:
            _cleanup(cur)
            conn.commit()
    finally:
        conn.close()


# ── T1 可用性方向：授权批了，旧版本也必须拿到 ────────────────────────────────
@requires_local_db
def test_old_active_version_gets_projected(_env, monkeypatch):
    """★ 方案 §0 实测一：v1 从未投影（allowed_depts=NULL / epoch=NULL），v2 已正确。

    改动前：materialize 只看 current(v2) ⇒ 报 unchanged，v1 三轮后仍是 NULL（永不收敛）。
    """
    _axis(monkeypatch, True)
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts

    def body(cur, conn):
        _seed(cur, versions={1: (None, None), 2: (json.dumps([_GRANTEE]), 5)})
        conn.commit()
        out = materialize_doc_allowed_depts(cur, _DOC, apply=True)
        conn.commit()
        st = _state(cur)
        assert st[1][0] == json.dumps([_GRANTEE], ensure_ascii=False), (
            f"旧 active 版本未被投影（版本轴没生效）：{st}，outcome={out}")
        assert st[1][1] == 5, f"旧版本 epoch 未追平：{st}"
        assert {p["version_no"] for p in out["per_version"]} == {1, 2}, out
        # v2 值本就对、章也对 ⇒ 该版本 unchanged；聚合态取"做了事"的那个
        assert out["status"] == "materialized", out

    _with_db(body)


@requires_local_db
def test_flag_off_leaves_old_version_untouched(_env, monkeypatch):
    """🔴 T1/T2 的**反证锚**：flag off ⇒ 版本集恒 [current] ⇒ 旧版本纹丝不动。

    没有这一条，上面那条在"值本来就对"时照样绿。它同时钉死 flag off 的回滚语义。
    """
    _axis(monkeypatch, False)
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts

    def body(cur, conn):
        _seed(cur, versions={1: (None, None), 2: (json.dumps([_GRANTEE]), 5)})
        conn.commit()
        out = materialize_doc_allowed_depts(cur, _DOC, apply=True)
        conn.commit()
        st = _state(cur)
        assert st[1][0] is None, f"flag off 时旧版本被改了（回滚语义破了）：{st}"
        assert st[1][1] is None, f"flag off 时旧版本被盖章了：{st}"
        assert [p["version_no"] for p in out["per_version"]] == [2], out

    _with_db(body)


# ── T2 机密性方向：授权撤了，旧版本也必须收回 ──────────────────────────────
@requires_local_db
def test_revoked_grant_retracts_old_active_version(_env, monkeypatch):
    """★ 方案 §0 实测二：授权已不 approved，v2 已收回而 v1 仍挂着组码。

    改动前：v1 永久对该组开放（机密性缺陷）。
    """
    _axis(monkeypatch, True)
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts

    def body(cur, conn):
        _seed(cur, versions={1: (json.dumps([_GRANTEE]), 5), 2: (None, 5)}, approved=None)
        conn.commit()
        materialize_doc_allowed_depts(cur, _DOC, apply=True)
        conn.commit()
        st = _state(cur)
        assert st[1][0] is None, f"授权撤销后旧 active 版本仍开放：{st}"

    _with_db(body)


# ── T9（G5）混级：预筛的跨版本 gate 会拦住修复 ─────────────────────────────
@requires_local_db
def test_mixed_permission_old_version_is_not_prescreened_away(_env, monkeypatch):
    """★ 方案 §0 实测四（升为 blocker 的那条）：v1=dept_internal / v2(current)=public。

    预筛若用**跨版本** permission 集合 `{dept_internal, public}` ⇒ 整篇 want=[] ⇒
    两版都是 NULL ⇒ 判 unchanged 直接跳过 ⇒ **materializer 根本进不到**。
    Sam 2026-08-07 拍板方案 A：逐版本 gate，v1 应拿到组码。
    """
    _axis(monkeypatch, True)
    from opensearch_pipeline.allowed_depts_reconcile import _prescreen_unchanged
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts

    def body(cur, conn):
        _seed(cur, versions={1: (None, 5), 2: (None, 5)},
              perm={1: "dept_internal", 2: "public"})
        conn.commit()
        assert _DOC not in _prescreen_unchanged(cur, [_DOC], all_versions=True), (
            "混级文档被预筛判 unchanged ⇒ 修了 materializer 也不生效（实测四）")
        materialize_doc_allowed_depts(cur, _DOC, apply=True)
        conn.commit()
        st = _state(cur)
        assert st[1][0] == json.dumps([_GRANTEE], ensure_ascii=False), (
            f"混级文档的 dept_internal 旧版本未拿到组码（方案 A）：{st}")
        assert st[2][0] is None, f"public 的 current 版本绝不能拿到组码：{st}"

    _with_db(body)


# ── T10（G6）epoch 门：值/owner 全对但章落后，预筛不得跳过 ──────────────────
@requires_local_db
def test_epoch_behind_is_not_prescreened_away(_env, monkeypatch):
    """★ codex BLOCKER-1：预筛不读 epoch ⇒ `epoch_dirty` 选进来的文档被预筛拦回，
    certify 盖章路径永远到不了 ⇒ 每轮进候选、每轮空转、G4 承诺的收敛不成立。

    并钉死 certify 的形态：**只盖章、不动 `index_status`**。
    """
    _axis(monkeypatch, True)
    from opensearch_pipeline.allowed_depts_reconcile import _prescreen_unchanged
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts

    def body(cur, conn):
        aj = json.dumps([_GRANTEE], ensure_ascii=False)
        _seed(cur, versions={1: (aj, 2), 2: (aj, 5)}, doc_epoch=5)   # v1 章落后
        conn.commit()
        assert _DOC not in _prescreen_unchanged(cur, [_DOC], all_versions=True), (
            "章落后的文档被预筛判 unchanged ⇒ 永远盖不上章（codex BLOCKER-1）")
        out = materialize_doc_allowed_depts(cur, _DOC, apply=True)
        conn.commit()
        st = _state(cur)
        assert st[1][1] == 5, f"章未追平：{st}"
        assert st[1][2] == "INDEXED", f"certify 顺手动了 index_status（会触发全量重推）：{st}"
        assert out["status"] == "certified", out

    _with_db(body)


# ── T12（G8）预览必须真正零写 ───────────────────────────────────────────────
@requires_local_db
def test_apply_false_emits_no_write(_env, monkeypatch):
    """★ codex C6（**当前 main 的既有缺陷**）：certify 的 `UPDATE … SET acl_epoch`
    改动前在 `if not apply` **之前**发出 ⇒ 宣称的"只读预览"其实会写。
    """
    _axis(monkeypatch, True)
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts

    def body(cur, conn):
        aj = json.dumps([_GRANTEE], ensure_ascii=False)
        _seed(cur, versions={1: (aj, 2), 2: (aj, 2)}, doc_epoch=5)   # 两版章都落后
        conn.commit()
        before = _state(cur)
        out = materialize_doc_allowed_depts(cur, _DOC, apply=False)
        conn.commit()
        assert _state(cur) == before, f"apply=False 改了库：{before} → {_state(cur)}"
        assert out["wrote"] is False, f"预览报了 wrote=True：{out}"
        # 预览仍须**如实报告**会发生什么（否则 --check 变成睁眼瞎）
        assert out["status"] == "certified", out

    _with_db(body)


# ── T7（§4-E1 / G11）无 document_version 行的版本 ──────────────────────────
@requires_local_db
def test_version_without_dv_row_is_reported_not_projected(_env, monkeypatch):
    """★ codex R6 确认：stage-3 loader 是 `INNER JOIN document_version`
    （`dataworks_orchestrator.py:516-517`）⇒ 无 dv 行的版本标脏了也没人 drain，
    会永久停在 `NOT_INDEXED`、HA3 永不更新。G11：不写它，报 `missing_version`。
    """
    _axis(monkeypatch, True)
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts

    def body(cur, conn):
        _seed(cur, versions={1: (None, None), 2: (None, 5)}, dv_versions=[2])
        conn.commit()
        out = materialize_doc_allowed_depts(cur, _DOC, apply=True)
        conn.commit()
        per = {p["version_no"]: p["status"] for p in out["per_version"]}
        assert per[1] == "missing_version", f"无 dv 行的版本未被单列：{out}"
        assert _state(cur)[1][0] is None, "无 dv 行的版本被写了（会留下不可 drain 的 chunk）"
        # 聚合态必须是非完成态 —— 否则 outbox 会把这篇标 done、意图丢失
        assert out["status"] == "missing_version", out

    _with_db(body)


# ── T11′ 版本上限：只告警、绝不截断 ──────────────────────────────────────────
@requires_local_db
def test_many_versions_are_all_processed_not_truncated(_env, monkeypatch, caplog):
    """★ codex MAJOR（第三轮）：旧方案"超限只处理最新 N 个"会让 N 之外的版本**永不收敛**
    —— outbox 只存 `doc_id`、没有版本游标（`schema/009`），重试永远重复命中同一批。
    处置：移除截断，超阈值只 `logger.error`。**断言"全部被处理"，不是"outbox 未 done"。**
    """
    _axis(monkeypatch, True)
    from opensearch_pipeline.access_grants import materialize_doc_allowed_depts, _MAX_VERSIONS_ALARM

    n = _MAX_VERSIONS_ALARM + 1

    def body(cur, conn):
        _seed(cur, versions={v: (None, 5) for v in range(1, n + 1)})
        conn.commit()
        with caplog.at_level("ERROR"):
            out = materialize_doc_allowed_depts(cur, _DOC, apply=True)
        conn.commit()
        assert len(out["per_version"]) == n, f"版本被截断了：只处理了 {len(out['per_version'])}/{n}"
        st = _state(cur)
        want = json.dumps([_GRANTEE], ensure_ascii=False)
        unprojected = sorted(v for v in range(1, n + 1) if st[v][0] != want)
        assert not unprojected, f"这些版本没被投影：{unprojected}"
        assert any("远超设计上界" in r.getMessage() for r in caplog.records), (
            "超阈值未告警（静默处理同样不可接受）")

    _with_db(body)


# ── T14（codex C4 / 我 R4）写预算按文档计，不按版本 ─────────────────────────
@requires_local_db
def test_write_budget_counts_documents_not_versions(_env, monkeypatch):
    """★ `_LIMIT` 的语义一直是"每轮写入的**文档**数"。版本轴下 `materialized`/`retracted`
    变成了**版本数** ⇒ 沿用旧判据会让双版本文档吃两份预算、单轮实际处理量腰斩。

    🔴 **必须两篇文档**才分得开：单篇时预算检查只在任何写之前跑一次，两种实现都不会
    capped —— 第一版用单篇写的这条断言是**空转的**（2026-08-07 变异验证 M5 当场抓出）。
    夹具：A=双版本漂移、B=单版本漂移，`_LIMIT=2`。
      · 按文档计（正确）：A 吃 1 份 ⇒ B 仍在预算内 ⇒ B 被投影、capped=False
      · 按版本计（缺陷）：A 吃 2 份 ⇒ 到 B 时判满 ⇒ **B 永远没被投影**、capped=True
    """
    _axis(monkeypatch, True)
    from opensearch_pipeline import allowed_depts_reconcile as adr

    doc_b = _DOC + "_B"

    def body(cur, conn):
        _seed(cur, versions={1: (None, 5), 2: (None, 5)})            # A：双版本
        _seed(cur, versions={1: (None, 5)}, doc=doc_b)               # B：单版本
        conn.commit()
        monkeypatch.setattr(adr, "_LIMIT", 2)
        res = adr.reconcile_allowed_depts(commit=True)
        conn.commit()
        assert not res.get("skipped"), res
        want = json.dumps([_GRANTEE], ensure_ascii=False)
        assert _state(cur, doc_b)[1][0] == want, (
            f"第二篇文档没被投影 ⇒ 预算按版本计、A 吃掉了两份：{res}")
        assert res["capped"] is False, f"双版本文档吃掉了两份预算：{res}"
        assert res["materialized"] >= 3, f"per_version 计数没生效（应为 2+1 个版本）：{res}"

    _with_db(body)
