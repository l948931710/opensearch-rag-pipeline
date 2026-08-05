# -*- coding: utf-8 -*-
"""
dingtalk_identity.py — 钉钉用户身份解析（OpenAPI 通讯录 / 免登）

这是机器人与小程序**共用**的身份基础设施，与「机器人收发消息」解耦：
  - _resolve_user_dept(staff_id)        : userid → ACL 权限组列表（RDS 缓存优先 + 钉钉 API 回退）
  - _fetch_dingtalk_user_info(user_id)  : 钉钉 user/get → {user_name, dept_name(全部门 CSV)}
  - _fetch_dept_name(token, dept_id)    : 部门 ID → 部门名称
  - _get_miniapp_access_token()         : 小程序应用 access_token（独立凭证，回退机器人应用）
  - _exchange_authcode_for_userid(code) : 小程序免登 authCode → userid（getuserinfo）
  - _resolve_user_identity(userid)      : userid → {dept:[组列表], name}（供 /api/auth/dingtalk 签发令牌）

设计要点：
  - **ACL 权限组**（H1）：一个钉钉叶子部门映射到一个或多个权限组（owner_dept 代码，如
    marketing/production），用户可属多组；解析结果是组【列表】，与 HA3 owner_dept 对齐。
  - 模块级无钉钉依赖（access_token / DB 连接均惰性导入），避免循环引用。
  - 模拟模式（simulate_api）下不发真实请求，返回可配置的测试身份，便于离线联调。
"""

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Union

import requests

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)


def _acl_cache_ttl_seconds() -> int:
    """_resolve_user_dept（机器人写路径）缓存命中的行级复核 TTL（秒）。超过则对【自动缓存的
    employee 行】穿透重查钉钉 API，自愈 department/get 瞬时失败留下的残缺 dept_code（F-22）。
    seeded 行（role≠employee）不受 TTL 约束，始终缓存优先（H3）。0=禁用穿透。默认 6 小时。"""
    try:
        return max(0, int(os.environ.get("RAG_ACL_CACHE_TTL", "21600")))
    except (TypeError, ValueError):
        return 21600


def _kb_db() -> str:
    """知识库库名（user_role/dept_admin_grant 所在库）；经 RAG_RDS_DATABASE 配置（STAGING=_stg）。"""
    return get_config().rds.database


def _acl_ancestry_enabled() -> bool:
    """RAG_ACL_ANCESTRY（默认关）：部门→组解析改走「最近祖先制」（dept_id 锚定，见
    dept_ancestry.py）。开 = cache-miss/穿透时按父链找最近锚、缓存**组码 CSV**；父链
    任一跳失败（partial）→ 整体落回现行名字口径，绝不缓存半截。关 = 行为与现行完全一致。"""
    return os.environ.get("RAG_ACL_ANCESTRY", "").strip().lower() in ("1", "true", "yes", "on")


# 祖先制「权威仅 public」的缓存哨兵：显式 [] 锚（如「其他」）解析出的 deny 结果既不能存
# 空串（cache-read 把空 dept_code 当 miss → 每条消息重走 API），也不能保留原始部门名
# （读回 _normalize_dept_to_codes 撞名字表会把显式 deny 悄悄变回授权）。存本哨兵：非空
# 可缓存、读回被白名单丢弃 = []（fail-closed 方向，round-trip 语义精确）。
_ACL_PUBLIC_ONLY_SENTINEL = "__public_only__"


def _VALID_ACL_GROUPS_FOR_GUARD() -> frozenset:
    """组码白名单（惰性 import 避免 import 环）——外部组码防投毒守卫的单一真值来源。
    绝不在本模块复制一份组码清单：加新组时守卫必须自动跟上。"""
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS
    return _VALID_ACL_GROUPS


# ═══════════════════════════════════════════════════════════════
# 钉钉部门名 → ACL 权限组 映射
# ───────────────────────────────────────────────────────────────
# ⚠️ 语义（H1）：右侧是【ACL 权限组】代码，不是组织部门。一个钉钉【叶子部门】可映射到
#    【多个】权限组（如 国际贸易部 → marketing + production）。chunk 的 owner_dept 来自
#    OSS 目录 raw/<group>/...，HA3 权限过滤要求组代码完全相等，故解析用户部门时把中文名
#    归一化为权限组代码【列表】。映射来源：用户提供的「权限单.xlsx」。
#
# ⚠️ 必须按【叶子部门(部门列)】键控，不能按中心：综合管理中心下 行政部→admin、
#    人力资源部→hr 不同；财务中心下 财务部→finance、自动化信息部→it 不同。跨多组的
#    中心名（如 财务中心/营销中心整体）【不要】映射，留作 fail-closed，避免越权。
#
# ⚠️ 安全约定：未命中的名字透传后会被 _VALID_ACL_GROUPS 白名单丢弃（fail-closed）——
#    匹配不到任何 chunk，仅 public 可见，绝不误授予 dept_internal。宁缺勿错。
#
# 个人级覆盖（如 乐敏杰 人力资源部 但应属 admin）用 seeded user_role 行实现，
# seeded 行优先于自动映射（见 _resolve_user_dept）。
_DEPT_NAME_TO_GROUPS = {
    # —— 叶子部门（权限单口径，主） ——
    "财务部": ["finance"],
    "自动化信息部": ["it"],
    "国际贸易部": ["marketing", "production"],
    "国内营销部": ["marketing", "production"],
    "电子商务部": ["marketing", "production"],
    "计划部": ["marketing", "pmc"],
    "行政部": ["admin"],
    "人力资源部": ["hr"],
    "生产部": ["production"],
    "研发部": ["rd"],
    "实验室": ["rd"],
    "技术部": ["quality"],
    "品质部": ["quality"],
    "资材部": ["supply", "pmc", "production"],   # 2026-07-03 拍板：归生产中心下，叠 production 可读
    # —— 2026-07-03 拍板批（与 dept_ancestry 锚表同步；名字条目让 flag 关时也生效） ——
    "海外中心": ["overseas", "production"],       # 自有组 + 维持 production 可读
    "印尼公司": ["overseas", "production"],
    "获胜工厂": ["overseas", "production"],
    "墨西哥公司": ["overseas", "production"],
    "海外生产中心国内办公室": ["overseas", "production"],
    "总经办": ["*"],                              # 全库可读（"*"=全组哨兵；非 kb_admin，无写权/管理台）
    "审计部": ["audit"],
    "审计一部": ["audit"],
    "审计二部": ["audit"],
    "法务": ["legal"],
    "工程": ["engineering"],
    "玉米环保": ["corn_eco"],
    "财务中心": ["finance", "it"],           # 2026-07-04 拍板：中心挂点=双职能（子部门有精确映射不受影响）
    # ⚠️「办公室」（综合管理中心/办公室→[admin,hr]）只在 dept_ancestry 锚表按 dept_id 设锚，
    #    刻意不进本名字表：通用名全局键控会误伤其他子树同名部门（如车间办公室）。
    # 「其他」：有意仅 public——名字表"未收录=fail-closed"即该语义；祖先制里是显式 [] 锚。
    # —— 中心级名（历史/兜底；待真钉钉账号确认 _fetch_dept_name 返回叶子还是中心。
    #    仅保留【单组无歧义】的中心名，多组中心名不放以免越权） ——
    "营销中心": ["marketing"],
    "生产中心": ["production"],
    "研发中心": ["rd"],  # 纯 rd 子树（研发部/实验室皆 →rd）；线上有 1 名用户直接挂中心节点
    "PMC部": ["pmc"],
}


# ───────────────────────────────────────────────────────────────
# 生产中心子树 → 'production' 伞组（H4：subline 用户实际拿到 production）
# ───────────────────────────────────────────────────────────────
# 钉钉把一线员工挂在【叶子部门】上（如 模具A / 三车间A区机修），_fetch_dept_name 返回的就是
# 这些叶子名，而非「生产中心」。所以仅映射中心/事业部名不够——大量真实产线用户会落到
# fail-closed 仅 public。下面这张【显式白名单】枚举了「生产中心」(钉钉 dept_id 599318766)
# 整棵子树的所有部门名（含事业部/车间/班组等中间与叶子节点），统一归一化为 'production'
# 伞组——一个 production 用户经 retriever._PRODUCTION_UMBRELLA_OWNERS 可读 production 及各
# production_* 子线内容（伞组是粗粒度的：生产中心全体员工共享 production dept_internal）。
#
# ⚠️ 排除 资材部：它结构上挂在生产中心下，但权限单口径属 [supply, pmc]——靠
#    _normalize_dept_to_codes 的「_DEPT_NAME_TO_GROUPS 优先」裁决保证不被覆盖（已从本集合剔除，
#    双保险）。品质/技术（品技中心）与研发（研发中心）是独立中心、不在本子树内，不受影响。
# ⚠️ 这是 2026-06-21 对线上钉钉组织树的快照（85 个节点）。组织调整后会新增/改名叶子；
#    未命中的新叶子 fail-closed（仅 public，安全的失败方向），由 audit 暴露后再回灌。
#    刷新：python scripts/gen_production_dept_names.py（遍历子树重出本集合，粘回此处）。
_PRODUCTION_WORKSHOP_DEPTS = frozenset({
    "F区机修",
    "G区机修",
    "一、四车间办公室",
    "一车间拉片",
    "三车间A区机修",
    "三车间B区机修",
    "三车间E区机修",
    "三车间办公室",
    "三车间印刷机修",
    "二车间C区机修",
    "二车间D区机修",
    "二车间办公室",
    "包装车间—其他人员",
    "包装车间—机修",
    "包装车间—管理员",
    "原辅料、五金仓库",
    "吸塑一、四车间",
    "吸塑一、四车间其他",
    "吸塑一、四车间拉片",
    "吸塑一、四车间料房",
    "吸塑一、四车间机修",
    "吸塑一、四车间班组长",
    "吸塑三车间",
    "吸塑三车间—其他人员",
    "吸塑三车间成型机修",
    "吸塑三车间拉片机修",
    "吸塑三车间料房",
    "吸塑三车间班组长",
    "吸塑事业部",
    "吸塑二车间",
    "吸塑二车间其他",
    "吸塑二车间拉片",
    "吸塑二车间料房",
    "吸塑二车间机修",
    "吸塑二车间班组长",
    "吸塑制程检",
    "吸塑办公室",
    "吸塑叉车",
    "吸塑成品仓管",
    "吸塑手包",
    "吸管1车间仓管",
    "吸管1车间叉车",
    "吸管1车间料房",
    "吸管1车间机修",
    "吸管1车间班长",
    "吸管2车间仓管",
    "吸管2车间其他",
    "吸管2车间料房",
    "吸管2车间机修",
    "吸管2车间班长",
    "吸管事业部",
    "吸管制程检",
    "吹膜—仓管",
    "吹膜—其他",
    "吹膜—切袋",
    "吹膜—吹膜机修",
    "吹膜—机修",
    "吹膜车间",
    "四车间拉片",
    "模具A",
    "模具B",
    "模具车间",
    "注塑事业部",
    "注塑制程检",
    "注塑叉车",
    "注塑成品仓管",
    "注塑车间—其他人员",
    "注塑车间—料房",
    "注塑车间—机修",
    "注塑车间—班组长",
    "生产部",
    "精益部",
    "纸杯—其他",
    "纸杯—办公室",
    "纸杯—半成品仓管",
    "纸杯—印刷",
    "纸杯—成品仓管、叉车",
    "纸杯—机修",
    "纸杯—模切",
    "纸杯—淋膜",
    "纸杯—班组长",
    "纸杯事业部",
    "纸杯制程检",
    "纸浆模塑事业部",
    "纸箱车间",
})


def _normalize_dept_to_codes(raw: Union[str, List[str], None]) -> List[str]:
    """把钉钉中文部门名 / 代码 / CSV / 列表 归一化为 ACL 权限组代码【列表】。

    - 已知中文叶子部门名 → 对应权限组列表（一名可映射多组）。
    - 生产中心子树叶子名（_PRODUCTION_WORKSHOP_DEPTS）→ ['production'] 伞组。
    - 已是组代码 / CSV / 列表 → 拆分后逐项透传。
    - 最终统一过 retriever._VALID_ACL_GROUPS 白名单 + 去重（H2 防御纵深）。
    - 未知 / 空 / 全非法 → []（fail-closed：匹配不到 chunk，仅 public 可见）。

    匹配优先级：_DEPT_NAME_TO_GROUPS（精确映射，含 资材部→[supply,pmc] 的反例）优先于
    _PRODUCTION_WORKSHOP_DEPTS（生产子树伞组），最后才透传——保证子树下的 资材部 不被
    误归一化为 production。
    """
    if not raw:
        return []
    items = raw.split(",") if isinstance(raw, str) else raw
    # 白名单：检索安全边界的同一份合法组集合（惰性 import 避免任何 import 环）
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS

    out: List[str] = []
    seen = set()
    for item in items:
        key = (item or "").strip() if isinstance(item, str) else str(item).strip()
        if not key:
            continue
        if key in _DEPT_NAME_TO_GROUPS:          # 精确映射优先（资材部 等反例在此裁决）
            mapped = _DEPT_NAME_TO_GROUPS[key]
        elif key in _PRODUCTION_WORKSHOP_DEPTS:  # 生产中心子树 → production 伞组
            mapped = ["production"]
        else:
            mapped = [key]                       # 透传（待白名单裁决：未知即 fail-closed）
        if "*" in mapped:                        # "*"=全组哨兵（总经办类全可见）→ 展开为白名单全量
            mapped = sorted(_VALID_ACL_GROUPS)
        for code in mapped:
            code = code.strip()
            if code and code in _VALID_ACL_GROUPS and code not in seen:
                seen.add(code)
                out.append(code)
    return out


# ═══════════════════════════════════════════════════════════════
# 用户部门解析（机器人 + 小程序共用）
# ═══════════════════════════════════════════════════════════════

# 机器人路径的进程内短 TTL 缓存（perf#17）：_process_rag_query 每条消息都同步调
# _resolve_user_dept，即使 user_role 命中也要一次阻塞 RDS 往返，压在用户首字延迟上。
# 只缓存【确定性来源】的非空解析（fresh 缓存行 / API 完整成功）——partial（等下次自愈）、
# API 失败回退旧缓存（等下次重试）、fail-closed []（未知用户逐次重查）一律不缓存，
# 各 fail-open/fail-closed 语义原样保留。撤销/调岗窗口 = TTL（默认 90s，远短于会话粒度）；
# RAG_BOT_DEPT_CACHE_TTL_SECONDS=0 关闭。conftest 每测清空。
_bot_dept_cache: dict = {}
_bot_dept_cache_lock = threading.Lock()


def _bot_dept_cache_ttl_seconds() -> float:
    try:
        return float(os.environ.get("RAG_BOT_DEPT_CACHE_TTL_SECONDS", "90"))
    except ValueError:
        return 90.0


def _bot_dept_cache_clear() -> None:
    with _bot_dept_cache_lock:
        _bot_dept_cache.clear()


def _resolve_user_dept(staff_id: str) -> List[str]:
    """从 RDS user_role 表查询用户所属 ACL 权限组【列表】（进程内 TTL 缓存，见 _bot_dept_cache）。

    语义与缓存策略详见 _resolve_user_dept_live；本包装层只对确定性非空结果做短 TTL 复用。
    """
    if not staff_id or staff_id.startswith("$:"):
        return []
    ttl = _bot_dept_cache_ttl_seconds()
    now = time.time()
    if ttl > 0:
        with _bot_dept_cache_lock:
            ent = _bot_dept_cache.get(staff_id)
            if ent is not None and ent[0] > now:
                return list(ent[1])
    codes, cacheable = _resolve_user_dept_live(staff_id)
    if ttl > 0 and cacheable and codes:
        with _bot_dept_cache_lock:
            _bot_dept_cache[staff_id] = (now + ttl, list(codes))
            if len(_bot_dept_cache) > 4096:   # 粗粒度防胀，同 _live_acl_cache
                _bot_dept_cache.clear()
    return codes


def _resolve_user_dept_live(staff_id: str) -> "tuple[List[str], bool]":
    """_resolve_user_dept 的真实解析体。返回 (组列表, cacheable)。

    user_role 中不存在时，自动通过钉钉 API 获取（遍历完整 dept_id_list）并缓存。
    查询失败或用户不存在时返回 []，调用方据此降级为只返回 public 文档（fail-closed）。

    ⚠️ seeded 行优先（H3）：本函数先 SELECT 缓存，命中即返回；【只有】缓存为空才调
    API 并 INSERT。因此人工 seeded 的 user_role 行（如个人级覆盖 乐敏杰→admin）会被
    SELECT 命中并返回，API 分支根本不触发，绝不会被自动部门映射覆盖。

    cacheable=True 仅限确定性来源：fresh 缓存行 / API 完整成功（含穿透刷新）。partial、
    API 失败回退旧缓存、fail-closed [] 都标 False——让下一条消息照旧重查/自愈。
    """
    try:
        from opensearch_pipeline.db import _get_db_conn

        conn = _get_db_conn()
        try:
            # F-22：过期 employee 行穿透重查时的旧缓存兜底——若 API 重查失败/不完整则退回它，
            # 绝不把已知部门的用户 fail-closed 掉到 public。
            _cached_codes = None
            # 1. 先查本地缓存（seeded 行在此命中并优先；按最新行取值，user_id 唯一键见
            #    schema/003_user_role_unique.sql；显式排序保证确定性）。一并取 role 与"已缓存秒数"
            #    （SQL 侧 TIMESTAMPDIFF，避免应用/DB 时区不一致——项目已知 tz=Pacific 陷阱）。
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT dept_code, role, TIMESTAMPDIFF(SECOND, updated_at, NOW()), is_active "
                    f"FROM {_kb_db()}.user_role "
                    "WHERE user_id = %s "
                    "ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (staff_id,),
                )
                row = cur.fetchone()
                # 评审F4d（2026-07-21）：is_active=0 = 权威墓碑（显式撤读权），不再被
                # is_active=1 过滤成「等同未见」→ 回钉钉 API 刷新出全组（撤销完全失效）。
                # 墓碑 → 权威空组、跳过 API、绝不经下方 upsert 复活。时延契约：bot 层
                # 90s / live-ACL 层 45s 暖缓存窗内旧组仍可能被复用（≤90s 收敛，仅
                # RAG_LIVE_ACL_REREAD=true 默认态下成立；关闭 reread 时收敛点=下次发令牌，
                # ≈令牌 TTL 2h + ≤90s）。应急即时撤权走 RAG_REVOKED_USER_IDS /
                # RAG_SESSION_TOKEN_MIN_IAT（钉钉侧移出组织**不是**应急路径——employee
                # 缓存行 6h 信任 + API miss 退回旧缓存）。注：当前无任何代码写 is_active=0
                # （kb_access 撤管理权只降 role），本语义为控制台/运维手工墓碑预留。
                if row is not None and not row[3]:
                    logger.info("用户已被墓碑（is_active=0）→ 权威空组: staff_id=%s", staff_id)
                    return [], True
                if row and row[0]:
                    # 缓存里存的可能是中文名(CSV) 或组代码(CSV)；归一化为组列表再返回。
                    # 未知项经白名单丢弃 = fail-closed（仅 public）。
                    codes = _normalize_dept_to_codes(row[0])
                    _ttl = _acl_cache_ttl_seconds()
                    _role = (row[1] or "employee")
                    _age = row[2]
                    # F-22 行级 TTL 复核：仅对自动缓存的 employee 行、且已过期时穿透重查钉钉 API
                    # （自愈 department/get 瞬时失败留下的残缺 dept_code）。seeded 行（role≠employee）
                    # 永远缓存优先（H3），绝不因 TTL 被 API 覆盖。TTL=0 或 age 取不到 → 不穿透。
                    _stale = (_ttl > 0 and _role == "employee"
                              and _age is not None and _age > _ttl)
                    if not _stale:
                        logger.info("用户权限组解析成功（缓存）: staff_id=%s → raw=%s（groups=%s）",
                                    staff_id, row[0], codes)
                        return codes, True
                    _cached_codes = codes
                    logger.info("ACL 缓存过期，穿透重查钉钉 API: staff_id=%s age=%ss ttl=%ss",
                                staff_id, _age, _ttl)

            # 2. cache-miss 或 过期 employee 行穿透：调钉钉 API 获取（dept_name 为全部部门名 CSV）
            user_info = _fetch_dingtalk_user_info(staff_id)
            if user_info:
                # 外部组码防投毒（2026-07-17 ultra P2 起「星号」，2026-08-04 扩到全部组码）：
                # dept_name 这一列是【名字域与组码域同居】的——_normalize_dept_to_codes 先查
                # 名字表，未命中则把 token 原样透传给组码白名单。于是一个恰好命名为 "*"（撞全组
                # 哨兵）或 "production"（撞组码）的钉钉部门，其成员会直接拿到对应权限，并随下方
                # 缓存写进 user_role.dept_code 持久化。组码只允许内部来源（映射表值 / 祖先制
                # 压缩回写），**API 名字口径里长得像组码的项一律按未知丢弃**（fail-closed）。
                # 2026-08-04 实测：线上 119 个活跃部门无一撞组码，本守卫零误伤。
                _names = [p.strip() for p in (user_info.get("dept_name") or "").split(",")]
                _forged = {"*"} | {n for n in _names if n and n in _VALID_ACL_GROUPS_FOR_GUARD()}
                if any(n in _forged for n in _names):
                    _kept = ",".join(s for s in _names if s and s not in _forged)
                    logger.warning("外部部门名撞组码/哨兵，已丢弃(fail-closed): staff_id=%s 丢弃=%s",
                                   staff_id, sorted(n for n in set(_names) if n in _forged))
                    user_info = dict(user_info, dept_name=_kept)
                # —— 最近祖先制（RAG_ACL_ANCESTRY，默认关）——非 partial 即权威：
                # 组码 CSV 顶替 dept_name 走下方同一缓存/返回路径（_normalize_dept_to_codes
                # 对组码幂等读回；且不受名字口径 is_partial 影响——id 父链是独立的完整解析）。
                # 三态落点：非空=授组；空+全支锚定(decided)=权威「有意仅 public」→ 存 deny
                # 哨兵、绝不落回名字口径（显式 [] 锚要能压过名字表撞名，否则 deny 腿失效）；
                # 空+存在未决定支=锚表覆盖缺口 → 名字口径兜底（现行为，tests 对照锁定）；
                # partial(None)=整体落回名字口径且绝不缓存半截。
                _anc_res = _resolve_groups_via_ancestry(user_info.get("dept_ids") or []) \
                    if _acl_ancestry_enabled() else None
                if _anc_res is not None:
                    _anc, _anc_undecided = _anc_res
                    if _anc:
                        # 全组结果压缩回 "*" 哨兵再落 dept_name/缓存：15 组码 CSV=104 字符会溢出
                        # user_role.dept_code VARCHAR(64)（strict 模式每次写失败被吞→永不缓存、每个
                        # TTL 重走全链；非 strict 静默截断→读回被白名单丢尾，总经办静默少 6/15 组）。
                        # 读侧 _normalize_dept_to_codes 本就把 "*" 展开为全量白名单（与 seeded 行同一
                        # round-trip），语义无损且加新组自动跟上。
                        from opensearch_pipeline.retriever import _VALID_ACL_GROUPS
                        _csv = "*" if set(_anc) == set(_VALID_ACL_GROUPS) else ",".join(_anc)
                        user_info = dict(user_info, dept_name=_csv, is_partial=False)
                    elif not _anc_undecided:
                        user_info = dict(user_info, dept_name=_ACL_PUBLIC_ONLY_SENTINEL,
                                         is_partial=False)
                if user_info.get("is_partial"):
                    # F-22：解析不完整（某 dept 瞬时失败）→ 绝不落缓存，避免残缺 CSV 永久少授权。
                    # 穿透场景退回旧缓存（更全）；纯 cache-miss 返回本次 best-effort 组（仅 public 之上、
                    # 仍是真实子集=fail-closed 方向）。下次调用重新走 API 复核，自愈。
                    fresh = _normalize_dept_to_codes(user_info.get("dept_name", ""))
                    logger.warning("用户部门解析不完整（department/get 瞬时失败），跳过缓存: staff_id=%s", staff_id)
                    return (_cached_codes if _cached_codes is not None else fresh), False
                dept_name = user_info.get("dept_name", "")
                user_name = user_info.get("user_name", "")
                # 3. 缓存到 user_role 表（employee 行；ON DUPLICATE KEY UPDATE 刷新 updated_at；
                #    绝不覆盖 seeded 行——seeded 行在上方缓存命中即返回，根本走不到这里）
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"""
                            INSERT INTO {_kb_db()}.user_role (user_id, user_name, dept_code, role, is_active)
                            VALUES (%s, %s, %s, %s, 1)
                            ON DUPLICATE KEY UPDATE
                                user_name = VALUES(user_name),
                                dept_code = VALUES(dept_code),
                                updated_at = NOW()
                            """,
                            (staff_id, user_name, dept_name, "employee"),
                        )
                    conn.commit()
                    logger.info("用户信息已缓存: staff_id=%s, name=%s, dept=%s", staff_id, user_name, dept_name)
                except Exception as cache_err:
                    logger.warning("缓存用户信息失败: %s", cache_err)
                # 缓存原始中文名（便于在 DMS/钉钉侧对照），返回时归一化为组列表
                return _normalize_dept_to_codes(dept_name), True
            else:
                # API 失败：穿透场景退回旧缓存（不丢已知部门）；纯 cache-miss 才 fail-closed []
                if _cached_codes is not None:
                    logger.warning("穿透重查 API 失败，退回旧缓存: staff_id=%s", staff_id)
                    return _cached_codes, False
                logger.warning("用户未在 user_role 表中注册且 API 查询失败: staff_id=%s", staff_id)
                return [], False
        finally:
            conn.close()
    except Exception as e:
        logger.warning("查询用户部门失败 staff_id=%s: %s", staff_id, e)
        return [], False


# 读时复核的进程内短 TTL 缓存（性能第一梯队 #3）：RAG_LIVE_ACL_REREAD 默认开 →
# 每个带令牌请求都多付一次阻塞 RDS 往返，且 RDS brownout 时逐请求卡 connect/read
# timeout、拖住本不依赖 RDS 的回答路径。短 TTL 内复用**DB 真值**（含"无在册行"），
# 撤销语义几乎不变：TTL 到期即重查，令牌本身 2h 兜底，跨部门授权另有实时拒绝路径。
# DB 异常不缓存（保持逐请求重试的 fail-open 原语义）。RAG_LIVE_ACL_TTL_SECONDS=0 关闭。
_LIVE_ACL_MISS = object()   # 区分"缓存的 None（DB 确认无在册行）"与"未缓存"
_live_acl_cache: dict = {}
_live_acl_cache_lock = threading.Lock()


def _live_acl_ttl_seconds() -> float:
    try:
        return float(os.environ.get("RAG_LIVE_ACL_TTL_SECONDS", "45"))
    except ValueError:
        return 45.0


def _live_acl_cache_clear() -> None:
    with _live_acl_cache_lock:
        _live_acl_cache.clear()


def _resolve_user_dept_cached(staff_id: str, *, with_status: bool = False):
    """读时实时重查（SELECT-only）：仅查 user_role 缓存，**不调钉钉 API、不 INSERT、无副作用**，
    供 current_identity 读路径对令牌内嵌 acl_groups 做实时复核（部门收紧/放宽即时生效，不等 TTL）。

    with_status（P0-04 fail-closed 需区分「DB 失败」与「无在册行」——两者原都返回 None）：
    True → 返回 (groups_or_None, db_ok)；db_ok=False 仅当 DB 查询**异常**（连接/查询失败），
    「无在册行」是成功查询故 db_ok=True。默认 False 时返回旧契约（仅 groups_or_None）。

    与 _resolve_user_dept 的关键区别（故意不复用——后者在 cache-miss 时有 API+写副作用，不可放热路径）：
      - 命中在册行 → 返回归一化组列表（含放宽/收紧）。
      - 无在册行 或 DB 失败 → 返回 **None**（调用方据此【保留令牌内嵌组】）。绝不因瞬时 DB 抖动或
        未缓存把用户降到仅 public（区别于 _resolve_user_dept 的 []=fail-closed）。撤销窗口由短 TTL 兜底。

    结果带 RAG_LIVE_ACL_TTL_SECONDS（默认 45s）进程内缓存——见 _live_acl_cache 注释。
    """
    def _ret(groups, db_ok=True):
        return (groups, db_ok) if with_status else groups

    if not staff_id or staff_id.startswith("$:"):
        return _ret(None)                      # 群聊/空 uid：非 DB 失败（db_ok=True）

    ttl = _live_acl_ttl_seconds()
    now = time.time()
    if ttl > 0:
        with _live_acl_cache_lock:
            ent = _live_acl_cache.get(staff_id)
            if ent is not None and ent[0] > now:
                val = ent[1]
                if val is _LIVE_ACL_MISS:
                    return _ret(None)          # 缓存的「无在册行」：成功查询结果
                return _ret(list(val))

    try:
        from opensearch_pipeline.db import _get_db_conn

        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT dept_code, is_active FROM {_kb_db()}.user_role "
                    "WHERE user_id = %s "
                    "ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (staff_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if row is not None and not row[1]:
            # 评审F4d：墓碑（is_active=0）= 权威空组 []（≠None）——api.py 的
            # `live is not None → groups=live` 在默认与 strict 两模式下都会应用 []，
            # strict 模式「无在册行保留令牌组」的既有语义不受影响（那是 None 腿）。
            groups = []
        elif row and row[0]:
            groups = _normalize_dept_to_codes(row[0])
        else:
            groups = None   # 无在册行：可能只是未缓存，不收紧到 public；保留令牌组，短 TTL 兜底
        if ttl > 0:
            with _live_acl_cache_lock:
                _live_acl_cache[staff_id] = (
                    now + ttl, _LIVE_ACL_MISS if groups is None else list(groups))
                # 粗粒度防胀：条目数超 4096 直接清空（正常在册用户 ~千级，不会触发）
                if len(_live_acl_cache) > 4096:
                    _live_acl_cache.clear()
        return _ret(groups)                    # 成功查询（groups=None 即无在册行，db_ok=True）
    except Exception as e:
        logger.warning("读时 acl 复核失败 staff_id=%s: %s", staff_id, e)
        return _ret(None, db_ok=False)         # DB 失败：db_ok=False（strict 据此 fail-closed）


def user_row_revoked(user_id: str) -> bool:
    """P1-08（外审核查 2026-07-16；2026-07-21 迁移批B2 自 claude/ontology-p0 移植）
    墓碑判定：user_role **有行但全部 is_active=0** = 该用户被显式停用——区别于
    「从未缓存」的无行（那是文档化的保留令牌组+短 TTL 兜底语义，本判定不碰）。
    用既有 is_active 列做 tombstone，零 schema 变更。
    读失败 → False（fail-open 到既有 TTL 语义：DB 抖动绝不误判成停用）。"""
    if not user_id or str(user_id).startswith("$:"):
        return False                           # 群聊/空 uid：无账号概念
    try:
        from opensearch_pipeline.db import _get_db_conn

        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT MAX(is_active) FROM {_kb_db()}.user_role WHERE user_id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        # NULL=无行（非停用）；0=有行且全部停用（墓碑）；1=仍有活跃行
        return bool(row) and row[0] is not None and int(row[0]) == 0
    except Exception as e:   # noqa: BLE001
        logger.warning("user_role 墓碑判定失败 user_id=%s（按未停用处理）: %s", user_id, e)
        return False


def _fetch_dingtalk_user_info(user_id: str) -> Optional[dict]:
    """
    通过钉钉 API 获取用户信息（姓名、部门等）。

    dept_name 是该用户【所有】所属部门名的 CSV（遍历完整 dept_id_list，H3），
    以便多部门用户拿到全部权限组（如 国际贸易部 → marketing+production）。

    Returns:
        {"user_name": "张三", "dept_name": "国际贸易部,行政部"} 或 None
    """
    from opensearch_pipeline.dingtalk_card import _get_access_token

    token = _get_access_token()
    if not token:
        return None

    try:
        # 使用旧版 API（更兼容）: /topapi/v2/user/get
        resp = requests.post(
            f"https://oapi.dingtalk.com/topapi/v2/user/get?access_token={token}",
            json={"userid": user_id},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                result = data.get("result", {})
                user_name = result.get("name", "")
                # 遍历完整 dept_id_list（H3），收集全部部门名 → CSV，支持多部门用户。
                # perf F#55：多部门用户此前串行逐部门 HTTP（每次 5s 超时）压在首问关键路径
                # （cache-miss 才走）——改小线程池并发拉取；结果仍按 dept_id_list 原顺序
                # 消费，去重/顺序/is_partial 判定与串行版等价。
                dept_id_list = result.get("dept_id_list", [])
                name_by_id = _fetch_dept_names_concurrent(token, dept_id_list)
                dept_names = []
                seen_names = set()
                # F-22：任一 dept_id 的 department/get 瞬时失败（返回空名）→ 解析不完整。
                # 多部门用户丢任一组即少授权，调用方据此【不落缓存】（避免残缺 CSV 永久少授权）。
                # dept_id_list 为空是合法的「无部门」，不算不完整。
                is_partial = False
                for did in dept_id_list:
                    nm = name_by_id.get(did, "")
                    if not nm:
                        is_partial = True
                        continue
                    if nm not in seen_names:
                        seen_names.add(nm)
                        dept_names.append(nm)
                dept_name = ",".join(dept_names)
                # dept_ids：最近祖先制（RAG_ACL_ANCESTRY）用原始 dept_id 列表沿父链找锚；
                # 名字口径的消费方不读此键（加键纯 additive，既有桩/调用零影响）。
                return {"user_name": user_name, "dept_name": dept_name, "is_partial": is_partial,
                        "dept_ids": list(dept_id_list)}
            logger.warning("用户查询业务失败: errcode=%s errmsg=%s", data.get("errcode"), data.get("errmsg"))
            return None
        logger.warning("用户查询 HTTP 失败: %s", resp.text[:300])
        return None
    except Exception as e:
        logger.warning("用户查询异常: %s", e)
        return None


def _dept_fetch_concurrency() -> int:
    """部门名并发拉取线程数（perf F#55）。RAG_DEPT_FETCH_CONCURRENCY，默认 4（只读钉钉
    department/get、仅 cache-miss 首解析才走）；<=1 退回串行。"""
    try:
        return max(1, int(os.environ.get("RAG_DEPT_FETCH_CONCURRENCY", "4")))
    except (TypeError, ValueError):
        return 4


def _fetch_dept_names_concurrent(token: str, dept_id_list) -> Dict[Any, str]:
    """并发解析 dept_id → 部门名，返回 {dept_id: name}（失败/超时值为 ""，语义同串行版）。

    cache-miss 首解析的关键路径优化：N 个部门从 N×RTT（最坏 N×5s）降到 ~1×RTT。
    单部门或并发=1 时退化为串行调用（零行为差异）；结果 dict 由调用方按原顺序消费，
    去重/顺序稳定性不在本函数内。_fetch_dept_name 按模块全局名调用（tests monkeypatch 兼容）。
    """
    ids = list(dept_id_list or [])
    if not ids:
        return {}
    conc = min(_dept_fetch_concurrency(), len(ids))
    if conc <= 1 or len(ids) == 1:
        return {did: _fetch_dept_name(token, did) for did in ids}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=conc) as pool:
        names = list(pool.map(lambda did: _fetch_dept_name(token, did), ids))
    return dict(zip(ids, names))


def _fetch_dept_name(token: str, dept_id: int) -> str:
    """通过部门 ID 获取部门名称。"""
    try:
        resp = requests.post(
            f"https://oapi.dingtalk.com/topapi/v2/department/get?access_token={token}",
            json={"dept_id": dept_id},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                return data.get("result", {}).get("name", "")
    except Exception:
        pass
    return ""


def _fetch_dept_parent(token: str, dept_id: int) -> Optional[int]:
    """dept_id → 父部门 id（department/get 的 parent_id；根返回 0）。失败 → None
    （dept_ancestry 契约：None = 该支 partial，调用方落回名字口径）。与 _fetch_dept_name
    分开：后者是既有名字口径的测试契约（monkeypatch 面），不动。"""
    try:
        resp = requests.post(
            f"https://oapi.dingtalk.com/topapi/v2/department/get?access_token={token}",
            json={"dept_id": dept_id},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                return int(data.get("result", {}).get("parent_id", 0) or 0)
    except Exception:
        pass
    return None


def _resolve_groups_via_ancestry(dept_ids) -> "Optional[tuple]":
    """最近祖先制解析（RAG_ACL_ANCESTRY 开时由 _resolve_user_dept_live 调用）。

    返回 (组码列表, undecided) = 权威结果：非空=授组；空+undecided=False=全部支路终结于锚
    （含显式 [] 锚）= 权威「有意仅 public」；空+undecided=True=存在到顶无锚支（锚表覆盖
    缺口）→ 调用方落回名字口径兜底。返回 None = partial（父链任一跳失败/环/异常 id）
    或无 token —— 调用方【整体落回名字口径】。
    每跳独立 department/get（仅 cache-miss/穿透路径才走，树深 ≤5，可接受）。
    """
    from opensearch_pipeline.dept_ancestry import resolve_dept_ids
    from opensearch_pipeline.dingtalk_card import _get_access_token

    token = _get_access_token()
    if not token:
        logger.warning("ACL 祖先制回退名字口径: 无 access_token dept_ids=%s", dept_ids)
        return None
    codes, partial, undecided = resolve_dept_ids(
        dept_ids or [], lambda did: _fetch_dept_parent(token, did))
    if partial:
        # 唯一的可观测点：本模块与 dept_ancestry 此前全文无日志，而下方调用点在 partial 时
        # 【整块跳过】、退到名字口径并【照常落缓存】(6h)——即显式 [] 锚的权威 deny 可被一次
        # department/get 超时击穿成名字口径授权并持久化。翻 RAG_ACL_ANCESTRY 前后靠这条盯窗口。
        logger.warning("ACL 祖先制 partial→回退名字口径(结果会被缓存): dept_ids=%s", dept_ids)
    return None if partial else (codes, undecided)


# ═══════════════════════════════════════════════════════════════
# 小程序免登：authCode → userid → 身份(部门/姓名)
# ═══════════════════════════════════════════════════════════════

# 小程序应用 access_token 缓存（独立凭证时使用；提前 5 分钟刷新）
_MINIAPP_TOKEN: Dict[str, Any] = {"token": None, "exp": 0.0}
_MINIAPP_TOKEN_LOCK = threading.Lock()


def _get_miniapp_access_token() -> Optional[str]:
    """获取小程序应用的 access_token。

    小程序通常是独立于机器人的新应用，拥有自己的 AppKey/AppSecret。优先读取
    DINGTALK_MINIAPP_CLIENT_ID / DINGTALK_MINIAPP_CLIENT_SECRET；未配置时回退到机器人
    应用的凭证（dingtalk_card._get_access_token），方便复用同一个应用。
    """
    client_id = os.environ.get("DINGTALK_MINIAPP_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DINGTALK_MINIAPP_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        # 未配置独立小程序凭证 → 复用机器人应用的 access_token（自带缓存）
        from opensearch_pipeline.dingtalk_card import _get_access_token
        return _get_access_token()

    with _MINIAPP_TOKEN_LOCK:
        if _MINIAPP_TOKEN["token"] and time.time() < _MINIAPP_TOKEN["exp"] - 300:
            return _MINIAPP_TOKEN["token"]
        try:
            resp = requests.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": client_id, "appSecret": client_secret},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                _MINIAPP_TOKEN["token"] = data.get("accessToken")
                _MINIAPP_TOKEN["exp"] = time.time() + data.get("expireIn", 7200)
                return _MINIAPP_TOKEN["token"]
            logger.error("获取小程序 access_token 失败: status=%s, body=%s",
                         resp.status_code, resp.text[:300])
        except Exception as e:
            logger.error("获取小程序 access_token 异常: %s", e, exc_info=True)
    return None


def _exchange_authcode_for_userid(code: str) -> Optional[str]:
    """用小程序免登 authCode 换取钉钉 userid。

    POST https://oapi.dingtalk.com/topapi/v2/user/getuserinfo?access_token=...  body {"code": code}
    模拟模式（simulate_api）下不发真实请求，返回可配置的测试 userid（RAG_SIM_USER_ID），便于离线联调。
    """
    if not code:
        return None
    try:
        if get_config().simulate_api:
            return os.environ.get("RAG_SIM_USER_ID", "SIM_USER")
    except Exception:
        pass

    token = _get_miniapp_access_token()
    if not token:
        logger.warning("无 access_token，无法用 authCode 换取 userid")
        return None
    try:
        resp = requests.post(
            f"https://oapi.dingtalk.com/topapi/v2/user/getuserinfo?access_token={token}",
            json={"code": code},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                return data.get("result", {}).get("userid")
            logger.warning("getuserinfo 业务失败: errcode=%s errmsg=%s",
                           data.get("errcode"), data.get("errmsg"))
        else:
            logger.warning("getuserinfo HTTP 失败: %s", resp.text[:300])
    except Exception as e:
        logger.warning("getuserinfo 异常: %s", e)
    return None


def _resolve_user_identity(userid: str) -> Dict[str, Any]:
    """解析用户身份：返回 {"dept": <ACL 权限组列表>, "name": <显示名>}。

    "dept" 键承载的是 ACL 权限组【列表】（如 ["marketing","production"]），供
    /api/auth/dingtalk 写入令牌的 acl_groups。复用 _resolve_user_dept 的「RDS 缓存优先 +
    钉钉 API 回退」逻辑；显示名从 user_role 缓存中取。
    模拟模式下从 RAG_SIM_USER_DEPT（可填中文名 / 组代码 / CSV）取，便于离线联调权限过滤。
    """
    if not userid:
        return {"dept": [], "name": ""}

    try:
        if get_config().simulate_api:
            return {
                "dept": _normalize_dept_to_codes(os.environ.get("RAG_SIM_USER_DEPT")),
                "name": userid,
            }
    except Exception:
        pass

    dept = _resolve_user_dept(userid)  # ACL 权限组列表（含缓存 + API 回退）
    name = ""
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT user_name FROM {_kb_db()}.user_role WHERE user_id=%s "
                    "ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (userid,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    name = row[0]
        finally:
            conn.close()
    except Exception:
        pass
    return {"dept": dept, "name": name}


def resolve_kb_identity(staff_id: str):
    """解析知识库【写授权】身份 → kb_authz.KbIdentity（role + managed_owner_depts）。

    ⚠️ 这是写授权的【权威现查】入口：每个特权写接口在写库前调用本函数（而非信任令牌里的
    role 提示），从 DB 现读 user_role.role + dept_admin_grant，从而撤销管理员/收回授权后即时生效。
    - role：user_role.role（seeded 行优先，与 _resolve_user_dept 同源语义）；缺省 → employee。
    - managed_owner_depts：dept_admin_grant 中该用户 is_active=1 的 owner_dept（kb_admin 不依赖此表，
      kb_authz.managed_owner_depts 直接给全量）。
    - acl_groups：复用读组解析，仅作审计/展示参考（kb_authz 不用它推导写权）。
    失败/未注册 → employee 空授权（fail-closed：无入口、无写权）。simulate 下从 env 取，便于离线联调。
    """
    from opensearch_pipeline.kb_authz import KbIdentity, ROLE_EMPLOYEE

    if not staff_id or staff_id.startswith("$:"):
        return KbIdentity.build(user_id=staff_id or "", role=ROLE_EMPLOYEE)

    # 模拟模式：从环境变量构造测试身份（RAG_SIM_USER_ROLE / RAG_SIM_MANAGED_OWNER_DEPTS）
    try:
        if get_config().simulate_api:
            ident = _resolve_user_identity(staff_id)
            return KbIdentity.build(
                user_id=staff_id,
                name=ident.get("name") or staff_id,
                role=os.environ.get("RAG_SIM_USER_ROLE", ROLE_EMPLOYEE),
                acl_groups=ident.get("dept") or [],
                granted_owner_depts=os.environ.get("RAG_SIM_MANAGED_OWNER_DEPTS", ""),
                granted_node_roots=[s for s in os.environ.get(
                    "RAG_SIM_MANAGED_NODE_ROOTS", "").split(",") if s.strip()],
            )
    except Exception:
        pass

    role = ROLE_EMPLOYEE
    name = ""
    managed: List[str] = []
    node_roots: List[int] = []
    try:
        from opensearch_pipeline.db import _get_db_conn

        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 角色（seeded 行优先，确定性排序）
                cur.execute(
                    f"SELECT role, user_name FROM {_kb_db()}.user_role "
                    "WHERE user_id=%s AND is_active=1 ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (staff_id,),
                )
                row = cur.fetchone()
                if row:
                    role = row[0] or ROLE_EMPLOYEE
                    name = row[1] or ""
                # 显式管理授权（dept_admin 用；kb_admin 不依赖此表）
                cur.execute(
                    f"SELECT managed_owner_dept FROM {_kb_db()}.dept_admin_grant "
                    "WHERE user_id=%s AND is_active=1",
                    (staff_id,),
                )
                managed = [r[0] for r in cur.fetchall() if r and r[0]]
                # 阶段 B 管理轴：node 管辖根（dept_admin_node_grant，auto+manual 有效行）。
                # 独立 try：060 未 apply 的环境（1146 表缺失）只让**节点轴**为空，
                # 绝不把整个身份 fail-closed 成 employee——那会误伤 legacy dept_admin。
                try:
                    cur.execute(
                        f"SELECT managed_dept_id FROM {_kb_db()}.dept_admin_node_grant "
                        "WHERE user_id=%s AND is_active=1",
                        (staff_id,),
                    )
                    node_roots = [r[0] for r in cur.fetchall() if r and r[0]]
                except Exception as ne:   # noqa: BLE001 — 表缺失/读失败 ⇒ 节点轴空（收紧方向）
                    logger.debug("dept_admin_node_grant 读取失败（节点轴按空处理）staff_id=%s: %s",
                                 staff_id, ne)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("解析知识库授权身份失败 staff_id=%s: %s（fail-closed→employee）", staff_id, e)
        return KbIdentity.build(user_id=staff_id, role=ROLE_EMPLOYEE)

    acl_groups = _resolve_user_dept(staff_id)
    return KbIdentity.build(
        user_id=staff_id, name=name, role=role,
        acl_groups=acl_groups, granted_owner_depts=managed,
        granted_node_roots=node_roots,
    )


# ── node-ACL：从 RDS 组织快照(dept_dim/staff_dim)构造读身份 ────────────────────
#
# 为什么走 RDS 而不是现有的 scratch/dingtalk_org_tree.json：那个文件同时被 .gitignore
# 与 .dockerignore 排除、Dockerfile 只 COPY opensearch_pipeline/ ⇒ **2026-07-23 起的镜像
# 应用里根本不存在**，/api/kb/org-tree 的 org_tree 在生产恒为 null。
#
# 快照过期(>48h)⇒ 节点通道整体不可用(node_channel_ok=False)，fail-closed 仅 public；
# legacy 组码通道**不受影响**(两条通道独立，绝不因节点通道失效而收紧现状行为)。
_DEPT_SNAPSHOT_MAX_AGE_H = 48
_org_snapshot_cache: dict = {}
_org_snapshot_lock = threading.Lock()


def _org_snapshot_ttl_s() -> float:
    try:
        return float(os.environ.get("RAG_ORG_SNAPSHOT_TTL_S", "300") or 0)
    except ValueError:
        return 300.0


def _load_org_snapshot() -> dict:
    """读 dept_dim → {"parents": {id: parent_id}, "fresh": bool}。进程内 TTL 缓存。

    表不存在 / 空表 / 最新 synced_at 超 48h ⇒ fresh=False（调用方据此关掉节点通道）。
    任何异常都不上抛 —— 节点通道是增量能力，绝不能因它把现状检索打挂。
    """
    ttl = _org_snapshot_ttl_s()
    now = time.monotonic()
    with _org_snapshot_lock:
        hit = _org_snapshot_cache.get("v")
        if hit and ttl > 0 and now - hit[0] < ttl:
            return hit[1]
    snap = {"parents": {}, "fresh": False}
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dept_id, parent_id FROM dept_dim WHERE is_active=1")
                snap["parents"] = {int(r[0]): int(r[1]) for r in cur.fetchall()}
                cur.execute(
                    "SELECT TIMESTAMPDIFF(HOUR, MAX(synced_at), NOW()) FROM dept_dim")
                row = cur.fetchone()
                age_h = row[0] if row and row[0] is not None else None
            snap["fresh"] = bool(snap["parents"]) and age_h is not None \
                and age_h <= _DEPT_SNAPSHOT_MAX_AGE_H
            if snap["parents"] and not snap["fresh"]:
                logger.warning("dept_dim 快照过期(%sh > %sh)⇒ 节点通道 fail-closed",
                               age_h, _DEPT_SNAPSHOT_MAX_AGE_H)
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 — 表未 apply / DB 抖动 ⇒ 节点通道关，legacy 不受影响
        logger.debug("dept_dim 不可用（node 通道关闭，legacy 不受影响）: %s", e)
    with _org_snapshot_lock:
        _org_snapshot_cache["v"] = (now, snap)
    return snap


def _load_direct_dept_ids(staff_id: str) -> Optional[List[int]]:
    """staff_dim → 该员工的直属部门 id 列表；表不可用/无该人 → None（节点通道关）。"""
    if not staff_id:
        return None
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dept_ids FROM staff_dim WHERE staff_id=%s AND is_active=1",
                    (staff_id,))
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001
        logger.debug("staff_dim 不可用（node 通道关闭）: %s", e)
        return None
    if not row or not row[0]:
        return None
    out = []
    for part in str(row[0]).split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out or None


def resolve_acl_context(staff_id: str, acl_groups: Optional[List[str]] = None):
    """staff_id(+已解析组码) → `acl_policy.AclContext`。两条入口(API / 钉钉机器人)共用。

    · groups            —— 沿用现有解析(未传则现查)，legacy 语义完全不变；
    · ancestor/direct   —— 来自 RDS 组织快照；快照缺失/过期/解析失败 ⇒ node_channel_ok=False；
    · org_wide_reader   —— 持有全部合法组 = 现行 `*` 哨兵语义(总经办)，与 legacy 判定一致。

    **绝不抛异常**：节点通道是增量能力，任何故障都只让它降级为不可用，不影响现状检索。
    """
    from opensearch_pipeline.acl_policy import AclContext
    from opensearch_pipeline.dept_ancestry import resolve_ancestor_chains
    from opensearch_pipeline.retriever import _VALID_ACL_GROUPS

    groups = list(acl_groups) if acl_groups is not None else list(_resolve_user_dept(staff_id))
    org_wide = bool(groups) and set(groups) >= set(_VALID_ACL_GROUPS)
    try:
        snap = _load_org_snapshot()
        direct = _load_direct_dept_ids(staff_id) if snap.get("fresh") else None
        if not snap.get("fresh") or not direct:
            return AclContext(groups=tuple(groups), node_channel_ok=False,
                              org_wide_reader=org_wide)
        parents = snap["parents"]
        chain, ok = resolve_ancestor_chains(direct, lambda d: parents.get(d))
        return AclContext(
            groups=tuple(groups), ancestor_dept_ids=tuple(chain),
            direct_dept_ids=tuple(direct), node_channel_ok=ok, org_wide_reader=org_wide,
        )
    except Exception as e:   # noqa: BLE001 — 任何异常 ⇒ 节点通道关，legacy 照常
        logger.warning("resolve_acl_context 失败（节点通道关闭，legacy 不受影响）: %s", e)
        return AclContext(groups=tuple(groups), node_channel_ok=False,
                          org_wide_reader=org_wide)
