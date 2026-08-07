# -*- coding: utf-8 -*-
"""退役期间改归属 → 恢复上线 → 投影收敛 的端到端真库验证（2026-08-06）。

## 为什么必须真库

这条链路上的每一步都由 **SQL 谓词**决定，桩游标一律证不了：

  · `materialize_doc_allowed_depts` 的两条投影 UPDATE 都带 `AND is_active=1`
    （access_grants.py:509/561），而 `kb_retire` 刚把 chunk 全置 `is_active=0`
    ⇒ 退役期间改归属，**chunk 侧一行都写不到**。这是"影响 0 行"，不是报错，
    桩游标不给 rowcount 语义、更不会替你把 WHERE 跑一遍。
  · 兜底靠 `allowed_depts_reconcile` 的 **epoch 候选**
    （`cm.acl_epoch IS NULL OR cm.acl_epoch < dm.acl_epoch`，:236），
    它同样带 `cm.is_active=1` —— 也就是说这条兜底**只在 restore 之后才够得着**。
  · 于是"先改归属后恢复"能不能收敛，完全取决于这几个谓词的实际取值组合。

## 本模块钉死的四件事

  1. 退役后 chunk 确实 `is_active=0`；
  2. 退役期间改归属：`document_meta` 改了、`acl_epoch` 涨了，**chunk 三列纹丝不动**；
  3. `kb_restore` 只重激活 chunk、**不做投影**，此时 chunk 的 epoch 落后于 doc；
  4. 下一轮 `reconcile_allowed_depts(commit=True)` 把投影收敛到新归属、epoch 追平。

🔴 **反证锚**（`test_..._without_reconcile_stays_stale`）：跳过第 4 步时 chunk 必须仍挂
旧归属。没有它，第 4 步的断言在"reconcile 其实什么都没做、而值本来就对"时照样绿 ——
这正是 2026-08-06 在退役行菜单硬门上刚踩过的坑（见 tokens.css:265 的判例）。

依赖本地 MySQL + schema/060 + **062**（`acl_epoch` 两列）。缺任一 → skip，不假绿。
"""
import json

import pytest

from tests.local_stack import requires_local_db

_DOC = "DOC_ITEST_RETIRE_OWNER"
_SRC_NODE = 999000201        # 原归属（填错的那个）
_DST_NODE = 999000202        # 改正后的归属
_USER = "ITEST-ROC-admin"


def _epoch_columns_present(cur) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='chunk_meta' AND COLUMN_NAME='acl_epoch'")
    return bool(cur.fetchone()[0])


def _cleanup(cur):
    cur.execute("DELETE FROM kb_doc_node_grant WHERE doc_id=%s", (_DOC,))
    cur.execute("DELETE FROM kb_acl_projection_outbox WHERE doc_id=%s", (_DOC,))
    cur.execute("DELETE FROM chunk_meta WHERE doc_id=%s", (_DOC,))
    cur.execute("DELETE FROM document_version WHERE doc_id=%s", (_DOC,))
    cur.execute("DELETE FROM document_meta WHERE doc_id=%s", (_DOC,))
    cur.execute("DELETE FROM dept_dim WHERE dept_id IN (%s,%s)", (_SRC_NODE, _DST_NODE))


def _seed(cur):
    """一篇**已上线的 node 文档**：归属 = 源节点，chunk 已按源节点投影完毕并盖了章。"""
    from opensearch_pipeline.acl_policy import NODE_OWNER_SENTINEL
    for nid, name in ((_SRC_NODE, "ITEST源部门"), (_DST_NODE, "ITEST目标部门")):
        cur.execute("INSERT INTO dept_dim (dept_id, parent_id, name, is_active) "
                    "VALUES (%s,0,%s,1) ON DUPLICATE KEY UPDATE name=VALUES(name), is_active=1",
                    (nid, name))
    cur.execute(
        "INSERT INTO document_meta (doc_id,title,original_filename,owner_dept,acl_mode,"
        "owner_dept_id,status,current_version_no,permission_level,acl_revision,acl_epoch) "
        "VALUES (%s,'归属填错的文档','wrong_owner.docx',NULL,'node',%s,'active',1,"
        "'dept_internal',1,1)", (_DOC, _SRC_NODE))
    cur.execute(
        "INSERT INTO document_version (doc_id,version_no,status,content_process_status,"
        "index_status,canonical_sha256) VALUES (%s,1,'active','DONE','INDEXED','itest-sha-roc')",
        (_DOC,))
    cur.execute(
        "INSERT INTO kb_doc_node_grant (doc_id,dept_id,scope,granted_by,note) "
        "VALUES (%s,%s,'subtree',%s,'itest-seed')", (_DOC, _SRC_NODE, _USER))
    # chunk 已投影到**源**节点并盖章 epoch=1（= 当时的 document_meta.acl_epoch）
    src_proj = json.dumps([f"d:{_SRC_NODE}"], ensure_ascii=False)
    for i in range(2):
        cur.execute(
            "INSERT INTO chunk_meta (chunk_id,doc_id,version_no,chunk_index,chunk_text,"
            "owner_dept,allowed_depts,permission_level,is_active,index_status,acl_epoch) "
            "VALUES (%s,%s,1,%s,'itest chunk',%s,%s,'dept_internal',1,'INDEXED',1)",
            (f"{_DOC}_c{i}", _DOC, i, NODE_OWNER_SENTINEL, src_proj))


def _chunk_state(cur):
    """(is_active 集合, allowed_depts 集合, owner 集合, acl_epoch 集合)。"""
    cur.execute("SELECT is_active, allowed_depts, owner_dept, acl_epoch FROM chunk_meta "
                "WHERE doc_id=%s ORDER BY chunk_index", (_DOC,))
    rows = cur.fetchall()
    return ({r[0] for r in rows}, {r[1] for r in rows},
            {r[2] for r in rows}, {r[3] for r in rows})


def _doc_epoch(cur):
    cur.execute("SELECT acl_epoch, owner_dept_id, status, acl_revision FROM document_meta "
                "WHERE doc_id=%s", (_DOC,))
    return cur.fetchone()


def _api_call(fn, req, user=_USER):
    """调 kb console 端点（SIM 身份 = kb_admin，绕开组织树后代集解析）。"""
    from opensearch_pipeline import api
    return fn(req, api.Request({"type": "http", "headers": [], "method": "POST", "path": "/"}),
              identity=api.Identity(user_id=user))


@pytest.fixture
def _env(monkeypatch):
    # 本模块要的是「真 DB + 桩身份」这一对：DB 必须真（谓词是被测对象），身份走 SIM 注入
    # 才拿得到 kb_admin。`RAG_ENV=local` 的 .env.local 设了 RAG_SIMULATE=false，故这里单独
    # 打开 API 侧模拟——`RAG_SIMULATE_DB` 一个字都不碰。
    # ⚠️ 之所以敢在 shell 侧 setenv：overlay 是 file-wins（override=True），但 .env.local
    #    **没有** RAG_SIMULATE_API 这一行，不会被回盖（有的话这里就得改成改文件）。
    monkeypatch.setenv("RAG_SIMULATE_DB", "false")   # 显式：被测的就是 SQL 谓词，绝不能走桩
    monkeypatch.setenv("RAG_SIMULATE_API", "true")
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    monkeypatch.setenv("RAG_NODE_ACL_GRANT", "true")
    monkeypatch.setenv("RAG_ALLOWED_DEPTS_ACL", "true")
    # 配置是模块级单例（config.py:1289 `_config`），setenv 之后必须打掉它才会按新 env 重建
    # ——与既有 7 个模块同一惯用法（见 tests/test_config_cache_isolation.py）。
    from opensearch_pipeline import config as _cfg
    _cfg._config = None
    if not _cfg.get_config().simulate_api:
        pytest.skip("SIM 身份注入不可用（RAG_SIMULATE_API 未生效）")
    yield
    _cfg._config = None


def _run_to_restore(cur, conn):
    """跑到「改归属 + 恢复上线」为止，返回改归属后记录的 doc epoch。两个用例共用。"""
    from opensearch_pipeline.routes import kb_console

    # ① 退役 → chunk 全下线
    _api_call(kb_console.kb_retire, kb_console.KbRetireRequest(doc_id=_DOC, reason="itest"))
    conn.commit()
    active, _, _, _ = _chunk_state(cur)
    assert active == {0}, "退役后 chunk 应全部 is_active=0"

    # ② 退役期间改归属 → doc 改了，chunk 一行都写不到（投影 UPDATE 带 is_active=1）
    before_epoch, _, _, before_rev = _doc_epoch(cur)
    _api_call(kb_console.kb_doc_meta_save, kb_console.KbDocMetaSaveRequest(
        doc_id=_DOC, expected_acl_revision=before_rev, owner_dept_id=_DST_NODE,
        visible_nodes=[kb_console.KbUploadNodePick(dept_id=_DST_NODE, subtree=True)],
        reason="itest 纠正归属"))
    conn.commit()
    epoch_after, oid_after, _, _ = _doc_epoch(cur)
    assert oid_after == _DST_NODE, "归属未落库"
    assert epoch_after > before_epoch, "改归属必须 bump acl_epoch，否则 sweep 永远发现不了"
    _, allowed, _, chunk_epoch = _chunk_state(cur)
    assert allowed == {json.dumps([f'd:{_SRC_NODE}'], ensure_ascii=False)}, (
        "退役态下 chunk 投影本就写不到（AND is_active=1）——若这里已经变了，说明"
        "投影谓词改过，本模块的前提与结论都要重写")
    assert chunk_epoch == {1}, "chunk 的章也不该被动过"

    # ③ 恢复上线 → chunk 重新激活，但 restore 不做投影 ⇒ epoch 仍落后
    _api_call(kb_console.kb_restore, kb_console.KbRetireRequest(doc_id=_DOC, reason="itest"))
    conn.commit()
    active, _, _, chunk_epoch = _chunk_state(cur)
    assert active == {1}, "恢复后 chunk 应重新 is_active=1"
    assert max(chunk_epoch) < epoch_after, (
        "restore 不触发投影重物化，chunk 的章必须仍落后于 doc —— 这正是 reconcile 的入口条件")
    return epoch_after


@requires_local_db
def test_retire_change_owner_restore_then_reconcile_converges(_env):
    """主链路：退役 → 改归属 → 恢复 → reconcile ⇒ 投影收敛到新归属、epoch 追平。"""
    from opensearch_pipeline.allowed_depts_reconcile import reconcile_allowed_depts
    from opensearch_pipeline.db import _get_db_conn

    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            if not _epoch_columns_present(cur):
                pytest.skip("schema/062 未 apply（chunk_meta.acl_epoch 缺列）")
            _cleanup(cur)
            _seed(cur)
            conn.commit()

            doc_epoch = _run_to_restore(cur, conn)

            # ④ 全扫 reconcile（stage-3 pre-drain 的那一次）
            res = reconcile_allowed_depts(commit=True)
            conn.commit()
            assert not res.get("skipped"), f"reconcile 被 flag 跳过，本用例无意义: {res}"

            _, allowed, owner, chunk_epoch = _chunk_state(cur)
            assert allowed == {json.dumps([f'd:{_DST_NODE}'], ensure_ascii=False)}, (
                f"投影未收敛到新归属：{allowed}（reconcile 结果={res}）")
            assert chunk_epoch == {doc_epoch}, f"epoch 未追平：chunk={chunk_epoch} doc={doc_epoch}"
            # 收敛必须同时标脏，否则 RDS 对了、HA3 还是旧权限
            cur.execute("SELECT DISTINCT index_status FROM chunk_meta WHERE doc_id=%s", (_DOC,))
            assert {r[0] for r in cur.fetchall()} == {"NOT_INDEXED"}, (
                "投影收敛后必须标脏待重推，否则 HA3 侧永远停在旧归属")
        with conn.cursor() as cur:
            _cleanup(cur)
            conn.commit()
    finally:
        conn.close()


_EPOCH_DOC = "DOC_ITEST_EPOCH_ONLY"


def _epoch_only_cleanup(cur):
    cur.execute("DELETE FROM chunk_meta WHERE doc_id=%s", (_EPOCH_DOC,))
    cur.execute("DELETE FROM document_version WHERE doc_id=%s", (_EPOCH_DOC,))
    cur.execute("DELETE FROM document_meta WHERE doc_id=%s", (_EPOCH_DOC,))
    cur.execute("DELETE FROM kb_doc_node_grant WHERE doc_id=%s", (_EPOCH_DOC,))


def _other_candidate_sources_miss(cur, doc_id) -> dict:
    """四条**非 epoch** 候选源是否都够不着该 doc（allowed_depts_reconcile.py:171-215）。

    有了它，「epoch 候选独立有效」才是**隔离**结论而不是并发功劳——不必去变异产品代码。
    """
    from opensearch_pipeline.acl_policy import NODE_OWNER_SENTINEL
    hits = {}
    cur.execute("SELECT COUNT(*) FROM kb_access_request WHERE doc_id=%s AND status='approved'",
                (doc_id,))
    hits["approved"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM chunk_meta WHERE doc_id=%s AND is_active=1 "
                "AND allowed_depts IS NOT NULL", (doc_id,))
    hits["have_allowed_depts"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM kb_doc_node_grant WHERE doc_id=%s AND revoked_at IS NULL",
                (doc_id,))
    hits["node_granted"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM document_meta dm JOIN chunk_meta cm "
                "  ON cm.doc_id=dm.doc_id AND cm.version_no=dm.current_version_no AND cm.is_active=1 "
                "WHERE dm.doc_id=%s AND dm.acl_mode='node' AND NOT (cm.owner_dept <=> %s)",
                (doc_id, NODE_OWNER_SENTINEL))
    hits["node_stale_owner"] = cur.fetchone()[0]
    return hits


@requires_local_db
def test_epoch_candidate_alone_certifies_never_projected_chunk(_env):
    """epoch 候选**独立**有效：四条老候选源全够不着的文档，仅凭 `acl_epoch IS NULL` 被捞起并盖章。

    为什么单列一条：主用例里 `have_allowed_depts` 与 `node_granted` 两条候选源同样命中，
    实测把 epoch 候选谓词变异成永假后主用例**照样绿** —— 也就是说主用例证明不了 epoch
    候选работает。这条构造「零授权 + allowed_depts IS NULL + owner 已是哨兵」的文档，
    让 epoch 成为唯一入口，并用 `_other_candidate_sources_miss` 把隔离性钉死。

    命中后走的是 C3′ 的 certify-only 分支（值本就正确、只补盖章），故断言落在 acl_epoch
    追平上，且 `index_status` **不得**被改动 —— certify 不是重投影。
    """
    from opensearch_pipeline.acl_policy import NODE_OWNER_SENTINEL
    from opensearch_pipeline.allowed_depts_reconcile import reconcile_allowed_depts
    from opensearch_pipeline.db import _get_db_conn

    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            if not _epoch_columns_present(cur):
                pytest.skip("schema/062 未 apply（chunk_meta.acl_epoch 缺列）")
            _epoch_only_cleanup(cur)
            cur.execute(
                "INSERT INTO document_meta (doc_id,title,original_filename,owner_dept,acl_mode,"
                "owner_dept_id,status,current_version_no,permission_level,acl_revision,acl_epoch) "
                "VALUES (%s,'零授权文档','zero_grant.docx',NULL,'node',%s,'active',1,"
                "'dept_internal',1,7)", (_EPOCH_DOC, _SRC_NODE))
            cur.execute("INSERT INTO document_version (doc_id,version_no,status,"
                        "content_process_status,index_status) VALUES (%s,1,'active','DONE','INDEXED')",
                        (_EPOCH_DOC,))
            # 投影值本就正确（哨兵 owner + 空 allowed_depts），但**从未盖过章**
            cur.execute(
                "INSERT INTO chunk_meta (chunk_id,doc_id,version_no,chunk_index,chunk_text,"
                "owner_dept,allowed_depts,permission_level,is_active,index_status,acl_epoch) "
                "VALUES (%s,%s,1,0,'itest',%s,NULL,'dept_internal',1,'INDEXED',NULL)",
                (f"{_EPOCH_DOC}_c0", _EPOCH_DOC, NODE_OWNER_SENTINEL))
            conn.commit()

            miss = _other_candidate_sources_miss(cur, _EPOCH_DOC)
            assert miss == {"approved": 0, "have_allowed_depts": 0,
                            "node_granted": 0, "node_stale_owner": 0}, (
                f"隔离前提被破坏，本用例证明不了 epoch 候选独立有效：{miss}")

            res = reconcile_allowed_depts(commit=True)
            conn.commit()
            assert not res.get("skipped"), f"reconcile 被 flag 跳过: {res}"

            cur.execute("SELECT acl_epoch, index_status FROM chunk_meta WHERE doc_id=%s",
                        (_EPOCH_DOC,))
            row = cur.fetchone()
            assert row[0] == 7, (
                f"epoch 候选没把「从未投影过」的 chunk 捞起来盖章（acl_epoch={row[0]}，"
                f"reconcile={res}）—— 这类文档在 diff 口径下永远判 unchanged，正是 C3 的永久态")
            assert row[1] == "INDEXED", "certify-only 不得改 index_status（那会触发无谓重推）"
        with conn.cursor() as cur:
            _epoch_only_cleanup(cur)
            conn.commit()
    finally:
        conn.close()


@requires_local_db
def test_without_reconcile_projection_stays_stale(_env):
    """🔴 反证锚：不跑 reconcile，投影必须仍挂**旧**归属。

    没有这条，主用例的"收敛"断言在 reconcile 其实是 no-op、而值本来就对时照样绿。
    """
    from opensearch_pipeline.db import _get_db_conn

    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            if not _epoch_columns_present(cur):
                pytest.skip("schema/062 未 apply（chunk_meta.acl_epoch 缺列）")
            _cleanup(cur)
            _seed(cur)
            conn.commit()

            _run_to_restore(cur, conn)      # 到 restore 为止，**不跑 reconcile**

            _, allowed, _, _ = _chunk_state(cur)
            assert allowed == {json.dumps([f'd:{_SRC_NODE}'], ensure_ascii=False)}, (
                "不跑 reconcile 却已经收敛 ⇒ 主用例测的不是 reconcile 的功劳，两条断言都失效")
        with conn.cursor() as cur:
            _cleanup(cur)
            conn.commit()
    finally:
        conn.close()
