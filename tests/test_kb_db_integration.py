# -*- coding: utf-8 -*-
"""kb console 聚合端点 × 真实 MySQL 的回归（requires_local_db 族，CI db-integration job 串行跑）。

2026-07-11 staging P1 的教训：/api/kb/feedback-review 的 SELECT 写了 qa_session_log
不存在的 q.question（实列 query_text）——桩游标不校验列名，单测全绿，staging 真库
pymysql 1054 必 500，且前端把 500 静默成区块消失，差评复核在治理面完全不可见。
mock 能证明分组/脱敏/作用域逻辑，「列存在于真表」只有真库能证明，本模块补上这一层：
按权威 schema/ 建出的库上 seed → 调端点 → 断言行回流。

跑本地 docker-compose dev 栈（fuling_knowledge + fuling_operation，tests/local_stack.py
接线）；CI db-integration job 先跑 scripts/ci_load_schema.sh 从零建双库。真实 DML：
固定主键 seed → finally 按同一组主键精确清理，开跑先预清一次故可重入。

## 为什么有重试（2026-08-04 flake 定位，实证复现）

本测试的断言要求 seed 的 document_meta 行在「commit → 调端点」这十几毫秒里活着——
端点 SQL 以 `JOIN document_meta` 收尾，那一行没了就**静默**掉整条记录（不是报错，是 0 行），
症状就是 `assert 'ITEST-FBR-M1' in {}`。而 `test_classification.py::clean_db`（autouse，
每个用例前跑）执行 **`DELETE FROM document_meta`（无 WHERE）**，正好把这一行连坐清掉。

同一个 pytest run 内这两个模块被 conftest 的 `_LOCAL_STACK_SERIAL_MODULES` 钉在同一
worker 上顺序跑，撞不上；**跨 pytest 进程（本机同时开第二个 `pytest`／另一个 worktree
在跑 make test）则零防护**——xdist 分组只在单进程内成立。复现实测：一边循环跑本模块、
一边另起进程循环跑 test_classification，30 次红 7 次，7 次的取证签名完全一致
（qa_session_log=1、user_feedback=1、**document_meta=0**，join 阶梯恰好断在 ⋈m）。

故本测试把「种子被外部整表清掉」与「产品坏了」分开判：回流为空时先回查自己的三行种子，
**三行都在 = 真回归 → 立刻红**（并逐层取证）；有行不翼而飞 = 外部干扰 → 重新 seed 再来，
连续 _MAX_ATTEMPTS 回合都被清掉才 skip（并说清是谁在清），绝不把环境噪声记成产品红。
根治属另一档（把跨进程互斥补进 local_stack/conftest），见 backlog。
"""
import json
import warnings

import pytest

from tests.local_stack import requires_local_db

# 本模块专属固定主键：seed 与清理都按它们定位，绝不触碰库里其他行
_MSG_ID = "ITEST-FBR-M1"
_SESSION_ID = "ITEST-FBR-S1"
_FEEDBACK_ID = "ITEST-FBR-F1"
_DOC_ID = "ITEST-FBR-D1"
_USER_ID = "ITEST-FBR-user"

# 一个回合 = 清干净 → seed → 调端点。只有「种子被外部进程清掉」才消耗回合数（见模块 docstring）
_MAX_ATTEMPTS = 3


def _skip_if_not_sim():
    from opensearch_pipeline.config import get_config
    if not get_config().simulate_api:
        pytest.skip("需 RAG_SIMULATE=true")


def _cleanup(conn):
    from opensearch_pipeline.api import _kb_db
    from opensearch_pipeline.qa_logger import _op_db
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM {_op_db()}.user_feedback WHERE feedback_id = %s", (_FEEDBACK_ID,))
        cur.execute(f"DELETE FROM {_op_db()}.qa_session_log WHERE message_id = %s", (_MSG_ID,))
        cur.execute(f"DELETE FROM {_kb_db()}.document_meta WHERE doc_id = %s", (_DOC_ID,))
    conn.commit()


def _seed(conn):
    """三张表各一行：qa_session_log（问题文本 + cited_docs_json）→ user_feedback（差评）
    → document_meta（被引用文档）。端点 SQL 四表联查，缺任何一行都会静默丢掉整条记录。"""
    from opensearch_pipeline.api import _kb_db
    from opensearch_pipeline.qa_logger import _op_db
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {_op_db()}.qa_session_log"
            " (session_id, message_id, user_id, query_text, answer_status, cited_docs_json)"
            " VALUES (%s, %s, %s, %s, 'SUCCESS', %s)",
            (_SESSION_ID, _MSG_ID, _USER_ID, "清洗流程有哪些注意事项？",
             json.dumps([{"doc_id": _DOC_ID}])))
        cur.execute(
            f"INSERT INTO {_op_db()}.user_feedback"
            " (feedback_id, message_id, user_id, feedback_type,"
            "  feedback_reason, feedback_comment)"
            " VALUES (%s, %s, %s, 'downvote', 'inaccurate', %s)",
            (_FEEDBACK_ID, _MSG_ID, _USER_ID, "步骤三和现场设备对不上"))
        cur.execute(
            f"INSERT INTO {_kb_db()}.document_meta (doc_id, title, owner_dept, status)"
            " VALUES (%s, %s, 'production', 'active')",
            (_DOC_ID, "设备清洗 SOP"))
    conn.commit()


def _lock_status() -> str:
    """把「刚才有没有握着跨进程锁」写进失败/skip 文案——降级是静默的，
    不如实交代就无从判断这条红/skip 该不该当真。"""
    from tests.local_stack import local_stack_lock_degraded_reason, local_stack_lock_held
    if local_stack_lock_held():
        return "持有跨进程锁（对面若是老 worktree 仍拦不住：锁是双边协议）"
    return f"**未持有**跨进程锁（降级原因={local_stack_lock_degraded_reason()!r}）"


def _seed_row_counts(conn):
    """调完端点后回查自己那三行还在不在 → {表名: 0/1}。

    这是「真回归 vs 外部整表清库」的判据（见模块 docstring）。先 commit 关掉可能开着的
    读事务：REPEATABLE READ 下沿用旧读视图会把「已被别人删掉」读成「还在」，判据就反了。
    """
    from opensearch_pipeline.api import _kb_db
    from opensearch_pipeline.qa_logger import _op_db
    conn.commit()
    counts = {}
    with conn.cursor() as cur:
        for table, sql, arg in (
            ("qa_session_log",
             f"SELECT COUNT(*) FROM {_op_db()}.qa_session_log WHERE message_id=%s", _MSG_ID),
            ("user_feedback",
             f"SELECT COUNT(*) FROM {_op_db()}.user_feedback WHERE feedback_id=%s", _FEEDBACK_ID),
            ("document_meta",
             f"SELECT COUNT(*) FROM {_kb_db()}.document_meta WHERE doc_id=%s", _DOC_ID),
        ):
            cur.execute(sql, (arg,))
            counts[table] = cur.fetchone()[0]
    conn.commit()
    return counts


def _diagnose(conn, resp):
    """种子三行俱在却仍无回流 = 真回归；此时把「到底哪一层空了」逐层打出来。

    分三层各自独立取证，避免只看到最终 0 行：
      L1 本连接（seed 用的那条）读三张表 → 行还在不在（seed 丢失 / 被删）；
      L2 新连接（新事务、新读视图）读同三张表 → 可见性（快照/未提交）；
      L3 用端点等价 SQL 逐项放宽 WHERE → 到底是哪个谓词把行滤掉了。
    另打身份/作用域/库名/时间，覆盖 role 漂移与时间窗两个候选。
    """
    import os
    from opensearch_pipeline.api import _kb_db, _kb_doc_owner_scope_sql, _kb_node_capability
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.db import _get_db_conn
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline.qa_facts import fact_join_enabled, qa_docs_join_sql
    from opensearch_pipeline.qa_logger import _op_db

    out = []
    op, kb_db = _op_db(), _kb_db()

    def _probe(c, tag):
        with c.cursor() as cur:
            for sql, args in (
                (f"SELECT COUNT(*) FROM {op}.qa_session_log WHERE message_id=%s", (_MSG_ID,)),
                (f"SELECT COUNT(*) FROM {op}.user_feedback WHERE feedback_id=%s", (_FEEDBACK_ID,)),
                (f"SELECT COUNT(*) FROM {kb_db}.document_meta WHERE doc_id=%s", (_DOC_ID,)),
            ):
                cur.execute(sql, args)
                out.append(f"  [{tag}] {sql.split('FROM ')[1].split(' WHERE')[0]} = {cur.fetchone()[0]}")
            cur.execute(f"SELECT created_at, feedback_type, handled_status FROM {op}.user_feedback"
                        " WHERE feedback_id=%s", (_FEEDBACK_ID,))
            out.append(f"  [{tag}] feedback row = {cur.fetchone()}")
            cur.execute("SELECT NOW(), @@transaction_isolation, CONNECTION_ID(), @@time_zone")
            out.append(f"  [{tag}] NOW/iso/conn/tz = {cur.fetchone()}")

    out.append(f"— resp: items={len(resp.items)} scope={resp.scope} win={resp.window_days} "
               f"trunc_msg={resp.truncated_messages} trunc_scan={resp.truncated_scan} "
               f"ids={[i.message_id for i in resp.items][:5]}")
    out.append(f"— db names: op={op} kb={kb_db} host={get_config().rds.host}:{get_config().rds.port} "
               f"sim_api={get_config().simulate_api} sim_db={get_config().simulate_db} "
               f"fact_join={fact_join_enabled()} SIM_ROLE={os.environ.get('RAG_SIM_USER_ROLE')!r}")
    try:
        _probe(conn, "L1 seed-conn")
    except Exception as e:   # noqa: BLE001
        out.append(f"  [L1] 探测失败: {type(e).__name__}: {e}")
    c2 = None
    try:
        c2 = _get_db_conn()
        _probe(c2, "L2 fresh-conn")
        ident = resolve_kb_identity("ITEST-FBR-adm")
        with c2.cursor() as cur:
            cap = _kb_node_capability(cur)
            clause, params, deg = _kb_doc_owner_scope_sql(ident, cap)
            out.append(f"— identity: role={ident.role!r} depts={ident.granted_owner_depts} "
                       f"roots={ident.granted_node_roots} cap={cap} scope_clause={clause!r} "
                       f"params={params} degraded={deg}")
            base = (f"SELECT COUNT(*) FROM {op}.user_feedback f"
                    f" JOIN {op}.qa_session_log q ON q.message_id = f.message_id")
            for tag, sql, args in (
                ("f only", f"SELECT COUNT(*) FROM {op}.user_feedback f WHERE f.message_id=%s", (_MSG_ID,)),
                ("f⋈q", base + " WHERE f.message_id=%s", (_MSG_ID,)),
                ("f⋈q⋈jt⋈m", base + qa_docs_join_sql(cited=True) + " WHERE f.message_id=%s", (_MSG_ID,)),
                ("+type", base + qa_docs_join_sql(cited=True)
                 + " WHERE f.message_id=%s AND f.feedback_type='downvote'", (_MSG_ID,)),
                ("+window", base + qa_docs_join_sql(cited=True)
                 + " WHERE f.message_id=%s AND f.feedback_type='downvote'"
                   " AND f.created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)", (_MSG_ID,)),
            ):
                try:
                    cur.execute(sql, args)
                    out.append(f"  [L3 {tag}] = {cur.fetchone()[0]}")
                except Exception as e:   # noqa: BLE001
                    out.append(f"  [L3 {tag}] 失败: {type(e).__name__}: {e}")
    except Exception as e:   # noqa: BLE001
        out.append(f"  [L2/L3] 探测失败: {type(e).__name__}: {e}")
    finally:
        if c2 is not None:
            c2.close()
    return "\n".join(out)


@requires_local_db
def test_xproc_lock_is_actually_engaged():
    """元测试：证明跨进程锁**此刻真的握在本进程手里**（conftest `_local_stack_xproc_lock`）。

    为什么需要一条正向断言：降级是**静默**的——`warnings.warn` 只进 run 末尾的 summary，
    `-q` / `-p no:warnings` / warning filter 一收窄就没了。没有这条，
    「锁名拼错 / 专用连接建不起来 / 逃生口被误设 off / fixture 压根没挂上」这几种
    「锁从来没生效」与「锁完美工作」在终端上**逐字节相同**，方案就退化成了自我安慰。

    刻意的分档：环境争用（等锁超时/连接异常）⇒ skip（属实况，不是缺陷）；
    没降级却也没持锁 ⇒ **红**（那是接线坏了，正是本测试要守的东西）。
    """
    from tests import local_stack
    from tests.conftest import _LOCAL_STACK_SERIAL_MODULES

    # 加害者的组成员资格：元测试只守「本条测试的接线」是不够的——把真正执行无 WHERE
    # `DELETE FROM document_meta|document_version` 的 test_classification 从组里删掉，
    # 保护会静默归零而本测试照绿。纯 Python 断言，零 DB 成本。
    assert {"test_classification.py", "test_pipeline.py"} <= _LOCAL_STACK_SERIAL_MODULES, (
        "执行无 WHERE 整表 DML 的模块不在串行组里 ⇒ 它既不取锁、也不与本组串行，"
        "跨进程保护静默归零")

    reason = local_stack.local_stack_lock_degraded_reason()
    stats = local_stack.local_stack_lock_stats()
    if reason == "disabled":
        pytest.skip("RAG_TEST_XPROC_LOCK 显式关闭，跨进程锁不生效（逃生口）")
    if reason == "error" and not (stats.keys() - {"error"}):
        # 连不上库：整组本来就跑不动，skip 才是如实的（不是「锁坏了」）
        pytest.skip(f"跨进程锁连接异常（{stats}）——本地栈不可达，本 run 无跨进程保护")
    # ⚠️ timeout/null **判红不判 skip**：等满 120s 意味着确有另一进程在撞（组内最长单测
    # 才 4.51s，正常争用根本等不到超时），此时本 run 的所有绿灯在跨进程维度上都不作数。
    # 早先这里 skip 自己，评审实跑出「exit=0 + 零保护 + 零痕迹」——警报器在最该响的时候闭嘴。
    bad = {k: v for k, v in stats.items() if k in ("timeout", "null", "unwired")}
    assert not bad, (
        f"本 run 出现过跨进程锁降级 {bad} ⇒ 期间**没有**跨进程保护，那些用例的绿灯在"
        "跨进程维度上不作数。常见成因：另一个 pytest 进程长时间占锁（含不带本 fixture 的"
        "老 worktree 把锁攥死）、或 MySQL 侧 GET_LOCK 返回异常值。")
    assert local_stack.local_stack_lock_held(), (
        "跨进程锁没有生效，且没有任何降级原因 ⇒ 接线坏了（fixture 未挂上／模块不在 "
        "_LOCAL_STACK_SERIAL_MODULES ／锁名或专用连接构造有误）。"
        "此时整组测试在多 pytest 进程下裸奔，而终端上看不出任何异样。")
    # 再从 MySQL 侧正向确认：锁的持有者就是本进程那条专用连接
    with local_stack._xproc_lock_connection().cursor() as cur:
        cur.execute("SELECT IS_USED_LOCK(%s)", (local_stack._xproc_lock_name(),))
        holder = cur.fetchone()[0]
        cur.execute("SELECT CONNECTION_ID()")
        me = cur.fetchone()[0]
    assert holder == me, f"锁持有者 {holder} 不是本进程专用连接 {me}——锁名或连接漂移了"


@requires_local_db
def test_feedback_review_executes_on_real_schema(monkeypatch):
    """真库执行 + 行回流：downvote × 引用文档 seed 后端点必须出行且逐字段成型。
    老代码（q.question）在这里直接 pymysql 1054 → HTTPException 500——本测试
    即该 bug 的最小真库复现，列名契约的「活体」版。

    回合循环**只**为跨进程整表清库兜底（判据=种子行是否还在），不放宽任何断言：
    种子俱在而无回流一律当场红。为什么需要它见模块 docstring。"""
    _skip_if_not_sim()
    monkeypatch.setenv("RAG_SIM_USER_ROLE", "kb_admin")
    # 缓存旁路：断言对象是 SQL 真跑，不是 TTL 缓存行为
    from opensearch_pipeline.routes import kb_console
    monkeypatch.setattr(kb_console, "_dashboard_cache_get", lambda k: None)
    monkeypatch.setattr(kb_console, "_dashboard_cache_put", lambda k, v: None)
    # 钉死 JSON_TABLE 回退形态（生产默认 RAG_QA_FACT_JOIN=off）：不随环境 flag 漂移，
    # 且该形态直接消费 qa_session_log 行本身，不依赖事实表回填
    monkeypatch.setattr("opensearch_pipeline.qa_facts.fact_join_enabled", lambda: False)

    from opensearch_pipeline import api
    from opensearch_pipeline.db import _get_db_conn

    conn = _get_db_conn()
    try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            _cleanup(conn)   # 上次中断/上一回合的残留先清，保证可重入
            _seed(conn)
            resp = api.kb_feedback_review(request=None, limit=20,
                                          identity=api.Identity(user_id="ITEST-FBR-adm"))
            items = {i.message_id: i for i in resp.items}
            if _MSG_ID in items:
                break
            # 没回流。先定性：种子三行还在 ⇒ 产品真坏了（列名/JOIN/时间窗），立刻红并取证；
            # 有行不翼而飞 ⇒ 只可能是外部进程的整表 DML（本进程只按主键删自己那三行）。
            counts = _seed_row_counts(conn)
            if all(counts.values()):
                pytest.fail("seed 的差评没有回流——列名/JOIN/时间窗有一个在真库上坏了\n"
                            + f"（本次{_lock_status()}）\n" + _diagnose(conn, resp))
            # 用 warning 而非 print：重试成功时 print 会被 pytest 吞掉（只有红/skip 才回显），
            # 外部干扰就成了看不见的隐性成本；warning 进 run 末尾的 warnings summary，可见可 grep。
            warnings.warn(
                f"[{__name__}] attempt {attempt}/{_MAX_ATTEMPTS}：种子行在断言前被外部进程"
                f"整表清掉（{counts}）——重新 seed 再来。本机是否同时在跑第二个 pytest？",
                stacklevel=1)
        else:
            pytest.skip(
                f"连续 {_MAX_ATTEMPTS} 回合的种子行都被外部进程整表清掉（{counts}，"
                f"本次{_lock_status()}）：本机是否同时在跑第二个 pytest（另一个 worktree / "
                "make test）？跨进程锁是**双边协议**，对面若是不带该 fixture 的老 worktree "
                "就拦不住。跨进程互踩不属产品缺陷，故 skip 而非红——见模块 docstring。")
        it = items[_MSG_ID]
        assert "清洗流程" in it.question            # 问题文本必须来自 qa_session_log.query_text
        assert it.reasons == ["不准确"]
        assert it.comment and it.handled is False   # PENDING（DDL 默认）= 未处置，进收件箱
        assert [d.doc_id for d in it.docs] == [_DOC_ID]
        assert it.docs[0].owner_dept == "production"
    finally:
        try:
            _cleanup(conn)
        finally:
            conn.close()


# ── 摄取侧 node 默认可见授权（2026-08-05）──────────────────────────────────────
# 为什么必须在**真库**：node 文档的可见性完全来自 kb_doc_node_grant，摄取此前一行不写
# ⇒ 按组织树重灌出来的文档 allowed_depts=[]，对所有人不可见，且检索静默返回空。
# 桩游标不校验唯一键、也不给 rowcount 语义，"缺则补/有则不动"这条只有真表能证。
_NG_DOC = "DOC_ITEST_NODEGRANT"
_NG_NODE = 999000111


def _ng_cleanup(cur):
    cur.execute("DELETE FROM kb_doc_node_grant WHERE doc_id=%s", (_NG_DOC,))
    cur.execute("DELETE FROM document_meta WHERE doc_id=%s", (_NG_DOC,))
    cur.execute("DELETE FROM kb_acl_projection_outbox WHERE doc_id=%s", (_NG_DOC,))


def _ng_register(cur, doc_id=_NG_DOC, node_id=_NG_NODE):
    """复刻 pipeline_nodes 摄取登记里 node 分支的两句（document_meta + 默认授权）。"""
    from opensearch_pipeline.access_grants import record_acl_projection_invalidation
    cur.execute(
        "INSERT INTO document_meta (doc_id,title,original_filename,owner_dept,acl_mode,"
        "owner_dept_id,status,current_version_no) VALUES (%s,'t','t.pdf',NULL,'node',%s,'active',1) "
        "ON DUPLICATE KEY UPDATE current_version_no=GREATEST(current_version_no,1)",
        (doc_id, node_id))
    cur.execute(
        "INSERT INTO kb_doc_node_grant (doc_id, dept_id, scope, granted_by, note) "
        "VALUES (%s,%s,'subtree',%s,%s) ON DUPLICATE KEY UPDATE id = id",
        (doc_id, node_id, "__ingest__", "ingest_default"))
    inserted = cur.rowcount == 1
    if inserted:
        record_acl_projection_invalidation(cur, doc_id, reason="ingest_node_default")
    return inserted


@requires_local_db
def test_ingest_node_default_grant_created_and_never_revives_revoked():
    """① 首次登记建 subtree 授权 ② 重灌幂等不重复 ③ **撤销后重灌绝不复活**。

    ③ 是本用例的重点：console 侧那条 ON DUPLICATE 会 `revoked_at=NULL` 复活（人主动带
    visible_nodes 上传），摄取若照抄，管理员撤销过的归属授权会在下次重灌被静默还原 ——
    ACL 回归且无人察觉。故摄取用 `ON DUPLICATE KEY UPDATE id=id` 的 no-op。
    """
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            _ng_cleanup(cur)
            conn.commit()

            # ① 首次登记 → 建出 subtree 授权 + 投影入队
            assert _ng_register(cur) is True, "首次登记应真插入授权行"
            conn.commit()
            cur.execute("SELECT scope, revoked_at, note FROM kb_doc_node_grant "
                        "WHERE doc_id=%s AND dept_id=%s", (_NG_DOC, _NG_NODE))
            row = cur.fetchone()
            assert row is not None, "摄取未写默认授权 ⇒ 该文档对所有人不可见"
            assert row[0] == "subtree" and row[1] is None
            cur.execute("SELECT COUNT(*) FROM kb_acl_projection_outbox WHERE doc_id=%s", (_NG_DOC,))
            assert cur.fetchone()[0] == 1, "首次建授权必须入队投影，否则 HA3 侧永不收敛"

            # ② 重灌（同 doc 同节点）→ 幂等，不重复建、不重复入队
            assert _ng_register(cur) is False, "重灌不应再次插入（rowcount 必须为 0）"
            conn.commit()
            cur.execute("SELECT COUNT(*) FROM kb_doc_node_grant WHERE doc_id=%s", (_NG_DOC,))
            assert cur.fetchone()[0] == 1

            # ③ 管理员撤销 → 重灌**不得**复活
            cur.execute("UPDATE kb_doc_node_grant SET revoked_at=NOW(), revoked_by='admin' "
                        "WHERE doc_id=%s AND dept_id=%s", (_NG_DOC, _NG_NODE))
            conn.commit()
            _ng_register(cur)
            conn.commit()
            cur.execute("SELECT revoked_at FROM kb_doc_node_grant "
                        "WHERE doc_id=%s AND dept_id=%s", (_NG_DOC, _NG_NODE))
            assert cur.fetchone()[0] is not None, (
                "撤销过的归属授权被重灌静默复活 —— ACL 回归（console 的复活语义不可照抄到摄取侧）")
        with conn.cursor() as cur:
            _ng_cleanup(cur)
            conn.commit()
    finally:
        conn.close()
