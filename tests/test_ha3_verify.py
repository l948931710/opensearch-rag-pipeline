# -*- coding: utf-8 -*-
"""ha3_verify(serving 可检索性 self-query 探针)+ ha3_reconcile 倒排枚举器的单测。

⚠️ 定位已按 2026-07-22 实证更正:self-query 走 ANN+融合+ACL,**不是物理存在性判据**
(未命中可能只是排序/阈值/权限);物理存在性唯官方 fetch 为准,发现未知 PK 唯倒排枚举为准。
枚举器侧的 loop-until-stable 亦已废除——盲区是确定性的,重扫无效。"""
import pytest

from opensearch_pipeline.ha3_verify import verify_chunks_present
from opensearch_pipeline.ha3_reconcile import _enumerate_ha3_pks


# ── fake RDS ────────────────────────────────────────────────────
# 探针现在**一律按权威 acl_mode** 决定查询身份（不看投影哨兵），所以替身必须按 SQL 分派：
# chunk_meta 回分块行，权威三表（information_schema / document_meta / 授权表）各自回自己的。
class _Cur:
    def __init__(self, rows, authority=None):
        self._rows, self._auth = rows, (authority or {})
        self._out = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        s = " ".join((sql or "").split())
        if "information_schema.COLUMNS" in s:
            self._out = [(1 if self._auth.get("has_node_col", True) else 0,)]
        elif "chunk_meta" in s:
            self._out = self._rows
        elif "document_meta" in s:
            self._out = self._auth.get("meta", [])
        elif "kb_access_request" in s:
            self._out = self._auth.get("grants", [])
        elif "kb_doc_node_grant" in s:
            self._out = self._auth.get("nodes", [])
        else:
            self._out = []

    def fetchall(self): return self._out
    def fetchone(self): return self._out[0] if self._out else None


class _Conn:
    def __init__(self, rows, authority=None):
        self._rows, self._auth = rows, authority
    def cursor(self): return _Cur(self._rows, self._auth)


def _legacy_authority(doc_id="D", owner="hr"):
    return {"meta": [(doc_id, "dept_internal", owner, "legacy")]}


def _node_authority(doc_id="N", owner="", nodes=((7, "subtree"), (9, "exact"))):
    return {"meta": [(doc_id, "dept_internal", owner, "node")],
            "nodes": [(doc_id, d, s) for d, s in nodes]}


def _chunks():
    return [
        {"id": 10, "chunk_id": "D_c0", "chunk_text": "procedure parent alpha", "chunk_type": "procedure_parent", "owner_dept": "hr"},
        {"id": 11, "chunk_id": "D_c1", "chunk_text": "step one beta content", "chunk_type": "step_card", "owner_dept": "hr"},
        {"id": 12, "chunk_id": "D_c2", "chunk_text": "step two gamma content", "chunk_type": "step_card", "owner_dept": "hr"},
    ]


def _retrieve(chunks, doc_id, miss=(), foreign=None):
    def rf(query, *, top_k=5, user_dept=None):
        out = []
        for c in chunks:
            if c["id"] in miss:
                continue
            if (c["chunk_text"] or "")[:160] == query:
                out.append({"id": str(c["id"]), "doc_id": doc_id, "chunk_id": c["chunk_id"]})
        if foreign:
            out.append(foreign)
        return out
    return rf


def test_all_chunks_present_ok():
    ch = _chunks()
    r = verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=_retrieve(ch, "D"))
    assert r["ok"] and r["present"] == 3 and r["missing_ids"] == []
    assert r["expected_ids"] == [10, 11, 12] and r["served_ids"] == [10, 11, 12]
    assert "self-query" in r["method"]


def test_missing_chunk_detected():
    ch = _chunks()
    r = verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=_retrieve(ch, "D", miss={12}))
    assert not r["ok"] and r["missing_ids"] == [12] and r["present"] == 2


def test_self_query_false_negative_when_retrieve_empty_is_caught():
    # simulate the G30 symptom at the SERVING layer would show as missing — verifier reports it,
    # never silently passes
    ch = _chunks()
    r = verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=lambda q, **k: [])
    assert not r["ok"] and r["missing_ids"] == [10, 11, 12]


def test_rds_expanded_sibling_does_not_prove_ha3_presence():
    """★ C2 假绿回归：缺失的 step_card 不得因"同家族兄弟被命中并展开出它"而判 present。

    生产 expand_step_context 把兄弟从 **RDS** 拉出，覆写展开行的 chunk_id(id/doc_id 仍继承
    命中行)。于是 c2 明明不在 HA3，只要 c1 被索引，自查就会命中 c1 → 展开出携带 c2 chunk_id
    的 RDS 行 → 判 present ⇒ missing_ids=[] 报全绿，修复被跳过、用户持续召回失败。
    """
    ch = _chunks()

    def rf(query, *, top_k=5, user_dept=None):
        # c2 不在 HA3。但用 c2 自己的文本自查时，语义上会命中同家族的 c1（c1 在 HA3），
        # 生产 expand_step_context 随即把 c1 的兄弟 c2 从 RDS 拉出并覆写 chunk_id ——
        # 于是返回集里【出现了 c2 的 chunk_id】，却没有任何 HA3 侧证据。
        if query not in ((ch[1]["chunk_text"] or "")[:160], (ch[2]["chunk_text"] or "")[:160]):
            return []
        return [
            {"id": "11", "doc_id": "D", "chunk_id": "D_c1", "_direct_hit": True},
            # 展开行：id 继承命中行 c1(11)——注意 ≠ c2 的 id(12)，故只能经 chunk_id 分支匹配
            {"id": "11", "doc_id": "D", "chunk_id": "D_c2",
             "is_expanded": True, "_direct_hit": False},
        ]

    r = verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=rf)
    assert 12 in r["missing_ids"], "RDS 展开行被误当 HA3 在场证据 —— 假绿复现"
    assert not r["ok"]


def test_direct_hit_displaced_by_higher_scored_sibling_is_still_present():
    """★ C2 假红回归：同家族双直接命中时，展开行可能在去重中压掉 direct 行(分数更高)，
    但该 chunk 【确实】被 HA3 命中过 —— 必须仍判 present。

    这正是"整行过滤 is_expanded"会踩的坑：幸存行 is_expanded=True，但 retriever 已按
    OR 合并把 direct 证据保留在 `_direct_hit` 上。
    """
    ch = _chunks()

    def rf(query, *, top_k=5, user_dept=None):
        if (ch[2]["chunk_text"] or "")[:160] != query:
            return []
        # c2 的幸存行来自 c1 展开(分数更高)，但 _direct_hit 记着"它也曾被直接命中"
        return [{"id": "11", "doc_id": "D", "chunk_id": "D_c2",
                 "is_expanded": True, "_direct_hit": True}]

    r = verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=rf)
    assert 12 not in r["missing_ids"], "OR 合并的 direct 证据被忽略 —— 真实命中被判假红"


def test_unexpanded_results_default_to_direct():
    """expand_step_context 异常/无 RDS 连接时"回退到原始结果"——那些裸 HA3 行既无
    `_direct_hit` 也无 `is_expanded`，必须默认按 direct 处理，否则整轮验证全判缺失。"""
    ch = _chunks()
    r = verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=_retrieve(ch, "D"))
    assert r["ok"] and r["missing_ids"] == []


def test_foreign_doc_surfaced_recorded():
    ch = _chunks()
    foreign = {"id": "999", "doc_id": "OTHER", "chunk_id": "OTHER_c0"}
    r = verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=_retrieve(ch, "D", foreign=foreign))
    assert r["ok"]                          # all D chunks still present
    # foreign id observed but not counted as present/served for D
    assert 999 not in r["served_ids"]
    # 契约键 foreign_ids 必须返回（此前漏返回 → 上线 verify 读它 KeyError、ACL 泄漏检查失效）
    assert r["foreign_ids"] == [999]


# ── node-ACL：哨兵 owner 不得当组码传，身份须来自权威节点授权 ──────────────────
def _node_chunks():
    from opensearch_pipeline.acl_policy import NODE_OWNER_SENTINEL
    return [{"id": 20, "chunk_id": "N_c0", "chunk_text": "node doc alpha",
             "chunk_type": "text", "owner_dept": NODE_OWNER_SENTINEL}]


def _enable_node_flags(monkeypatch, *, grant=True):
    """打开 node-ACL flag。`can_read_doc` 在 GRANT=false 时对 node 文档**无条件拒绝**，
    因此凡断言 node 文档 ok=True 的测试都必须显式开 flag（这正是回滚姿态的语义）。"""
    from opensearch_pipeline import retriever

    class _Rag:
        allowed_depts_acl = True
        node_acl_grant = grant
        node_acl_enforce = True

    class _Cfg:
        rag = _Rag()

    monkeypatch.setattr(retriever, "get_config", lambda: _Cfg())


def _real_filter_retrieve_fn(monkeypatch, chunk, *, grant=True):
    """**真组合 seam**：retrieve_fn 内部调真实 `retriever._build_permission_filter`，
    按产出的过滤表达式是否放行该 chunk 决定返不返回命中。

    这样 `verify_chunks_present → retrieve_fn → _build_permission_filter` 任一段接线被删
    都会红 —— 早先那版 fake 直接返回命中，把「过滤器空 groups 早退导致 node 分支不可达」
    整个掩盖掉了（2026-07-31 codex 评审）。
    """
    from opensearch_pipeline import retriever

    class _Rag:
        allowed_depts_acl = True
        node_acl_grant = grant
        node_acl_enforce = True

    class _Cfg:
        rag = _Rag()

    monkeypatch.setattr(retriever, "get_config", lambda: _Cfg())

    def rf(query, *, top_k=5, user_dept=None, acl_ctx=None):
        expr = retriever._build_permission_filter(user_dept, acl_ctx=acl_ctx)
        # 该 chunk 的投影：dept_internal + allowed_depts=["d:7","dx:9"]（哨兵 owner）
        allowed = any(f'allowed_depts="{v}"' in expr for v in ("d:7", "dx:9"))
        if not allowed:
            return []
        return [{"id": str(chunk["id"]), "doc_id": "N", "chunk_id": chunk["chunk_id"]}]

    return rf


def test_node_doc_reachable_through_real_permission_filter(monkeypatch):
    """★ 真 seam：node 文档必须经【真实过滤器】被自身内容召回。

    修复前两处叠加导致假绿：探针把哨兵当组码传（永远匹配不上 d:/dx:），而过滤器又在
    空 groups 时早退 public-only。任一处回退，本测试都会红。
    """
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, DocAcl

    monkeypatch.setattr(
        "opensearch_pipeline.access_grants.resolve_doc_acl",
        lambda ids, cur, **kw: {"N": DocAcl(mode=ACL_MODE_NODE, permission_level="dept_internal",
                                            owner_dept="", node_ids=(7,), exact_node_ids=(9,))})
    ch = _node_chunks()
    r = ha3_verify.verify_chunks_present(
        "N", conn=_Conn(ch, _node_authority()), retrieve_fn=_real_filter_retrieve_fn(monkeypatch, ch[0]))
    assert r["ok"] and r["acl_mode"] == "node" and r["missing_ids"] == []


def test_node_doc_unreachable_when_grant_off_through_real_filter(monkeypatch):
    """GRANT=false ⇒ 真实过滤器不产生节点分支 ⇒ 探针如实判缺（不是假绿）。"""
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, DocAcl

    monkeypatch.setattr(
        "opensearch_pipeline.access_grants.resolve_doc_acl",
        lambda ids, cur, **kw: {"N": DocAcl(mode=ACL_MODE_NODE, permission_level="dept_internal",
                                            node_ids=(7,), exact_node_ids=(9,))})
    ch = _node_chunks()
    r = ha3_verify.verify_chunks_present(
        "N", conn=_Conn(ch, _node_authority()),
        retrieve_fn=_real_filter_retrieve_fn(monkeypatch, ch[0], grant=False))
    assert not r["ok"] and r["missing_ids"] == [20]


def test_unconverged_projection_still_uses_node_identity(monkeypatch):
    """★★ 权威已切 node、**投影尚未收敛**（chunk 仍带旧真实 owner，哨兵还没写下）。

    初版实现用「投影里有没有哨兵」反推 mode ⇒ 这个窗口里 acl_ctx 恒为 None ⇒ 探针拿旧
    owner 当组码查 ⇒ 文档经 legacy owner 分支被找到 ⇒ **报 ok=True 的假绿**，而真正被节点
    授权的用户走 d:/dx: 根本召不回它。查询身份必须取自权威（codex BLOCKER 2026-07-31）。
    """
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, DocAcl

    _enable_node_flags(monkeypatch)
    stale = [{"id": 30, "chunk_id": "S_c0", "chunk_text": "unconverged doc",
              "chunk_type": "text", "owner_dept": "hr"}]        # ← 旧真实 owner，不是哨兵
    monkeypatch.setattr(
        "opensearch_pipeline.access_grants.resolve_doc_acl",
        lambda ids, cur, **kw: {"S": DocAcl(mode=ACL_MODE_NODE, permission_level="dept_internal",
                                            owner_dept="hr", node_ids=(7,), exact_node_ids=(9,))})
    seen = {}

    def rf(query, *, top_k=5, user_dept=None, acl_ctx=None):
        seen["user_dept"], seen["acl_ctx"] = user_dept, acl_ctx
        # 模拟真实过滤器：node 身份才召得回（旧 owner 组码走的是另一条分支，这里不给命中）
        return [{"id": "30", "doc_id": "S", "chunk_id": "S_c0"}] if acl_ctx is not None else []

    r = ha3_verify.verify_chunks_present("S", conn=_Conn(stale, _node_authority("S")), retrieve_fn=rf)
    assert r["acl_mode"] == "node", "用投影哨兵反推 mode ⇒ 这里会退回 legacy 并产出假绿"
    assert not seen["user_dept"] and seen["acl_ctx"].ancestor_dept_ids == (7,)   # 无组码
    assert r["ok"]


def test_legacy_identity_comes_from_authority_not_stale_projection():
    """★★ legacy 身份必须取自**权威** owner，不是逐块从 `chunk_meta.owner_dept` 取。

    owner 变更后投影未收敛时（权威 finance / 投影仍 hr），逐块取投影 ⇒ 探针以**旧 owner**
    身份去查、找到文档、报 ok=True；而真正的 finance 用户拿到的过滤器是
    `owner_dept="finance"`，根本召不回（codex 2026-07-31 实测复现）。
    投影是被验对象，不能同时充当身份来源 —— 这是同一根因栽的第三次。
    """
    from opensearch_pipeline import ha3_verify
    stale = [{"id": 50, "chunk_id": "L_c0", "chunk_text": "owner drifted doc",
              "chunk_type": "text", "owner_dept": "hr"}]        # 投影里还是旧 owner
    seen = {}

    def rf(query, *, top_k=5, user_dept=None):
        seen["user_dept"] = user_dept
        # 只有以旧 owner 'hr' 查才"找得到"——模拟陈旧投影
        return [{"id": "50", "doc_id": "L", "chunk_id": "L_c0"}] if user_dept == ["hr"] else []

    r = ha3_verify.verify_chunks_present(
        "L", conn=_Conn(stale, _legacy_authority("L", owner="finance")), retrieve_fn=rf)
    assert seen["user_dept"] == ["finance"], "身份取自权威 owner，不是投影里的旧 owner"
    assert r["missing_ids"] == [50] and not r["ok"]              # 如实判缺，不是假绿


def test_identity_covers_production_subline_owner():
    """★★ owner 值域 ≠ 组码值域：`production_mold` 是合法 owner 但**不是**用户组码。

    直接拿它当组码查 ⇒ 组码白名单丢掉它 ⇒ 过滤器只剩 public ⇒ 探针误报整篇缺失
    （codex 2026-07-31 实测：`_build_permission_filter(["production_mold"])` → public-only）。
    能读该 owner 的是 `production` / `marketing`，反查走 retriever 的单一 taxonomy 来源。
    """
    from opensearch_pipeline import ha3_verify
    ch = [{"id": 70, "chunk_id": "M_c0", "chunk_text": "mold sop",
           "chunk_type": "text", "owner_dept": "production_mold"}]
    seen = {}

    def rf(query, *, top_k=5, user_dept=None):
        seen["user_dept"] = user_dept
        return [{"id": "70", "doc_id": "M", "chunk_id": "M_c0"}]

    r = ha3_verify.verify_chunks_present(
        "M", conn=_Conn(ch, _legacy_authority("M", owner="production_mold")), retrieve_fn=rf)
    assert seen["user_dept"] == ["marketing", "production"], "应反查出能读该 owner 的组码"
    assert "production_mold" not in seen["user_dept"]     # 它不是组码，传了等于没传
    assert r["ok"] and r["identity_authorized"]


def test_identity_covers_approved_cross_dept_grants():
    """★★ owner 侧为空、只剩 approved 跨部门授权的文档，经 `allowed_depts` 分支本可检索。

    只用 owner 构造身份 ⇒ ud=None ⇒ public-only ⇒ 误报缺失（假阴性，codex 实测）。
    """
    from opensearch_pipeline import ha3_verify
    ch = [{"id": 80, "chunk_id": "G_c0", "chunk_text": "granted doc",
           "chunk_type": "text", "owner_dept": ""}]
    auth = {"meta": [("G", "dept_internal", "", "legacy")], "grants": [("G", "hr")]}
    seen = {}

    def rf(query, *, top_k=5, user_dept=None):
        seen["user_dept"] = user_dept
        return [{"id": "80", "doc_id": "G", "chunk_id": "G_c0"}] if user_dept == ["hr"] else []

    r = ha3_verify.verify_chunks_present("G", conn=_Conn(ch, auth), retrieve_fn=rf)
    assert seen["user_dept"] == ["hr"] and r["ok"] and r["identity_authorized"]


def test_no_authority_audience_is_flagged_not_reported_as_missing_index():
    """权威上无任何可见受众 ⇒ `identity_authorized=False`；判缺是 ACL 事实，不是索引缺块。"""
    from opensearch_pipeline import ha3_verify
    ch = [{"id": 90, "chunk_id": "Z_c0", "chunk_text": "orphan doc",
           "chunk_type": "text", "owner_dept": ""}]
    auth = {"meta": [("Z", "dept_internal", "", "legacy")]}      # 无 owner、无 grants
    r = ha3_verify.verify_chunks_present("Z", conn=_Conn(ch, auth),
                                         retrieve_fn=lambda q, **k: [])
    assert r["identity_authorized"] is False and not r["ok"]


# ── 投影漂移窗口：权威说不可读，陈旧投影却仍放行 ⇒ 找到了也**不是**通过 ──────────
# 这三条的共同点：`retrieve_fn` 全部返回完整命中（missing_ids == []），但 `ok` 必须 False。
# 只测"正常投影下自然判缺"覆盖不到这个窗口（codex 2026-07-31 三条实测反例）。
def _always_found(chunk_id, doc_id, rid):
    return lambda q, **k: [{"id": str(rid), "doc_id": doc_id, "chunk_id": chunk_id}]


def test_no_audience_but_stale_projection_still_serves_is_not_ok():
    """权威 dept_internal 无 owner 无 grants + 陈旧 public 投影仍能召回 ⇒ ok 必须 False。"""
    from opensearch_pipeline import ha3_verify
    ch = [{"id": 1, "chunk_id": "X_c0", "chunk_text": "x", "chunk_type": "text", "owner_dept": ""}]
    auth = {"meta": [("X", "dept_internal", "", "legacy")]}
    r = ha3_verify.verify_chunks_present("X", conn=_Conn(ch, auth),
                                         retrieve_fn=_always_found("X_c0", "X", 1))
    assert r["missing_ids"] == [] and r["conclusive"] and r["error_count"] == 0
    assert r["identity_authorized"] is False and not r["ok"]


def test_restricted_doc_served_by_stale_projection_is_not_ok():
    """权威 restricted（永不放行）+ 陈旧 dept_internal 投影仍能召回 ⇒ ok 必须 False。"""
    from opensearch_pipeline import ha3_verify
    ch = [{"id": 2, "chunk_id": "R_c0", "chunk_text": "r", "chunk_type": "text",
           "owner_dept": "finance"}]
    auth = {"meta": [("R", "restricted", "finance", "legacy")]}
    r = ha3_verify.verify_chunks_present("R", conn=_Conn(ch, auth),
                                         retrieve_fn=_always_found("R_c0", "R", 2))
    assert r["missing_ids"] == [] and r["identity_authorized"] is False and not r["ok"]


def test_node_doc_with_zero_grants_served_by_stale_projection_is_not_ok(monkeypatch):
    """权威 node 但节点授权已撤光 + 陈旧 public 投影仍能召回 ⇒ ok 必须 False。

    此前仅因自动构造了一个空 `AclContext`（对象非空）就被当成"有身份"，与
    `_ctx_from_node_acl` 自己"零授权=无人可见"的告警自相矛盾。
    """
    from opensearch_pipeline import ha3_verify
    _enable_node_flags(monkeypatch)
    ch = _node_chunks()
    auth = {"meta": [("N", "dept_internal", "", "node")], "nodes": []}   # 授权已撤光
    r = ha3_verify.verify_chunks_present("N", conn=_Conn(ch, auth),
                                         retrieve_fn=_always_found("N_c0", "N", 20))
    assert r["acl_mode"] == "node" and r["missing_ids"] == []
    assert r["identity_authorized"] is False and not r["ok"]


# ── 检索侧与权威判定侧必须用**同一份**净化后组码 ────────────────────────────
def test_whitespace_group_normalized_on_both_sides():
    """`[" hr "]`：真实过滤器净化后放行，权威判定若用原始值就会判 False ⇒ 假阴性。"""
    from opensearch_pipeline import ha3_verify
    ch = [{"id": 3, "chunk_id": "W_c0", "chunk_text": "w", "chunk_type": "text",
           "owner_dept": "hr"}]
    seen = {}

    def rf(query, *, top_k=5, user_dept=None):
        seen["user_dept"] = user_dept
        return [{"id": "3", "doc_id": "W", "chunk_id": "W_c0"}]

    r = ha3_verify.verify_chunks_present(
        "W", conn=_Conn(ch, _legacy_authority("W", owner="hr")),
        owner_dept_override=[" hr "], retrieve_fn=rf)
    assert seen["user_dept"] == ["hr"], "送去检索的组码也应是净化后的"
    assert r["identity_authorized"] and r["ok"]


def test_non_whitelisted_group_is_denied_on_both_sides():
    """★ `["evil"]`：过滤器白名单会丢弃它（只剩 public），权威判定若用原始组码却会放行 ——
    再配上陈旧 public 投影就是假绿。两侧必须同口径。"""
    from opensearch_pipeline import ha3_verify
    ch = [{"id": 4, "chunk_id": "E_c0", "chunk_text": "e", "chunk_type": "text",
           "owner_dept": "evil"}]
    auth = {"meta": [("E", "dept_internal", "evil", "legacy")]}
    seen = {}

    def rf(query, *, top_k=5, user_dept=None):
        seen["user_dept"] = user_dept
        return [{"id": "4", "doc_id": "E", "chunk_id": "E_c0"}]   # 陈旧投影仍放行

    r = ha3_verify.verify_chunks_present("E", conn=_Conn(ch, auth),
                                         owner_dept_override=["evil"], retrieve_fn=rf)
    assert seen["user_dept"] is None, "非白名单组码不得送进检索"
    assert r["missing_ids"] == [] and r["identity_authorized"] is False and not r["ok"]


def test_explicit_ctx_reaches_retrieve_fn_normalized():
    """★ 规范化后的 ctx 必须**同时送进检索** —— 真实 retriever 的事后 `can_read_doc`
    读的就是传入 ctx 的 groups。只在判定侧规范化会让两侧再次分叉（假阴性）。"""
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import AclContext
    ch = [{"id": 5, "chunk_id": "C_c0", "chunk_text": "c", "chunk_type": "text",
           "owner_dept": "finance"}]
    auth = {"meta": [("C", "dept_internal", "finance", "legacy")], "grants": [("C", "hr")]}
    seen = {}

    def rf(query, *, top_k=5, user_dept=None, acl_ctx=None):
        seen["user_dept"], seen["groups"] = user_dept, tuple(acl_ctx.groups)
        return [{"id": "5", "doc_id": "C", "chunk_id": "C_c0"}]

    r = ha3_verify.verify_chunks_present(
        "C", conn=_Conn(ch, auth), acl_ctx=AclContext(groups=(" hr ",)), retrieve_fn=rf)
    assert seen["user_dept"] == ["hr"] and seen["groups"] == ("hr",), "检索侧拿到的仍是原始值"
    assert r["identity_authorized"] and r["ok"]


def test_authority_decision_failure_lands_in_errors(monkeypatch):
    """权威判定本身故障（flag/config 读失败）⇒ 进 `errors`，不得伪装成"身份无权"。"""
    from opensearch_pipeline import ha3_verify

    def _boom():
        raise RuntimeError("flag 姿态读取失败")

    monkeypatch.setattr("opensearch_pipeline.retriever._node_acl_flags", _boom)
    ch = _chunks()
    r = ha3_verify.verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()),
                                         retrieve_fn=_retrieve(ch, "D"))
    assert r["identity_authorized"] is False and not r["ok"]
    assert r["error_count"] == 1 and r["errors"][0]["error_type"] == "RuntimeError"


def test_explicit_acl_ctx_passes_its_groups_as_user_dept():
    """显式 `acl_ctx` 时必须同时按真实 serving 形态传 `user_dept=ctx.groups`（api.py:989）。

    此前固定传 None ⇒ 一篇健康的 legacy 文档配显式 ctx 会被误报缺失（假**阴**性）。
    """
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import AclContext
    ch = [{"id": 60, "chunk_id": "F_c0", "chunk_text": "finance doc",
           "chunk_type": "text", "owner_dept": "finance"}]
    seen = {}

    def rf(query, *, top_k=5, user_dept=None, acl_ctx=None):
        seen["user_dept"] = user_dept
        return [{"id": "60", "doc_id": "F", "chunk_id": "F_c0"}] if user_dept == ["finance"] else []

    r = ha3_verify.verify_chunks_present(
        "F", conn=_Conn(ch, _legacy_authority("F", owner="finance")),
        acl_ctx=AclContext(groups=("finance",)), retrieve_fn=rf)
    assert seen["user_dept"] == ["finance"] and r["ok"]


def test_override_and_acl_ctx_are_mutually_exclusive():
    """两者都在指定查询身份 ⇒ 同时传直接报错（此前 override 被静默忽略）。"""
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import AclContext
    with pytest.raises(ValueError, match="互斥"):
        ha3_verify.verify_chunks_present(
            "D", conn=_Conn(_chunks(), _legacy_authority()), owner_dept_override=["hr"],
            acl_ctx=AclContext(groups=("hr",)), retrieve_fn=lambda q, **k: [])


def test_capability_probe_failure_is_not_silently_legacy():
    """★★ 不 patch `resolve_doc_acl`，走**真实 helper**：capability 探测异常时探针必须
    判 `unknown` 且不报绿。

    修前实测（`_node_acl_context` 没传 strict=True）：
        information_schema 异常 + 权威实为 node + 陈旧 owner=hr
        → {'acl_mode': 'legacy', 'ok': True, 'error_count': 0}     ← 假绿，errors 还是空
    宽松路径会把 capability 异常改写成「未 apply ⇒ 全 legacy」。上一版新增的测试直接
    monkeypatch 掉 resolve_doc_acl 抛异常，正好**绕过**了 helper 内部这次吞错（codex 2026-07-31）。
    """
    from opensearch_pipeline import ha3_verify

    class _ProbeRaises(_Cur):
        def execute(self, sql, params=None):
            if "information_schema.COLUMNS" in " ".join((sql or "").split()):
                raise RuntimeError("information_schema 不可达")
            super().execute(sql, params)

    class _C2(_Conn):
        def cursor(self): return _ProbeRaises(self._rows, self._auth)

    stale = [{"id": 40, "chunk_id": "P_c0", "chunk_text": "probe doc",
              "chunk_type": "text", "owner_dept": "hr"}]
    r = ha3_verify.verify_chunks_present(
        "P", conn=_C2(stale, _node_authority("P")),
        retrieve_fn=lambda q, **k: [{"id": "40", "doc_id": "P", "chunk_id": "P_c0"}])
    assert r["acl_mode"] == "unknown" and r["error_count"] == 1
    assert not r["ok"] and r["conclusive"] is False


def test_owner_dept_override_refused_for_node_doc(monkeypatch):
    """★★ 权威为 node 时传 `owner_dept_override` ⇒ 报错并判 unknown，不得报绿。

    修前实测：`authority=node + 陈旧 owner=hr + owner_dept_override=["hr"]`
        → {'acl_mode': 'unknown', 'ok': True, 'error_count': 0}
    组码 override 表达不了 `d:`/`dx:`，用它"验证"一篇 node 文档只能验到"陈旧投影还能被旧
    owner 找到"——与"被授权的人能不能搜到它"无关。"运维显式指定即知情"不成立（codex）。
    """
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, DocAcl

    monkeypatch.setattr(
        "opensearch_pipeline.access_grants.resolve_doc_acl",
        lambda ids, cur, **kw: {"P": DocAcl(mode=ACL_MODE_NODE, permission_level="dept_internal",
                                            owner_dept="hr", node_ids=(7,))})
    stale = [{"id": 40, "chunk_id": "P_c0", "chunk_text": "probe doc",
              "chunk_type": "text", "owner_dept": "hr"}]
    with pytest.raises(ValueError, match="owner_dept_override"):
        ha3_verify.verify_chunks_present(
            "P", conn=_Conn(stale, _node_authority("P")), owner_dept_override=["hr"],
            retrieve_fn=lambda q, **k: [{"id": "40", "doc_id": "P", "chunk_id": "P_c0"}])


def test_inconclusive_mode_blocks_ok_even_with_everything_found():
    """`conclusive=False` ⇒ 总绿灯必须为 False（自动化消费者通常只读 `ok`）。"""
    from opensearch_pipeline import ha3_verify
    ch = _node_chunks()
    r = ha3_verify.verify_chunks_present(
        "N", conn=_Conn(ch, _legacy_authority("N")),
        retrieve_fn=lambda q, **k: [{"id": "20", "doc_id": "N", "chunk_id": "N_c0"}])
    assert r["missing_ids"] == [] and r["acl_mode"] == "legacy?sentinel"
    assert r["conclusive"] is False and not r["ok"]


def test_authority_read_failure_marks_mode_unknown_and_blocks_ok(monkeypatch):
    """权威读不到 ⇒ mode 判不了 ⇒ 标 `unknown`、进 errors、ok 恒 False（结论不可信）。"""
    from opensearch_pipeline import ha3_verify

    def _boom(ids, cur, **kw):
        raise RuntimeError("information_schema 不可达")

    monkeypatch.setattr("opensearch_pipeline.access_grants.resolve_doc_acl", _boom)
    ch = _chunks()
    r = ha3_verify.verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()),
                                         retrieve_fn=_retrieve(ch, "D"))
    assert r["acl_mode"] == "unknown" and not r["ok"]
    assert r["error_count"] == 1 and r["errors"][0]["chunk_id"] is None


def test_reverse_unconverged_marked_for_human_review(monkeypatch):
    """权威 legacy 但投影里还是哨兵（node→legacy 回滚未收敛）⇒ 标 `legacy?sentinel`。"""
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import DocAcl

    monkeypatch.setattr("opensearch_pipeline.access_grants.resolve_doc_acl",
                        lambda ids, cur, **kw: {"N": DocAcl(permission_level="dept_internal",
                                                            owner_dept="hr")})
    r = ha3_verify.verify_chunks_present(
        "N", conn=_Conn(_node_chunks(), _legacy_authority("N")), retrieve_fn=lambda q, **k: [])
    assert r["acl_mode"] == "legacy?sentinel"


def test_node_doc_queries_with_node_identity_not_sentinel(monkeypatch):
    """node 文档 → 用其授权节点构造 AclContext 查询；哨兵绝不作为 user_dept 传出去。"""
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, DocAcl
    _enable_node_flags(monkeypatch)

    monkeypatch.setattr(
        "opensearch_pipeline.access_grants.resolve_doc_acl",
        lambda ids, cur, **kw: {"N": DocAcl(mode=ACL_MODE_NODE, permission_level="dept_internal",
                                            owner_dept="", node_ids=(7,), exact_node_ids=(9,))})
    seen = {}

    def rf(query, *, top_k=5, user_dept=None, acl_ctx=None):
        seen["user_dept"], seen["acl_ctx"] = user_dept, acl_ctx
        return [{"id": "20", "doc_id": "N", "chunk_id": "N_c0"}]

    ch = _node_chunks()
    r = ha3_verify.verify_chunks_present("N", conn=_Conn(ch, _node_authority()), retrieve_fn=rf)
    assert r["ok"] and r["acl_mode"] == "node"
    from opensearch_pipeline.acl_policy import NODE_OWNER_SENTINEL
    assert not seen["user_dept"]                            # 无组码;哨兵绝不当组码传
    assert NODE_OWNER_SENTINEL not in (seen["user_dept"] or [])
    assert seen["acl_ctx"].ancestor_dept_ids == (7,)        # 子树授权 → d:7
    assert seen["acl_ctx"].direct_dept_ids == (9,)          # 仅本节点授权 → dx:9
    assert seen["acl_ctx"].node_channel_ok is True


# ── A3：探针自身故障 ≠ 不可检索 ─────────────────────────────────────────────
def test_retrieve_errors_are_reported_and_block_ok(monkeypatch):
    """某块查询抛异常 ⇒ 继续验证其余块、错误进 errors，且 ok 恒 False。

    修复前静默 `results = []` 会把探针故障伪装成「该 chunk 不可检索」。
    """
    from opensearch_pipeline import ha3_verify
    ch = _chunks()

    def rf(query, *, top_k=5, user_dept=None):
        if query.startswith("step one"):
            raise RuntimeError("HA3 连接超时\n第二行")
        return [{"id": str(c["id"]), "doc_id": "D", "chunk_id": c["chunk_id"]}
                for c in ch if (c["chunk_text"] or "")[:160] == query]

    r = ha3_verify.verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=rf)
    assert r["error_count"] == 1 and not r["ok"]
    assert r["errors"][0]["chunk_id"] == "D_c1"
    assert r["errors"][0]["error_type"] == "RuntimeError"
    assert "\n" not in r["errors"][0]["message"]            # 换行已替换
    assert r["present"] == 2                                # 其余块照常验证完


def test_probe_failure_is_distinguishable_from_real_miss(monkeypatch):
    """★ 探针故障与真实判缺必须可辨。

    两者都会让该块进 `missing_ids`（每块的 present 只由它自己那次查询决定，故查询一失败
    该块必然判缺）—— 正因如此，光看 missing 分不清「索引里真没有」和「探针自己坏了」。
    `errors` 就是那条区分信号：同样是 missing=[11]，一个 error_count=0、一个 =1。
    """
    from opensearch_pipeline import ha3_verify
    ch = _chunks()

    def _found_except(skip_id):
        def rf(query, *, top_k=5, user_dept=None):
            return [{"id": str(c["id"]), "doc_id": "D", "chunk_id": c["chunk_id"]}
                    for c in ch if c["id"] != skip_id and (c["chunk_text"] or "")[:160] == query]
        return rf

    def _raises_on(chunk_id):
        def rf(query, *, top_k=5, user_dept=None):
            if query.startswith("step one"):
                raise RuntimeError("HA3 连接超时")
            return [{"id": str(c["id"]), "doc_id": "D", "chunk_id": c["chunk_id"]}
                    for c in ch if (c["chunk_text"] or "")[:160] == query]
        return rf

    real_miss = ha3_verify.verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=_found_except(11))
    probe_bad = ha3_verify.verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=_raises_on("D_c1"))
    assert real_miss["missing_ids"] == probe_bad["missing_ids"] == [11]   # missing 完全一样
    assert not real_miss["ok"] and not probe_bad["ok"]
    assert real_miss["error_count"] == 0 and probe_bad["error_count"] == 1   # ← 唯一的区分信号


def test_node_doc_rejects_retrieve_fn_without_acl_ctx(monkeypatch):
    """retrieve_fn 收不了 acl_ctx ⇒ 明确抛错，绝不静默降级成「整篇缺失」的假结论。"""
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import ACL_MODE_NODE, DocAcl

    monkeypatch.setattr(
        "opensearch_pipeline.access_grants.resolve_doc_acl",
        lambda ids, cur, **kw: {"N": DocAcl(mode=ACL_MODE_NODE, permission_level="dept_internal",
                                            node_ids=(7,))})

    def rf(query, *, top_k=5, user_dept=None):     # 老签名
        return []

    with pytest.raises(TypeError, match="acl_ctx"):
        ha3_verify.verify_chunks_present("N", conn=_Conn(_node_chunks(), _node_authority()), retrieve_fn=rf)


def test_migration_absent_yields_legacy_and_sentinel_never_sent_as_group(monkeypatch):
    """schema/060 未 apply ⇒ 权威解析回 legacy。分块仍带哨兵 ⇒ 标 `legacy?sentinel`；
    哨兵**在任何模式下**都不得当组码传出去。"""
    from opensearch_pipeline import ha3_verify
    from opensearch_pipeline.acl_policy import DocAcl

    monkeypatch.setattr("opensearch_pipeline.access_grants.resolve_doc_acl",
                        lambda ids, cur, **kw: {"N": DocAcl()})    # 未 apply ⇒ legacy 形态
    seen = {}

    def rf(query, *, top_k=5, user_dept=None):
        seen["user_dept"] = user_dept
        return []

    r = ha3_verify.verify_chunks_present("N", conn=_Conn(_node_chunks(), _node_authority()),
                                         retrieve_fn=rf)
    assert r["acl_mode"] == "legacy?sentinel" and not r["ok"]
    assert seen["user_dept"] is None


def test_legacy_doc_consults_authority_once_and_keeps_owner_identity(monkeypatch):
    """legacy 文档：**照样查一次权威**（这是判定 mode 的唯一可信来源），但查询身份仍是
    各自的 owner 组码，行为与历史一致。

    ⚠️ 这条替代了早先那版「legacy 文档一次权威查询都不发」的断言 —— 那个"零开销"设计
    正是假绿 BLOCKER 的根因：不查权威就只能用投影哨兵反推 mode，而投影会滞后。
    """
    from opensearch_pipeline import ha3_verify
    calls = {"authority": 0}
    ch = _chunks()

    def _count(ids, cur, **kw):
        calls["authority"] += 1
        from opensearch_pipeline.acl_policy import DocAcl
        return {"D": DocAcl(permission_level="dept_internal", owner_dept="hr")}

    monkeypatch.setattr("opensearch_pipeline.access_grants.resolve_doc_acl", _count)
    seen = []

    def rf(query, *, top_k=5, user_dept=None):
        seen.append(user_dept)
        return [{"id": str(c["id"]), "doc_id": "D", "chunk_id": c["chunk_id"]}
                for c in ch if (c["chunk_text"] or "")[:160] == query]

    r = ha3_verify.verify_chunks_present("D", conn=_Conn(ch, _legacy_authority()), retrieve_fn=rf)
    assert r["ok"] and r["acl_mode"] == "legacy"
    assert calls["authority"] == 1, "权威只查一次（整篇一次，不是每块一次）"
    assert seen == [["hr"], ["hr"], ["hr"]]


# ── enumerator：倒排单页（2026-07-22 替换零向量 loop-until-stable）───────────────
class _Cfg:
    table_name = "t"


def _inv_body(pks, covered=1.0, error=None):
    import json as _json
    body = {"totalCount": len(pks), "coveredPercent": covered,
            "result": [{"id": p, "score": 0.0,
                        "fields": {"chunk_id": f"c{p}", "doc_id": "D", "is_active": 1}}
                       for p in pks]}
    if error is not None:
        body["errorCode"] = error
    return type("R", (), {"body": _json.dumps(body)})()


class _FakeClient:
    """倒排路假客户端：按桶起点返回 PK；记录每次 search 的请求。"""

    def __init__(self, by_bucket, covered=1.0, error=None):
        self.by_bucket, self.covered, self.error = by_bucket, covered, error
        self.reqs = []

    def search(self, req):
        self.reqs.append(req)
        lo = int(str(req.text.filter).split("id>=")[1].split(" ")[0])
        return _inv_body(self.by_bucket.get(lo, []), self.covered, self.error)

    def query(self, req):   # 绝不应被调用：零向量枚举已废除
        raise AssertionError("不得回退零向量 query 枚举")


def _parse(resp):
    return []      # 已弃用参数，保留仅为签名兼容


class _QReq:
    def __init__(self, **kw): self.kw = kw


def test_enumerate_uses_single_page_inverted_search():
    """每桶恰好一次 search、无翻页；max_rounds 传入即被忽略（倒排是确定性的）。"""
    client = _FakeClient({0: [1, 2], 500: [600]})
    out = _enumerate_ha3_pks(client, _Cfg(), _parse, ["id"], _QReq,
                             id_hi=800, bucket=500, max_rounds=5)
    assert set(out) == {1, 2, 600}
    assert len(client.reqs) == 2, "两个桶 → 恰好两次 search（max_rounds 被忽略）"
    assert client.reqs[0].text.query_string == "is_active:'1' OR is_active:'0'"
    assert client.reqs[0].text.filter == "id>=0 AND id<500"
    assert client.reqs[1].text.filter == "id>=500 AND id<800"   # 末桶严格半开


def test_enumerate_raises_on_unhealthy_bucket():
    """桶不健康 → 抛 Ha3EnumerationUnhealthy（而非静默返回缺失结果）。
    契约刻意选异常：DAG-3 用 `set(seen)` 直接消费返回值，改结构体会把键当 PK。"""
    from opensearch_pipeline.ha3_reconcile import Ha3EnumerationUnhealthy
    client = _FakeClient({0: [1]}, covered=0.5)
    with pytest.raises(Ha3EnumerationUnhealthy):
        _enumerate_ha3_pks(client, _Cfg(), _parse, ["id"], _QReq, id_hi=100, bucket=500)
