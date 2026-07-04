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
