# -*- coding: utf-8 -*-
"""test_dept_ancestry.py — 「最近祖先制」ACL 解析器：walker 语义 + 与现行名字口径的全树对照。

对照基准 = tests/fixtures/dingtalk_org_snapshot_20260703.json（2026-07-03 线上组织树快照，
131 部门，仅结构无人员）。三条铁律：
  1) 现行名字口径【命中】的每个部门，祖先制解析必须【完全相等】（不回退、不改变、不放大）；
  2) 现行未命中的 26+3 个部门里，祖先制只把「有锚祖先的叶子/挂点」修成族内组
     （行政 7 / 品质 2 / 营销 3 / 海外 4 = 16 个，显式枚举锁死，防意外放权）；
  3) 其余未命中（总经办/审计/法务/「其他」等待拍板单元）必须保持 []（fail-closed 不动）。
"""
import json
from pathlib import Path

import pytest

from opensearch_pipeline.dept_ancestry import (
    ANCHOR_GROUPS_BY_DEPT_ID,
    build_parent_index,
    parent_getter_from_index,
    resolve_dept_ids,
    resolve_descendant_ids,
)
from opensearch_pipeline.dingtalk_identity import _normalize_dept_to_codes

_FIXTURE = Path(__file__).parent / "fixtures" / "dingtalk_org_snapshot_20260703.json"


# ══ 第一部分：walker 语义（合成小树） ══════════════════════════════
#      1(根)
#      ├─ 10 生产中心锚[production]
#      │   ├─ 11 事业部 ── 12 车间叶
#      │   ├─ 20 资材部锚[supply,pmc] ── 21 仓库叶     ← 例外=更近的锚
#      │   └─ 30 保密处锚[]           ── 31 保密叶     ← 显式仅 public，截断继承
#      ├─ 40 未决定部门
#      └─ 60 坏码锚[production, typo_dept]
_TREE = {10: 1, 11: 10, 12: 11, 20: 10, 21: 20, 30: 10, 31: 30, 40: 1, 60: 1}
_ANCHORS = {10: ["production"], 20: ["supply", "pmc"], 30: [], 60: ["production", "typo_dept"]}
_GET = parent_getter_from_index(_TREE)


def _resolve(ids, get=_GET, anchors=_ANCHORS, **kw):
    return resolve_dept_ids(ids, get, anchors=anchors, **kw)


def test_inherits_from_nearest_ancestor():
    assert _resolve([12]) == (["production"], False, False)     # 叶 → 事业部 → 中心锚
    assert _resolve([11]) == (["production"], False, False)


def test_exception_anchor_nearer_wins_over_umbrella():
    # 资材部反例零特判：更近的锚天然赢过上层 production
    assert _resolve([21]) == (["supply", "pmc"], False, False)
    assert _resolve([20]) == (["supply", "pmc"], False, False)


def test_explicit_empty_anchor_blocks_inheritance():
    # 显式 [] = 「有意仅 public」：截断继承，非 partial 且 decided（这是决定，不是失败/缺口）
    assert _resolve([30]) == ([], False, False)
    assert _resolve([31]) == ([], False, False)


def test_unanchored_to_top_is_failclosed_nonpartial():
    assert _resolve([40]) == ([], False, True)           # 真·未决定：空、非 partial、undecided


def test_undecided_distinguishes_deny_from_gap():
    # 接线层靠第三元区分两种「空结果」：显式 [] 锚 = 权威 deny（不落名字口径）；
    # 到顶无锚 = 覆盖缺口（落名字口径兜底）。混合时缺口优先（保守：兜底不吃授权）。
    assert _resolve([31]) == ([], False, False)          # 权威 deny
    assert _resolve([40]) == ([], False, True)           # 缺口
    assert _resolve([31, 40]) == ([], False, True)       # 混合 → 按缺口兜底


def test_multi_dept_union_dedup_keeps_encounter_order():
    codes, partial, undecided = _resolve([12, 21, 31])
    assert codes == ["production", "supply", "pmc"] and partial is False and undecided is False


def test_whitelist_filters_bogus_anchor_codes():
    # typo_dept 被白名单丢弃；支路仍终结于锚 → decided
    assert _resolve([60]) == (["production"], False, False)


def test_all_groups_sentinel_expands_to_whitelist():
    # "*" 哨兵（总经办类全可见）→ 展开为全量白名单；哨兵本身绝不出现在结果里
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS
    codes, partial, undecided = resolve_dept_ids(
        [80], parent_getter_from_index({80: 1}), anchors={80: ["*"]})
    assert sorted(codes) == sorted(_VALID_ACL_GROUPS) and "*" not in codes
    assert partial is False and undecided is False


def test_lookup_failure_marks_partial_and_failscloses_branch():
    # 索引外 id → get 返回 None（等价钉钉 department/get 失败）
    codes, partial, _ = _resolve([999])
    assert codes == [] and partial is True
    # 混合：失败支不影响成功支的组，但整体 partial（调用方落回名字口径）
    codes, partial, _ = _resolve([12, 999])
    assert codes == ["production"] and partial is True


def test_cycle_and_depth_cap_mark_partial():
    get_cyc = parent_getter_from_index({50: 51, 51: 50})
    assert _resolve([50], get=get_cyc) == ([], True, False)     # 环 = 数据异常 → partial
    deep = {i: i + 1 for i in range(100, 200)}                  # 100 跳长链，无锚
    assert _resolve([100], get=parent_getter_from_index(deep), max_hops=15) == ([], True, False)


def test_root_level_anchor_reachable():
    # 锚在顶层节点（parent=1）也要能命中，不被"到顶即停"提前吃掉
    get = parent_getter_from_index({70: 1, 71: 70})
    assert resolve_dept_ids([71], get, anchors={70: ["hr"]}) == (["hr"], False, False)


def test_invalid_dept_id_marks_partial():
    assert _resolve(["not-an-id"]) == ([], True, False)


def test_empty_input_ok():
    assert _resolve([]) == ([], False, True)             # 无部门信息 → 按未决定兜底名字口径


# ══ 第二部分：全树对照（现行名字口径 vs 祖先制） ══════════════════
# 2026-07-03 拍板批（海外/总经办/审计/法务/工程/玉米环保/资材部）已同步写进名字表 →
# 那些部门进入"逐一全等"桶（铁律 1 自动覆盖）。此处仅剩【祖先制独有】的继承修复
# ——名字表刻意不再为这些叶子扩名（那正是要退役的枚举模式）。
_EXPECTED_FIX = {
    # 行政部叶子 → admin（继承 综合管理中心/行政部 锚）
    "综合管理中心/行政部/行政—食堂": {"admin"},
    "综合管理中心/行政部/行政—后勤": {"admin"},
    "综合管理中心/行政部/行政—保安": {"admin"},
    "综合管理中心/行政部/行政—司机": {"admin"},
    "综合管理中心/行政部/行政—松门保安": {"admin"},
    "综合管理中心/行政部/行政—松门厨房": {"admin"},
    "综合管理中心/行政部/行政—松门后勤": {"admin"},   # 今日空部门：进人即得，不再漏
    # 品技中心族 → quality（中心锚）
    "品技中心": {"quality"},
    "品技中心/品质部/质量管理": {"quality"},
    # 营销叶子 → 继承各自部级锚
    "营销中心/电子商务部/杭州分公司": {"marketing", "production"},
    "营销中心/国际贸易部/外贸监装": {"marketing", "production"},
    "营销中心/国内营销部/内销监装": {"marketing", "production"},
    # 双职能挂点（2026-07-04 拍板；「办公室」仅锚表，名字表刻意不收通用名）
    "综合管理中心/办公室": {"admin", "hr"},
    "综合管理中心": {"admin", "hr"},          # 直挂 3 人：中心双职能（子树 行政部/人力资源部 有更近锚）
}


@pytest.fixture(scope="module")
def snapshot():
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    rows = data["depts"]
    return rows, parent_getter_from_index(build_parent_index(rows))


def test_parity_mapped_depts_resolve_identically(snapshot):
    """铁律 1：现行命中的每个部门（explicit/umbrella/passthru）祖先制完全相等。"""
    rows, get = snapshot
    checked = 0
    for r in rows:
        cur = set(_normalize_dept_to_codes(r["name"]))
        if not cur:
            continue
        anc, partial, _ = resolve_dept_ids([r["dept_id"]], get)
        assert partial is False, r["path"]
        assert set(anc) == cur, f"{r['path']}: 现行 {sorted(cur)} vs 祖先制 {sorted(anc)}"
        checked += 1
    assert checked >= 100   # 快照里 102 个命中部门（防 fixture 变形导致对照空转）


def test_parity_gap_depts_fixed_exactly_as_designed(snapshot):
    """铁律 2+3：现行未命中的部门——16 个族内叶子/挂点修成预期组，其余保持 [] 不动。"""
    rows, get = snapshot
    fixed, still_empty = {}, []
    for r in rows:
        if _normalize_dept_to_codes(r["name"]):
            continue
        anc, partial, _ = resolve_dept_ids([r["dept_id"]], get)
        assert partial is False, r["path"]
        if anc:
            fixed[r["path"]] = set(anc)
        else:
            still_empty.append(r["path"])
    assert fixed == _EXPECTED_FIX          # 修谁、修成什么，逐 path 锁死（防意外放权）
    # 拍板三轮后仅剩「其他」（显式 [] 锚=有意仅 public）+ 两个空节点仍 fail-closed
    assert "其他" in still_empty
    assert len(still_empty) == 3           # 其他(显式) + lzdqr + 实习生


def test_parity_dead_key_not_resurrected(snapshot):
    """死键 PMC部 只在名字表里（树上无此部门）；锚表按 id 键控，不存在被"复活"的路径——
    但名字口径对孤名 'PMC部' 仍返回 pmc（兜底语义保留，两口径互不干扰）。"""
    assert _normalize_dept_to_codes("PMC部") == ["pmc"]
    rows, _ = snapshot
    assert all(r["name"] != "PMC部" for r in rows)


# ══ 第三部分：RAG_ACL_ANCESTRY 接线（_resolve_user_dept_live） ══════
from unittest.mock import MagicMock  # noqa: E402

import opensearch_pipeline.dingtalk_identity as di  # noqa: E402


class _FakeCur:
    def __init__(self, conn):
        self.conn = conn
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        self._last = sql

    def fetchone(self):
        if "SELECT dept_code" in self._last:
            return self.conn.cache_row
        return None


class _FakeConn:
    def __init__(self, cache_row=None):
        self.cache_row = cache_row
        self.calls = []

    def cursor(self):
        return _FakeCur(self)

    def commit(self):
        pass

    def close(self):
        pass


def _insert_params(conn):
    for sql, params in conn.calls:
        if "INSERT INTO" in sql and "user_role" in sql:
            return params
    return None


def _wire(monkeypatch, conn, user_info, parents):
    monkeypatch.setattr("opensearch_pipeline.db._get_db_conn", lambda: conn)
    monkeypatch.setattr(di, "_fetch_dingtalk_user_info", lambda uid: user_info)
    monkeypatch.setattr("opensearch_pipeline.dingtalk_card._get_access_token", lambda: "tok")
    monkeypatch.setattr(di, "_fetch_dept_parent",
                        lambda tok, did: parents.get(did))   # 缺键 = None = 该支失败


def test_flag_off_keeps_name_behavior(monkeypatch):
    monkeypatch.delenv("RAG_ACL_ANCESTRY", raising=False)
    spy = MagicMock(side_effect=AssertionError("flag 关时不得调用祖先制"))
    monkeypatch.setattr(di, "_resolve_groups_via_ancestry", spy)
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn, {"user_name": "张三", "dept_name": "行政—食堂",
                              "is_partial": False, "dept_ids": [555]}, {})
    codes, cacheable = di._resolve_user_dept_live("u1")
    assert codes == [] and cacheable is True               # 现行：名字未命中 → fail-closed
    assert _insert_params(conn)[2] == "行政—食堂"           # 缓存原名（现行为）
    spy.assert_not_called()


def test_flag_on_caches_group_codes_via_nearest_anchor(monkeypatch):
    """开 flag：行政—食堂(555) 沿父链 → 行政部锚(34274162) → admin；缓存组码 CSV。"""
    monkeypatch.setenv("RAG_ACL_ANCESTRY", "1")
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn,
          {"user_name": "张三", "dept_name": "行政—食堂", "is_partial": False, "dept_ids": [555]},
          {555: 34274162})                                  # 34274162 自身是锚，走到即命中
    codes, cacheable = di._resolve_user_dept_live("u1")
    assert codes == ["admin"] and cacheable is True
    assert _insert_params(conn)[2] == "admin"               # dept_code 存组码 CSV
    assert ANCHOR_GROUPS_BY_DEPT_ID[34274162] == ["admin"]  # 锚表口径自证


def test_flag_on_partial_falls_back_to_name_path(monkeypatch):
    """父链某跳失败（partial）→ 整体落回名字口径：缓存原名、返回名字归一化结果。"""
    monkeypatch.setenv("RAG_ACL_ANCESTRY", "1")
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn,
          {"user_name": "李四", "dept_name": "财务部", "is_partial": False, "dept_ids": [777]},
          {})                                               # 777 缺键 → 该支 None → partial
    codes, cacheable = di._resolve_user_dept_live("u2")
    assert codes == ["finance"] and cacheable is True       # 名字口径兜住
    assert _insert_params(conn)[2] == "财务部"


def test_flag_on_ancestry_overrides_partial_names(monkeypatch):
    """名字口径 partial（某支名字拉取失败）但 id 父链完整 → 祖先制权威、照常缓存组码。"""
    monkeypatch.setenv("RAG_ACL_ANCESTRY", "1")
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn,
          {"user_name": "王五", "dept_name": "", "is_partial": True, "dept_ids": [12345]},
          {12345: 599318766})                               # → 生产中心锚
    codes, cacheable = di._resolve_user_dept_live("u3")
    assert codes == ["production"] and cacheable is True
    assert _insert_params(conn)[2] == "production"


def test_flag_on_explicit_deny_beats_name_table_collision(monkeypatch):
    """显式 [] 锚必须压得过名字表撞名：部门名叫「品质部」但挂在「其他」(68112184) 显式仅-public
    子树下 → 权威 deny（此前 `if _anc:` 把权威空当 falsy 落回名字口径 → 错误授 quality）。
    缓存存 deny 哨兵（非空可缓存、读回白名单丢弃=[]），不存原名防读回再撞表。"""
    monkeypatch.setenv("RAG_ACL_ANCESTRY", "1")
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn,
          {"user_name": "钱七", "dept_name": "品质部", "is_partial": False, "dept_ids": [888]},
          {888: 68112184})                                  # 父=「其他」显式 [] 锚
    codes, cacheable = di._resolve_user_dept_live("u4")
    assert codes == [] and cacheable is True
    cached = _insert_params(conn)[2]
    assert cached == di._ACL_PUBLIC_ONLY_SENTINEL
    assert di._normalize_dept_to_codes(cached) == []        # 读回 round-trip 仍 deny


def test_flag_on_undecided_gap_falls_back_to_names(monkeypatch):
    """真·未决定（父链到顶无锚 = 锚表覆盖缺口）保持名字口径兜底——缺口绝不吃掉名字表授权。"""
    monkeypatch.setenv("RAG_ACL_ANCESTRY", "1")
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn,
          {"user_name": "孙八", "dept_name": "财务部", "is_partial": False, "dept_ids": [999]},
          {999: 1})                                         # 到顶（根）无锚 → undecided
    codes, cacheable = di._resolve_user_dept_live("u5")
    assert codes == ["finance"] and cacheable is True
    assert _insert_params(conn)[2] == "财务部"               # 现行为保留：缓存原名


def test_flag_on_sentinel_compresses_cache_to_star(monkeypatch):
    """总经办（锚=["*"]）：返回全量白名单，但缓存压缩回 "*"——15 组码 CSV=104 字符会溢出
    user_role.dept_code VARCHAR(64)（strict 写失败→永不缓存；非 strict 截断→读回丢组）。
    读侧 _normalize_dept_to_codes("*") 展开为全量，round-trip 无损。"""
    monkeypatch.setenv("RAG_ACL_ANCESTRY", "1")
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn,
          {"user_name": "赵总", "dept_name": "总经办", "is_partial": False, "dept_ids": [14930012]},
          {})                                               # 14930012 自身是锚，不需父链
    codes, cacheable = di._resolve_user_dept_live("u9")
    assert set(codes) == set(_VALID_ACL_GROUPS) and cacheable is True
    cached = _insert_params(conn)[2]
    assert cached == "*"                                    # 不是 104 字符 CSV
    assert len(cached) <= 64                                # VARCHAR(64) 契约
    assert di._normalize_dept_to_codes(cached) == sorted(_VALID_ACL_GROUPS)   # 读回展开无损


# ── 阶段 B WP0：resolve_descendant_ids（管辖根 → 后代集，含根自身）──────────────
_IDX = {1: [2, 3], 2: [4, 5], 3: [6], 6: [7]}


def test_descendants_include_root_itself():
    got, ok = resolve_descendant_ids(_IDX, [2])
    assert ok and got == {2, 4, 5}


def test_descendants_union_multiple_roots():
    got, ok = resolve_descendant_ids(_IDX, [2, 3])
    assert ok and got == {2, 3, 4, 5, 6, 7}


def test_descendants_empty_roots_is_legal_no_grant():
    """空 roots = 该 dept_admin 尚未获授节点轴，合法：空集匹配空 = 天然无权，非失败。"""
    got, ok = resolve_descendant_ids(_IDX, [])
    assert ok and got == set()


def test_descendants_virtual_root_fails_closed():
    """管辖根 = 钉钉根 1（全库语义）非法——那是 kb_admin 的语义，不该由节点表达。"""
    assert resolve_descendant_ids(_IDX, [1]) == (set(), False)


def test_descendants_bad_id_fails_closed():
    assert resolve_descendant_ids(_IDX, ["x"]) == (set(), False)
    assert resolve_descendant_ids(_IDX, [0]) == (set(), False)
    assert resolve_descendant_ids(_IDX, [-3]) == (set(), False)


def test_descendants_leaf_not_in_index_returns_self():
    """根无子女（叶子）≠ 失败：返回 {root}。失活节点检出归 org_sync 孤儿报告。"""
    got, ok = resolve_descendant_ids(_IDX, [9])
    assert ok and got == {9}


def test_descendants_cycle_and_diamond_defused_by_dedup():
    """脏数据环/菱形被去重集消化：下行 BFS 终止且展开完整，不失效整条通道。"""
    got, ok = resolve_descendant_ids({10: [11], 11: [10]}, [10])
    assert ok and got == {10, 11}


def test_descendants_oversize_fails_closed_not_truncated():
    """超上限 fail-closed 不截断——截断 = dept_admin 静默少管一片，比失效更难发现。"""
    assert resolve_descendant_ids(_IDX, [2], max_nodes=2) == (set(), False)


# ══ 第四部分：2026-08-04 部门名漂移批（08-04 树精确值对照） ══════════
# 背景：钉钉侧改名/重组把名字表打出 9 个死键（资材部→采购部 等），并把海外系三个单位
# 升为顶层树 —— 07-03 锚表够不到。本节以 08-04 真实树锁死「补锚后每个部门解析成什么」。
#
# ⚠️ 为什么必须逐 path 锁【值】而不是断言「非空」：非空断言对越权变异 0 检出 ——
#    把 92 人子树误配 ["*"] 全组哨兵、漏掉 overseas、错配 finance、删掉「其他」的 deny 锚，
#    四种变异都能通过非空断言（2026-08-04 变异实测）。ACL 场景下那是不可接受的降级。
# ⚠️ 本 fixture 锁的是【该快照树形态下锚表的正确性】，不等于运行时覆盖率：运行时 dept_ids
#    来自钉钉 user/get、父链来自 department/get，fixture 只是 dept_dim 的日快照。
#    它也【不能】发现"组织又漂移了"（静态快照永远绿）——那需要定时对 dept_dim 跑覆盖扫描。
_FIXTURE_0804 = Path(__file__).parent / "fixtures" / "dingtalk_org_snapshot_20260804.json"

# 名字口径 != 祖先制 的全部 path → 祖先制值。28 条 = 27 条「名字口径为空的缺口修复」
# + 1 条「纸浆模塑事业部 production → overseas+production」（该部门 2026-08 随整建制迁移
# 挂进了获胜子公司树，同 dept_id 921614009，非同名误授）。
_DIVERGE_0804 = {
    "印尼富岭": {"overseas", "production"},
    "墨西哥富岭": {"overseas", "production"},
    "获胜包装/获胜生产中心": {"overseas", "production"},
    "获胜包装/获胜生产中心/纸浆模塑事业部": {"overseas", "production"},
    "获胜包装/获胜行政中心": {"admin"},        # 2026-08-04 拍板：行政线给 admin，不叠生产伞组
    "海外中心/海外服务部": {"overseas", "production"},
    "法务部": {"legal"},
    "财务中心/信息部": {"it"},
    "生产中心/采购部": {"supply", "pmc", "production"},
    "品技中心": {"quality"},
    "品技中心/品质部/质量管理": {"quality"},
    "生产中心/薄膜车间": {"production"},
    "生产中心/薄膜车间/薄膜—仓管": {"production"},
    "生产中心/薄膜车间/薄膜—其他": {"production"},
    "生产中心/薄膜车间/薄膜—切袋": {"production"},
    "生产中心/薄膜车间/薄膜—吹膜机修": {"production"},
    "生产中心/薄膜车间/薄膜—机修": {"production"},
    "综合管理中心": {"admin", "hr"},
    "综合管理中心/办公室": {"admin", "hr"},
    "综合管理中心/行政部/行政—保安": {"admin"},
    "综合管理中心/行政部/行政—司机": {"admin"},
    "综合管理中心/行政部/行政—后勤": {"admin"},
    "综合管理中心/行政部/行政—松门保安": {"admin"},
    "综合管理中心/行政部/行政—松门厨房": {"admin"},
    "综合管理中心/行政部/行政—松门后勤": {"admin"},
    "综合管理中心/行政部/行政—食堂": {"admin"},
    "营销中心/国内营销部/内销监装": {"marketing", "production"},
    "营销中心/国际贸易部/外贸监装": {"marketing", "production"},
    "营销中心/电子商务部/杭州分公司": {"marketing", "production"},
}

# 解析为空的部门 —— 按 dept_id 键控而非名字（本次修复的立论就是「名字会漂移」，
# 用名字做豁免名单等于把同一个坑再挖一遍）。
_EMPTY_DENY_0804 = {68112184}                       # 「其他」：显式 [] 锚 = 权威仅 public
_EMPTY_UNDECIDED_0804 = {                           # 真·未决定（锚表覆盖缺口，fail-closed）
    417762615,          # lzdqr（0 人）
    920067054,          # 实习生（0 人）
    1068136163,         # 获胜包装（树根，直挂 11 人）——刻意不设锚：树根是「公司」语义、
    #                     职能不明，两条职能线各自有锚（生产/行政），树根宁缺勿错
}
# 锚 id 在 08-04 活跃树中已不存在者（组织撤销）。保留锚 = dept_id 若被钉钉回收则误授（理论
# 风险）；删除 = 该部门恢复时静默失权。当前选择保留并在此显式登记，防"悄悄多出一个"。
_KNOWN_DEAD_ANCHORS = {842763367}                   # 工程 → engineering


@pytest.fixture(scope="module")
def snapshot_0804():
    data = json.loads(_FIXTURE_0804.read_text(encoding="utf-8"))
    rows = data["depts"]
    return rows, parent_getter_from_index(build_parent_index(rows))


def test_0804_divergences_are_exactly_as_designed(snapshot_0804):
    """08-04 树上「名字口径 vs 祖先制」的每一处差异，逐 path 锁值（防意外放权/漏授）。"""
    rows, get = snapshot_0804
    diverge = {}
    for r in rows:
        cur = set(_normalize_dept_to_codes(r["name"]))
        anc, partial, _ = resolve_dept_ids([r["dept_id"]], get)
        assert partial is False, r["path"]
        if set(anc) != cur:
            diverge[r["path"]] = set(anc)
    assert diverge == _DIVERGE_0804


def test_0804_name_hits_otherwise_resolve_identically(snapshot_0804):
    """铁律 1 的 08-04 版：名字口径命中的部门，除上表登记的差异外必须逐一全等。"""
    rows, get = snapshot_0804
    checked = 0
    for r in rows:
        cur = set(_normalize_dept_to_codes(r["name"]))
        if not cur or r["path"] in _DIVERGE_0804:
            continue
        anc, partial, _ = resolve_dept_ids([r["dept_id"]], get)
        assert partial is False and set(anc) == cur, r["path"]
        checked += 1
    assert checked >= 80        # 08-04 树 87 个命中部门、1 个登记差异；防 fixture 变形空转


def test_0804_empty_results_split_deny_from_gap(snapshot_0804):
    """空结果必须分成两类：显式 deny（decided）与覆盖缺口（undecided）——
    两者都表现为空集但语义相反：前者是权威拒绝、压过名字撞名，后者会落回名字口径兜底。"""
    rows, get = snapshot_0804
    deny, gap = set(), set()
    for r in rows:
        anc, partial, undecided = resolve_dept_ids([r["dept_id"]], get)
        assert partial is False, r["path"]
        if anc:
            continue
        (gap if undecided else deny).add(r["dept_id"])
    assert deny == _EMPTY_DENY_0804
    assert gap == _EMPTY_UNDECIDED_0804


def test_0804_rename_immunity_supply_anchor(snapshot_0804):
    """改名免疫：资材部 2026-08 改名「采购部」，dept_id 728779788 不变 ⇒ 组不变。
    这正是 dept_id 键控相对名字表的核心优势（名字表 key「资材部」已成死键）。"""
    rows, get = snapshot_0804
    row = next(r for r in rows if r["dept_id"] == 728779788)
    assert row["name"] == "采购部"                       # fixture 自证现名已漂
    assert _normalize_dept_to_codes("采购部") == []       # 名字口径确已失效
    anc, partial, _ = resolve_dept_ids([728779788], get)
    assert partial is False and set(anc) == {"supply", "pmc", "production"}


def test_0804_child_inherits_new_overseas_anchor(snapshot_0804):
    """新锚的价值在【子节点一跳继承】而非自锚（自锚在 walker 循环顶即命中、父链一次都不查）。
    纸浆模塑事业部 → 获胜生产中心锚，跨的正是 2026-08 重组后的新父链。"""
    rows, get = snapshot_0804
    anc, partial, undecided = resolve_dept_ids([921614009], get)
    assert partial is False and undecided is False
    assert set(anc) == {"overseas", "production"}


def test_0804_huosheng_root_stays_failclosed_while_branches_anchored(snapshot_0804):
    """获胜树：两条职能线各自设锚，【树根不设锚】。
    根设锚会让行政线拿到大陆生产伞组（07-03 拍板对象只是个无子女叶子）；反过来，
    行政线的 admin 也绝不能漏到生产线上去。树根语义是「公司」而非职能 ⇒ fail-closed。"""
    rows, get = snapshot_0804
    root, partial, undecided = resolve_dept_ids([1068136163], get)
    assert partial is False and root == [] and undecided is True

    prod, _, _ = resolve_dept_ids([1091525269], get)
    adm, _, _ = resolve_dept_ids([1091358296], get)
    assert set(prod) == {"overseas", "production"}
    assert set(adm) == {"admin"}                 # 行政线不叠生产伞组
    assert "production" not in adm and "overseas" not in adm


def test_0804_every_anchor_exists_in_tree(snapshot_0804):
    """锚 id 打错/组织撤销都表现为「该子树整片掉空」。登记已知死锚，其余必须在树上存在。"""
    rows, _ = snapshot_0804
    tree_ids = {r["dept_id"] for r in rows}
    missing = set(ANCHOR_GROUPS_BY_DEPT_ID) - tree_ids
    assert missing == _KNOWN_DEAD_ANCHORS

# ══ 第五部分：外部组码防投毒（2026-08-04 扩面：从只挡 "*" 到挡全部组码） ══
def test_guard_drops_external_dept_named_like_group_code(monkeypatch):
    """一个字面命名为 'production' 的钉钉部门不得让其成员拿到生产伞组。
    dept_name 列是名字域与组码域同居的：名字表未命中即原样透传给组码白名单，
    因此外部可控的部门名能直接冒充组码——必须在入口丢弃（fail-closed）。"""
    monkeypatch.delenv("RAG_ACL_ANCESTRY", raising=False)
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn, {"user_name": "李四", "dept_name": "production,财务部",
                              "is_partial": False, "dept_ids": [777]}, {})
    codes, cacheable = di._resolve_user_dept_live("u_forge")
    assert codes == ["finance"]                     # 冒充项被丢，正常部门不受影响
    assert "production" not in _insert_params(conn)[2]


def test_guard_still_drops_star_sentinel(monkeypatch):
    """原「星号防投毒」（2026-07-17 ultra P2）语义不得回退。"""
    monkeypatch.delenv("RAG_ACL_ANCESTRY", raising=False)
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn, {"user_name": "王五", "dept_name": "*,行政部",
                              "is_partial": False, "dept_ids": [888]}, {})
    codes, _ = di._resolve_user_dept_live("u_star")
    assert codes == ["admin"]                       # 不是全量白名单


def test_guard_does_not_touch_normal_chinese_dept_names(monkeypatch):
    """反证：正常中文部门名一个都不能被误伤（线上 119 个活跃部门无一撞组码）。"""
    monkeypatch.delenv("RAG_ACL_ANCESTRY", raising=False)
    conn = _FakeConn(cache_row=None)
    _wire(monkeypatch, conn, {"user_name": "赵六", "dept_name": "财务部,人力资源部",
                              "is_partial": False, "dept_ids": [999]}, {})
    codes, _ = di._resolve_user_dept_live("u_ok")
    assert codes == ["finance", "hr"]


def test_guard_never_hardcodes_group_list():
    """守卫必须以 retriever._VALID_ACL_GROUPS 为单一真值来源——加新组时自动跟上。"""
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS
    assert di._VALID_ACL_GROUPS_FOR_GUARD() is _VALID_ACL_GROUPS
