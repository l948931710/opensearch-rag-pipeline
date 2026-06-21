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
    "海外中心": ["production"],
    "研发部": ["rd"],
    "实验室": ["rd"],
    "技术部": ["quality"],
    "品质部": ["quality"],
    "资材部": ["supply", "pmc"],
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
        for code in mapped:
            code = code.strip()
            if code and code in _VALID_ACL_GROUPS and code not in seen:
                seen.add(code)
                out.append(code)
    return out


# ═══════════════════════════════════════════════════════════════
# 用户部门解析（机器人 + 小程序共用）
# ═══════════════════════════════════════════════════════════════

def _resolve_user_dept(staff_id: str) -> List[str]:
    """从 RDS user_role 表查询用户所属 ACL 权限组【列表】。

    user_role 中不存在时，自动通过钉钉 API 获取（遍历完整 dept_id_list）并缓存。
    查询失败或用户不存在时返回 []，调用方据此降级为只返回 public 文档（fail-closed）。

    ⚠️ seeded 行优先（H3）：本函数先 SELECT 缓存，命中即返回；【只有】缓存为空才调
    API 并 INSERT。因此人工 seeded 的 user_role 行（如个人级覆盖 乐敏杰→admin）会被
    SELECT 命中并返回，API 分支根本不触发，绝不会被自动部门映射覆盖。
    """
    if not staff_id or staff_id.startswith("$:"):
        return []

    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn

        conn = _get_db_conn()
        try:
            # 1. 先查本地缓存（seeded 行在此命中并优先；按最新行取值，user_id 唯一键见
            #    schema/003_user_role_unique.sql；显式排序保证确定性）
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dept_code FROM fuling_knowledge.user_role "
                    "WHERE user_id = %s AND is_active = 1 "
                    "ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (staff_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    # 缓存里存的可能是中文名(CSV) 或组代码(CSV)；归一化为组列表再返回。
                    # 未知项经白名单丢弃 = fail-closed（仅 public）。
                    codes = _normalize_dept_to_codes(row[0])
                    logger.info("用户权限组解析成功（缓存）: staff_id=%s → raw=%s（groups=%s）",
                                staff_id, row[0], codes)
                    return codes

            # 2. 本地没有，调钉钉 API 获取（dept_name 为该用户所有部门名的 CSV）
            user_info = _fetch_dingtalk_user_info(staff_id)
            if user_info:
                dept_name = user_info.get("dept_name", "")
                user_name = user_info.get("user_name", "")
                # 3. 缓存到 user_role 表（仅 cache-miss 分支，不会覆盖 seeded 行）
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO fuling_knowledge.user_role (user_id, user_name, dept_code, role, is_active)
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
                return _normalize_dept_to_codes(dept_name)
            else:
                logger.warning("用户未在 user_role 表中注册且 API 查询失败: staff_id=%s", staff_id)
                return []
        finally:
            conn.close()
    except Exception as e:
        logger.warning("查询用户部门失败 staff_id=%s: %s", staff_id, e)
        return []


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
                # 遍历完整 dept_id_list（H3），收集全部部门名 → CSV，支持多部门用户
                dept_id_list = result.get("dept_id_list", [])
                dept_names = []
                seen_names = set()
                for did in dept_id_list:
                    nm = _fetch_dept_name(token, did)
                    if nm and nm not in seen_names:
                        seen_names.add(nm)
                        dept_names.append(nm)
                dept_name = ",".join(dept_names)
                return {"user_name": user_name, "dept_name": dept_name}
            logger.warning("用户查询业务失败: errcode=%s errmsg=%s", data.get("errcode"), data.get("errmsg"))
            return None
        logger.warning("用户查询 HTTP 失败: %s", resp.text[:300])
        return None
    except Exception as e:
        logger.warning("用户查询异常: %s", e)
        return None


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
        from opensearch_pipeline.pipeline_nodes import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_name FROM fuling_knowledge.user_role WHERE user_id=%s "
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
