# -*- coding: utf-8 -*-
"""admin_notify.py — 管理员钉钉工作通知（RAG_ADMIN_NOTIFY，默认关）。

审批类事件（跨部门检索申请 / 知识贡献 / 待审批上传）此前只落库，管理员必须主动开
控制台才看得到待办——现实是没人天天开，审批流的时效全靠运气。本模块把三类事件
推成钉钉【工作通知】（topapi/message/corpconversation/asyncsend_v2，与机器人同一
应用凭证；须另配 RAG_DINGTALK_AGENT_ID=该应用的 AgentId，钉钉开发者后台可查）。

铁律（graceful degradation，与全仓约定一致）：
  - 全程 best-effort：flag 关 / 缺 agent_id / 收件人为空 / DB 或 HTTP 任何异常，
    都只落日志，**绝不**影响主流程（调用点全部在业务 commit 之后）；
  - 发送默认走后台 daemon 线程（请求路径零延迟）；tests 置 _SEND_ASYNC=False
    并 monkeypatch 本模块 _http_post（DashScope mock 同款 seam 约定）。

收件人解析（与授权体系同源，不另建名单）：
  - 部门事件 → dept_admin_grant（managed_owner_dept=该 owner_dept 且 is_active=1）；
  - 入库审批 → user_role（role='kb_admin' 且 is_active=1）。
"""

import os
import threading
from typing import List, Optional

import requests

from opensearch_pipeline.config import get_config

import logging

logger = logging.getLogger(__name__)

# tests 置 False：_dispatch 变同步直调，便于断言；生产恒 True（fire-and-forget）。
_SEND_ASYNC = True

_MAX_RECIPIENTS = 100        # asyncsend_v2 单次 userid_list 上限


def _enabled() -> bool:
    return os.environ.get("RAG_ADMIN_NOTIFY", "").strip().lower() in ("1", "true", "yes", "on")


def _agent_id() -> str:
    return os.environ.get("RAG_DINGTALK_AGENT_ID", "").strip()


def _kb_db() -> str:
    return get_config().rds.database


def _dept_label(code: str) -> str:
    """组码 → 中文标签（惰性取 api._KB_ACL_GROUP_LABELS，失败回组码——通知文案降级可读）。"""
    try:
        from opensearch_pipeline.api import _KB_ACL_GROUP_LABELS
        return _KB_ACL_GROUP_LABELS.get(code, code)
    except Exception:   # noqa: BLE001
        return code


def _http_post(url: str, payload: dict) -> dict:
    """HTTP seam（tests monkeypatch 这里）。"""
    resp = requests.post(url, json=payload, timeout=5)
    return resp.json() if resp.status_code == 200 else {"errcode": resp.status_code,
                                                        "errmsg": resp.text[:200]}


def _send_work_notice(user_ids: List[str], text: str) -> None:
    """发一条文本工作通知。仅由 _dispatch 调用（已在 try 保护内）。"""
    agent = _agent_id()
    if not agent:
        logger.debug("admin_notify: 未配置 RAG_DINGTALK_AGENT_ID，跳过发送")
        return
    from opensearch_pipeline.dingtalk_card import _get_access_token
    token = _get_access_token()
    if not token:
        logger.warning("admin_notify: 获取钉钉 access_token 失败，放弃本条通知")
        return
    data = _http_post(
        f"https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2?access_token={token}",
        {"agent_id": agent,
         "userid_list": ",".join(user_ids[:_MAX_RECIPIENTS]),
         "msg": {"msgtype": "text", "text": {"content": text}}},
    )
    if data.get("errcode") == 0:
        logger.info("admin_notify: 工作通知已发 %d 人", len(user_ids[:_MAX_RECIPIENTS]))
    else:
        logger.warning("admin_notify: 发送失败 errcode=%s errmsg=%s",
                       data.get("errcode"), str(data.get("errmsg"))[:200])


def _dispatch(user_ids: List[str], text: str) -> None:
    ids = []
    seen = set()
    for u in user_ids or []:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            ids.append(u)
    if not ids:
        logger.debug("admin_notify: 收件人为空，跳过（text=%s…）", text[:40])
        return
    if _SEND_ASYNC:
        threading.Thread(target=lambda: _safe_send(ids, text), daemon=True,
                         name="admin-notify").start()
    else:
        _safe_send(ids, text)


def _safe_send(ids: List[str], text: str) -> None:
    try:
        _send_work_notice(ids, text)
    except Exception as e:   # noqa: BLE001 —— 后台线程/同步兜底：绝不外抛
        logger.warning("admin_notify: 发送异常（忽略）: %s", e)


def _dept_admin_ids(owner_dept: str) -> List[str]:
    """该 owner_dept 的管理员 user_id 列表（dept_admin_grant 是唯一权威，与写授权同源）。"""
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT user_id FROM {_kb_db()}.dept_admin_grant "
                        "WHERE managed_owner_dept=%s AND is_active=1", (owner_dept,))
            return [r[0] for r in (cur.fetchall() or []) if r and r[0]]
    finally:
        conn.close()


def _node_admin_ids(dept_id: int):
    """覆盖该组织节点的管理员 user_id 列表 → `(ids, unavailable)`（方案 M6）。

    "覆盖" = 该节点的**祖先链（含自身）** ∩ `dept_admin_node_grant.managed_dept_id`（active）
    —— 与读侧 node 通道同一套展开（`resolve_ancestor_chains`），不另建名单。

    `unavailable=True` = 组织快照过期/链解析失败/DB 读失败 ⇒ **算不出收件人**（≠ 无人管辖）。
    调用方据此走 kb_admin 兜底：与 `_contrib_orphan_sql` 的 node 支 fail-open 对齐——队列
    对 kb_admin 可见了，就必须有人被叫到，否则「看得见但没人知道」。
    """
    try:
        from opensearch_pipeline.dept_ancestry import resolve_ancestor_chains
        from opensearch_pipeline.dingtalk_identity import _load_org_snapshot
        snap = _load_org_snapshot()
        if not snap.get("fresh"):
            logger.warning("admin_notify: 组织快照过期 ⇒ node 收件人算不出（走 kb_admin 兜底）")
            return [], True
        parents = snap.get("parents") or {}
        chain, ok = resolve_ancestor_chains([int(dept_id)], lambda d: parents.get(d))
        if not ok or not chain:
            logger.warning("admin_notify: 节点 %s 祖先链不可得 ⇒ 走 kb_admin 兜底", dept_id)
            return [], True
    except Exception as e:   # noqa: BLE001
        logger.warning("admin_notify: node 祖先链解析失败（走 kb_admin 兜底）: %s", e)
        return [], True
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                ph = ",".join(["%s"] * len(chain))
                cur.execute(f"SELECT DISTINCT user_id FROM {_kb_db()}.dept_admin_node_grant "
                            f"WHERE is_active=1 AND managed_dept_id IN ({ph})", tuple(chain))
                return [r[0] for r in (cur.fetchall() or []) if r and r[0]], False
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001 — 060 未 apply / 读失败 ⇒ 算不出，不是"无人管辖"
        logger.warning("admin_notify: dept_admin_node_grant 读取失败（走 kb_admin 兜底）: %s", e)
        return [], True


def _node_label(dept_id: int) -> str:
    """节点 id → 部门名（dept_dim 现查；查不到回落 `节点 <id>`——通知文案降级可读，绝不空）。"""
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT name FROM {_kb_db()}.dept_dim WHERE dept_id=%s LIMIT 1",
                            (int(dept_id),))
                row = cur.fetchone()
                if row and row[0]:
                    return str(row[0])
        finally:
            conn.close()
    except Exception as e:   # noqa: BLE001
        logger.debug("admin_notify: dept_dim 名字解析失败 dept_id=%s: %s", dept_id, e)
    return f"节点 {int(dept_id)}"


def _kb_admin_ids() -> List[str]:
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT user_id FROM {_kb_db()}.user_role "
                        "WHERE role='kb_admin' AND is_active=1")
            return [r[0] for r in (cur.fetchall() or []) if r and r[0]]
    finally:
        conn.close()


def _contrib_author_question(contribution_id: str) -> tuple:
    """贡献行的 (author_id, question)（批次ε-2：审核结果通知提交人用；查不到=(None, '')）。"""
    from opensearch_pipeline.db import _get_db_conn
    op_db = get_config().rds.operation_database
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT author_id, question FROM {op_db}.kb_contribution"
                        " WHERE contribution_id=%s LIMIT 1", (contribution_id,))
            row = cur.fetchone()
            return (row[0], row[1] or "") if row else (None, "")
    finally:
        conn.close()


def _doc_title(doc_id: str) -> str:
    from opensearch_pipeline.db import _get_db_conn
    conn = _get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT title, original_filename FROM {_kb_db()}.document_meta "
                        "WHERE doc_id=%s LIMIT 1", (doc_id,))
            row = cur.fetchone()
            return (row[0] or row[1] or doc_id) if row else doc_id
    finally:
        conn.close()


# ── 公开挂点（全部 no-raise；调用点在业务 commit 之后）──────────────────────────

def notify_access_request(owner_dept: str, doc_id: str, requester_depts: str) -> None:
    """跨部门检索申请已提交 → 通知文档归属部门管理员。"""
    if not _enabled():
        return
    try:
        ids = _dept_admin_ids(owner_dept)
        title = _doc_title(doc_id)
        req_label = "、".join(_dept_label(p.strip()) for p in str(requester_depts or "").split(",")
                              if p.strip()) or "其他部门"
        _dispatch(ids, f"【富岭知识库】跨部门检索申请：{req_label} 申请检索《{title}》"
                       f"（归属 {_dept_label(owner_dept)}）。请到知识库控制台「授权申请」处理。")
    except Exception as e:   # noqa: BLE001
        logger.warning("admin_notify: access_request 通知失败（忽略）: %s", e)


def notify_contribution(category_dept: str, question: str,
                        category_dept_id: Optional[int] = None) -> None:
    """新知识贡献待审核 → 通知归属管理员；无人管辖 → kb_admin 兜底。

    批次δ-3 顺带修复的既有缺口：此前孤儿部门收件人为空即静默跳过——兜底审核队列既已
    归 kb_admin（contributions/pending 的孤儿作用域），新单必须有人被叫到。

    方案 M6/M7（2026-08-07）：`category_dept_id` 非空 ⇒ **node 轴**，按覆盖该节点的管辖根
    持有者取收件人；文案也按轴分支（Codex C10 推翻了 v2 的全局统一文案）——
      · node 无人覆盖 → 「归属节点不在任何部门管理员的管辖范围内」
      · legacy 无人管辖 → 保留原组码语义文案
      · 收件人算不出（快照过期等）→ 通用兜底句，不谎称"无人管辖"
    """
    if not _enabled():
        return
    try:
        unavailable = False
        if category_dept_id:
            ids, unavailable = _node_admin_ids(int(category_dept_id))
            label = _node_label(int(category_dept_id))
        else:
            ids = _dept_admin_ids(category_dept)
            label = _dept_label(category_dept)
        fallback = not ids
        if fallback:
            ids = _kb_admin_ids()
        q = (question or "").strip()
        q = q[:40] + ("…" if len(q) > 40 else "")
        if not fallback:
            suffix = ""
        elif unavailable:
            suffix = "（该贡献当前未匹配到可审核的部门管理员，由你兜底审核）"
        elif category_dept_id:
            suffix = "（归属节点不在任何部门管理员的管辖范围内，由你兜底审核）"
        else:
            suffix = "（该部门暂无部门管理员，由你兜底审核）"
        _dispatch(ids, f"【富岭知识库】新知识贡献待审核：「{q}」"
                       f"（归属 {label}）{suffix}。请到控制台「知识贡献」审核采纳。")
    except Exception as e:   # noqa: BLE001
        logger.warning("admin_notify: contribution 通知失败（忽略）: %s", e)


def notify_contribution_result(contribution_id: str, outcome: str, note: str = "",
                               error: str = "", actor_id: str = "") -> None:
    """审核结果 → 通知【提交人】（批次ε-2 Round1：补齐激励闭环的「告知」半环——此前采纳/
    待放行/入库失败/驳回四种结果全部静默，作者只能自己回控制台翻「我的贡献」）。

    outcome ∈ accepted | pending_approval | failed | rejected，文案四分支互斥可区分；
    note/error 空值均有兜底句，绝不拼空串。actor_id=操作人：审核人=作者（自采/自驳/作者自己
    点重试）时跳过——自己的操作无需通知自己。author/question 模块内自查（_doc_title 同款
    模式），routes 挂点零 SELECT 负担；全程 best-effort no-raise。"""
    if not _enabled():
        return
    try:
        author_id, question = _contrib_author_question(contribution_id)
        if not author_id or author_id == (actor_id or ""):
            return
        q = (question or "").strip()
        q = q[:40] + ("…" if len(q) > 40 else "")
        if outcome == "accepted":
            # 措辞不做无条件承诺：PII 隔离等管线判定发生在本推送之后的异步 DAG 里（ε-3 审计 B-1）
            text = (f"【富岭知识库】你的知识贡献「{q}」已被采纳，正在入库，"
                    "入库完成后即可被检索到。感谢贡献！")
        elif outcome == "pending_approval":
            text = (f"【富岭知识库】你的知识贡献「{q}」已被采纳（全员公开），"
                    "需知识库管理员放行后入库，请留意后续状态。")
        elif outcome == "failed":
            reason = (error or "").strip()[:80] or "系统原因"
            # ε-5 R1 措辞修正：重试端点是管理员专属，对作者说「你可重试」是假承诺
            text = (f"【富岭知识库】你的知识贡献「{q}」已被采纳但入库失败（{reason}），"
                    "可到控制台「知识贡献」修改后重新提交，或联系管理员重试。")
        elif outcome == "rejected":
            reason = (note or "").strip()[:80] or "未填写理由"
            text = (f"【富岭知识库】你的知识贡献「{q}」未被采纳（{reason}）。"
                    "可在控制台「知识贡献」修改后重新提交。")
        else:
            return
        _dispatch([author_id], text)
    except Exception as e:   # noqa: BLE001
        logger.warning("admin_notify: contribution_result 通知失败（忽略）: %s", e)


def notify_upload_approval(owner_dept: str, title: str,
                           owner_dept_id: Optional[int] = None) -> None:
    """上传/升版进入待审批（涉公开等）→ 通知 kb_admin。

    `owner_dept_id` 非空 ⇒ node 归属，标签走 `dept_dim`（组码标签词表里没有节点，
    直接喂 `_dept_label` 会把空串原样印进文案）。"""
    if not _enabled():
        return
    try:
        ids = _kb_admin_ids()
        label = _node_label(int(owner_dept_id)) if owner_dept_id else _dept_label(owner_dept)
        _dispatch(ids, f"【富岭知识库】新上传待审批：《{title or '未命名文档'}》"
                       f"（归属 {label}）。请到控制台「待审批」处理。")
    except Exception as e:   # noqa: BLE001
        logger.warning("admin_notify: upload_approval 通知失败（忽略）: %s", e)
