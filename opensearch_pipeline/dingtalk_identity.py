# -*- coding: utf-8 -*-
"""
dingtalk_identity.py — 钉钉用户身份解析（OpenAPI 通讯录 / 免登）

这是机器人与小程序**共用**的身份基础设施，与「机器人收发消息」解耦：
  - _resolve_user_dept(staff_id)        : userid → 部门名称（RDS 缓存优先 + 钉钉 API 回退）
  - _fetch_dingtalk_user_info(user_id)  : 钉钉 user/get → {user_name, dept_name}
  - _fetch_dept_name(token, dept_id)    : 部门 ID → 部门名称
  - _get_miniapp_access_token()         : 小程序应用 access_token（独立凭证，回退机器人应用）
  - _exchange_authcode_for_userid(code) : 小程序免登 authCode → userid（getuserinfo）
  - _resolve_user_identity(userid)      : userid → {dept, name}（供 /api/auth/dingtalk 签发令牌）

设计要点：
  - 部门是**名称字符串**（如「行政部」），与 HA3 owner_dept 对齐 —— 不用 dept_id。
  - 模块级无钉钉依赖（access_token / DB 连接均惰性导入），避免循环引用。
  - 模拟模式（simulate_api）下不发真实请求，返回可配置的测试身份，便于离线联调。
"""

import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import requests

from opensearch_pipeline.config import get_config

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 用户部门解析（机器人 + 小程序共用）
# ═══════════════════════════════════════════════════════════════

def _resolve_user_dept(staff_id: str) -> Optional[str]:
    """
    从 RDS user_role 表查询用户所属部门。
    如果 user_role 中不存在，自动通过钉钉 API 获取并缓存。

    查询失败或用户不存在时返回 None，调用方会降级为只返回 public 文档。
    """
    if not staff_id or staff_id.startswith("$:"):
        return None

    try:
        from opensearch_pipeline.pipeline_nodes import _get_db_conn

        conn = _get_db_conn()
        try:
            # 1. 先查本地缓存（按最新行取值：历史上 user_id 无唯一键可能产生重复行，
            #    见 schema/003_user_role_unique.sql；显式排序保证确定性）
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT dept_code FROM fuling_knowledge.user_role "
                    "WHERE user_id = %s AND is_active = 1 "
                    "ORDER BY updated_at DESC, id DESC LIMIT 1",
                    (staff_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    logger.info("用户部门解析成功（缓存）: staff_id=%s → dept=%s", staff_id, row[0])
                    return row[0]

            # 2. 本地没有，调钉钉 API 获取
            user_info = _fetch_dingtalk_user_info(staff_id)
            if user_info:
                dept_name = user_info.get("dept_name", "")
                user_name = user_info.get("user_name", "")
                # 3. 缓存到 user_role 表
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
                return dept_name or None
            else:
                logger.warning("用户未在 user_role 表中注册且 API 查询失败: staff_id=%s", staff_id)
                return None
        finally:
            conn.close()
    except Exception as e:
        logger.warning("查询用户部门失败 staff_id=%s: %s", staff_id, e)
        return None


def _fetch_dingtalk_user_info(user_id: str) -> Optional[dict]:
    """
    通过钉钉 API 获取用户信息（姓名、部门等）。

    Returns:
        {"user_name": "张三", "dept_name": "行政部"} 或 None
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
                dept_name = ""
                # 获取部门 ID 列表，取第一个部门名称
                dept_id_list = result.get("dept_id_list", [])
                if dept_id_list:
                    dept_name = _fetch_dept_name(token, dept_id_list[0])
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


def _resolve_user_identity(userid: str) -> Dict[str, Optional[str]]:
    """解析用户身份：返回 {"dept": <部门名称>, "name": <显示名>}。

    复用 _resolve_user_dept 的「RDS 缓存优先 + 钉钉 API 回退」逻辑获取部门名称（与 HA3
    owner_dept 对齐的名称字符串，不是 dept_id）；显示名从 user_role 缓存中取。
    模拟模式下从 RAG_SIM_USER_DEPT 取部门，便于离线联调权限过滤。
    """
    if not userid:
        return {"dept": None, "name": ""}

    try:
        if get_config().simulate_api:
            return {
                "dept": os.environ.get("RAG_SIM_USER_DEPT") or None,
                "name": userid,
            }
    except Exception:
        pass

    dept = _resolve_user_dept(userid)  # 名称字符串；含缓存 + API 回退（并顺带缓存 user_name）
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
