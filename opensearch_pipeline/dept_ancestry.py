# -*- coding: utf-8 -*-
"""dept_ancestry.py — 部门 ACL 归属的「最近祖先制」解析（dept_id 锚定，纯函数）。

替代"按部门名称枚举"的两张表（_DEPT_NAME_TO_GROUPS 19 条 + _PRODUCTION_WORKSHOP_DEPTS
85 名快照）的结构性方案：**锚挂在 dept_id 上、设在语义无歧义的最高层级**，解析用户部门时
沿父链向上找【最近】的锚。由此：

  - 新叶子自动继承（行政部新建"行政—松门后勤"进人即得 admin，不再漏）；
  - 改名免疫（锚定 id，不定名；PMC部 这类死键不再产生）；
  - 例外零特判（资材部挂生产中心下但属 [supply,pmc]——它自己就是更近的锚，天然赢）；
  - **三态语义**：锚值为非空列表=授组；锚值为空列表 []=「有意仅 public」（显式决定，
    并【截断】继承——不再上溯）；无锚祖先=真·未决定（fail-closed 仅 public，可被
    scan_dept_mapping_gaps.py 检出为"漏"）。后两态由返回值第三元 undecided 区分
    （decided-空=权威 deny，接线层不得再落回名字口径；undecided-空=名字口径兜底）。

锚点表来源：2026-07-03 全树扫描（scratch/dept_mapping_scan_20260703）+ 权限单口径。
14 个锚覆盖现行 104 个名字条目的全部语义（对照测试见 tests/test_dept_ancestry.py）。

本模块保持纯函数（无 HTTP / 无 DB / 无 config）：父链查询由调用方以 callable 注入
（生产 = dingtalk_identity._fetch_dept_parent；测试 = 组织树快照 dict）。接线开关
RAG_ACL_ANCESTRY（默认关）在 dingtalk_identity，本模块不读环境。
"""

from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

# 钉钉根部门恒为 1；department/get 对根返回 parent_id=0。
ROOT_DEPT_ID = 1

# ═══════════════════════════════════════════════════════════════
# 锚点表：dept_id → ACL 组列表（[] = 显式「有意仅 public」，截断继承）
# ───────────────────────────────────────────────────────────────
# 设锚原则：单一职能的子树在【根】设锚（生产/品技/研发/海外中心）；多职能中心
# （综合管理/财务/营销的多组部门）不在中心设锚，锚下沉到二级部门——叶子找"最近"，
# 综合管理中心挂点用户不会误得 admin。dept_id 取自 2026-07-03 线上组织树。
# 锚值里的 "*" = 「全部组」哨兵：解析时展开为整个 _VALID_ACL_GROUPS（读全库 dept_internal，
# 但**不是** kb_admin——三分授权：读组 ≠ 可管理 ≠ 可授权，管理台入口/写权分毫不给）。
# 用哨兵而非枚举：将来加新组，全可见单位自动跟上，不留"加组忘更新总经办"的坑。
ALL_GROUPS_SENTINEL = "*"

ANCHOR_GROUPS_BY_DEPT_ID: Dict[int, List[str]] = {
    # —— 单一职能中心（整棵子树一锚） ——
    599318766: ["production"],              # 生产中心（替代 85 名车间快照）
    598440841: ["quality"],                 # 品技中心（品质部/技术部/质量管理皆 quality）
    598477962: ["rd"],                      # 研发中心（研发部/实验室皆 rd）
    599944033: ["overseas", "production"],  # 海外中心：自有组 overseas + 维持 production 可读（2026-07-03 拍板）
    # —— 子树内例外（比中心更近 → 天然覆盖，无需特判） ——
    728779788: ["supply", "pmc", "production"],  # 生产中心/资材部：权限单 [supply,pmc] + 归生产中心下叠 production（2026-07-03 拍板）
    # —— 多职能中心：锚下沉到二级部门 ——
    34274162: ["admin"],                    # 综合管理中心/行政部（食堂/保安/司机等叶子继承）
    34265162: ["hr"],                       # 综合管理中心/人力资源部
    33952854: ["finance"],                  # 财务中心/财务部
    14858885: ["it"],                       # 财务中心/自动化信息部
    # —— 营销中心：中心级兜底 + 部级细化（部级更近 → 叶子拿部级值） ——
    599986031: ["marketing"],               # 营销中心（兜底，直挂人员）
    34301155: ["marketing", "production"],  # 营销中心/国际贸易部
    149975081: ["marketing", "production"], # 营销中心/国内营销部
    599754116: ["marketing", "production"], # 营销中心/电子商务部
    1084116184: ["marketing", "pmc"],       # 营销中心/计划部
    # —— 独立单元（2026-07-03 拍板：各设自有组 / 全可见 / 显式仅 public） ——
    14930012: [ALL_GROUPS_SENTINEL],        # 总经办：全库可读（非 kb_admin，无管理/授权权）
    997711587: ["audit"],                   # 审计部（审计一部/二部 按最近祖先自动继承）
    44083880: ["legal"],                    # 法务
    842763367: ["engineering"],             # 工程
    474053554: ["corn_eco"],                # 玉米环保
    68112184: [],                           # 「其他」：显式仅 public（有意决定，非漏；截断继承）
    # —— 双职能挂点（2026-07-04 拍板）：中心两职能并授。锚只进本表（dept_id 键控）——
    #    「办公室」是通用名，进全局名字表会误伤其他子树的同名部门。 ——
    100648646: ["admin", "hr"],             # 综合管理中心/办公室（中心双职能）
    598500891: ["finance", "it"],           # 财务中心（挂点人员；财务部/自动化信息部有更近锚不受影响）
    599502818: ["admin", "hr"],             # 综合管理中心（直挂 3 人；行政部/人力资源部子树有更近锚不受影响）
    # ⚠️ 个人级 kb_admin（如 赖俊成）不在本表——那是 person-level 角色，走 seeded user_role 行
    #    （resolve_kb_identity 现查、seeded 优先于自动映射），与本 dept-level 读组解析正交。
}

# 父链查询 callable 契约：dept_id → 父 dept_id；返回 None = 查询失败（partial，
# 该支 fail-closed 且调用方应落回名字口径）；返回 0 / ROOT_DEPT_ID = 已到顶。
ParentGetter = Callable[[int], Optional[int]]

_MAX_HOPS_DEFAULT = 15   # 现网树深 ≤5；防异常数据无界上溯


def resolve_dept_ids(
    dept_ids: Iterable[int],
    get_parent_id: ParentGetter,
    *,
    anchors: Optional[Dict[int, List[str]]] = None,
    max_hops: int = _MAX_HOPS_DEFAULT,
) -> Tuple[List[str], bool, bool]:
    """把用户的 dept_id 列表解析为 ACL 组列表（最近祖先制）。返回 (组列表, partial, undecided)。

    每个 dept_id：自身即锚 → 取锚值（[] = 有意仅 public，该支到此为止）；否则沿
    get_parent_id 上溯，命中最近锚为止；到顶未命中 → 该支空（fail-closed）。
    partial=True 的情形（调用方据此【落回现行名字口径】，绝不缓存半截结果）：
      - 任一跳 get_parent_id 返回 None（查询失败）；
      - 上溯出现环（数据异常）或超 max_hops。
    undecided=True：≥1 支上溯到顶（parent∈{0,根}）都没碰到任何锚 = 存在真·未决定支。
    调用方据此区分两种「空结果」：空 + undecided=False = 全部支路终结于锚（显式 [] 或
    锚值被白名单滤空）= 权威「有意仅 public」，不得落回名字口径（显式 deny 要能压过
    名字表撞名）；空 + undecided=True = 锚表覆盖缺口 → 名字口径兜底。partial 支不计入
    undecided（partial 本身已强制整体回退）。
    输出与 _normalize_dept_to_codes 同口径：过 retriever._VALID_ACL_GROUPS 白名单、
    按首次出现顺序去重。空输入 → ([], False, True)（无部门信息=按未决定兜底）。
    """
    table = ANCHOR_GROUPS_BY_DEPT_ID if anchors is None else anchors
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS  # 惰性：白名单单一来源

    out: List[str] = []
    seen = set()
    partial = False
    undecided = False
    branches = 0
    for raw in dept_ids or []:
        branches += 1
        try:
            dept_id = int(raw)
        except (TypeError, ValueError):
            partial = True                        # 异常 id：该支 fail-closed + 标记
            continue
        visited = set()
        cur: Optional[int] = dept_id
        hops = 0
        while True:
            if cur in table:
                vals = table[cur]
                if ALL_GROUPS_SENTINEL in vals:   # "*" → 展开为全量白名单（总经办类全可见）
                    vals = sorted(_VALID_ACL_GROUPS)
                for code in vals:                 # [] 锚：循环体不执行 = 该支贡献空
                    code = (code or "").strip()
                    if code and code in _VALID_ACL_GROUPS and code not in seen:
                        seen.add(code)
                        out.append(code)
                break
            visited.add(cur)
            hops += 1
            if hops > max_hops:
                partial = True                    # 深度异常：按查询失败处理
                break
            parent = get_parent_id(cur)
            if parent is None:
                partial = True                    # 父链查询失败：该支 fail-closed
                break
            if parent == cur:
                partial = True                    # 自指（数据异常）：按失败处理
                break
            if parent in visited:
                partial = True                    # 环（数据异常）：按失败处理
                break
            if parent in table:
                cur = parent                      # 顶层/根锚也要进 loop 顶命中，不提前 break
                continue
            if parent in (0, ROOT_DEPT_ID):
                undecided = True                  # 到顶未命中锚：真·未决定，该支空
                break
            cur = parent
    if branches == 0:
        undecided = True                          # 空输入：无部门信息，按未决定兜底
    return out, partial, undecided


# ── 测试/离线辅助：用组织树快照构造 ParentGetter ─────────────────────────────
def build_parent_index(rows: Iterable[dict]) -> Dict[int, int]:
    """[{dept_id, parent_id, ...}] → {dept_id: parent_id}（快照 fixture / 扫描产物通用）。"""
    return {int(r["dept_id"]): int(r["parent_id"]) for r in rows}


def parent_getter_from_index(index: Dict[int, int]) -> ParentGetter:
    """快照索引 → ParentGetter。索引外的 dept_id 返回 None（等价"查询失败"，fail-closed）。"""
    def _get(dept_id: int) -> Optional[int]:
        return index.get(dept_id)
    return _get


# ── node-ACL：物理祖先链（与上面的"组码解析"是两条独立通道）────────────────────
def resolve_ancestor_chains(
    dept_ids: Iterable[int],
    get_parent_id: ParentGetter,
    *,
    max_hops: int = _MAX_HOPS_DEFAULT,
    max_direct_depts: int = 8,
) -> Tuple[List[int], bool]:
    """把用户的直属 dept_id 列表展开为【物理祖先链并集】→ (id 列表, ok)。

    node-ACL 的读侧展开(设计稿 §2):可见 ⟺ 祖先链 ∩ 文档授权节点集 ≠ ∅。文档只存被勾
    节点、查询发用户祖先链 ⇒ 新建叶子部门自动继承、改名免疫、**零文档重推**。

    ⚠️ **与 `resolve_dept_ids` 是两条独立通道,语义刻意不同**:
      · `resolve_dept_ids` 走锚点表求【组码】,显式 `[]` 锚 = "组码解析到此为止"(截断继承);
      · 本函数求【物理链】,**任何锚点都不截断它** —— 否则授权某个根节点将覆盖不到该部门,
        直接违反纯子树语义。故本函数根本不读 `ANCHOR_GROUPS_BY_DEPT_ID`。

    返回 ok=False(节点通道整体不可用,调用方 fail-closed 仅 public,**绝不回退名字/组码口径**):
      · 直属部门数超 `max_direct_depts`;
      · 任一跳 `get_parent_id` 返回 None(查询失败/快照缺该节点);
      · 环 / 自指 / 超 `max_hops`。
    链含节点自身,**不含虚拟根 ROOT_DEPT_ID**(授权到根 = 全员可见,那是 public 的语义,
    不该由节点表达)。输出按首次出现顺序去重。
    """
    raw = [d for d in (dept_ids or [])]
    if len(raw) > max_direct_depts:
        return [], False

    out: List[int] = []
    seen: Set[int] = set()
    for item in raw:
        try:
            dept_id = int(item)
        except (TypeError, ValueError):
            return [], False                  # 异常 id：整条通道不可信
        if dept_id <= 0 or dept_id == ROOT_DEPT_ID:
            continue
        visited: Set[int] = set()
        cur: Optional[int] = dept_id
        hops = 0
        while cur is not None and cur != ROOT_DEPT_ID and cur > 0:
            if cur in visited:
                return [], False              # 环
            visited.add(cur)
            if cur not in seen:
                seen.add(cur)
                out.append(cur)
            hops += 1
            if hops > max_hops:
                return [], False              # 深度异常
            parent = get_parent_id(cur)
            if parent is None:
                return [], False              # 父链查询失败
            if parent == cur:
                return [], False              # 自指
            cur = parent
    return out, True
