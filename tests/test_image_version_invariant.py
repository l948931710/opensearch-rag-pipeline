# -*- coding: utf-8 -*-
"""图片「当前版本」不变量 —— `_deny_stale_version_images`（2026-08-06）。

codex 补评审第一轮的第四条（Sam 拍板四项里范围最大的一条，经四轮 REVISE 收敛）。

缺陷：
  1. 🔴 4c(`_revalidate_main_hits`) 的 SQL 只取
     `chunk_id/is_active/permission_level/owner_dept/id` —— **无版本列**。双活版本窗口里
     旧版图 `is_active=1` 干净通过；而 `cosurface_doc_images` 遇到 `any(chunk_type=='image')`
     **直接短路返回** ⇒ cosurface 内那段版本 fail-closed 根本没机会跑。
  2. 🔴 `_probe_pool_image_refs` 在 **rerank 之前**从 RDS 附 `image_refs`，
     `reranker._img_key` 取它、`_signed()` 签名后进 `docs[].image_url`
     ⇒ **旧版本图会先被外发给 DashScope 的 qwen3-vl-rerank**。不是显示问题，是数据流出第三方。

权威 = **该 doc 最高的「完整 INDEXED」active 版本**，无完整版本时按单 active 版本降级，
多版本歧义则 fail-closed（`_resolve_serving_versions`）。原定的
`document_meta.current_version_no` 已被证伪并推翻——理由见该函数 docstring。
"""
import types

import pytest

from opensearch_pipeline import retriever


# ── 脚手架 ──────────────────────────────────────────────────────────────────

def _cfg(*, sim_db=False, sim_os=False):
    return types.SimpleNamespace(
        simulate_db=sim_db, simulate_opensearch=sim_os, simulate=False,
        rag=types.SimpleNamespace(main_hit_revalidate=True, allowed_depts_acl=False),
    )


def _crow(cid, pk, ctype, ver, doc="A"):
    """chunk 身份行：(chunk_id, id, chunk_type, version_no, doc_id)"""
    return (cid, pk, ctype, ver, doc)


def _srow(doc, complete_max, active_max, n_ver):
    """serving 解析行：(doc_id, complete_max, active_max, active_version_count)"""
    return (doc, complete_max, active_max, n_ver)


class _Cur:
    """SQL 感知：身份查询与 serving 聚合查询的返回形态不同，混用即假绿/假红。"""

    def __init__(self, crows, srows):
        self.crows, self.srows = crows, srows
        self.sql = []
        self._serving = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self._serving = "active_version_count" in sql

    def fetchall(self):
        return self.srows if self._serving else self.crows

    def close(self):
        pass


class _Conn:
    def __init__(self, crows, srows):
        self.crows, self.srows = crows, srows
        self.cursors = []

    def cursor(self, *a, **k):
        c = _Cur(self.crows, self.srows)
        self.cursors.append(c)
        return c

    def close(self):
        pass


def _gate(monkeypatch, rows, crows, srows, *, cfg=None, raises=False):
    from opensearch_pipeline import db as _db
    monkeypatch.setattr(retriever, "get_config", lambda: cfg or _cfg())

    def _conn():
        if raises:
            raise RuntimeError("RDS unreachable")
        return _Conn(crows, srows)

    monkeypatch.setattr(_db, "_get_db_conn", _conn)
    return retriever._deny_stale_version_images(rows, source="t")


def _img(cid="c1", pk="1", ver=3):
    return {"doc_id": "A", "chunk_id": cid, "id": pk, "chunk_type": "image",
            "version_no": ver, "source_image": f"oss/{cid}.png", "visual_summary": cid}


def _step(cid="s1", pk="2", refs=True):
    c = {"doc_id": "A", "chunk_id": cid, "id": pk, "chunk_type": "step_card",
         "chunk_text": "步骤正文", "version_no": 3}
    if refs:
        c["image_refs"] = [{"oss_key": f"oss/{cid}.png", "visual_summary": "s"}]
    return c


# ── serving 版本解析：DAG2→DAG3 生命周期状态矩阵（codex 指定）──────────────────

class _MatrixCur:
    def __init__(self, srows):
        self.srows = srows

    def execute(self, sql, params=None):
        assert "active_version_count" in sql

    def fetchall(self):
        return self.srows


@pytest.mark.parametrize("name,srow,expect", [
    # complete_max, active_max, active_version_count
    ("vN 已完整索引、vN+1 还没产 chunk",           _srow("A", 3, 3, 1), 3),
    ("vN+1 已落库但 NOT_INDEXED（DAG2 后 DAG3 前）", _srow("A", 3, 4, 2), 3),
    ("vN+1 部分 INDEXED（DAG3 半途）",              _srow("A", 3, 4, 2), 3),
    ("vN+1 全部 INDEXED、vN 尚未停用（双活窗口）",   _srow("A", 4, 4, 2), 4),
    ("vN+1 FAILED / DEAD，vN 完整",                _srow("A", 3, 4, 2), 3),
    ("skip-gate：根本没产新 chunk",                 _srow("A", 3, 3, 1), 3),
    ("单 active 版本被投影标脏（ACL/标题/可见性）",  _srow("A", None, 3, 1), 3),
    ("同版本重切提交后整版 NOT_INDEXED（单版本）",    _srow("A", None, 5, 1), 5),
    ("★ 两个 active 版本且都不完整 ⇒ 歧义",         _srow("A", None, 4, 2), None),
])
def test_serving_version_state_matrix(name, srow, expect):
    """★ 权威解析的完整生命周期矩阵。

    最后一条是**宽 fallback 的关键变异体**：vN 正在服务但被投影标脏、vN+1 在 DAG3 部分
    推送成功（图片 chunk 已进 HA3、兄弟 FAILED）。此时两版都不完整——「无完整版本就取
    MAX(active)」会选中 vN+1，而系统正因它不完整才刻意保留 vN ⇒ **错版本投放**。
    只测「脏存量能 fallback 成功」锁不住这条边界。
    """
    got = retriever._resolve_serving_versions(_MatrixCur([srow]), ["A"])
    assert got == {"A": expect}, f"{name}: 期望 {expect}，实得 {got}"


class _SqliteCur:
    """让 `_resolve_serving_versions` 跑在**真** SQL 引擎上。

    ★ 为什么必须有这一条：完整性判据（`SUM(CASE WHEN index_status='INDEXED' THEN 0 ELSE 1 END)`
    的嵌套聚合）**整个活在 SQL 里**，Python 侧一行都没有。纯桩测试对它零覆盖 ——
    把该判据改成 `SUM(0)`（即"任何版本都算完整"）时全套件依然全绿（2026-08-06 变异实证）。
    这里用 sqlite 跑代码**真正产出的那条 SQL**（只机械替换 `%s`→`?`，且下方断言原串未被
    改写过判据），嵌套 GROUP BY / CASE / MAX(CASE...) 两个引擎语义一致。
    """

    def __init__(self, rows):
        import sqlite3
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE chunk_meta (doc_id TEXT, version_no INT,"
                          " is_active INT, index_status TEXT)")
        self.conn.executemany("INSERT INTO chunk_meta VALUES (?,?,?,?)", rows)
        self.sql = ""
        self._cur = None

    def execute(self, sql, params=None):
        self.sql = sql
        self._cur = self.conn.execute(sql.replace("%s", "?"), tuple(params or ()))

    def fetchall(self):
        return self._cur.fetchall()


@pytest.mark.parametrize("name,rows,expect", [
    ("单版本全 INDEXED",
     [("A", 3, 1, "INDEXED"), ("A", 3, 1, "INDEXED")], 3),
    ("vN 完整、vN+1 全 NOT_INDEXED（DAG2 后）",
     [("A", 3, 1, "INDEXED"), ("A", 4, 1, "NOT_INDEXED")], 3),
    ("★ vN 完整、vN+1 **部分** INDEXED（DAG3 半途）",
     [("A", 3, 1, "INDEXED"), ("A", 4, 1, "INDEXED"), ("A", 4, 1, "FAILED")], 3),
    ("双活窗口：vN+1 全 INDEXED",
     [("A", 3, 1, "INDEXED"), ("A", 4, 1, "INDEXED")], 4),
    ("★ 两版都不完整 ⇒ 歧义",
     [("A", 3, 1, "NOT_INDEXED"), ("A", 4, 1, "INDEXED"), ("A", 4, 1, "FAILED")], None),
    ("单版本被投影标脏 ⇒ fallback",
     [("A", 3, 1, "NOT_INDEXED"), ("A", 3, 1, "NOT_INDEXED")], 3),
    # NULL 必须计作**不完整**。用「v3 全 INDEXED + v4 = INDEXED+NULL」才判别得出：
    # 若把 NULL 错算完整，complete_max 会变成 4。同版本内混 NULL 的写法判别不出 ——
    # 单版本 fallback 也返回同一个号（codex 2026-08-06 点名的空转形态）。
    ("index_status 为 NULL 计作不完整",
     [("A", 3, 1, "INDEXED"), ("A", 4, 1, "INDEXED"), ("A", 4, 1, None)], 3),
    ("inactive 行不参与（旧版已停用）",
     [("A", 3, 1, "INDEXED"), ("A", 2, 0, "INDEXED")], 3),
])
def test_serving_version_sql_semantics_on_real_engine(name, rows, expect):
    cur = _SqliteCur(rows)
    got = retriever._resolve_serving_versions(cur, ["A"])
    assert got == {"A": expect}, f"{name}: 期望 {expect}，实得 {got}"
    # 判据本体的钉子：被改成恒完整（如 SUM(0)）时上面第 3/5 条会红，这条再兜一层
    assert "CASE WHEN index_status = 'INDEXED' THEN 0 ELSE 1 END" in cur.sql


class _DictRowCur:
    """真 `pymysql.cursors.DictCursor` 的行形态（字典，不是元组）。"""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.rows


def test_serving_version_accepts_dict_cursor_rows():
    """★ 行契约必须同时吃元组与 dict（PyMySQL `DictCursor` 的行形态）。

    `_probe_pool_image_refs` 用的是 `pymysql.cursors.DictCursor`，版本门用默认元组游标 ——
    同一个 helper 被两种游标调用。按四元组解包 dict 会解出**键名**，
    `int("complete_max")` 抛 ValueError，再被 probe 的 fail-open 吞掉
    ⇒ **生产上 probe 永远装不上图**（功能静默失效，而测试因为桩的 `cursor(*a, **k)`
    忽略 cursor 类而全绿）。codex 2026-08-06 用真字典行直驱实证。
    """
    rows = [{"doc_id": "D1", "complete_max": 3, "active_max": 3, "active_version_count": 1},
            {"doc_id": "D2", "complete_max": None, "active_max": 4, "active_version_count": 2},
            {"doc_id": "D3", "complete_max": None, "active_max": 7, "active_version_count": 1}]
    got = retriever._resolve_serving_versions(_DictRowCur(rows), ["D1", "D2", "D3"])
    assert got == {"D1": 3, "D2": None, "D3": 7}


def test_probe_uses_dict_cursor_and_still_resolves(monkeypatch):
    """★ 端到端钉住上一条的生产形态：probe 建的是 DictCursor，
    整条链（serving 解析 → refs 查询 → 附 refs）必须真的走通。"""
    import json
    from opensearch_pipeline import db as _db

    class _DictCur:
        def __init__(self):
            self.rows = []

        def execute(self, sql, params=None):
            if "active_version_count" in sql:
                self.rows = [{"doc_id": "D1", "complete_max": 3,
                              "active_max": 3, "active_version_count": 1}]
            else:
                self.rows = [{"chunk_id": "S1",
                              "image_refs_json": json.dumps([{"oss_key": "s1.png"}])}]

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class _C:
        def cursor(self, klass=None):
            import pymysql.cursors
            assert klass is pymysql.cursors.DictCursor, "probe 应建 DictCursor"
            return _DictCur()

        def close(self):
            pass

    monkeypatch.setattr(_db, "_get_db_conn", lambda: _C())
    out = retriever._probe_pool_image_refs(
        [{"chunk_type": "step_card", "chunk_id": "S1", "doc_id": "D1"}])
    assert out[0]["image_refs"][0]["oss_key"] == "s1.png", "DictCursor 路径下 refs 没装上"


def test_serving_version_no_active_rows_is_unverifiable():
    """整篇退役（无 active 行）⇒ 聚合无该 doc ⇒ 无权威 ⇒ fail-closed。
    不得把 active_version_count=0 当成"单版本降级"。"""
    assert retriever._resolve_serving_versions(_MatrixCur([]), ["A"]) == {}


def test_serving_version_skips_query_when_no_docs():
    class _Boom:
        def execute(self, *a, **k):
            raise AssertionError("无 doc 不该发查询")

    assert retriever._resolve_serving_versions(_Boom(), []) == {}


# ── 逐行五类处置 ────────────────────────────────────────────────────────────

def test_current_version_row_passes_through(monkeypatch):
    """反证锚：serving 版本原样放行。没有它，下面所有「被丢弃」断言都可能是空转的。"""
    rows = [_img()]
    out = _gate(monkeypatch, rows, [_crow("c1", "1", "image", 3)], [_srow("A", 3, 3, 1)])
    assert out == rows


def test_stale_image_row_is_dropped(monkeypatch):
    """`image` 整条就是图 ⇒ 陈旧时**丢行**（剥图会留下一条空壳）。"""
    out = _gate(monkeypatch, [_img(ver=2)], [_crow("c1", "1", "image", 2)],
                [_srow("A", 3, 3, 1)])
    assert out == []


def test_stale_visual_knowledge_row_is_dropped(monkeypatch):
    """`visual_knowledge` 的 chunk_text 本就是图 caption 派生 ⇒ 留下等于留旧版内容。"""
    vk = dict(_img(), chunk_type="visual_knowledge", chunk_text="图注派生正文")
    out = _gate(monkeypatch, [vk], [_crow("c1", "1", "visual_knowledge", 2)],
                [_srow("A", 3, 3, 1)])
    assert out == []


def test_stale_step_card_keeps_text_but_strips_images(monkeypatch):
    """★ 带图正文类 ⇒ **剥图保正文**：正文是它自己那版的正文，不该被图连坐。"""
    out = _gate(monkeypatch, [_step()], [_crow("s1", "2", "step_card", 2)],
                [_srow("A", 3, 3, 1)])
    assert len(out) == 1, "正文行不该被丢"
    assert out[0]["chunk_text"] == "步骤正文"
    assert out[0]["image_refs"] == []
    assert not out[0]["source_image"] and not out[0]["oss_key"]


@pytest.mark.parametrize("ctype", ["text_chunk", "clause_chunk", "ocr_chunk"])
def test_stale_text_family_strips_images_not_rows(monkeypatch, ctype):
    """`llm_generator` 的出图分支不只有 image —— 这三类带 image_refs/source_image
    同样注入 `<<IMG:N>>`。谓词只留 `chunk_type=='image'` ⇒ 本组即红。"""
    row = {"doc_id": "A", "chunk_id": "t1", "id": "9", "chunk_type": ctype,
           "chunk_text": "正文", "source_image": "oss/t.png", "version_no": 2}
    out = _gate(monkeypatch, [row], [_crow("t1", "9", ctype, 2)], [_srow("A", 3, 3, 1)])
    assert len(out) == 1 and out[0]["chunk_text"] == "正文"
    assert not out[0]["source_image"]


def test_keyless_image_row_is_fail_closed(monkeypatch):
    """两个轴都没有键 ⇒ 无从复核 ⇒ 不放行（无从复核 ≠ 放行）。"""
    ghost = {"doc_id": "A", "chunk_id": "", "id": "", "chunk_type": "image",
             "source_image": "oss/ghost.png", "version_no": 3}
    assert _gate(monkeypatch, [ghost], [_crow("c1", "1", "image", 3)],
                 [_srow("A", 3, 3, 1)]) == []


def test_row_absent_from_authority_is_fail_closed(monkeypatch):
    """RDS 查不到该行（purge/删除后 HA3 残留）⇒ fail-closed。"""
    assert _gate(monkeypatch, [_img()], [], []) == []


def test_doc_without_serving_version_is_fail_closed(monkeypatch):
    """该 doc 解析不出 serving 版本（多 active 版本且都不完整）⇒ fail-closed。"""
    assert _gate(monkeypatch, [_img()], [_crow("c1", "1", "image", 3)],
                 [_srow("A", None, 4, 2)]) == []


def test_pk_axis_resolves_rows_without_chunk_id(monkeypatch):
    """两条查询轴必须**都**查：只有 HA3 id 的历史行不能永远查不到
    （4c 的老教训——把两轴压进同一列表按 chunk_id 查，所谓 id 兜底是假的）。"""
    row = dict(_img(), chunk_id="")
    out = _gate(monkeypatch, [row], [_crow("", "1", "image", 3)], [_srow("A", 3, 3, 1)])
    assert out == [row], "PK 轴没被查 ⇒ 该行会被误判无从复核"


def test_version_authority_is_rds_not_ha3(monkeypatch):
    """★ 版本取 **RDS 的 chunk_meta.version_no**，绝不取 HA3 自报的那个 ——
    HA3 投影正是被复核的对象，拿它当依据等于让投影自证。

    本条把两者刻意对立：HA3 谎报 v3（=serving），RDS 说这条其实是 v2。
    """
    liar = dict(_img(cid="c_lie"), version_no=3)
    out = _gate(monkeypatch, [liar], [_crow("c_lie", "1", "image", 2)], [_srow("A", 3, 3, 1)])
    assert out == [], "HA3 自报版本被当成了权威"


def test_disposition_is_per_row_not_per_call(monkeypatch):
    """★ 逐行分派，**不是**调用级参数（codex 明确否掉 `drop_refs_only` 当调用级开关）。

    同一批里三种行同时在场：陈旧 image 丢行、陈旧 step 剥图留正文、serving 版本原样。
    改成"整批一种处置"⇒ 本条必红。
    """
    rows = [_img("bad", "1", ver=2), _step("s1", "2"), _img("ok", "3")]
    out = _gate(monkeypatch, rows,
                [_crow("bad", "1", "image", 2), _crow("s1", "2", "step_card", 2),
                 _crow("ok", "3", "image", 3)],
                [_srow("A", 3, 3, 1)])
    kinds = [(c.get("chunk_id"), c.get("chunk_type"), bool(c.get("image_refs")
                                                          or c.get("source_image")))
             for c in out]
    assert kinds == [("s1", "step_card", False), ("ok", "image", True)]


def test_image_type_without_payload_still_gated(monkeypatch):
    """★ 谓词的**类型分支**承重：`image`/`visual_knowledge` 即便投影里没有
    `source_image`/`image_refs` 也要进作用域 —— 那种行的 `chunk_text`/`visual_summary`
    本身就是旧版内容。"""
    bare_img = {"doc_id": "A", "chunk_id": "b1", "id": "7", "chunk_type": "image",
                "visual_summary": "旧版图注", "version_no": 2}
    assert _gate(monkeypatch, [bare_img], [_crow("b1", "7", "image", 2)],
                 [_srow("A", 3, 3, 1)]) == []

    bare_vk = {"doc_id": "A", "chunk_id": "b2", "id": "8", "chunk_type": "visual_knowledge",
               "chunk_text": "旧版图注派生正文", "version_no": 2}
    assert _gate(monkeypatch, [bare_vk], [_crow("b2", "8", "visual_knowledge", 2)],
                 [_srow("A", 3, 3, 1)]) == []


def test_pure_text_batch_costs_no_extra_query(monkeypatch):
    """★ 作用域**有意不含**无载荷的 text_chunk/step_card：否则几乎每一批检索都多一次
    RDS 查表（`text_chunk` 无处不在），而对无载荷的行本门什么也做不了 —— 纯粹的成本。

    下游那两个装载面各自已堵：probe 共用同一权威解析，expand 后有消费点③。
    """
    from opensearch_pipeline import db as _db
    monkeypatch.setattr(retriever, "get_config", lambda: _cfg())
    monkeypatch.setattr(_db, "_get_db_conn",
                        lambda: (_ for _ in ()).throw(AssertionError("纯文本批不该连库")))
    rows = [{"doc_id": "A", "chunk_id": "t", "chunk_type": "text_chunk", "chunk_text": "x"},
            _step("s0", "5", refs=False)]
    assert retriever._deny_stale_version_images(rows) == rows


# ── 失败语义 ────────────────────────────────────────────────────────────────

def test_authority_unreachable_strips_images_but_keeps_text(monkeypatch):
    """权威不可达 ⇒ fail-closed，但**只丢图不丢正文**：本仓优雅降级铁律的边界 ——
    RDS 故障不该放大成"没有答案"，只该放大成"没有配图"。"""
    out = _gate(monkeypatch, [_img(), _step()], [], [], raises=True)
    assert len(out) == 1 and out[0]["chunk_type"] == "step_card"
    assert out[0]["chunk_text"] == "步骤正文" and out[0]["image_refs"] == []


def test_sim_tiers_are_not_interchangeable(monkeypatch):
    """★ SIM 两档不可合并（codex 点名）：
    全模拟 no-op（无真数据面）/ 半模拟 fail-closed（`_validate_environment_target_consistency`
    在 simulate_db 时跳过 RDS 目标校验 ⇒ 该档完全没有守卫，不能 warning 后放行）。"""
    rows = [_img()]
    assert _gate(monkeypatch, rows, [], [], cfg=_cfg(sim_db=True, sim_os=True)) == rows
    assert _gate(monkeypatch, rows, [], [], cfg=_cfg(sim_db=True, sim_os=False)) == []


def test_gate_is_independent_of_main_hit_revalidate(monkeypatch):
    """★ 版本门**不受 `main_hit_revalidate` 门控**（codex 第一轮 REVISE 打掉的正是 v1 那版）。

    4c 在任何 SQL 之前就早退（`not main_hit_revalidate` / `simulate_db`）——把版本门塞进
    4c，等于让一个**性能开关**能关掉一道**安全门**。
    """
    cfg = _cfg()
    cfg.rag.main_hit_revalidate = False
    assert _gate(monkeypatch, [_img(ver=2)], [_crow("c1", "1", "image", 2)],
                 [_srow("A", 3, 3, 1)], cfg=cfg) == []


# ── 消费点接线 ──────────────────────────────────────────────────────────────

def test_probe_pool_shares_the_same_authority(monkeypatch):
    """★ 消费点②：`_probe_pool_image_refs` 跑在 **rerank 之前**，装上的 refs 会被
    `reranker._img_key` 取走并签名后外发给 qwen3-vl-rerank ⇒ 不能"先装上、下游再剥"，
    必须**根本不装**。且必须与主门**共用**同一个权威解析（两套 SQL 迟早漂移）。
    """
    from opensearch_pipeline import db as _db
    conn = _Conn([], [_srow("A", 3, 3, 1)])
    monkeypatch.setattr(_db, "_get_db_conn", lambda: conn)
    retriever._probe_pool_image_refs([_step("s9", "9", refs=False)])
    sql = " ".join(" ".join(c.sql) for c in conn.cursors)
    assert "active_version_count" in sql, "probe 没走共用的 serving 权威解析"
    assert "cm.version_no" in sql and "cm.is_active = 1" in sql, \
        f"probe 的 refs 查询缺版本/活性约束：{sql}"


def test_probe_pool_loads_nothing_when_authority_is_ambiguous(monkeypatch):
    """★ 歧义态（多 active 版本且都不完整）⇒ probe **一张图都不装**，
    而不是"装上再交给下游剥" —— 下游就是 rerank 外发。"""
    from opensearch_pipeline import db as _db
    conn = _Conn([], [_srow("A", None, 4, 2)])
    monkeypatch.setattr(_db, "_get_db_conn", lambda: conn)
    step = _step("s9", "9", refs=False)
    out = retriever._probe_pool_image_refs([step])
    assert out == [step] and not out[0].get("image_refs")
    sql = " ".join(" ".join(c.sql) for c in conn.cursors)
    assert "image_refs_json" not in sql, "歧义态下不该发出 refs 查询"


def test_cosurface_passes_acl_ctx_to_4b(monkeypatch):
    """★ codex 点名的测试旁路①：cosurface 里 4b 调用删掉 `acl_ctx=acl_ctx` 后**全套件仍绿**。

    本条钉住该关键字实参本身——node-ACL 的读侧 fail-closed 复核整段挂在
    `acl_ctx is not None` 之下，漏传 = 那段复核对补图路径整体失效。
    """
    seen = {}

    def _spy(rows, user_dept, *, acl_ctx=None):
        seen["acl_ctx"] = acl_ctx
        return rows

    monkeypatch.setattr(retriever, "_deny_revoked_cross_dept", _spy)
    monkeypatch.setattr(retriever, "_fetch_cosurface_images", lambda *a, **k: [_img()])
    monkeypatch.setattr(retriever, "_revalidate_main_hits", lambda rows, **k: rows)
    monkeypatch.setattr(retriever, "_deny_stale_version_images", lambda rows, **k: rows)
    monkeypatch.setattr(retriever, "get_config", lambda: _cfg())
    sentinel = object()
    retriever.cosurface_doc_images(
        "q", [{"doc_id": "A", "chunk_type": "text_chunk", "chunk_text": "t"}],
        acl_ctx=sentinel)
    assert seen.get("acl_ctx") is sentinel, "cosurface 的 4b 调用漏传 acl_ctx"


def test_all_wiring_points_are_present():
    """★ 接线点计数守卫：本仓 flag 传播的老教训 —— 只接一条执行路径，另一条恒不触发。

    四个调用点：主命中(4d) / 本地 OS 回退 / expand 后 / cosurface 内；
    `_probe_pool_image_refs` 走共用的 `_resolve_serving_versions`，由上面两条钉。
    删掉任意一个 ⇒ 本条即红。
    """
    import inspect
    src = inspect.getsource(retriever)
    calls = src.count("_deny_stale_version_images(")
    assert calls >= 5, f"版本门调用点从 4 个掉到了 {calls - 1} 个"
    assert src.count("_resolve_serving_versions(") >= 3, \
        "权威解析的共用调用点缺失（主门 / probe 必须都走它）"
    for fn in (retriever.search_chunks, retriever.cosurface_doc_images,
               retriever.retrieve_and_enrich):
        assert "_deny_stale_version_images" in inspect.getsource(fn), \
            f"{fn.__name__} 上没有版本门"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
