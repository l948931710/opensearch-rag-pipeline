# -*- coding: utf-8 -*-
"""
routes/kb_console.py — KB 控制台管理域：whoami/组织树/我的文档/浏览、
统计/成效/治理看板、配置/版本历史/文档状态、上传/登记/审批/退役/恢复。

F-A2 结构债拆分（2026-07-01）：从 api.py 机械搬移，行为不变。api.py 底部
include_router 并 re-export 全部端点函数/模型（tests 直接调用 api.<endpoint> /
引用 api.Kb* 模型）。本模块**不得**定义或遮蔽任何被 tests monkeypatch 的
api 属性（规则见 routes/__init__.py）。
"""

import os
import threading
import time
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from opensearch_pipeline.config import get_config
from opensearch_pipeline.qa_logger import _op_db
from opensearch_pipeline.reindex_states import ChunkIndexStatus, DocVersionIndexStatus, sql_in_list
from opensearch_pipeline.request_context import get_request_id

# api 驻留共享件（模型/助手/依赖）。from-import 拷贝绑定在这里是安全的：
# 这些名字均不在 tests 的 api monkeypatch 清单内（见 routes/__init__.py）。
from opensearch_pipeline.api import (
    Identity,
    KbDocItem,
    KbDocStatusResponse,
    KbDupDoc,
    KbMyDocsResponse,
    KbOrgTreeResponse,
    KbVersionHistoryResponse,
    KbVersionItem,
    _KB_ACL_GROUP_LABELS,
    _KB_BAD_BADGES,
    _KB_BADGE_CASE_SQL,
    _KB_MAX_OFFSET,
    _enforce_rate_limit,
    _kb_can_manage,
    _kb_content_dups,
    _kb_db,
    _kb_owner_scope_sql,
    _kb_status_badge,
    _load_org_tree_snapshot,
    _require_kb_admin,
    _require_kb_console,
    current_identity,
    logger,
)

router = APIRouter()


class KbWhoamiResponse(BaseModel):
    user_id: str
    display_name: str = ""
    role: str = "employee"
    can_manage_kb: bool = False
    managed_owner_depts: List[str] = Field(default_factory=list)
    # 用户所属 ACL 读权限组（仅展示/审计，写授权不据此推导）。与 /api/auth/dingtalk 的 acl_groups 同源，
    # 补齐后 web-view ?token= 直登路径也能拿到部门信息（员工概览「我的部门」依赖它）。
    acl_groups: List[str] = Field(default_factory=list)


@router.get("/api/kb/whoami", response_model=KbWhoamiResponse)
def kb_whoami(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """当前 Bearer 身份的角色/可管理范围（DB 现查）。供 web-view 上传页用传入 token 拿身份，
    无需在 H5 里再走 requestAuthCode 免登（token 由小程序传入）。仅要求登录，不要求管理员。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    from opensearch_pipeline.dingtalk_identity import resolve_kb_identity
    from opensearch_pipeline.kb_authz import can_access_console, managed_owner_depts
    kb = resolve_kb_identity(identity.user_id)
    return KbWhoamiResponse(
        user_id=kb.user_id, display_name=kb.name or "", role=kb.role,
        can_manage_kb=can_access_console(kb), managed_owner_depts=managed_owner_depts(kb),
        acl_groups=list(kb.acl_groups),
    )


@router.get("/api/kb/org-tree", response_model=KbOrgTreeResponse)
def kb_org_tree(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """权限选择器数据：10 个 ACL 组 + 钉钉部门→组映射 + 调用者自身可管理/可授权范围 + org 快照。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.dingtalk_identity import _DEPT_NAME_TO_GROUPS
    from opensearch_pipeline.kb_authz import managed_owner_depts, grantable_owner_depts
    return KbOrgTreeResponse(
        acl_groups=[{"code": c, "label": _KB_ACL_GROUP_LABELS.get(c, c)}
                    for c in sorted(_KB_ACL_GROUP_LABELS)],
        dept_name_to_groups={k: list(v) for k, v in _DEPT_NAME_TO_GROUPS.items()},
        my_role=kb.role,
        my_managed_owner_depts=managed_owner_depts(kb),
        my_grantable_owner_depts=grantable_owner_depts(kb),
        org_tree=_load_org_tree_snapshot(),
    )


def _kb_usage_enrich(cur, doc_ids):
    """页内文档的利用度：doc_id → (被引用问答数, 最近被引用时间)。

    仅在 qa_facts.fact_join_enabled()（RAG_QA_FACT_JOIN 开 + qa_retrieved_doc 表探测通过）
    时启用——索引点查，页内 ≤50 个 id 一次聚合，成本恒定。不走 JSON_TABLE 回退：那要
    全表展开 qa_session_log 才能按 doc 过滤，不适合每次列表加载都付。
    返回 None=数据不可用（flag 关/表缺/查询失败 → cited_count=None，前端不显示）；
    返回 dict=查询成功（缺席的 doc = 0 次，真·从未被引用）——0 与「不知道」必须可区分。
    fail-open：任何异常绝不影响台账主查询。"""
    if not doc_ids:
        return {}
    try:
        from opensearch_pipeline.qa_facts import FACT_TABLE, fact_join_enabled
        if not fact_join_enabled():
            return None
        ph = ",".join(["%s"] * len(doc_ids))
        cur.execute(
            f"SELECT jt.doc_id, COUNT(DISTINCT jt.message_id), MAX(jt.created_at) "
            f"FROM {_op_db()}.{FACT_TABLE} jt "
            f"WHERE jt.cited=1 AND jt.doc_id IN ({ph}) GROUP BY jt.doc_id",
            tuple(doc_ids))
        return {r[0]: (int(r[1] or 0), str(r[2] or "")) for r in (cur.fetchall() or [])}
    except Exception as e:   # noqa: BLE001
        logger.debug("利用度 enrich 失败（fail-open）: %s", e)
        return None


def _kb_like_search_sql(q: str):
    """文档名搜索片段（标题/原始文件名子串）：显式 '!' 转义 LIKE 通配符（% _ !），
    不依赖 DB sql_mode（NO_BACKSLASH_ESCAPES 开启时反斜杠转义会失效）。空 q → ('', [])。"""
    q = (q or "").strip()[:80]
    if not q:
        return "", []
    esc = q.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    like = "%" + esc + "%"
    return "AND (m.title LIKE %s ESCAPE '!' OR m.original_filename LIKE %s ESCAPE '!')", [like, like]


def _kb_ledger_filter_sql(perm: str = "", badge: str = "", cited: str = ""):
    """台账结构化筛选（可见范围 / 徽章 / 利用度）→ (SQL 片段, 参数列表)；空参数不产子句。
    引用 m/v 别名（与 my-docs/browse 主查询一致）。徽章走 _KB_BADGE_CASE_SQL（_kb_status_badge
    的 SQL 镜像）；利用度用事实表相关 EXISTS（仅 RAG_QA_FACT_JOIN 开时生效，否则忽略——回退
    JSON_TABLE 做相关子查询太贵）。全部服务端过滤 → 计数/翻页覆盖全库，不再只筛已加载页（#7）。"""
    from opensearch_pipeline.kb_authz import _SANITIZE_RE
    clauses: List[str] = []
    params: list = []
    if perm:
        p = _SANITIZE_RE.sub("", perm.strip())[:32]
        if p:
            clauses.append("AND m.permission_level = %s")
            params.append(p)
    if badge:
        b = badge.strip()[:16]
        if b == "异常":
            # 「异常」聚合筛选（前端待办条/台账 chip 同口径）：一次圈出全部坏徽章
            ph = ",".join(["%s"] * len(_KB_BAD_BADGES))
            clauses.append(f"AND ({_KB_BADGE_CASE_SQL}) IN ({ph})")
            params.extend(_KB_BAD_BADGES)
        else:
            clauses.append(f"AND ({_KB_BADGE_CASE_SQL}) = %s")
            params.append(b)
    if cited in ("never", "used"):
        try:
            from opensearch_pipeline.qa_facts import FACT_TABLE, fact_join_enabled
            if fact_join_enabled():
                exists = (f"EXISTS (SELECT 1 FROM {_op_db()}.{FACT_TABLE} jt"
                          " WHERE jt.doc_id = m.doc_id AND jt.cited=1)")
                clauses.append(f"AND {exists}" if cited == "used" else f"AND NOT {exists}")
        except Exception:   # noqa: BLE001
            pass   # fact 不可用 → 忽略利用度筛选（fail-open；客户端仍可按已加载页兜底筛）
    return ((" " + " ".join(clauses)) if clauses else ""), params


def _kb_owner_facet_sql(owner_dept: str):
    """按归属部门精确筛选（含生产子线，如 production_mold）→ (SQL 片段, 参数)。
    参数化本身防注入，这里再剥离注入字符 + 限长做纵深防御。返回 (None, None) = 非法 facet（fail-closed）。"""
    from opensearch_pipeline.kb_authz import _SANITIZE_RE
    facet = _SANITIZE_RE.sub("", (owner_dept or "").strip())[:64]
    if owner_dept and not facet:
        return None, None   # 传了但清洗后为空 = 非法 → 调用方 fail-closed 空
    if facet:
        return "AND m.owner_dept = %s", [facet]
    return "", []


@router.get("/api/kb/my-docs", response_model=KbMyDocsResponse)
def kb_my_docs(request: Request, limit: int = 20, offset: int = 0, q: str = "",
               owner_dept: str = "", perm: str = "", badge: str = "", cited: str = "",
               identity: Optional[Identity] = Depends(current_identity)):
    """管理员可管理的文档列表（kb_admin 全量；dept_admin 限其 managed owner_dept）。只读。

    q：文档名搜索（标题 / 原始文件名子串匹配），用于"是否已有现存版本"自查。
    owner_dept/perm/badge/cited：结构化筛选，全部【服务端】执行 → 覆盖全库而非只筛已加载页（#7）。
      badge = 用户可读徽章（已上线/未入索引/处理失败/…，经 _KB_BADGE_CASE_SQL 镜像判定）；
      cited = never（从未被引用，退役候选）/ used（有引用）——仅 RAG_QA_FACT_JOIN 开时生效。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    limit = max(1, min(limit, 50))
    offset = max(0, min(offset, _KB_MAX_OFFSET))   # 上界防深分页扫表（全库 ~1600，1万 offset 绰绰有余，G7）
    clause, params = _kb_owner_scope_sql(kb, "m.owner_dept")
    search_clause, search_params = _kb_like_search_sql(q)
    owner_clause, owner_params = _kb_owner_facet_sql(owner_dept)
    if owner_clause is None:
        return KbMyDocsResponse(items=[], has_more=False)   # 非法 facet → fail-closed 空
    filter_clause, filter_params = _kb_ledger_filter_sql(perm, badge, cited)
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT m.doc_id, m.title, m.original_filename, m.owner_dept,
                           m.permission_level, m.current_version_no, m.status, m.updated_at,
                           v.content_process_status, v.index_status, v.publish_status,
                           v.chunk_status, v.content_process_error
                    FROM {_kb_db()}.document_meta m
                    LEFT JOIN {_kb_db()}.document_version v
                      ON v.doc_id = m.doc_id AND v.version_no = m.current_version_no
                    WHERE 1=1 {clause} {search_clause} {owner_clause} {filter_clause}
                    ORDER BY (m.status='active') DESC, m.updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (*params, *search_params, *owner_params, *filter_params, limit + 1, offset),
                )
                rows = cur.fetchall()
                usage = _kb_usage_enrich(cur, [r[0] for r in rows[:limit]])
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_my_docs 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档列表查询失败 (trace: {trace_id})")

    has_more = len(rows) > limit
    items = []
    for r in rows[:limit]:
        (doc_id, title, fname, owner, perm, cur_ver, status, updated, cps, ixs, pubs, chks, cpe) = r
        _u = usage.get(doc_id) if usage is not None else None
        items.append(KbDocItem(
            doc_id=doc_id or "", title=title or "", original_filename=fname or "",
            owner_dept=owner or "", permission_level=perm or "public",
            current_version_no=int(cur_ver or 1), status=status or "active",
            status_badge=_kb_status_badge(cps, ixs, status, publish_status=pubs, chunk_status=chks),
            updated_at=str(updated) if updated else "",
            cited_count=(None if usage is None else (_u[0] if _u else 0)),
            last_cited_at=(_u[1] if _u else ""),
            # 驳回原因只在被驳回态外露（其他失败态的 content_process_error 是内部诊断文案，不外发）
            reject_reason=(str(cpe)[:200] if (cps == "REJECTED" and cpe) else ""),
        ))
    return KbMyDocsResponse(items=items, has_more=has_more)


@router.get("/api/kb/browse", response_model=KbMyDocsResponse)
def kb_browse(request: Request, scope: str = "all", q: str = "", owner_dept: str = "",
              perm: str = "", badge: str = "", cited: str = "",
              limit: int = 20, offset: int = 0,
              identity: Optional[Identity] = Depends(current_identity)):
    """全部门只读浏览：部门管理员看【其他部门】文档（可见、不可操作）。只读。

    与 my-docs 的根本区别——**绝不复用 _kb_owner_scope_sql（写作用域）**：
      · 可见范围 = 全部门（不按 managed 过滤）；可操作(can_manage) 仍 = 写作用域 managed。
      · 只列 permission_level ∈ {public, dept_internal}（**允许清单**，restricted 及任何未知值
        一律排除）——审计/法务/总经办等 restricted 敏感件连标题都不外露（锁定决策 2026-06-26）。
      · 只列 status='active'（退役件无需被申请检索）。
      · 每行带 can_manage（kb_admin 全 True；dept_admin 仅其 managed owner_dept）。
    申请其他部门文档检索 → 授权申请（Phase C）；真正放行检索 → allowed_depts 接入检索（Phase D）。
    employee/匿名在任何 DB 查询【之前】被 401/403（_require_kb_console 先行）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    if scope != "all":
        # 目前仅 all 语义（本部门用 my-docs）；非法 scope fail-closed 空，避免静默当全量。
        return KbMyDocsResponse(items=[], has_more=False)
    limit = max(1, min(limit, 50))
    offset = max(0, min(offset, _KB_MAX_OFFSET))   # 上界防深分页扫表（G7）

    owner_clause, owner_params = _kb_owner_facet_sql(owner_dept)
    if owner_clause is None:
        return KbMyDocsResponse(items=[], has_more=False)   # 非法 facet → fail-closed 空
    search_clause, search_params = _kb_like_search_sql(q)
    # perm facet 受浏览白名单收窄：browse 恒只列 public/dept_internal，故 restricted 等即使传入也无效。
    filter_clause, filter_params = _kb_ledger_filter_sql(perm, badge, cited)

    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT m.doc_id, m.title, m.original_filename, m.owner_dept,
                           m.permission_level, m.current_version_no, m.status, m.updated_at,
                           v.content_process_status, v.index_status, v.publish_status,
                           v.chunk_status
                    FROM {_kb_db()}.document_meta m
                    LEFT JOIN {_kb_db()}.document_version v
                      ON v.doc_id = m.doc_id AND v.version_no = m.current_version_no
                    WHERE m.status='active'
                      AND m.permission_level IN ('public','dept_internal')
                      {owner_clause} {search_clause} {filter_clause}
                    ORDER BY m.owner_dept ASC, m.updated_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (*owner_params, *search_params, *filter_params, limit + 1, offset),
                )
                rows = cur.fetchall()
                usage = _kb_usage_enrich(cur, [r[0] for r in rows[:limit]])
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_browse 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"全部门浏览查询失败 (trace: {trace_id})")

    has_more = len(rows) > limit
    items = []
    for r in rows[:limit]:
        (doc_id, title, fname, owner, perm, cur_ver, status, updated, cps, ixs, pubs, chks) = r
        _u = usage.get(doc_id) if usage is not None else None
        items.append(KbDocItem(
            doc_id=doc_id or "", title=title or "", original_filename=fname or "",
            owner_dept=owner or "", permission_level=perm or "dept_internal",
            current_version_no=int(cur_ver or 1), status=status or "active",
            status_badge=_kb_status_badge(cps, ixs, status, publish_status=pubs, chunk_status=chks),
            updated_at=str(updated) if updated else "",
            can_manage=_kb_can_manage(kb, owner or ""),
            cited_count=(None if usage is None else (_u[0] if _u else 0)),
            last_cited_at=(_u[1] if _u else ""),
        ))
    return KbMyDocsResponse(items=items, has_more=has_more)


class KbStatsResponse(BaseModel):
    total: int = 0
    active: int = 0
    retired: int = 0
    chunks: int = 0                      # 作用域内当前已索引分块数（is_active=1 AND index_status='INDEXED'）
    new_this_month: int = 0              # 本月新增文档数（document_meta.created_at 落在当月，active）
    by_badge: Dict[str, int] = Field(default_factory=dict)
    # 归属部门 facet（全作用域去重，含生产子线）：台账「按归属筛选」下拉的全库口径来源，
    # 不再只从已加载页派生（否则 >50 篇时下拉漏掉未翻到的部门）。#7
    owner_depts: List[str] = Field(default_factory=list)


@router.get("/api/kb/stats", response_model=KbStatsResponse)
def kb_stats(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """管理范围内文档聚合（真实总数 + 状态分布 + 已索引分块数 + 归属 facet），不受 my-docs 的 50 上限影响。

    只读、按 owner 作用域过滤（与 my-docs 同一 _kb_owner_scope_sql，不会越权统计他部门）；
    徽章在 Python 端按与 my-docs 相同的 _kb_status_badge 复算（含 chunk_status，故 0-chunk 文档
    的「未入索引」口径与台账徽章一致，不再分叉）。台账的状态 chip 计数 / 异常文档待办数 / 归属下拉
    都以此为全库真值源，替代只覆盖已加载页的客户端计数（#7）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    clause, params = _kb_owner_scope_sql(kb, "m.owner_dept")
    ck_clause, ck_params = _kb_owner_scope_sql(kb, "owner_dept")   # chunk_meta.owner_dept 同口径作用域
    dm_clause, dm_params = _kb_owner_scope_sql(kb, "owner_dept")   # document_meta.owner_dept（本月新增计数）
    from datetime import date
    month_start = date.today().replace(day=1).isoformat()         # 当月首日；以参数传入避免 % 转义坑
    chunks = new_this_month = 0
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT m.status, v.content_process_status, v.index_status, v.publish_status,
                           v.chunk_status, m.owner_dept
                    FROM {_kb_db()}.document_meta m
                    LEFT JOIN {_kb_db()}.document_version v
                      ON v.doc_id = m.doc_id AND v.version_no = m.current_version_no
                    WHERE 1=1 {clause}
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
                # 当前已索引分块总数（设计「全库已索引 chunk」口径）；取数失败仅置 0，不拖垮主统计。
                try:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {_kb_db()}.chunk_meta "
                        f"WHERE is_active=1 AND index_status='{ChunkIndexStatus.INDEXED}' {ck_clause}",
                        tuple(ck_params),
                    )
                    chunks = int((cur.fetchone() or (0,))[0] or 0)
                except Exception as e:
                    logger.warning("kb_stats 分块计数失败: %s", e)
                # 本月新增文档数（设计「+N 本月新增」徽标）；月首日以参数传入；取数失败仅置 0。
                try:
                    cur.execute(
                        f"SELECT COUNT(*) FROM {_kb_db()}.document_meta "
                        f"WHERE created_at >= %s AND status='active' {dm_clause}",
                        tuple([month_start] + dm_params),
                    )
                    new_this_month = int((cur.fetchone() or (0,))[0] or 0)
                except Exception as e:
                    logger.warning("kb_stats 本月新增计数失败: %s", e)
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_stats 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"统计查询失败 (trace: {trace_id})")
    active = retired = 0
    by_badge: Dict[str, int] = {}
    owner_set = set()
    for row in rows:
        status, cps, ixs, pubs, chks, owner = row[0], row[1], row[2], row[3], row[4], row[5]
        if (status or "active") == "active":
            active += 1
        else:
            retired += 1
        # chunk_status 一并传入 → 0-chunk/跳过版本如实归「未入索引」，与台账徽章口径一致（此前漏传
        # 会把这些文档误计成「处理中」，看板分布与台账 chip 对同一文档分叉）。
        badge = _kb_status_badge(cps, ixs, status, publish_status=pubs, chunk_status=chks)
        by_badge[badge] = by_badge.get(badge, 0) + 1
        if owner:
            owner_set.add(owner)
    return KbStatsResponse(total=len(rows), active=active, retired=retired, chunks=chunks,
                           new_this_month=new_this_month, by_badge=by_badge,
                           owner_depts=sorted(owner_set))


# ─────────────────────────────────────────────────────────────────────────────
# Phase E — 概览看板的真实数据（不造数）。两个只读聚合端点，口径全部来自真实 RDS 表：
#   GET /api/kb/insights    —— owner 作用域的「知识使用成效 + 知识缺口」（dept_admin 看本部门、
#                              kb_admin 看全库；经 retrieved_docs_json→doc_id→owner_dept 归属）
#   GET /api/kb/governance  —— 全库运行健康 / 治理风险 / 部门覆盖（仅 kb_admin）
#
# 关键事实（scratch/phase_e_data_probe.py 实测 + qa-log-analytics-gotchas）：
#  · qa_session_log / user_feedback / escalation_ticket 在 fuling_operation；document_meta /
#    chunk_meta / pipeline_run / document_sensitive_finding 在 fuling_knowledge —— 同实例可跨库 JOIN。
#  · retrieved_docs_json 元素只留 doc_id 等 7 键、**不含 owner_dept** → 必须 JOIN document_meta 取归属。
#    JSON_TABLE 抽出的串默认 utf8mb4_0900_ai_ci，与 document_meta.doc_id(unicode_ci) 直接 JOIN 报
#    1267（kb_access_request 同坑），必须 CONVERT(... USING utf8mb4) COLLATE utf8mb4_unicode_ci。
#  · answer_status ∈ {SUCCESS, NO_RESULT, REFUSAL, LLM_ERROR}（无裸 'ERROR'，错误用 LIKE '%ERROR%'）。
#  · created_at 是 SAE 容器太平洋时间：日历分桶用 CONVERT_TZ(created_at,'America/Los_Angeles','Asia/Shanghai')
#    —— DST-correct（夏令时 +15h / 冬令时 +16h）；旧硬编码 +15h 在美国冬令时(PST)会有 1 小时跨天偏移。
#    依赖 RDS 已加载具名时区表（已核实生产可用）。
#  · 每个子查询独立 try/except：单指标取数失败只让该指标诚实空，不拖垮整块看板（auxiliary fail-open）。
# ─────────────────────────────────────────────────────────────────────────────
_KB_INSIGHTS_WINDOW_DAYS = 30

# 点踩原因码 → 中文标签（差评复核逐条 + 治理聚合「点踩原因分布」共用；null/未知码归「未注明/其他」）。
# feedback_reason 是逗号拼接的多选码（见 feedback_handler 写侧）。
_KB_DOWNVOTE_REASON_LABELS = {
    "inaccurate": "不准确", "irrelevant": "不相关", "incomplete": "不完整",
    "outdated": "已过时", "not_found": "未找到", "other": "其他",
}


def _kb_reason_labels(reason: str) -> List[str]:
    """把逗号拼接的原因码翻成中文标签列表（去空、保序、去重）。空 → []。"""
    out: List[str] = []
    for code in (reason or "").split(","):
        code = code.strip()
        if not code:
            continue
        label = _KB_DOWNVOTE_REASON_LABELS.get(code, code)
        if label not in out:
            out.append(label)
    return out


# P1-09：PDF 原生抽取页上限（治理看板「截断文档数」判据）。与抽取侧共用同一
# config 值（G2 env 化：RAG_PDF_NATIVE_MAX_PAGES），消除此前双处硬编码漂移风险；
# 经 config 读取而非 import extraction，保持 serving 进程不拖重抽取依赖（F-A1 拆分意图）。


def _pdf_native_max_pages() -> int:
    from opensearch_pipeline.config import get_config
    return int(getattr(get_config(), "pdf_native_max_pages", 200) or 200)

# 看板聚合 TTL 缓存（性能第一梯队 #6）：insights/governance 每请求现算 4-14 条聚合
# 子查询（含 JSON_TABLE 跨库 JOIN），30 天窗口对分钟级 staleness 完全不敏感。
# 键含作用域（dept_admin 的 managed 部门集 / kb_admin=GLOBAL），角色校验先于缓存读；
# 子查询有失败（降级响应）不缓存。RAG_KB_DASHBOARD_CACHE_TTL=0 关闭；conftest 每测清空。
_dashboard_cache: dict = {}
_dashboard_cache_lock = threading.Lock()


def _dashboard_cache_ttl() -> float:
    try:
        return float(os.environ.get("RAG_KB_DASHBOARD_CACHE_TTL", "60"))
    except ValueError:
        return 60.0


def _dashboard_cache_clear() -> None:
    with _dashboard_cache_lock:
        _dashboard_cache.clear()


def _dashboard_cache_get(key):
    if _dashboard_cache_ttl() <= 0:
        return None
    with _dashboard_cache_lock:
        ent = _dashboard_cache.get(key)
        if ent is not None and ent[0] > time.time():
            return ent[1]
    return None


def _dashboard_cache_put(key, value) -> None:
    ttl = _dashboard_cache_ttl()
    if ttl <= 0:
        return
    with _dashboard_cache_lock:
        _dashboard_cache[key] = (time.time() + ttl, value)

# retrieved_docs_json → doc_id → document_meta.owner_dept 的归属 JOIN。perf#3 后按环境二态：
# 事实表 qa_retrieved_doc 可用（schema/013 + RAG_QA_FACT_JOIN）→ 普通索引 JOIN；
# 否则回退历史 JSON_TABLE+collation-cast 展开。片段构造收敛在 qa_facts.qa_docs_join_sql。
# 末尾 WHERE 已含窗口占位符 %s；调用处再拼 _kb_owner_scope_sql 的作用域子句（kb_admin 为空 = 全库）。
def _kb_qa_owner_join() -> str:
    from opensearch_pipeline.qa_facts import qa_docs_join_sql
    return (
        f" FROM {_op_db()}.qa_session_log q"
        + qa_docs_join_sql(cited=False)
        + " WHERE q.retrieved_docs_json IS NOT NULL"
          "   AND q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
    )


class KbTopDocItem(BaseModel):
    title: str = ""
    owner_dept: str = ""
    hits: int = 0


class KbGapQueryItem(BaseModel):
    query: str = ""
    count: int = 0
    avg_top: float = 0.0


class KbInsightsResponse(BaseModel):
    scope: str = "dept"                  # 'global'（kb_admin 全库）| 'dept'（dept_admin 本部门）
    window_days: int = _KB_INSIGHTS_WINDOW_DAYS
    questions: int = 0                   # 命中所辖文档的提问数（DISTINCT message_id，去 JSON 扇出重复）
    askers: int = 0
    success: int = 0
    refusal: int = 0
    cited: int = 0                       # 所辖文档被「实际引用」的提问数
    helped_users: int = 0                # 被「实际引用」所辖文档的不同用户数（= 真正被本部门知识帮到的人数）
    effective_rate: float = 0.0          # success / questions
    top_docs: List[KbTopDocItem] = Field(default_factory=list)
    gap_queries: List[KbGapQueryItem] = Field(default_factory=list)


@router.get("/api/kb/insights", response_model=KbInsightsResponse)
def kb_insights(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """知识使用成效 + 知识缺口（owner 作用域；真实口径，无造数）。

    归属链 retrieved_docs_json→doc_id→document_meta.owner_dept，按 _kb_owner_scope_sql 作用域：
    dept_admin 只见本部门文档被使用情况，kb_admin 见全库。各子查询独立降级，缺数据诚实空。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    scope_clause, scope_params = _kb_owner_scope_sql(kb, "m.owner_dept")
    win = _KB_INSIGHTS_WINDOW_DAYS
    # 角色/作用域解析之后才查缓存：键按作用域分片，永不跨权限串数据
    cache_key = ("insights",
                 tuple(sorted(str(p) for p in scope_params)) if scope_clause else "GLOBAL", win)
    cached = _dashboard_cache_get(cache_key)
    if cached is not None:
        return cached
    base = _kb_qa_owner_join() + (" " + scope_clause if scope_clause else "")
    args = tuple([win] + scope_params)
    out = KbInsightsResponse(scope=("global" if kb.role == ROLE_KB_ADMIN else "dept"), window_days=win)
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_insights 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"洞察查询失败 (trace: {trace_id})")
    fails = 0   # 子查询失败计数；全失败 = 连接级故障 → 诚实 500（而非 all-zeros 伪装无数据）
    try:
        # 共享一个游标跑多条子查询：依赖 pymysql 默认 buffered Cursor（_init_db_pool 未设 SSCursor），
        # 某子查询异常后结果已全量缓冲，下一句 execute 不会 "Commands out of sync (2014)"。
        with conn.cursor() as cur:
            # 1) 使用聚合：提问数 / 提问人 / 成功 / 拒答（DISTINCT message_id 去 JSON 扇出）
            try:
                cur.execute(
                    "SELECT COUNT(DISTINCT q.message_id), COUNT(DISTINCT q.user_id),"
                    " COUNT(DISTINCT CASE WHEN q.answer_status='SUCCESS' THEN q.message_id END),"
                    " COUNT(DISTINCT CASE WHEN q.answer_status='REFUSAL' THEN q.message_id END)" + base,
                    args)
                r = cur.fetchone() or (0, 0, 0, 0)
                out.questions, out.askers = int(r[0] or 0), int(r[1] or 0)
                out.success, out.refusal = int(r[2] or 0), int(r[3] or 0)
                out.effective_rate = round(out.success / out.questions, 4) if out.questions else 0.0
            except Exception as e:
                fails += 1; logger.warning("kb_insights 使用聚合失败: %s", e)
            # 2) 被引用问题数 + 被帮到的不同用户数（cited_docs_json JOIN；NO_RESULT/REFUSAL 行该列为空，
            #    故 cited 天然「成功且实际用到本部门文档」，不会高估）。helped_users = 同一 JOIN 上按 user_id
            #    去重 → 真正被本部门知识帮到的人数（与 cited=提问数 配对：帮了 helped_users 人 / cited 个问题）。
            try:
                from opensearch_pipeline.qa_facts import qa_docs_join_sql
                cur.execute(
                    "SELECT COUNT(DISTINCT q.message_id), COUNT(DISTINCT q.user_id)"
                    f" FROM {_op_db()}.qa_session_log q"
                    + qa_docs_join_sql(cited=True)
                    + " WHERE q.cited_docs_json IS NOT NULL"
                    "   AND q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    + (" " + scope_clause if scope_clause else ""), args)
                r2 = cur.fetchone() or (0, 0)
                out.cited = int(r2[0] or 0)
                out.helped_users = int(r2[1] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_insights cited/helped 失败: %s", e)
            # 3) 最常被检索的文档（COUNT(DISTINCT message_id) 去扇出，与其它计数同一纪律）
            try:
                cur.execute(
                    "SELECT m.title, m.owner_dept, COUNT(DISTINCT q.message_id)" + base
                    + " GROUP BY m.doc_id, m.title, m.owner_dept"
                    " ORDER BY COUNT(DISTINCT q.message_id) DESC LIMIT 8", args)
                out.top_docs = [KbTopDocItem(title=row[0] or "", owner_dept=row[1] or "", hits=int(row[2] or 0))
                                for row in cur.fetchall()]
            except Exception as e:
                fails += 1; logger.warning("kb_insights top_docs 失败: %s", e)
            # 4) 知识缺口：所辖文档上「未答好」的提问（REFUSAL = 召回了我的文档但没答好，最可行动）。
            #    avg_top 必须在「去扇出后的每问一行」上求均值——直接 AVG(q.top_score) 会被检索文档数
            #    （最多 top_k=7）加权失真，故先 DISTINCT message_id 折叠扇出再外层 AVG。
            try:
                cur.execute(
                    "SELECT d.query_text, COUNT(*), ROUND(AVG(d.top_score), 3) FROM ("
                    "SELECT DISTINCT q.message_id, q.query_text, q.top_score" + base
                    + " AND q.answer_status='REFUSAL') d"
                    " GROUP BY d.query_text ORDER BY COUNT(*) DESC LIMIT 10", args)
                # 跨用户展示：gap_queries 是【他人】的原始提问，必须无条件 PII 脱敏（与 /api/kb/gaps
                # 一致），否则 admin 面板泄露他人身份证/手机号/姓名。
                from opensearch_pipeline import contribution as _C
                out.gap_queries = [
                    KbGapQueryItem(query=_C.redact_query_text(row[0] or ""), count=int(row[1] or 0),
                                   avg_top=float(row[2]) if row[2] is not None else 0.0)
                    for row in cur.fetchall()]
            except Exception as e:
                fails += 1; logger.warning("kb_insights gap_queries 失败: %s", e)
    finally:
        conn.close()
    if fails >= 4:   # 4 条子查询全失败 = 连接级故障：诚实 500，前端据此显「加载中」而非 0
        trace_id = get_request_id()
        logger.error("kb_insights 全部子查询失败 [trace=%s]", trace_id)
        raise HTTPException(status_code=500, detail=f"洞察查询失败 (trace: {trace_id})")
    if fails == 0:   # 降级响应（部分子查询失败）不缓存——下一请求重试取全量
        _dashboard_cache_put(cache_key, out)
    return out


class KbFeedbackDocRef(BaseModel):
    doc_id: str = ""
    title: str = ""
    owner_dept: str = ""


class KbFeedbackReviewItem(BaseModel):
    message_id: str = ""
    question: str = ""                  # 已 PII 脱敏（他人原始提问，与 gap_queries 同纪律）
    created_at: str = ""                # 差评时间
    reasons: List[str] = Field(default_factory=list)   # 点踩原因（中文标签；用户多选，故为列表）
    comment: str = ""                   # 用户「补充原因」自由文本（已 PII 脱敏，与 question 同纪律）
    handled: bool = False               # 是否已被管理员复核处置（handled_status ∈ RESOLVED/DISMISSED）
    handled_status: str = ""            # 原始处置态：''/PENDING/AWAITING_COMMENT/RESOLVED/DISMISSED
    docs: List[KbFeedbackDocRef] = Field(default_factory=list)   # 该回答引用的【作用域内】文档


class KbFeedbackReviewResponse(BaseModel):
    scope: str = "dept"
    window_days: int = _KB_INSIGHTS_WINDOW_DAYS
    items: List[KbFeedbackReviewItem] = Field(default_factory=list)


# 管理员对差评的处置态（写入 user_feedback.handled_status）。RESOLVED=已修复/已跟进、
# DISMISSED=已忽略（无需动作）、PENDING=重开（撤销处置）。AWAITING_COMMENT 是钉钉「补充原因」
# 流程的中转态（feedback_handler owns），复核处置动作不产出它。
_KB_FEEDBACK_HANDLED_ACTIONS = {"resolve": "RESOLVED", "dismiss": "DISMISSED", "reopen": "PENDING"}
_KB_FEEDBACK_HANDLED_DONE = ("RESOLVED", "DISMISSED")


@router.get("/api/kb/feedback-review", response_model=KbFeedbackReviewResponse)
def kb_feedback_review(request: Request, limit: int = 20, include_resolved: bool = False,
                       identity: Optional[Identity] = Depends(current_identity)):
    """差评联动复核队列（只读）：引用了我作用域文档的回答收到 👎 → 逐条列出
    （脱敏提问 + 点踩原因 + 用户补充说明 + 涉及的本部门文档）——「文档质量 → 答案质量」
    最直接的改进线索。原因/补充说明是修文档时该看的关键上下文，缺了它复核者无从判断该改什么。

    归属链与 insights 同源（qa_docs_join_sql cited=1：差评回答【实际引用】了谁的文档，
    谁来复核）；dept_admin 只见涉本部门文档的差评，kb_admin 全库。空=近窗口无差评；
    连接级失败诚实 500。同一回答多条差评（schema/016 去重前）在按 message 分组时自然合并。

    include_resolved=False（默认）：只列未处置（handled_status 非 RESOLVED/DISMISSED），
    即「收件箱」语义——已处置的沉入历史不占屏；True 则连已处置一并返回（供「显示已处理」切换）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    scope_clause, scope_params = _kb_owner_scope_sql(kb, "m.owner_dept")
    win = _KB_INSIGHTS_WINDOW_DAYS
    limit = max(1, min(limit, 50))
    # 处置态过滤：默认只收未处置（handled_status IS NULL / '' / PENDING / AWAITING_COMMENT）。
    handled_clause = "" if include_resolved else (
        " AND (f.handled_status IS NULL OR f.handled_status NOT IN ('RESOLVED','DISMISSED'))")
    cache_key = ("fb_review",
                 tuple(sorted(str(p) for p in scope_params)) if scope_clause else "GLOBAL",
                 win, limit, include_resolved)
    cached = _dashboard_cache_get(cache_key)
    if cached is not None:
        return cached
    out = KbFeedbackReviewResponse(scope=("global" if kb.role == ROLE_KB_ADMIN else "dept"),
                                   window_days=win)
    try:
        from opensearch_pipeline.db import _get_db_conn
        from opensearch_pipeline.qa_facts import qa_docs_join_sql
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 平铺 (message, doc) 行，Python 侧按 message 分组保序（GROUP_CONCAT 拼结构太脆）。
                # LIMIT 300 行 ≈ 数十条差评 × 引用文档数，上限后截 limit 条消息。
                cur.execute(
                    "SELECT f.message_id, f.created_at, q.query_text, m.doc_id, m.title, m.owner_dept,"
                    " f.feedback_reason, f.feedback_comment, f.handled_status"
                    f" FROM {_op_db()}.user_feedback f"
                    f" JOIN {_op_db()}.qa_session_log q ON q.message_id = f.message_id"
                    + qa_docs_join_sql(cited=True)
                    + " WHERE f.feedback_type='downvote'"
                    "   AND f.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    + handled_clause
                    + (" " + scope_clause if scope_clause else "")
                    + " ORDER BY f.created_at DESC, f.message_id LIMIT 300",
                    tuple([win] + scope_params))
                rows = cur.fetchall() or []
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_feedback_review 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"差评复核查询失败 (trace: {trace_id})")

    from opensearch_pipeline import contribution as _C
    by_msg: Dict[str, KbFeedbackReviewItem] = {}
    for row in rows:
        (mid, created, question, doc_id, title, owner, reason, comment, handled_status) = row
        it = by_msg.get(mid)
        if it is None:
            if len(by_msg) >= limit:
                continue
            hs = str(handled_status or "").upper()
            # 跨用户展示：他人原始提问/补充说明无条件 PII 脱敏（与 insights.gap_queries 同一纪律）。
            it = KbFeedbackReviewItem(
                message_id=str(mid),
                question=_C.redact_query_text(str(question or "")),
                created_at=str(created or ""),
                reasons=_kb_reason_labels(str(reason or "")),
                comment=_C.redact_query_text(str(comment or "")) if comment else "",
                handled=hs in _KB_FEEDBACK_HANDLED_DONE,
                handled_status=hs,
            )
            by_msg[mid] = it
        if doc_id and all(d.doc_id != doc_id for d in it.docs):
            it.docs.append(KbFeedbackDocRef(doc_id=str(doc_id), title=str(title or ""),
                                            owner_dept=str(owner or "")))
    out.items = list(by_msg.values())
    _dashboard_cache_put(cache_key, out)
    return out


class KbFeedbackResolveRequest(BaseModel):
    message_id: str
    action: Literal["resolve", "dismiss", "reopen"] = "resolve"


@router.post("/api/kb/feedback-review/resolve")
def kb_feedback_resolve(req: KbFeedbackResolveRequest, request: Request,
                        identity: Optional[Identity] = Depends(current_identity)):
    """管理员对一条差评的复核处置：RESOLVED（已修复/跟进）/ DISMISSED（忽略）/ PENDING（重开）。

    授权（现查 DB）：kb_admin 任意；dept_admin 仅当该回答【实际引用】了其 managed owner_dept 的
    文档时才可处置（与 feedback-review 列表可见性同源，防越权改他部门差评）。写入
    user_feedback.handled_status/handled_by/handled_at；按 message_id 覆盖该回答的全部差评行
    （schema/016 去重前可能多行）。不触碰 AWAITING_COMMENT 之外的钉钉「补充原因」时序——那由
    feedback_handler 拥有；这里只写终态/重开态，feedback_handler 的 IF 守卫会保留它。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    if not req.message_id:
        raise HTTPException(status_code=400, detail="缺少 message_id")
    new_status = _KB_FEEDBACK_HANDLED_ACTIONS.get(req.action)
    if not new_status:
        raise HTTPException(status_code=400, detail="非法处置动作")
    assert_metadata_write_allowed("kb_feedback_resolve", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    scope_clause, scope_params = _kb_owner_scope_sql(kb, "m.owner_dept")
    try:
        from opensearch_pipeline.db import _get_db_conn
        from opensearch_pipeline.qa_facts import qa_docs_join_sql
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # dept_admin 越权守卫：确认该差评回答引用了调用者作用域内文档（kb_admin scope 为空 → 恒过）。
                if kb.role != ROLE_KB_ADMIN:
                    cur.execute(
                        "SELECT 1"
                        f" FROM {_op_db()}.user_feedback f"
                        f" JOIN {_op_db()}.qa_session_log q ON q.message_id = f.message_id"
                        + qa_docs_join_sql(cited=True)
                        + " WHERE f.message_id=%s AND f.feedback_type='downvote'"
                        + (" " + scope_clause if scope_clause else "")
                        + " LIMIT 1",
                        tuple([req.message_id] + scope_params))
                    if not cur.fetchone():
                        conn.rollback()
                        raise HTTPException(status_code=403, detail="无权处置该差评（不在管理范围内）")
                n = cur.execute(
                    f"UPDATE {_op_db()}.user_feedback"
                    " SET handled_status=%s, handled_by=%s, handled_at=NOW(), updated_at=NOW()"
                    " WHERE message_id=%s AND feedback_type='downvote'",
                    (new_status, kb.user_id, req.message_id))
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_feedback_resolve 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"处置失败 (trace: {trace_id})")
    _dashboard_cache_clear()   # 处置改变了「收件箱」与已处理集，缓存作废重算
    return {"status": "ok", "message_id": req.message_id, "handled_status": new_status, "updated": n}


# ═══ 转人工工单队列（盲区审计 P1-2）═══════════════════════════════════════════
# escalation_ticket 此前是"只写不读"：钉钉「转人工」INSERT 一条 PENDING 后全仓无人出队、
# assigned_*/expert_answer/closed_at 零写入，用户却被告知"相关同事会尽快跟进"。这里补上
# 消费端：按龄列队（老单先出）→ 答复/忽略处置 → 带答复的处置把人工答案推回提问者钉钉。
# 可见性与差评复核同源：dept_admin 只见【实际引用】了其作用域文档的工单（谁的文档谁跟进），
# kb_admin 全库（含无引用文档的 NO_RESULT 工单 = 语料缺口，本就归入库管理员）。

class KbEscalationItem(BaseModel):
    ticket_id: str = ""
    message_id: str = ""
    question: str = ""                  # 已 PII 脱敏（他人原始提问，与差评复核同纪律）
    ai_answer_excerpt: str = ""         # AI 原答案节选（已脱敏；判断哪里没答好的上下文）
    user_name: str = ""                 # 提问者显示名（答复要回到真人，是工作流数据非泄露）
    user_dept: str = ""
    created_at: str = ""
    age_days: int = 0                   # 工单龄（天）——按龄列队的 SLA 视角
    status: str = "PENDING"             # PENDING / RESOLVED / DISMISSED
    closed: bool = False
    expert_answer: str = ""             # 人工答复（已处置视图回显）
    assigned_user_name: str = ""        # 处置人
    docs: List[KbFeedbackDocRef] = Field(default_factory=list)   # 该回答引用的作用域内文档


class KbEscalationsResponse(BaseModel):
    scope: str = "dept"
    items: List[KbEscalationItem] = Field(default_factory=list)


_KB_ESCALATION_DONE = ("RESOLVED", "DISMISSED")
_KB_ESCALATION_ACTIONS = {"resolve": "RESOLVED", "dismiss": "DISMISSED", "reopen": "PENDING"}
_KB_ESCALATION_ANSWER_MAX = 4000


def _kb_escalation_scope_exists(scope_clause: str) -> str:
    """dept_admin 作用域：工单对应回答【实际引用】了调用者管辖 owner_dept 的文档。
    与差评复核同一条归属链（qa_docs_join cited=1）；kb_admin scope 为空 → 不加此谓词。"""
    from opensearch_pipeline.qa_facts import qa_docs_join_sql
    return (f" AND EXISTS (SELECT 1 FROM {_op_db()}.qa_session_log q"
            + qa_docs_join_sql(cited=True)
            + " WHERE q.message_id = e.message_id " + scope_clause + ")")


@router.get("/api/kb/escalations", response_model=KbEscalationsResponse)
def kb_escalations(request: Request, limit: int = 20, include_closed: bool = False,
                   identity: Optional[Identity] = Depends(current_identity)):
    """转人工工单队列（只读）。默认只列未处置（收件箱），**不设时间窗**且按龄升序——
    工单是承诺不是日志，挂 40 天的老单必须顶在最上面而不是滚出窗口；include_closed=True
    时连已处置一并返回（近 90 天，新的在前，供「显示已处理」切换）。空=无工单；
    连接级失败诚实 500。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    scope_clause, scope_params = _kb_owner_scope_sql(kb, "m.owner_dept")
    limit = max(1, min(limit, 50))
    cache_key = ("escalations",
                 tuple(sorted(str(p) for p in scope_params)) if scope_clause else "GLOBAL",
                 limit, include_closed)
    cached = _dashboard_cache_get(cache_key)
    if cached is not None:
        return cached
    open_pred = f"(e.ticket_status IS NULL OR e.ticket_status NOT IN ({sql_in_list(_KB_ESCALATION_DONE)}))"
    if include_closed:
        where = f"(({open_pred}) OR e.created_at >= DATE_SUB(NOW(), INTERVAL 90 DAY))"
        order = "ORDER BY (" + open_pred + ") DESC, e.created_at DESC"   # 未处置置顶，其余新在前
    else:
        where = open_pred
        order = "ORDER BY e.created_at ASC"                              # 按龄：老单先出
    scoped = "" if kb.role == ROLE_KB_ADMIN else _kb_escalation_scope_exists(scope_clause)
    try:
        from opensearch_pipeline.db import _get_db_conn
        from opensearch_pipeline.qa_facts import qa_docs_join_sql
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT e.ticket_id, e.message_id, e.user_name, e.user_dept, e.query_text,"
                    " e.ai_answer, e.ticket_status, e.expert_answer, e.assigned_user_name,"
                    " e.created_at, DATEDIFF(NOW(), e.created_at)"
                    f" FROM {_op_db()}.escalation_ticket e"
                    f" WHERE {where}{scoped} {order} LIMIT %s",
                    tuple(([] if kb.role == ROLE_KB_ADMIN else scope_params) + [limit]))
                rows = cur.fetchall() or []
                # 引用文档 chips（第二查：按 message_id 批取；dept_admin 只回其作用域内文档）
                doc_map: Dict[str, List[KbFeedbackDocRef]] = {}
                mids = [str(r[1]) for r in rows if r[1]]
                if mids:
                    ph = ",".join(["%s"] * len(mids))
                    cur.execute(
                        "SELECT q.message_id, m.doc_id, m.title, m.owner_dept"
                        f" FROM {_op_db()}.qa_session_log q"
                        + qa_docs_join_sql(cited=True)
                        + f" WHERE q.message_id IN ({ph})"
                        + (" " + scope_clause if scope_clause else ""),
                        tuple(mids + scope_params))
                    for mid, doc_id, title, owner in cur.fetchall() or []:
                        lst = doc_map.setdefault(str(mid), [])
                        if doc_id and all(d.doc_id != str(doc_id) for d in lst):
                            lst.append(KbFeedbackDocRef(doc_id=str(doc_id), title=str(title or ""),
                                                        owner_dept=str(owner or "")))
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_escalations 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"转人工工单查询失败 (trace: {trace_id})")

    from opensearch_pipeline import contribution as _C
    out = KbEscalationsResponse(scope=("global" if kb.role == ROLE_KB_ADMIN else "dept"))
    for (tid, mid, uname, udept, qtext, ans, st, expert, assigned, created, age) in rows:
        st = str(st or "PENDING").upper() or "PENDING"
        excerpt = _C.redact_query_text(str(ans or ""))[:300]
        out.items.append(KbEscalationItem(
            ticket_id=str(tid), message_id=str(mid or ""),
            question=_C.redact_query_text(str(qtext or "")),
            ai_answer_excerpt=excerpt,
            user_name=str(uname or ""), user_dept=str(udept or ""),
            created_at=str(created or ""), age_days=max(0, int(age or 0)),
            status=st, closed=st in _KB_ESCALATION_DONE,
            expert_answer=str(expert or ""), assigned_user_name=str(assigned or ""),
            docs=doc_map.get(str(mid or ""), [])))
    _dashboard_cache_put(cache_key, out)
    return out


class KbEscalationResolveRequest(BaseModel):
    ticket_id: str
    action: Literal["resolve", "dismiss", "reopen"] = "resolve"
    expert_answer: str = ""     # resolve 时可选：人工答复内容（非空 → 推回提问者钉钉）


@router.post("/api/kb/escalations/resolve")
def kb_escalation_resolve(req: KbEscalationResolveRequest, request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """转人工工单处置：resolve（已答复/已跟进，expert_answer 非空则钉钉 1 对 1 推回提问者——
    信任恢复闭环的最后一步）/ dismiss（忽略）/ reopen（重开）。

    授权（现查 DB）：kb_admin 任意；dept_admin 仅当该工单回答【实际引用】了其 managed
    owner_dept 的文档（与队列可见性同源，防越权处置他部门工单）。写 ticket_status/
    assigned_*/expert_answer/answered_at/closed_at；reopen 清 closed_at 但保留已有答复痕迹。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    if not req.ticket_id:
        raise HTTPException(status_code=400, detail="缺少 ticket_id")
    new_status = _KB_ESCALATION_ACTIONS.get(req.action)
    if not new_status:
        raise HTTPException(status_code=400, detail="非法处置动作")
    answer = (req.expert_answer or "").strip()[:_KB_ESCALATION_ANSWER_MAX] \
        if req.action == "resolve" else ""
    assert_metadata_write_allowed("kb_escalation_resolve", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    scope_clause, scope_params = _kb_owner_scope_sql(kb, "m.owner_dept")
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT message_id, user_id, query_text FROM {_op_db()}.escalation_ticket"
                    " WHERE ticket_id=%s LIMIT 1", (req.ticket_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="工单不存在")
                t_mid, t_uid, t_query = str(row[0] or ""), str(row[1] or ""), str(row[2] or "")
                # dept_admin 越权守卫：工单回答须引用调用者作用域内文档（与队列可见性同源）
                if kb.role != ROLE_KB_ADMIN:
                    from opensearch_pipeline.qa_facts import qa_docs_join_sql
                    cur.execute(
                        f"SELECT 1 FROM {_op_db()}.qa_session_log q"
                        + qa_docs_join_sql(cited=True)
                        + " WHERE q.message_id=%s"
                        + (" " + scope_clause if scope_clause else "")
                        + " LIMIT 1",
                        tuple([t_mid] + scope_params))
                    if not cur.fetchone():
                        conn.rollback()
                        raise HTTPException(status_code=403, detail="无权处置该工单（不在管理范围内）")
                sets = ["ticket_status=%s", "assigned_user_id=%s", "assigned_user_name=%s",
                        "assigned_at=COALESCE(assigned_at, NOW())", "updated_at=NOW()"]
                params: List = [new_status, kb.user_id, kb.name or kb.user_id]
                if req.action == "reopen":
                    sets.append("closed_at=NULL")
                else:
                    sets.append("closed_at=NOW()")
                if answer:
                    sets += ["expert_answer=%s", "answered_at=NOW()"]
                    params.append(answer)
                params.append(req.ticket_id)
                n = cur.execute(
                    f"UPDATE {_op_db()}.escalation_ticket SET {', '.join(sets)}"
                    " WHERE ticket_id=%s", tuple(params))
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_escalation_resolve 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"处置失败 (trace: {trace_id})")
    # 人工答复推回提问者钉钉（best-effort：推送失败不回滚处置，只如实报 user_notified=false）
    user_notified = False
    if req.action == "resolve" and answer and t_uid:
        try:
            from opensearch_pipeline.dingtalk_card import send_text_to_user
            q = t_query.strip()
            q = q[:40] + ("…" if len(q) > 40 else "")
            user_notified = bool(send_text_to_user(
                t_uid, f"🙋 你转人工的问题「{q}」已有人工答复：\n\n{answer}"))
        except Exception as e:   # noqa: BLE001
            logger.warning("工单答复推送用户失败（忽略）: %s", e)
    _dashboard_cache_clear()   # 处置改变了收件箱与已处理集，缓存作废重算
    return {"status": "ok", "ticket_id": req.ticket_id, "ticket_status": new_status,
            "updated": n, "user_notified": user_notified}


# ═══ 入库复审任务队列（盲区审计 P2-33）══════════════════════════════════════
# review_task 此前"只写不读"：三个生产者（spot_checker 权限泄露安全网 / classify 失败 /
# cost_breaker 隔离登记）INSERT PENDING，全仓无任何端点/worker 出队——被标记为"实时权限比
# LLM 建议更宽松"的文档持续投放，设计好的人工安全网无人值守。kb_admin 专属（入库/安全职权）。

class KbReviewTaskItem(BaseModel):
    task_id: str = ""
    doc_id: str = ""
    title: str = ""                       # document_meta 现查（缺失回 doc_id）
    version_no: int = 0
    review_type: str = ""                 # spot_check_mismatch / classify 失败 / …
    review_reason: str = ""               # 截断展示
    owner_dept: str = ""
    suggested_permission_level: str = ""
    created_at: str = ""
    age_days: int = 0
    status: str = "PENDING"               # PENDING / RESOLVED / DISMISSED
    closed: bool = False
    reviewer_name: str = ""


class KbReviewTasksResponse(BaseModel):
    items: List[KbReviewTaskItem] = Field(default_factory=list)


_KB_REVIEW_DONE = ("RESOLVED", "DISMISSED")
_KB_REVIEW_ACTIONS = {"resolve": "RESOLVED", "dismiss": "DISMISSED", "reopen": "PENDING"}

# P2-14：监控心跳 stale 兜底告警的进程内日界节流（governance 端点每次渲染都读心跳）
_MONITOR_STALE_ALERT_DAY = ""


@router.get("/api/kb/review-tasks", response_model=KbReviewTasksResponse)
def kb_review_tasks(request: Request, limit: int = 20, include_closed: bool = False,
                    identity: Optional[Identity] = Depends(current_identity)):
    """入库复审任务队列（只读，kb_admin）。默认只列 PENDING、不设时间窗、按龄升序
    （安全网任务是承诺不是日志）；include_closed=True 连已处置一并返回（近 90 天）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    _require_kb_admin(identity)
    limit = max(1, min(limit, 50))
    cache_key = ("review_tasks", limit, include_closed)
    cached = _dashboard_cache_get(cache_key)
    if cached is not None:
        return cached
    open_pred = f"(t.review_status IS NULL OR t.review_status NOT IN ({sql_in_list(_KB_REVIEW_DONE)}))"
    if include_closed:
        where = f"(({open_pred}) OR t.created_at >= DATE_SUB(NOW(), INTERVAL 90 DAY))"
        order = "ORDER BY (" + open_pred + ") DESC, t.created_at DESC"
    else:
        where = open_pred
        order = "ORDER BY t.created_at ASC"
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT t.task_id, t.doc_id, COALESCE(m.title, m.original_filename, t.doc_id),"
                    " t.version_no, t.review_type, t.review_reason, t.owner_dept,"
                    " t.suggested_permission_level, t.review_status, t.reviewer_name,"
                    " t.created_at, DATEDIFF(NOW(), t.created_at)"
                    f" FROM {_kb_db()}.review_task t"
                    f" LEFT JOIN {_kb_db()}.document_meta m ON m.doc_id = t.doc_id"
                    f" WHERE {where} {order} LIMIT %s", (limit,))
                rows = cur.fetchall() or []
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        # fail-open：复审任务是只读看板聚合，review_task 表缺失/查询失败返回空列表而非拖垮
        # 整个管理台（与 governance/insights 等 dashboard 接口一致；2026-07-15 现网：生产漏建
        # 001 权威 review_task 表，1146 冒泡把 kb_admin 管理台整页打 500）。
        trace_id = get_request_id()
        logger.warning("kb_review_tasks 查询失败（空列表，non-fatal）[trace=%s]: %s", trace_id, e)
        return KbReviewTasksResponse()
    out = KbReviewTasksResponse()
    for (tid, doc_id, title, ver, rtype, reason, owner, sperm, st, rname, created, age) in rows:
        st = str(st or "PENDING").upper() or "PENDING"
        out.items.append(KbReviewTaskItem(
            task_id=str(tid), doc_id=str(doc_id or ""), title=str(title or ""),
            version_no=int(ver or 0), review_type=str(rtype or ""),
            review_reason=str(reason or "")[:300], owner_dept=str(owner or ""),
            suggested_permission_level=str(sperm or ""),
            created_at=str(created or ""), age_days=max(0, int(age or 0)),
            status=st, closed=st in _KB_REVIEW_DONE, reviewer_name=str(rname or "")))
    _dashboard_cache_put(cache_key, out)
    return out


class KbReviewTaskResolveRequest(BaseModel):
    task_id: str
    action: Literal["resolve", "dismiss", "reopen"] = "resolve"
    comment: str = ""


@router.post("/api/kb/review-tasks/resolve")
def kb_review_task_resolve(req: KbReviewTaskResolveRequest, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    """复审任务处置（kb_admin）：resolve（已核实/已修正——实际整改用既有工具：可见范围/退役/
    重灌）/ dismiss（误报）/ reopen。写 review_status/reviewer_*/reviewed_at。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_admin(identity)
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    if not req.task_id:
        raise HTTPException(status_code=400, detail="缺少 task_id")
    new_status = _KB_REVIEW_ACTIONS.get(req.action)
    if not new_status:
        raise HTTPException(status_code=400, detail="非法处置动作")
    assert_metadata_write_allowed("kb_review_task_resolve", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                sets = ["review_status=%s", "reviewer_user_id=%s", "reviewer_name=%s",
                        "updated_at=NOW()"]
                params: List = [new_status, kb.user_id, kb.name or kb.user_id]
                if req.action == "reopen":
                    sets.append("reviewed_at=NULL")
                else:
                    sets.append("reviewed_at=NOW()")
                comment = (req.comment or "").strip()[:1000]
                if comment:
                    sets.append("reviewer_comment=%s")
                    params.append(comment)
                params.append(req.task_id)
                n = cur.execute(
                    f"UPDATE {_kb_db()}.review_task SET {', '.join(sets)}"
                    " WHERE task_id=%s", tuple(params))
                if isinstance(n, int) and n == 0:
                    conn.rollback()
                    raise HTTPException(status_code=404, detail="复审任务不存在")
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_review_task_resolve 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"处置失败 (trace: {trace_id})")
    _dashboard_cache_clear()
    return {"status": "ok", "task_id": req.task_id, "review_status": new_status}


class KbEmbedRunItem(BaseModel):
    bizdate: str = ""
    embedded: int = 0
    failed: int = 0
    fail_rate: float = 0.0


class KbDeptCoverageItem(BaseModel):
    owner_dept: str = ""
    docs: int = 0                        # 已上线（active）文档数
    new_month: int = 0                   # 本月新增
    qa_hits: int = 0                     # 使用量（命中本部门文档的提问数）
    no_answer_rate: float = 0.0          # 无答案率（命中本部门文档的提问中 REFUSAL 占比）
    pii_docs: int = 0                    # 风险（含 PII 脱敏/隔离的文档数）
    # 文档总量周环比：本周净变化 = active 新增 − 本周退役（退役仅计上周末前已存在者）。
    #   wow_net  = 净变化「篇数」（前端徽标主显，对大部门比百分比更可读）。
    #   wow_total = 净变化 / 上周末总量（比率）；无上周基数(全新部门)→ null。
    # 近似口径：退役时点用 updated_at（retire 即 status='retired'+updated_at=NOW()，无独立 retired_at）；
    #   superseded（版本/去重转移）不计入本指标；故为估算非账面精确值。
    wow_net: Optional[int] = None
    wow_total: Optional[float] = None
    # 使用量周环比：近7天 vs 前7天 命中提问数（COUNT(DISTINCT message_id)）。
    #   qa_wow_net = 净变化「次」（徽标主显）；qa_wow = 净变化 / 上周使用量（无上周使用→ null）。
    qa_wow_net: Optional[int] = None
    qa_wow: Optional[float] = None
    # 批次δ-2：近 7 天绝对使用量（看板 7/30 窗口切换）——来自 wow 子查询现成的 qa7，
    # 与 qa_wow_net 同源同降级（wow 子查询失败 → None=未知，区别于真·零使用）。
    qa_hits_7d: Optional[int] = None


class KbFeedbackDay(BaseModel):
    day: str = ""
    up: int = 0
    down: int = 0


class KbDownvoteReason(BaseModel):
    reason: str = ""                     # 中文原因标签
    count: int = 0


class KbFileType(BaseModel):
    ftype: str = ""                      # PDF / DOCX / XLSX / PPTX / 图片 / 其他
    count: int = 0


class KbGovernanceResponse(BaseModel):
    window_days: int = _KB_INSIGHTS_WINDOW_DAYS
    # P2-14（盲区审计）：监控链路存活证明——ops_monitor 每次运行写心跳（rag_runtime_contract），
    # 这里读回龄（小时）。None=未知（表未建/从未跑）；>26h=监控本身死了（笔记本 cron 停/
    # 凭据过期），前端亮红。serving 是全系统最活的组件，让它当被动监工。
    monitor_heartbeat_age_h: Optional[float] = None
    monitor_stale: bool = False
    # 资产构成
    file_types: List[KbFileType] = Field(default_factory=list)   # 文件类型分布（按扩展名归类）
    # 运行健康
    docs_active: int = 0
    docs_in_index: int = 0
    dual_version_docs: int = 0
    pdf_truncated_docs: int = 0          # P1-09：现役 PDF 总页数超原生抽取上限（仅前 N 页上线）的文档数
    avg_latency_ms: int = 0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    avg_retrieval_ms: int = 0
    avg_llm_ms: int = 0
    embed_runs: List[KbEmbedRunItem] = Field(default_factory=list)
    # 服务可用性（近 30 天 + 近 24h）
    qa_api_success_rate: float = 0.0     # (总 - LLM_ERROR)/总
    retrieval_api_success_rate: float = 0.0   # (总 - 检索未完成 hit_count IS NULL)/总
    errors_24h: int = 0                  # 近 24 小时错误请求数
    qa_total_30d: int = 0                # 近 30 天问答总数（成功率分母）
    # 治理风险 / 知识效果
    pii_redacted_docs: int = 0
    pii_quarantined_docs: int = 0
    answer_total: int = 0
    answer_success: int = 0
    answer_refusal: int = 0
    answer_no_result: int = 0
    answer_error: int = 0
    effective_rate: float = 0.0
    feedback_up: int = 0
    feedback_down: int = 0
    feedback_total: int = 0
    helpful_rate: float = 0.0
    feedback_last7: int = 0              # 近 7 天反馈数
    feedback_daily: List[KbFeedbackDay] = Field(default_factory=list)   # 近 30 北京日 up/down 趋势
    downvote_reasons: List[KbDownvoteReason] = Field(default_factory=list)  # 点踩原因分布
    escalations: int = 0
    # 部门覆盖 / 使用失衡
    dept_coverage: List[KbDeptCoverageItem] = Field(default_factory=list)


@router.get("/api/kb/governance", response_model=KbGovernanceResponse)
def kb_governance(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """全库运行健康 / 治理风险 / 部门覆盖（仅 kb_admin；真实口径，无造数）。

    延迟为端到端（含钉钉打字机流式渲染，非纯推理）；嵌入失败率仅取 OBS-3 列非空的 stage-3 跑批，
    NULL 视为「未知」绝不当 0；PII/隔离按 document_sensitive_finding 的 COUNT(DISTINCT doc_id)。
    各子查询独立降级，缺数据诚实空。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    _require_kb_admin(identity)
    win = _KB_INSIGHTS_WINDOW_DAYS
    # kb_admin-only → 全局单键；角色校验先于缓存读
    cache_key = ("governance", "GLOBAL", win)
    cached = _dashboard_cache_get(cache_key)
    if cached is not None:
        return cached
    out = KbGovernanceResponse(window_days=win)
    # P2-14：监控心跳（fail-open；表未建 → None 前端显「未知」）。>26h → stale 亮红 +
    # 兜底告警（serving 作被动死人开关；每进程每日至多一次防刷）。
    try:
        from opensearch_pipeline.queue_monitor import read_heartbeat_age_hours
        out.monitor_heartbeat_age_h = read_heartbeat_age_hours()
        if out.monitor_heartbeat_age_h is not None and out.monitor_heartbeat_age_h > 26:
            out.monitor_stale = True
            global _MONITOR_STALE_ALERT_DAY
            import datetime as _dt
            _today = _dt.date.today().isoformat()
            if _MONITOR_STALE_ALERT_DAY != _today:
                _MONITOR_STALE_ALERT_DAY = _today
                from opensearch_pipeline.alerting import send_ops_alert
                send_ops_alert("监控链路心跳超时",
                               f"ops_monitor 心跳已 {out.monitor_heartbeat_age_h}h 未刷新——"
                               "笔记本 crontab/凭据/网络可能失效，所有 parity/SLO 检查处于停摆。",
                               severity="critical", dedup_key="monitor-heartbeat")
    except Exception:   # noqa: BLE001
        logger.debug("monitor heartbeat 读取失败（忽略）", exc_info=True)
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_governance 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"治理查询失败 (trace: {trace_id})")
    fails = 0   # 子查询失败计数；全失败 = 连接级故障 → 诚实 500（而非 all-zeros 伪装健康）
    try:
        # 共享一个游标跑多条子查询：依赖 pymysql 默认 buffered Cursor（_init_db_pool 未设 SSCursor），
        # 某子查询异常后结果已全量缓冲，下一句 execute 不会 "Commands out of sync (2014)"。
        with conn.cursor() as cur:
            # 1) 资产 / 索引可见性
            try:
                cur.execute(
                    f"SELECT (SELECT COUNT(*) FROM {_kb_db()}.document_meta WHERE status='active'),"
                    f" (SELECT COUNT(DISTINCT doc_id) FROM {_kb_db()}.chunk_meta"
                    f"   WHERE is_active=1 AND index_status='{ChunkIndexStatus.INDEXED}')")
                r = cur.fetchone() or (0, 0)
                out.docs_active, out.docs_in_index = int(r[0] or 0), int(r[1] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance 资产 失败: %s", e)
            # 2) 双版本残留（stage-3 不变量被破坏的信号；健康应为 0）
            try:
                cur.execute(
                    f"SELECT COUNT(*) FROM (SELECT doc_id FROM {_kb_db()}.chunk_meta"
                    f" WHERE is_active=1 AND index_status='{ChunkIndexStatus.INDEXED}'"
                    " GROUP BY doc_id HAVING COUNT(DISTINCT version_no) > 1) t")
                out.dual_version_docs = int((cur.fetchone() or (0,))[0] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance dual_version 失败: %s", e)
            # 2b) PDF 页上限截断（P1-09）：现役文档的当前版本是 PDF 且真实总页数 > 原生抽取上限，
            #     即「仅前 N 页上线」的文档——超限页的内容既无原生文本也未进 OCR，静默丢知识。
            #     上限值与抽取侧共用 config.pdf_native_max_pages（G2 env 化）。
            try:
                cur.execute(
                    f"SELECT COUNT(*) FROM {_kb_db()}.document_meta m"
                    f" JOIN {_kb_db()}.document_version v"
                    "   ON v.doc_id=m.doc_id AND v.version_no=m.current_version_no"
                    " WHERE m.status='active' AND v.file_ext='pdf' AND v.page_count > %s",
                    (_pdf_native_max_pages(),))
                out.pdf_truncated_docs = int((cur.fetchone() or (0,))[0] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance pdf_truncated 失败: %s", e)
            # 3) 端到端延迟（avg + p50/p95 + 检索/生成分段；窗口内 latency_ms>0）
            try:
                cur.execute(
                    "SELECT ROUND(AVG(latency_ms)), ROUND(AVG(retrieval_latency_ms)), ROUND(AVG(llm_latency_ms)),"
                    " MAX(CASE WHEN pr<=0.5 THEN latency_ms END), MAX(CASE WHEN pr<=0.95 THEN latency_ms END)"
                    " FROM (SELECT latency_ms, retrieval_latency_ms, llm_latency_ms,"
                    "   PERCENT_RANK() OVER (ORDER BY latency_ms) pr"
                    f"   FROM {_op_db()}.qa_session_log"
                    "   WHERE latency_ms > 0 AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)) t",
                    (win,))
                r = cur.fetchone() or (0, 0, 0, 0, 0)
                out.avg_latency_ms = int(r[0] or 0); out.avg_retrieval_ms = int(r[1] or 0)
                out.avg_llm_ms = int(r[2] or 0)
                out.p50_latency_ms = int(r[3] or 0); out.p95_latency_ms = int(r[4] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance latency 失败: %s", e)
            # 4) 嵌入失败率（OBS-3）：两列都必须非空，否则失败数未知。embedding_failed_chunks 是
            #    独立可空列（embedded_chunks=100、failed=NULL 是合法「未知」），若只判 embedded_chunks
            #    非空会把 NULL 当 0 → 伪造 0% 完美率。故 WHERE 同时要求 failed 非空，未知批次整条不计入。
            try:
                cur.execute(
                    "SELECT bizdate, embedded_chunks, embedding_failed_chunks"
                    f" FROM {_kb_db()}.pipeline_run"
                    " WHERE stage=3 AND embedded_chunks IS NOT NULL AND embedding_failed_chunks IS NOT NULL"
                    " ORDER BY started_at DESC LIMIT 8")
                runs = []
                for row in cur.fetchall():
                    emb, fail = int(row[1] or 0), int(row[2] or 0)
                    denom = emb + fail
                    runs.append(KbEmbedRunItem(bizdate=str(row[0] or ""), embedded=emb, failed=fail,
                                               fail_rate=round(fail / denom, 4) if denom else 0.0))
                out.embed_runs = runs
            except Exception as e:
                fails += 1; logger.warning("kb_governance embed_runs 失败: %s", e)
            # 5) 全库回答结果分布（原始 qa_session_log，含 NO_RESULT）
            try:
                cur.execute(
                    f"SELECT answer_status, COUNT(*) FROM {_op_db()}.qa_session_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY answer_status", (win,))
                for status, n in cur.fetchall():
                    n = int(n or 0); st = (status or "").upper()
                    out.answer_total += n
                    if st == "SUCCESS":
                        out.answer_success += n
                    elif st == "REFUSAL":
                        out.answer_refusal += n
                    elif st == "NO_RESULT":
                        out.answer_no_result += n
                    elif "ERROR" in st:
                        out.answer_error += n
                out.effective_rate = round(out.answer_success / out.answer_total, 4) if out.answer_total else 0.0
            except Exception as e:
                fails += 1; logger.warning("kb_governance answer_mix 失败: %s", e)
            # 6) PII：已脱敏 / 已隔离文档数（COUNT DISTINCT doc_id，按动作）
            try:
                cur.execute(
                    f"SELECT (SELECT COUNT(DISTINCT doc_id) FROM {_kb_db()}.document_sensitive_finding"
                    "   WHERE action='REDACTED'),"
                    f" (SELECT COUNT(DISTINCT doc_id) FROM {_kb_db()}.document_sensitive_finding"
                    "   WHERE action='QUARANTINED')")
                r = cur.fetchone() or (0, 0)
                out.pii_redacted_docs, out.pii_quarantined_docs = int(r[0] or 0), int(r[1] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance pii 失败: %s", e)
            # 7) 用户反馈（二元好评率 + 近7天量）：按 win 天窗口切齐——【与 answer_total 同窗】。
            #    此前 feedback_total 是全历史累计、answer_total 是近 30 天，前端 覆盖率=前者/后者 混窗
            #    必然失真（可 >100%）。统一窗口后 覆盖率/点赞/点踩/正反馈率 都是「近 win 天」口径（#10）。
            try:
                cur.execute(
                    "SELECT SUM(feedback_type='upvote'), SUM(feedback_type='downvote'), COUNT(*),"
                    " SUM(created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY))"
                    f" FROM {_op_db()}.user_feedback WHERE feedback_type IN ('upvote','downvote')"
                    "   AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)",
                    (win,))
                r = cur.fetchone() or (0, 0, 0, 0)
                out.feedback_up, out.feedback_down = int(r[0] or 0), int(r[1] or 0)
                out.feedback_total = int(r[2] or 0); out.feedback_last7 = int(r[3] or 0)
                out.helpful_rate = round(out.feedback_up / out.feedback_total, 4) if out.feedback_total else 0.0
            except Exception as e:
                fails += 1; logger.warning("kb_governance feedback 失败: %s", e)
            # 7b) 反馈趋势：近 30 北京日 up/down（DST-correct 分桶）
            try:
                cur.execute(
                    "SELECT DATE(CONVERT_TZ(created_at, 'America/Los_Angeles', 'Asia/Shanghai')),"
                    " SUM(feedback_type='upvote'), SUM(feedback_type='downvote')"
                    f" FROM {_op_db()}.user_feedback"
                    " WHERE feedback_type IN ('upvote','downvote')"
                    "   AND created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)"
                    " GROUP BY 1 ORDER BY 1")
                out.feedback_daily = [KbFeedbackDay(day=str(row[0]), up=int(row[1] or 0), down=int(row[2] or 0))
                                      for row in cur.fetchall()]
            except Exception as e:
                fails += 1; logger.warning("kb_governance feedback_daily 失败: %s", e)
            # 7c) 点踩原因分布（feedback_reason 多选逗号拼接 → Python 拆分计数 + 中文标签；null=未注明）。
            #     同样按 win 天窗口切齐（#10）——否则「共 N 条」与已窗口化的 feedback_down 对不上。
            #     中文标签复用 _KB_DOWNVOTE_REASON_LABELS（与差评复核逐条同源）。
            try:
                cur.execute(
                    f"SELECT feedback_reason, COUNT(*) FROM {_op_db()}.user_feedback"
                    " WHERE feedback_type='downvote'"
                    "   AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    " GROUP BY feedback_reason",
                    (win,))
                rcount: Dict[str, int] = {}
                for reason, n in cur.fetchall():
                    n = int(n or 0)
                    codes = [x.strip() for x in (reason or "").split(",") if x.strip()] or ["__none__"]
                    for code in codes:
                        label = "未注明" if code == "__none__" else _KB_DOWNVOTE_REASON_LABELS.get(code, code)
                        rcount[label] = rcount.get(label, 0) + n
                out.downvote_reasons = sorted(
                    [KbDownvoteReason(reason=k, count=v) for k, v in rcount.items()],
                    key=lambda x: x.count, reverse=True)
            except Exception as e:
                fails += 1; logger.warning("kb_governance downvote_reasons 失败: %s", e)
            # 8) 转人工工单数
            try:
                cur.execute(f"SELECT COUNT(*) FROM {_op_db()}.escalation_ticket")
                out.escalations = int((cur.fetchone() or (0,))[0] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance escalations 失败: %s", e)
            # 9) 部门覆盖与失衡：已上线 / 本月新增 / 使用量(命中提问数) / 无答案率(refusal占比) / 风险(PII文档)。
            #    qa_hits + refusal 用 COUNT(DISTINCT message_id) 去 chunk 扇出；PII JOIN 同样需 collation-cast。
            try:
                from datetime import date as _date
                ms = _date.today().replace(day=1).isoformat()
                cov: Dict[str, Dict[str, int]] = {}

                def _cell(d):
                    return cov.setdefault(d or "unknown", {"docs": 0, "new_month": 0, "qa_hits": 0, "refusal": 0, "pii": 0, "new7": 0, "ret7": 0, "qa7": 0, "qa_prev7": 0})

                cur.execute(f"SELECT owner_dept, COUNT(*) FROM {_kb_db()}.document_meta"
                            " WHERE status='active' GROUP BY owner_dept")
                for dept, docs in cur.fetchall():
                    _cell(dept)["docs"] = int(docs or 0)
                cur.execute(f"SELECT owner_dept, COUNT(*) FROM {_kb_db()}.document_meta"
                            " WHERE status='active' AND created_at >= %s GROUP BY owner_dept", (ms,))
                for dept, n in cur.fetchall():
                    _cell(dept)["new_month"] = int(n or 0)
                # 文档总量周环比：本周 active 新增；本周退役只计【上周末前已存在】者（created_at < 7d），
                # 否则「同周内先建后退役」会被算成 −1 幻影下跌（该文档上/本周末都不在 active 集，净贡献应为 0）。
                # updated_at 近似退役时点（无独立 retired_at）。
                wow_ok = True
                try:
                    cur.execute(f"SELECT owner_dept, COUNT(*) FROM {_kb_db()}.document_meta"
                                " WHERE status='active' AND created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY owner_dept")
                    for dept, n in cur.fetchall():
                        _cell(dept)["new7"] = int(n or 0)
                    cur.execute(f"SELECT owner_dept, COUNT(*) FROM {_kb_db()}.document_meta"
                                " WHERE status='retired' AND updated_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)"
                                " AND created_at < DATE_SUB(NOW(), INTERVAL 7 DAY) GROUP BY owner_dept")
                    for dept, n in cur.fetchall():
                        _cell(dept)["ret7"] = int(n or 0)
                except Exception as e:
                    wow_ok = False; logger.warning("kb_governance dept wow 失败: %s", e)
                from opensearch_pipeline.qa_facts import qa_docs_join_sql
                cur.execute(
                    "SELECT m.owner_dept, COUNT(DISTINCT q.message_id),"
                    " COUNT(DISTINCT CASE WHEN q.answer_status='REFUSAL' THEN q.message_id END)"
                    f" FROM {_op_db()}.qa_session_log q"
                    + qa_docs_join_sql()
                    + " WHERE q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY m.owner_dept", (win,))
                for dept, hits, refu in cur.fetchall():
                    cell = _cell(dept); cell["qa_hits"] = int(hits or 0); cell["refusal"] = int(refu or 0)
                # 各部门使用量周环比：近7天 vs 前7天 命中提问数（与 qa_hits 同 DISTINCT message_id 去 chunk 扇出口径）。
                qa_wow_ok = True
                try:
                    cur.execute(
                        "SELECT m.owner_dept,"
                        " COUNT(DISTINCT CASE WHEN q.created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY) THEN q.message_id END),"
                        " COUNT(DISTINCT CASE WHEN q.created_at >= DATE_SUB(NOW(), INTERVAL 14 DAY)"
                        "   AND q.created_at < DATE_SUB(NOW(), INTERVAL 7 DAY) THEN q.message_id END)"
                        f" FROM {_op_db()}.qa_session_log q"
                        + qa_docs_join_sql()
                        + " WHERE q.created_at >= DATE_SUB(NOW(), INTERVAL 14 DAY) GROUP BY m.owner_dept")
                    for dept, q7, qp7 in cur.fetchall():
                        cell = _cell(dept); cell["qa7"] = int(q7 or 0); cell["qa_prev7"] = int(qp7 or 0)
                except Exception as e:
                    qa_wow_ok = False; logger.warning("kb_governance dept usage wow 失败: %s", e)
                cur.execute(
                    "SELECT m.owner_dept, COUNT(DISTINCT f.doc_id)"
                    f" FROM {_kb_db()}.document_sensitive_finding f"
                    f" JOIN {_kb_db()}.document_meta m"
                    "   ON m.doc_id = CONVERT(f.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci"
                    " WHERE f.action IN ('QUARANTINED','REDACTED') GROUP BY m.owner_dept")
                for dept, n in cur.fetchall():
                    _cell(dept)["pii"] = int(n or 0)
                def _wow_net(v):                          # 本周净变化「篇数」
                    return (v["new7"] - v["ret7"]) if wow_ok else None
                def _wow_pct(v):                          # 净变化 / 上周末总量（无上周基数→null）
                    if not wow_ok:
                        return None
                    delta = v["new7"] - v["ret7"]
                    base = v["docs"] - delta               # 上周末总量 = 今总量 − 净变化
                    return round(delta / base, 4) if base > 0 else None
                def _qa_wow_net(v):                        # 使用量本周净变化「次」
                    return (v["qa7"] - v["qa_prev7"]) if qa_wow_ok else None
                def _qa_wow(v):                            # 使用量周环比（无上周使用→null）
                    if not qa_wow_ok:
                        return None
                    return round((v["qa7"] - v["qa_prev7"]) / v["qa_prev7"], 4) if v["qa_prev7"] > 0 else None
                out.dept_coverage = sorted(
                    [KbDeptCoverageItem(
                        owner_dept=k, docs=v["docs"], new_month=v["new_month"], qa_hits=v["qa_hits"],
                        no_answer_rate=round(v["refusal"] / v["qa_hits"], 4) if v["qa_hits"] else 0.0,
                        pii_docs=v["pii"], wow_net=_wow_net(v), wow_total=_wow_pct(v),
                        qa_wow_net=_qa_wow_net(v), qa_wow=_qa_wow(v),
                        # 批次δ-2：近 7 天绝对使用量——就是 wow 子查询现成的 qa7（零新增扫描）。
                        # 与 qa_wow_net/qa_wow 同生共死：子查询失败给 None（未知），绝不伪装成 0。
                        qa_hits_7d=(v["qa7"] if qa_wow_ok else None)) for k, v in cov.items()],
                    key=lambda x: x.docs, reverse=True)
            except Exception as e:
                fails += 1; logger.warning("kb_governance dept_coverage 失败: %s", e)
            # 10) 文件类型分布（按 original_filename 扩展名归类；Python 端合并到 PDF/DOCX/XLSX/PPTX/图片/其他）
            try:
                cur.execute(
                    "SELECT LOWER(SUBSTRING_INDEX(original_filename, '.', -1)) ext, COUNT(*)"
                    f" FROM {_kb_db()}.document_meta"
                    " WHERE status='active' AND original_filename LIKE '%.%' GROUP BY ext")
                _EXT2T = {"pdf": "PDF", "docx": "DOCX", "doc": "DOCX", "xlsx": "XLSX", "xls": "XLSX",
                          "pptx": "PPTX", "ppt": "PPTX",
                          "png": "图片", "jpg": "图片", "jpeg": "图片", "gif": "图片", "webp": "图片", "bmp": "图片"}
                _ORDER = ["PDF", "DOCX", "XLSX", "PPTX", "图片", "其他"]
                ftc: Dict[str, int] = {}
                for ext, n in cur.fetchall():
                    ftc[_EXT2T.get((ext or "").strip(), "其他")] = ftc.get(_EXT2T.get((ext or "").strip(), "其他"), 0) + int(n or 0)
                out.file_types = [KbFileType(ftype=t, count=ftc[t]) for t in _ORDER if ftc.get(t)]
            except Exception as e:
                fails += 1; logger.warning("kb_governance file_types 失败: %s", e)
            # 11) 服务可用性：问答API成功率(非 LLM_ERROR) / 检索API成功率(hit_count 非空) / 近30天总数 / 近24h错误数。
            #     检索错误（HA3 connection refused）在 serving 里落到 LLM_ERROR + hit_count=NULL，故用 NULL 判检索未完成。
            try:
                cur.execute(
                    "SELECT COUNT(*), SUM(answer_status='LLM_ERROR'), SUM(opensearch_hit_count IS NULL)"
                    f" FROM {_op_db()}.qa_session_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)", (win,))
                r = cur.fetchone() or (0, 0, 0)
                tot = int(r[0] or 0); llm_err = int(r[1] or 0); hit_null = int(r[2] or 0)
                out.qa_total_30d = tot
                out.qa_api_success_rate = round((tot - llm_err) / tot, 4) if tot else 0.0
                out.retrieval_api_success_rate = round((tot - hit_null) / tot, 4) if tot else 0.0
                cur.execute(
                    f"SELECT SUM(answer_status LIKE '%ERROR%') FROM {_op_db()}.qa_session_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)")
                out.errors_24h = int((cur.fetchone() or (0,))[0] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_governance availability 失败: %s", e)
    finally:
        conn.close()
    if fails >= 13:   # 13 条子查询全失败 = 连接级故障：诚实 500，前端据此显「加载中」而非伪造健康
        trace_id = get_request_id()
        logger.error("kb_governance 全部子查询失败 [trace=%s]", trace_id)
        raise HTTPException(status_code=500, detail=f"治理查询失败 (trace: {trace_id})")
    if fails == 0:   # 降级响应（部分子查询失败）不缓存——下一请求重试取全量
        _dashboard_cache_put(cache_key, out)
    return out


# ── 运营数据面（批次γ，docs/console_kb_admin_ux_review_2026-07-14.md D1-D3）────────────
# 三块只读聚合：LLM 用量（llm_call_log，schema/023）/ SLO 日趋势（qa_daily_metrics，schema/004+017）
# / 限流准入（qa_admission_reject，schema/017）。全部既有表，无 migration；kb_admin 专属。
class KbOpsLlmModelRow(BaseModel):
    model: str = ""
    calls: int = 0
    error_calls: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    avg_latency_ms: int = 0


class KbOpsBucketRow(BaseModel):
    """category / dept_group 共用的聚合行（key=桶名）。"""
    key: str = ""
    calls: int = 0
    tokens_total: int = 0


class KbOpsDailyLlmRow(BaseModel):
    d: str = ""
    calls: int = 0
    tokens_total: int = 0


class KbOpsSloDayRow(BaseModel):
    d: str = ""
    total: int = 0
    answer_rate: Optional[float] = None
    no_result_rate: Optional[float] = None
    error_rate: Optional[float] = None
    p95_latency_ms: Optional[int] = None
    distinct_users: int = 0
    slo_ok: Optional[bool] = None                       # None=rollup 未判（历史行）
    breaches: List[str] = Field(default_factory=list)   # 违约 SLO 名（阈值细节留在库里，看板只点名）
    rejected_count: Optional[int] = None                # None=017 前历史行


class KbOpsAdmissionDayRow(BaseModel):
    d: str = ""
    admitted: int = 0          # __admitted__ 伪行：当日准入量
    rejected: int = 0          # 全部拒绝原因合计


class KbOpsAdmissionReasonRow(BaseModel):
    reason: str = ""
    count: int = 0


class KbOpsMetricsResponse(BaseModel):
    window_days: int = 30
    # LLM 用量（成本底座）。cost_estimate 尊重 schema/023 拍板：价表未配=NULL，不编造单价。
    llm_available: bool = False
    llm_total_calls: int = 0
    llm_error_calls: int = 0
    llm_tokens_prompt: int = 0
    llm_tokens_completion: int = 0
    llm_cost_estimate: Optional[float] = None
    llm_p50_latency_ms: int = 0
    llm_p95_latency_ms: int = 0
    llm_by_model: List[KbOpsLlmModelRow] = Field(default_factory=list)
    llm_by_category: List[KbOpsBucketRow] = Field(default_factory=list)
    llm_by_dept: List[KbOpsBucketRow] = Field(default_factory=list)
    llm_daily: List[KbOpsDailyLlmRow] = Field(default_factory=list)
    # SLO 日趋势（qa_rollup 物化；governance 只有 30 天快照，这里给逐日序列）
    slo_available: bool = False
    slo_daily: List[KbOpsSloDayRow] = Field(default_factory=list)
    slo_breach_days: int = 0
    # 限流准入（offered vs admitted 的缺失半边——被拒请求不进 qa_session_log）
    admission_available: bool = False
    admission_daily: List[KbOpsAdmissionDayRow] = Field(default_factory=list)
    admission_reasons: List[KbOpsAdmissionReasonRow] = Field(default_factory=list)


@router.get("/api/kb/ops-metrics", response_model=KbOpsMetricsResponse)
def kb_ops_metrics(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """运营数据面（仅 kb_admin）：LLM 用量/成本底座 + SLO 日趋势 + 限流准入。

    三块各自独立降级：表未建/查询失败 → 对应 available=False + 空列表（诚实「未知」，
    绝不 all-zeros 伪装健康）；全部失败 = 连接级故障 → 500。cost_estimate 价表未配时
    诚实 NULL（schema/023「不编造模型单价」），前端只展示 token 量。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    _require_kb_admin(identity)
    win = _KB_INSIGHTS_WINDOW_DAYS
    cache_key = ("ops_metrics", "GLOBAL", win)
    cached = _dashboard_cache_get(cache_key)
    if cached is not None:
        return cached
    out = KbOpsMetricsResponse(window_days=win)
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_ops_metrics 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"运营指标查询失败 (trace: {trace_id})")
    fails = 0
    _tok = "COALESCE(tokens_prompt,0)+COALESCE(tokens_completion,0)"
    try:
        with conn.cursor() as cur:
            # 1) LLM 总量（COUNT=0 也算 available：表可读、真没有调用）
            try:
                cur.execute(
                    "SELECT COUNT(*), COALESCE(SUM(status='error'),0),"
                    " COALESCE(SUM(tokens_prompt),0), COALESCE(SUM(tokens_completion),0), SUM(cost_estimate)"
                    f" FROM {_op_db()}.llm_call_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)", (win,))
                r = cur.fetchone() or (0, 0, 0, 0, None)
                out.llm_total_calls = int(r[0] or 0)
                out.llm_error_calls = int(r[1] or 0)
                out.llm_tokens_prompt = int(r[2] or 0)
                out.llm_tokens_completion = int(r[3] or 0)
                out.llm_cost_estimate = float(r[4]) if r[4] is not None else None
                out.llm_available = True
            except Exception as e:
                fails += 1; logger.warning("kb_ops_metrics llm 总量 失败: %s", e)
            # 2) LLM 延迟分位（governance 同款 PERCENT_RANK；latency>0 才计）
            try:
                cur.execute(
                    "SELECT MAX(CASE WHEN pr<=0.5 THEN latency_ms END), MAX(CASE WHEN pr<=0.95 THEN latency_ms END)"
                    " FROM (SELECT latency_ms, PERCENT_RANK() OVER (ORDER BY latency_ms) pr"
                    f"   FROM {_op_db()}.llm_call_log"
                    "   WHERE latency_ms > 0 AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)) t", (win,))
                r = cur.fetchone() or (0, 0)
                out.llm_p50_latency_ms = int(r[0] or 0); out.llm_p95_latency_ms = int(r[1] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_ops_metrics llm 延迟 失败: %s", e)
            # 3) 按模型（调用量降序，封顶 20——模型是有限集合，防脏数据爆行）
            try:
                cur.execute(
                    "SELECT model, COUNT(*), COALESCE(SUM(status='error'),0),"
                    " COALESCE(SUM(tokens_prompt),0), COALESCE(SUM(tokens_completion),0), ROUND(COALESCE(AVG(latency_ms),0))"
                    f" FROM {_op_db()}.llm_call_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    " GROUP BY model ORDER BY COUNT(*) DESC LIMIT 20", (win,))
                out.llm_by_model = [KbOpsLlmModelRow(
                    model=str(x[0] or ''), calls=int(x[1] or 0), error_calls=int(x[2] or 0),
                    tokens_prompt=int(x[3] or 0), tokens_completion=int(x[4] or 0), avg_latency_ms=int(x[5] or 0),
                ) for x in (cur.fetchall() or [])]
            except Exception as e:
                fails += 1; logger.warning("kb_ops_metrics llm 按模型 失败: %s", e)
            # 4) 按类别（deep/default/quick/sql；NULL → 未标注）
            try:
                cur.execute(
                    f"SELECT COALESCE(category,'未标注'), COUNT(*), COALESCE(SUM({_tok}),0)"
                    f" FROM {_op_db()}.llm_call_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    " GROUP BY 1 ORDER BY 2 DESC LIMIT 10", (win,))
                out.llm_by_category = [KbOpsBucketRow(key=str(x[0] or ''), calls=int(x[1] or 0), tokens_total=int(x[2] or 0))
                                       for x in (cur.fetchall() or [])]
            except Exception as e:
                fails += 1; logger.warning("kb_ops_metrics llm 按类别 失败: %s", e)
            # 5) 按部门（成本归集口径 dept_group；NULL → 未归集）
            try:
                cur.execute(
                    f"SELECT COALESCE(dept_group,'未归集'), COUNT(*), COALESCE(SUM({_tok}),0)"
                    f" FROM {_op_db()}.llm_call_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    " GROUP BY 1 ORDER BY 2 DESC LIMIT 15", (win,))
                out.llm_by_dept = [KbOpsBucketRow(key=str(x[0] or ''), calls=int(x[1] or 0), tokens_total=int(x[2] or 0))
                                   for x in (cur.fetchall() or [])]
            except Exception as e:
                fails += 1; logger.warning("kb_ops_metrics llm 按部门 失败: %s", e)
            # 6) 逐日调用/token（趋势图）
            try:
                cur.execute(
                    f"SELECT DATE(created_at), COUNT(*), COALESCE(SUM({_tok}),0)"
                    f" FROM {_op_db()}.llm_call_log"
                    " WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    " GROUP BY 1 ORDER BY 1", (win,))
                out.llm_daily = [KbOpsDailyLlmRow(d=str(x[0] or ''), calls=int(x[1] or 0), tokens_total=int(x[2] or 0))
                                 for x in (cur.fetchall() or [])]
            except Exception as e:
                fails += 1; logger.warning("kb_ops_metrics llm 逐日 失败: %s", e)
            # 7) SLO 日趋势（qa_rollup 物化行；slo_breaches_json 只取违约名）
            try:
                cur.execute(
                    "SELECT metric_date, total_queries, answer_rate, no_result_rate, error_rate,"
                    " p95_latency_ms, distinct_users, slo_ok, slo_breaches_json, rejected_count"
                    f" FROM {_op_db()}.qa_daily_metrics"
                    " WHERE metric_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"
                    " ORDER BY metric_date", (win,))
                import json as _json
                rows = []
                for x in (cur.fetchall() or []):
                    breaches: List[str] = []
                    if x[8]:
                        try:
                            parsed = _json.loads(x[8]) if isinstance(x[8], (str, bytes)) else x[8]
                            breaches = [str(b.get('slo', '')) for b in parsed if isinstance(b, dict) and b.get('slo')]
                        except Exception:   # noqa: BLE001  JSON 脏行不拖垮整列
                            breaches = []
                    rows.append(KbOpsSloDayRow(
                        d=str(x[0] or ''), total=int(x[1] or 0),
                        answer_rate=float(x[2]) if x[2] is not None else None,
                        no_result_rate=float(x[3]) if x[3] is not None else None,
                        error_rate=float(x[4]) if x[4] is not None else None,
                        p95_latency_ms=int(x[5]) if x[5] is not None else None,
                        distinct_users=int(x[6] or 0),
                        slo_ok=bool(x[7]) if x[7] is not None else None,
                        breaches=breaches,
                        rejected_count=int(x[9]) if x[9] is not None else None,
                    ))
                out.slo_daily = rows
                out.slo_breach_days = sum(1 for r2 in rows if r2.slo_ok is False)
                out.slo_available = True
            except Exception as e:
                fails += 1; logger.warning("kb_ops_metrics slo 失败: %s", e)
            # 8) 限流准入（__ 前缀=非拒绝伪行：__admitted__=当日准入量）
            try:
                cur.execute(
                    "SELECT stat_date, reason, reject_count"
                    f" FROM {_op_db()}.qa_admission_reject"
                    " WHERE stat_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"
                    " ORDER BY stat_date", (win,))
                day: dict = {}
                reason_total: dict = {}
                for x in (cur.fetchall() or []):
                    d = str(x[0] or ''); reason = str(x[1] or ''); n = int(x[2] or 0)
                    row = day.setdefault(d, {"admitted": 0, "rejected": 0})
                    if reason == '__admitted__':
                        row["admitted"] += n
                    elif not reason.startswith('__'):
                        row["rejected"] += n
                        reason_total[reason] = reason_total.get(reason, 0) + n
                out.admission_daily = [KbOpsAdmissionDayRow(d=k, admitted=v["admitted"], rejected=v["rejected"])
                                       for k, v in sorted(day.items())]
                out.admission_reasons = [KbOpsAdmissionReasonRow(reason=k, count=v)
                                         for k, v in sorted(reason_total.items(), key=lambda kv: -kv[1])]
                out.admission_available = True
            except Exception as e:
                fails += 1; logger.warning("kb_ops_metrics admission 失败: %s", e)
    finally:
        conn.close()
    if fails >= 8:   # 8 条子查询全失败 = 连接级故障：诚实 500
        trace_id = get_request_id()
        logger.error("kb_ops_metrics 全部子查询失败 [trace=%s]", trace_id)
        raise HTTPException(status_code=500, detail=f"运营指标查询失败 (trace: {trace_id})")
    if fails == 0:
        _dashboard_cache_put(cache_key, out)
    return out


class KbConfigResponse(BaseModel):
    max_upload_bytes: int = 0
    accepted_exts: List[str] = Field(default_factory=list)


@router.get("/api/kb/config", response_model=KbConfigResponse)
def kb_config(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """前端能力配置（上传上限/受理类型）—— 后端权威，省得客户端硬编码 50MB/类型导致"传完才 413"漂移。

    **有意公开**（不加 _require_kb_console）：仅暴露静态能力常量（上传字节上限 + 扩展名白名单），
    非敏感、无部门/文档数据；客户端在上传前自检需要它，限流即足以防滥用（G6）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    from opensearch_pipeline.kb_upload import MAX_UPLOAD_BYTES, _PHASE1_EXTS
    return KbConfigResponse(
        max_upload_bytes=int(MAX_UPLOAD_BYTES),
        accepted_exts=sorted(_PHASE1_EXTS),
    )


@router.get("/api/kb/version-history", response_model=KbVersionHistoryResponse)
def kb_version_history(request: Request, doc_id: str,
                       identity: Optional[Identity] = Depends(current_identity)):
    """某文档的版本历史（含每版管线状态）。授权：kb_admin 或文档 owner_dept 在调用者 managed 内。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    if not doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT owner_dept, status FROM {_kb_db()}.document_meta "
                            "WHERE doc_id=%s LIMIT 1", (doc_id,))
                meta = cur.fetchone()
                if not meta:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, _doc_status = meta[0] or "", meta[1]
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权查看该文档")
                cur.execute(
                    f"""
                    SELECT version_no, content_process_status, chunk_status, index_status,
                           publish_status, error_message, created_at
                    FROM {_kb_db()}.document_version
                    WHERE doc_id=%s ORDER BY version_no DESC
                    """,
                    (doc_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_version_history 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"版本历史查询失败 (trace: {trace_id})")

    versions = []
    for r in rows:
        (vno, cps, chs, ixs, pubs, err, created) = r
        versions.append(KbVersionItem(
            version_no=int(vno or 0), content_process_status=cps or "",
            chunk_status=chs or "", index_status=ixs or "", publish_status=pubs or "",
            status_badge=_kb_status_badge(cps, ixs, _doc_status),   # 传 doc 级状态 → 退役文档各版本如实显「已退役」(B4)
            error_message=err or "", created_at=str(created) if created else "",
        ))
    return KbVersionHistoryResponse(doc_id=doc_id, owner_dept=owner_dept, versions=versions)


@router.get("/api/kb/doc-status", response_model=KbDocStatusResponse)
def kb_doc_status(request: Request, doc_id: str, version: Optional[int] = None,
                  identity: Optional[Identity] = Depends(current_identity)):
    """某文档某版本的详细管线状态 + chunk 计数（不传 version → 取 current_version_no）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    if not doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT owner_dept, status, current_version_no "
                            f"FROM {_kb_db()}.document_meta WHERE doc_id=%s LIMIT 1", (doc_id,))
                meta = cur.fetchone()
                if not meta:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, doc_status, cur_ver = meta[0] or "", meta[1], int(meta[2] or 1)
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权查看该文档")
                vno = int(version) if version else cur_ver
                cur.execute(
                    "SELECT content_process_status, chunk_status, index_status, error_message "
                    f"FROM {_kb_db()}.document_version WHERE doc_id=%s AND version_no=%s LIMIT 1",
                    (doc_id, vno),
                )
                dv = cur.fetchone()
                cur.execute(
                    f"SELECT COUNT(*), SUM(is_active=1), SUM(index_status='{ChunkIndexStatus.INDEXED}') "
                    f"FROM {_kb_db()}.chunk_meta WHERE doc_id=%s AND version_no=%s",
                    (doc_id, vno),
                )
                total, active, indexed = cur.fetchone() or (0, 0, 0)
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_doc_status 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档状态查询失败 (trace: {trace_id})")

    cps, chs, ixs, err = (dv or ("", "", "", ""))
    active = int(active or 0)
    return KbDocStatusResponse(
        doc_id=doc_id, version_no=vno, owner_dept=owner_dept,
        content_process_status=cps or "", chunk_status=chs or "", index_status=ixs or "",
        chunk_total=int(total or 0), chunk_active=active, chunk_indexed=int(indexed or 0),
        status_badge=_kb_status_badge(cps, ixs, doc_status, active),
        error_message=err or "",
    )


# 审批预览 TTL：签名 URL 短时有效（管理员看一眼即够，不做长效外链）。
_KB_PREVIEW_TTL_SECONDS = 300


class KbDocPreviewResponse(BaseModel):
    doc_id: str = ""
    version_no: int = 0
    filename: str = ""
    content_type: str = ""
    url: str = ""              # 短时签名 GET URL；'' = 原件缺失 / OSS 未配置（前端据 available 提示）
    expires_in: int = 0
    available: bool = False


@router.get("/api/kb/doc-preview", response_model=KbDocPreviewResponse)
def kb_doc_preview(request: Request, doc_id: str, version: int = 0,
                   identity: Optional[Identity] = Depends(current_identity)):
    """审批/台账「预览原件」：返回该版本原始上传文件（OSS raw_key）的短时签名 GET URL。

    存在意义（补 kb_admin 唯一的把关盲区）：公开/跨组上传在 content_process_status='PENDING_APPROVAL'
    时【管线尚未跑】，没有 canonical/chunk 可看——此刻唯一的实物就是 raw 文件。签名直看原件，
    审批人才不是凭标题盲批。台账已入库文档同样可看原件（current 版本）。

    授权（现查 DB）：kb_admin 任意；dept_admin 仅其 managed owner_dept（与 doc-status 同源）——
    受限/他部门原件不外露。签名 URL 仅 300s 有效、只读。version=0 → current_version_no；否则取指定版本
    （审批队列传 pending 版本号）。原件缺失 / OSS 未配置 → available=False，前端如实提示不可预览。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    if not doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 一次 JOIN 取 归属 + 原文件名 + 该版本 raw_key（version=0 → 落到 current_version_no）。
                cur.execute(
                    "SELECT m.owner_dept, m.original_filename, v.raw_key, v.version_no"
                    f" FROM {_kb_db()}.document_meta m"
                    f" JOIN {_kb_db()}.document_version v"
                    "   ON v.doc_id = m.doc_id AND v.version_no = IF(%s > 0, %s, m.current_version_no)"
                    " WHERE m.doc_id = %s LIMIT 1",
                    (int(version or 0), int(version or 0), doc_id),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_doc_preview 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"预览查询失败 (trace: {trace_id})")

    if not row:
        raise HTTPException(status_code=404, detail="文档或版本不存在")
    owner_dept, filename, raw_key, vno = (row[0] or ""), (row[1] or ""), (row[2] or ""), int(row[3] or 0)
    # 授权先于任何 URL 生成：受限/他部门原件绝不外露（dept_admin 越权直接 403）。
    if not _kb_can_manage(kb, owner_dept):
        raise HTTPException(status_code=403, detail="无权预览该文档")
    if not raw_key:
        return KbDocPreviewResponse(doc_id=doc_id, version_no=vno, filename=filename, available=False)
    from opensearch_pipeline.oss_url import generate_signed_url, mime_for_ext
    url = generate_signed_url(raw_key, expires=_KB_PREVIEW_TTL_SECONDS, method="GET")
    return KbDocPreviewResponse(
        doc_id=doc_id, version_no=vno, filename=filename,
        content_type=mime_for_ext(filename), url=url or "",
        expires_in=_KB_PREVIEW_TTL_SECONDS, available=bool(url),
    )


# ═══════════════════════════════════════════════════════════════
# 知识库管理 — Phase 1 上传/升版/审批（写）
# 两段式：upload-url 颁发后端钦定 raw_key + 签名 PUT + upload token；客户端直传 OSS；
# register 校验 token（HMAC）+ OSS-HEAD 实物 + 现查授权 + 事务内分配 version_no（行锁）+ 幂等。
# 公开 / 跨组共享 → content_process_status='PENDING_APPROVAL'（scanner 不认领，等 kb_admin 审批）。
# 写守卫用【轻量】assert_metadata_write_allowed（≠ HA3 删除级开关）。
# ═══════════════════════════════════════════════════════════════

class KbUploadUrlRequest(BaseModel):
    action: Literal["new", "version"] = "new"
    filename: str
    owner_dept: str
    permission_level: str = "dept_internal"
    title: Optional[str] = None
    category_l1: Optional[str] = None
    category_l2: Optional[str] = None
    doc_id: Optional[str] = None                       # action=version 必填
    share_owner_depts: Optional[List[str]] = None      # 多部门共享意图（Phase 2 才在检索侧生效）


class KbUploadUrlResponse(BaseModel):
    upload_token: str
    put_url: str
    raw_key: str
    doc_id: str
    expires_in: int
    requires_kb_admin_approval: bool = False
    content_type: str = ""   # 客户端 PUT 必须发此 Content-Type（已签入 put_url，不一致 OSS 403）；G4


class KbRegisterRequest(BaseModel):
    upload_token: str


class KbRegisterResponse(BaseModel):
    doc_id: str
    version_no: int
    content_process_status: str
    requires_kb_admin_approval: bool = False
    status_badge: str = ""
    idempotent: bool = False
    title: str = ""
    # 内容查重（按 OSS ETag = 字节级指纹，跨部门）。advisory，不拦上传。
    content_dups: List[KbDupDoc] = Field(default_factory=list)   # 调用者可见范围内的同内容文档
    content_dups_other: int = 0                                   # 可见范围外的同内容文档计数（仅提示存在，不泄露部门/标题）


class KbApprovalRequest(BaseModel):
    doc_id: str
    version_no: Optional[int] = None
    reason: Optional[str] = None


class KbRetireRequest(BaseModel):
    doc_id: str
    reason: Optional[str] = None


class KbRetireResponse(BaseModel):
    status: str = "ok"
    doc_id: str
    retired: bool = False
    already: bool = False
    status_badge: str = "已退役"
    note: str = ""


class KbRestoreResponse(BaseModel):
    status: str = "ok"
    doc_id: str
    restored: bool = False
    already: bool = False
    status_badge: str = "在线"
    note: str = ""


class KbSetVisibilityRequest(BaseModel):
    doc_id: str
    permission_level: str            # dept_internal / public / restricted（受 sanitize）
    reason: Optional[str] = None


class KbSetVisibilityResponse(BaseModel):
    doc_id: str = ""
    permission_level: str = ""
    changed: bool = False
    already: bool = False
    note: str = ""


@router.post("/api/kb/upload-url", response_model=KbUploadUrlResponse)
def kb_upload_url(req: KbUploadUrlRequest, request: Request,
                  identity: Optional[Identity] = Depends(current_identity)):
    """颁发签名 PUT URL + upload token。后端钦定 raw_key/doc_id（客户端不可改）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline import kb_upload, kb_authz
    from opensearch_pipeline.oss_url import generate_signed_url
    from opensearch_pipeline.config import get_config

    ok, ext, reason = kb_upload.validate_upload_filename(req.filename)
    if not ok:
        msg = {"legacy_format": "旧版 Office 格式（.doc/.xls/.ppt）暂不支持，请另存为 .docx/.xlsx/.pptx 后重传",
               "unsupported_format": "不支持的文件类型",
               "no_extension": "文件缺少扩展名"}.get(reason, "文件名非法")
        raise HTTPException(status_code=400, detail=msg)

    owner = (req.owner_dept or "").strip()
    perm = req.permission_level

    if req.action == "version":
        if not req.doc_id:
            raise HTTPException(status_code=400, detail="升版需提供 doc_id")
        try:
            from opensearch_pipeline.db import _get_db_conn
            conn = _get_db_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT owner_dept, permission_level, status FROM {_kb_db()}.document_meta "
                                "WHERE doc_id=%s LIMIT 1", (req.doc_id,))
                    row = cur.fetchone()
            finally:
                conn.close()
        except HTTPException:
            raise
        except Exception as e:
            trace_id = get_request_id()
            logger.error("upload-url 查 doc 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
            raise HTTPException(status_code=500, detail=f"查询文档失败 (trace: {trace_id})")
        if not row:
            raise HTTPException(status_code=404, detail="升版目标文档不存在")
        if (row[0] or "") != owner or not _kb_can_manage(kb, owner):
            raise HTTPException(status_code=403, detail="无权升版该文档（owner_dept 不在管理范围）")
        # F-37 早失败：退役文档禁止升版——否则新版本会被 stage-1 认领复活（认领只看 dv.status）。
        # 连 PUT URL 都不颁发，客户端根本传不了文件。恢复上线走 /api/kb/restore。
        if str(row[2] or "active").lower() != "active":
            raise HTTPException(status_code=409, detail="该文档已退役，请先在控制台恢复上线后再升版")
        # 升版强制继承原文档 permission_level —— 忽略客户端传值（升版不得改可见范围，防越权）。
        perm = row[1] or perm
        doc_id = req.doc_id
    else:
        doc_id = kb_upload.new_doc_id()

    # 授权裁决用最终生效的 perm（新建=客户端选；升版=原文档继承）。
    decision = kb_authz.authorize_upload(kb, owner, perm, req.share_owner_depts)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=f"无权上传：{decision.reason}")

    upload_id = kb_upload.new_ulid()
    # 可见范围编码进路径段，防管线 stage-2 把 dept_internal/restricted 升成 public（自助上传/贡献同款）。
    raw_key = kb_upload.build_raw_key(owner, doc_id, upload_id, req.filename, permission_level=perm)
    token = kb_upload.sign_upload_token({
        "uid": kb.user_id, "action": req.action, "doc_id": doc_id, "owner_dept": owner,
        "raw_key": raw_key, "filename": kb_upload.safe_filename(req.filename), "ext": ext,
        "title": req.title or kb_upload.safe_filename(req.filename),
        "category_l1": req.category_l1 or "", "category_l2": req.category_l2 or "",
        "permission_level": perm,
        "share_owner_depts": kb_authz.sanitize_owner_depts(req.share_owner_depts),
        "max_size": kb_upload.MAX_UPLOAD_BYTES,
        "requires_approval": bool(decision.requires_kb_admin_approval),
        "owner_name": kb.name,
    })
    bucket = get_config().oss.bucket_name
    # G4：把 Content-Type 按申报扩展名钉死并签入 PUT URL —— 客户端须发完全一致的 Content-Type，
    # 否则 OSS 拒签（403），杜绝持 URL 者上传任意类型/与扩展名不符的字节。content_type 回传客户端。
    from opensearch_pipeline.oss_url import mime_for_ext
    content_type = mime_for_ext(ext)
    put_url = generate_signed_url(raw_key, expires=kb_upload.UPLOAD_TOKEN_TTL, method="PUT",
                                  content_type=content_type)
    logger.info("kb upload-url: uid=%s action=%s doc_id=%s owner=%s bucket=%s ctype=%s",
                kb.user_id, req.action, doc_id, owner, bucket, content_type)
    return KbUploadUrlResponse(
        upload_token=token, put_url=put_url, raw_key=raw_key, doc_id=doc_id,
        expires_in=kb_upload.UPLOAD_TOKEN_TTL,
        requires_kb_admin_approval=bool(decision.requires_kb_admin_approval),
        content_type=content_type,
    )


@router.post("/api/kb/register", response_model=KbRegisterResponse)
def kb_register(req: KbRegisterRequest, request: Request,
                identity: Optional[Identity] = Depends(current_identity)):
    """登记上传：校验 token + OSS-HEAD + 现查授权 → 事务内分配 version_no（行锁）写 RDS（幂等）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    import hashlib
    from opensearch_pipeline import kb_upload, kb_authz
    from opensearch_pipeline.oss_url import head_object
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config

    payload = kb_upload.verify_upload_token(req.upload_token)
    if not payload:
        raise HTTPException(status_code=400, detail="upload_token 无效或已过期")
    if (payload.get("uid") or "") != kb.user_id:
        raise HTTPException(status_code=403, detail="upload_token 与当前用户不符")

    owner = payload["owner_dept"]
    raw_key = payload["raw_key"]
    perm = payload["permission_level"]
    # 现查授权（撤销/收回授权后即时生效，绝不信旧 token 的判断）
    decision = kb_authz.authorize_upload(kb, owner, perm, payload.get("share_owner_depts"))
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=f"无权登记：{decision.reason}")
    requires_approval = bool(decision.requires_kb_admin_approval)

    # OSS-HEAD 实物校验：存在 + 大小
    meta = head_object(raw_key)
    if not meta:
        raise HTTPException(status_code=400, detail="未检测到已上传的文件（请先完成直传，或 PUT 已过期）")
    size = int(meta.get("size") or 0)
    if size <= 0:
        raise HTTPException(status_code=400, detail="上传的文件为空")
    if size > int(payload.get("max_size") or kb_upload.MAX_UPLOAD_BYTES):
        raise HTTPException(status_code=413, detail="文件超过大小上限")
    # OSS ETag = 内容指纹（自助上传单次 PUT ⇒ 内容 MD5，与路径/部门无关）→ 用于跨部门内容查重。
    etag_val = (meta.get("etag") or "")[:128]

    cfg = get_config()
    assert_metadata_write_allowed("kb_register_upload", cfg.rds.host, kind="rds")

    cps = "PENDING_APPROVAL" if requires_approval else "NOT_STARTED"
    appr = "PENDING" if requires_approval else "APPROVED"
    action = payload.get("action", "new")
    bucket = cfg.oss.bucket_name
    trace_id = get_request_id()

    # raw_key 幂等点查统一走 raw_key_hash 索引（perf#5，schema/014）：谓词形态
    # `raw_key=%s AND (raw_key_hash=%s OR raw_key_hash IS NULL)` —— hash 等值 + IS NULL 走
    # idx_raw_key_hash 的 ref_or_null 访问，raw_key 等值仍是权威判定；014 回填前的存量
    # NULL 行 / 索引未建的环境退化为与旧行为等价的正确结果，无部署顺序风险。
    raw_key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    _RAW_KEY_PRED = "WHERE raw_key=%s AND (raw_key_hash=%s OR raw_key_hash IS NULL)"
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 幂等：同一 raw_key 已登记 → 直接返回既有行
                cur.execute("SELECT doc_id, version_no, content_process_status "
                            f"FROM {_kb_db()}.document_version {_RAW_KEY_PRED} LIMIT 1",
                            (raw_key, raw_key_hash))
                exist = cur.fetchone()
                if exist:
                    conn.commit()
                    return KbRegisterResponse(
                        doc_id=exist[0], version_no=int(exist[1]),
                        content_process_status=exist[2] or cps,
                        requires_kb_admin_approval=requires_approval,
                        status_badge=_kb_status_badge(exist[2], None, "active"),
                        idempotent=True,
                        title=payload.get("title") or "",
                    )
                doc_id = payload["doc_id"]
                if action == "version":
                    # 行锁串行化版本号分配，避免并发升版撞号
                    cur.execute(f"SELECT current_version_no, permission_level, status FROM {_kb_db()}.document_meta "
                                "WHERE doc_id=%s FOR UPDATE", (doc_id,))
                    mrow = cur.fetchone()
                    if not mrow:
                        raise HTTPException(status_code=404, detail="升版目标文档不存在")
                    # F-37 纵深防御（行锁内，与并发退役串行化）：退役文档禁止升版——否则新 document_version
                    # 行 status='active'（下方硬编码）会被 stage-1 认领（认领只看 dv.status，不看 dm.status），
                    # 退役文档次日复活、全员可检索。upload-url 已早拦一次，此处是写库入口再核（token TTL 窗口内文档可能被退役）。
                    if str(mrow[2] or "active").lower() != "active":
                        raise HTTPException(status_code=409, detail="该文档已退役，请先在控制台恢复上线后再升版")
                    # F-38：拿到 document_meta 行锁【之后】再查一次 raw_key。并发升版双击时，锁前那次幂等
                    # SELECT 可能两边都读空（都在赢家 commit 前）；升版路径 FOR UPDATE 串行化后各自算出不同
                    # 版本号 → uk_doc_version 不撞、1062 兜底也不触发 → 会落成两个版本。持锁后按 raw_key 复查，
                    # 命中赢家已提交行即幂等返回，不再推高 current_version_no（避免版本空洞 + 双份抽取/嵌入）。
                    cur.execute("SELECT doc_id, version_no, content_process_status "
                                f"FROM {_kb_db()}.document_version {_RAW_KEY_PRED} LIMIT 1",
                                (raw_key, raw_key_hash))
                    _relock = cur.fetchone()
                    if _relock:
                        conn.commit()   # 释放 document_meta 行锁
                        return KbRegisterResponse(
                            doc_id=_relock[0], version_no=int(_relock[1]),
                            content_process_status=_relock[2] or cps,
                            requires_kb_admin_approval=requires_approval,
                            status_badge=_kb_status_badge(_relock[2], None, "active"),
                            idempotent=True, title=payload.get("title") or "",
                        )
                    # 纵深防御：升版绝不改可见范围（token 由 upload-url 钦定继承，此处再核一次）
                    if perm != (mrow[1] or perm):
                        raise HTTPException(status_code=403, detail="升版不可改变可见范围")
                    version_no = int(mrow[0] or 1) + 1
                    cur.execute(f"UPDATE {_kb_db()}.document_meta "
                                "SET current_version_no=%s, updated_at=NOW() WHERE doc_id=%s",
                                (version_no, doc_id))
                else:
                    version_no = 1
                    cur.execute(
                        f"""
                        INSERT INTO {_kb_db()}.document_meta
                          (doc_id, title, original_filename, owner_dept, owner_user_id, owner_name,
                           category_l1, category_l2, permission_level, kb_type, status, current_version_no)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',1)
                        ON DUPLICATE KEY UPDATE current_version_no=GREATEST(current_version_no,1),
                                                updated_at=NOW()
                        """,
                        (doc_id, payload.get("title"), payload.get("filename"), owner,
                         kb.user_id, payload.get("owner_name") or kb.name,
                         payload.get("category_l1") or None, payload.get("category_l2") or None,
                         perm, ("public" if perm == "public" else "private")),
                    )
                # raw_key_hash 与生产管线/批量注册一致写入（自助路径此前置 NULL）——供 reconcile/dedup
                # 工具按内容键去重（hash 已在函数入口处统一计算，幂等点查同键）。
                try:
                    cur.execute(
                        f"""
                        INSERT INTO {_kb_db()}.document_version
                          (doc_id, version_no, bucket_name, raw_key, raw_key_hash, etag, file_ext, mime_type,
                           file_size_bytes, content_process_status, approval_status, status, received_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',NOW())
                        """,
                        (doc_id, version_no, bucket, raw_key, raw_key_hash, etag_val, payload.get("ext"),
                         kb_upload.expected_mime(payload.get("ext")), size, cps, appr),
                    )
                except Exception as ins_err:
                    # uk_doc_version(doc_id,version_no) 唯一键 1062：并发双提交（同一 upload_token 双击/
                    # 重试，共用 upload-url 钦定的 doc_id+version_no）。赢家事务已提交该版本（InnoDB 唯一键
                    # 把输家的 INSERT 阻塞到赢家 commit 才抛 1062），故回滚本事务（连带撤销 meta 的
                    # current_version_no 副作用，避免输家留下半截写入），按 raw_key 重查赢家行返回幂等成功——
                    # 而非把可预期的竞态当 500 抛给用户。非 1062 的完整性错误照常上抛走 500 分支。
                    if (getattr(ins_err, "args", None) or (None,))[0] != 1062:
                        raise
                    conn.rollback()
                    with conn.cursor() as c2:
                        c2.execute("SELECT doc_id, version_no, content_process_status "
                                   f"FROM {_kb_db()}.document_version {_RAW_KEY_PRED} LIMIT 1",
                                   (raw_key, raw_key_hash))
                        won = c2.fetchone()
                    if not won:
                        raise   # 1062 但查不到赢家行 → 非预期，按 500 处理
                    logger.info("kb_register 并发幂等命中：raw_key=%s 赢家 doc=%s v=%s", raw_key, won[0], won[1])
                    return KbRegisterResponse(
                        doc_id=won[0], version_no=int(won[1]),
                        content_process_status=won[2] or cps,
                        requires_kb_admin_approval=requires_approval,
                        status_badge=_kb_status_badge(won[2], None, "active"),
                        idempotent=True, title=payload.get("title") or "",
                    )
            # P2-06：登记与审计【同事务】原子提交——审计行随该版本一并 commit（失败则一并回滚 →
            # 500 可重试）。此前 audit 在 commit 之后用独立连接写且吞异常：进程崩在 commit 与 audit
            # 之间 → 留下有登记、无审计的业务变更。kb_audit_log 与 document_version 同库同服务器
            # （均 {_kb_db()}，_audit_insert_sql 全限定表名），故可复用本连接的事务。
            with conn.cursor() as _acur:
                write_audit(doc_id=doc_id, version_no=version_no,
                            action_type=("VERSION_UP" if action == "version" else "UPLOAD_REGISTER"),
                            operator_type="user", operator_id=kb.user_id, oss_key=raw_key,
                            trace_id=trace_id,
                            message=f"owner={owner} perm={perm} approval={appr} share={payload.get('share_owner_depts')}",
                            cursor=_acur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_register 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"登记失败 (trace: {trace_id})")
    # 钉钉工作通知（RAG_ADMIN_NOTIFY 门控，best-effort no-raise）：进了待审批队列就
    # 即时告知 kb_admin——否则全靠人主动开控制台，公开件在队列里干等。
    if requires_approval:
        from opensearch_pipeline.admin_notify import notify_upload_approval
        notify_upload_approval(owner_dept=owner, title=payload.get("title") or payload.get("filename") or doc_id)
    # 跨部门内容查重（按 ETag 字节指纹）：advisory，命中也不拦上传——仅在响应里提示，让上传者决定是否退役其一。
    # 升版（同 doc_id 换文件）天然不算重复，故仅新建查；fail-open。
    dups, dups_other = ([], 0)
    if action != "version":
        dups, dups_other = _kb_content_dups(etag_val, doc_id, kb)
    return KbRegisterResponse(
        doc_id=doc_id, version_no=version_no, content_process_status=cps,
        requires_kb_admin_approval=requires_approval,
        status_badge=_kb_status_badge(cps, None, "active"),
        title=payload.get("title") or "",
        content_dups=dups, content_dups_other=dups_other,
    )


@router.post("/api/kb/approve")
def kb_approve(req: KbApprovalRequest, request: Request,
               identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 审批放行：PENDING_APPROVAL → NOT_STARTED（下一批入库）。仅 kb_admin。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if kb.role != ROLE_KB_ADMIN:
        raise HTTPException(status_code=403, detail="仅知识库管理员可审批")
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    assert_metadata_write_allowed("kb_approve", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # F-37 纵深防御：文档已退役则不放行任何 PENDING 版本。堵"多 pending 版本 + 退役后审批"
                # 复活窗口——kb_retire 只把 current 版本置 retired，更早的 pending 版本可能仍 status=active，
                # 审批放行后会被 stage-1 认领复活。FOR UPDATE 与 kb_retire（同样锁 document_meta）串行化。
                cur.execute(f"SELECT status FROM {_kb_db()}.document_meta WHERE doc_id=%s FOR UPDATE", (req.doc_id,))
                _m = cur.fetchone()
                if _m and str(_m[0] or "active").lower() != "active":
                    conn.commit()
                    return {"status": "ok", "approved": 0, "note": "文档已退役，未放行任何版本"}
                vfilter = "AND version_no=%s" if req.version_no else ""
                vargs = (req.version_no,) if req.version_no else ()
                n = cur.execute(
                    f"UPDATE {_kb_db()}.document_version "
                    f"SET content_process_status='NOT_STARTED', approval_status='APPROVED', updated_at=NOW() "
                    f"WHERE doc_id=%s {vfilter} AND content_process_status='PENDING_APPROVAL'",
                    (req.doc_id, *vargs),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("kb_approve 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"审批失败 (trace: {trace_id})")
    write_audit(doc_id=req.doc_id, version_no=req.version_no, action_type="APPROVE",
                operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                message=f"approved {n} version(s)")
    return {"status": "ok", "approved": n}


@router.post("/api/kb/reject")
def kb_reject(req: KbApprovalRequest, request: Request,
              identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 驳回：PENDING_APPROVAL → REJECTED（永不入库）。仅 kb_admin。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if kb.role != ROLE_KB_ADMIN:
        raise HTTPException(status_code=403, detail="仅知识库管理员可驳回")
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    assert_metadata_write_allowed("kb_reject", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                vfilter = "AND version_no=%s" if req.version_no else ""
                vargs = (req.version_no,) if req.version_no else ()
                n = cur.execute(
                    f"UPDATE {_kb_db()}.document_version "
                    f"SET content_process_status='REJECTED', approval_status='REJECTED', "
                    f"    content_process_error=%s, updated_at=NOW() "
                    f"WHERE doc_id=%s {vfilter} AND content_process_status='PENDING_APPROVAL'",
                    ((req.reason or "rejected")[:500], req.doc_id, *vargs),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("kb_reject 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"驳回失败 (trace: {trace_id})")
    write_audit(doc_id=req.doc_id, version_no=req.version_no, action_type="REJECT",
                operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                message=(req.reason or "")[:200])
    return {"status": "ok", "rejected": n}


@router.post("/api/kb/retire", response_model=KbRetireResponse)
def kb_retire(req: KbRetireRequest, request: Request,
              identity: Optional[Identity] = Depends(current_identity)):
    """软退役（可逆）：标记下线 + 停用 RDS chunk + 喂 PENDING_DELETE outbox（HA3 自动清除）。

    授权：kb_admin 任意；dept_admin 限其 managed owner_dept，且【公开文档需 kb_admin】（影响全公司）。
    本接口不直接删 HA3，但把全部版本 index_status 置 PENDING_DELETE——stage-3 每轮
    reconcile_pending_deletes 自动 drain 删 HA3（与 kb_set_visibility→restricted 同一握手；
    盲区审计 P2-1：此前只写 RDS 且无任何以 status='retired' 为键的清除器，被退役文档的向量
    永久留在 HA3 可检索）。可逆：kb_restore 撤销 PENDING_DELETE 并标脏重推。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.config import get_config
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    assert_metadata_write_allowed("kb_retire", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    owner_dept = perm = ""
    cur_ver = 1
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                # 行锁文档元数据，串行化并发退役 / 退役-vs-升版
                cur.execute("SELECT owner_dept, permission_level, status, current_version_no "
                            f"FROM {_kb_db()}.document_meta WHERE doc_id=%s FOR UPDATE", (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm = (row[0] or ""), (row[1] or "")
                status, cur_ver = (row[2] or "active"), int(row[3] or 1)
                # 授权：先作用域，再"公开需 kb_admin"（与上传同款不对称——公开影响全公司）
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权退役该文档（owner_dept 不在管理范围）")
                if perm == "public" and kb.role != ROLE_KB_ADMIN:
                    raise HTTPException(status_code=403, detail="公开文档需知识库管理员退役")
                if str(status).lower() != "active":
                    conn.commit()       # 幂等：已退役/非活跃 → 直接回既有态
                    return KbRetireResponse(doc_id=req.doc_id, retired=False, already=True,
                                            note="该文档已是退役/非活跃状态")
                cur.execute(f"UPDATE {_kb_db()}.document_meta SET status='retired', updated_at=NOW() "
                            "WHERE doc_id=%s", (req.doc_id,))
                cur.execute(f"UPDATE {_kb_db()}.document_version SET status='retired', updated_at=NOW() "
                            "WHERE doc_id=%s AND version_no=%s", (req.doc_id, cur_ver))
                # RDS 侧停用该文档【全部活跃版本】chunk（不限当前版本）——若此前部分入库/搬迁残留了旧版本
                # is_active=1（双版本 gap），只停当前版本会让它们退役后仍存活、被邻居拼接复用、且 HA3 清除
                # 漏删而无限期滞留。退役语义是「整篇下线」，故停全部活跃 chunk（stage-3 reconcile 再兜底 HA3）。
                cur.execute(f"UPDATE {_kb_db()}.chunk_meta SET is_active=0 "
                            "WHERE doc_id=%s AND is_active=1", (req.doc_id,))
                # 真实检索下线不能只靠 RDS（盲区审计 P2-1）：HA3 行仍在且带原 permission_level，
                # 检索照常命中。喂 PENDING_DELETE outbox——stage-3 每轮 reconcile_pending_deletes
                # 自动 drain 删 HA3 后落 DELETED（全版本入队，顺带清双版本残留；与
                # kb_set_visibility→restricted 同一握手，kb_restore 会对称撤销）。
                cur.execute(f"UPDATE {_kb_db()}.document_version "
                            f"SET index_status='{DocVersionIndexStatus.PENDING_DELETE}' "
                            f"WHERE doc_id=%s AND index_status NOT IN "
                            f"({sql_in_list((DocVersionIndexStatus.DELETED, DocVersionIndexStatus.PENDING_DELETE))})",
                            (req.doc_id,))
                # 审计行入【同事务】（commit 前、同 cursor）：与退役变更原子提交，杜绝 commit 与审计之间
                # 崩溃丢记录的窗口（B1）。失败 → 整笔回滚 → 500 可重试。
                write_audit(doc_id=req.doc_id, version_no=cur_ver, action_type="RETIRE_REQUEST",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"owner={owner_dept} perm={perm} reason={(req.reason or '')[:200]}",
                            cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_retire 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"退役失败 (trace: {trace_id})")
    return KbRetireResponse(
        doc_id=req.doc_id, retired=True,
        note="已退役：已标记下线并加入索引清除队列，下次入库批处理自动从检索移除（本操作可逆）")


def _assert_version_not_quarantined(cur, doc_id: str, version_no: int) -> None:
    """安全隔离防线（restore / set-visibility 共用）：spot_checker/cost_breaker 的追溯隔离只写
    document_version（publish_status='QUARANTINED' / gate_status='quarantined'）+ 停 chunk
    （is_active=0），document_meta.status 仍是 'active'、permission_level 被收紧为 'restricted'——
    在本文件各 guard 眼里与普通"受限/下线"文档不可区分。恢复类分支若盲目重激活
    （is_active=1 + NOT_INDEXED），stage-3 drain 无隔离过滤，会把未脱敏 chunk 重嵌重推 HA3 → PII 泄漏。
    故隔离版本一律 409：唯一出路是脱敏重灌（derivative pipeline），不是控制台改可见度/恢复。"""
    cur.execute(f"SELECT publish_status, gate_status FROM {_kb_db()}.document_version "
                "WHERE doc_id=%s AND version_no=%s", (doc_id, version_no))
    vrow = cur.fetchone()
    if vrow and (str(vrow[0] or "").upper() == "QUARANTINED"
                 or str(vrow[1] or "").lower() == "quarantined"):
        raise HTTPException(
            status_code=409,
            detail="该文档处于安全隔离（PII/敏感内容），不能经可见范围/恢复操作重新上线；请走脱敏重灌流程")


@router.post("/api/kb/restore", response_model=KbRestoreResponse)
def kb_restore(req: KbRetireRequest, request: Request,
               identity: Optional[Identity] = Depends(current_identity)):
    """恢复上线（退役的逆操作）：把退役文档重新激活 + 标脏待重索引。授权与退役同款。

    仅改 RDS（document_meta/version.status='active' + chunk_meta.is_active=1 + index_status='NOT_INDEXED'）。
    软退役不删 HA3（is_active=0 仅 RDS 标记）：若退役后【尚未】跑 HA3 清除维护，chunk 仍在 HA3 →
    本操作即时恢复检索；若已被 gated 维护从 HA3 删除，则标脏 NOT_INDEXED，下次 stage-3 drain 重嵌+重推
    后恢复（与退役"可逆"承诺对齐，且覆盖已清除的边界情形）。不触碰 HA3（重推交 stage-3）。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    assert_metadata_write_allowed("kb_restore", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    owner_dept = perm = ""
    cur_ver = 1
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT owner_dept, permission_level, status, current_version_no "
                            f"FROM {_kb_db()}.document_meta WHERE doc_id=%s FOR UPDATE", (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, perm = (row[0] or ""), (row[1] or "")
                status, cur_ver = (row[2] or "active"), int(row[3] or 1)
                # 授权：与退役同款不对称——作用域 + 公开文档需 kb_admin
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权恢复该文档（owner_dept 不在管理范围）")
                if perm == "public" and kb.role != ROLE_KB_ADMIN:
                    raise HTTPException(status_code=403, detail="公开文档需知识库管理员恢复")
                if str(status).lower() == "active":
                    conn.commit()       # 幂等：已在线 → 直接回既有态
                    return KbRestoreResponse(doc_id=req.doc_id, restored=False, already=True,
                                             note="该文档已是在线状态")
                # 退役→恢复 不能成为隔离文档复活通道（隔离 chunk is_active=0 会被下面的重激活扫中）
                _assert_version_not_quarantined(cur, req.doc_id, cur_ver)
                cur.execute(f"UPDATE {_kb_db()}.document_meta SET status='active', updated_at=NOW() "
                            "WHERE doc_id=%s", (req.doc_id,))
                cur.execute(f"UPDATE {_kb_db()}.document_version SET status='active', updated_at=NOW() "
                            "WHERE doc_id=%s AND version_no=%s", (req.doc_id, cur_ver))
                # 重新激活本版本 chunk + 标脏 NOT_INDEXED（下次 stage-3 重推 HA3；若 HA3 未删则为幂等重推）。
                cur.execute(f"UPDATE {_kb_db()}.chunk_meta SET is_active=1, index_status='{ChunkIndexStatus.NOT_INDEXED}' "
                            "WHERE doc_id=%s AND version_no=%s AND is_active=0", (req.doc_id, cur_ver))
                # 撤销退役挂上的 PENDING_DELETE（P2-1 起 retire 喂 outbox）：否则下轮 reconcile 删 HA3
                # 后把 chunk 打回 is_active=0/DELETED，恰好撤销这次恢复。DELETED（已删过）也拨回
                # NOT_INDEXED 交 stage-3 重推。仅当前版本；旧版本照删（与 set_visibility 恢复分支同款）。
                cur.execute(f"UPDATE {_kb_db()}.document_version "
                            f"SET index_status='{DocVersionIndexStatus.NOT_INDEXED}' "
                            f"WHERE doc_id=%s AND version_no=%s AND index_status IN "
                            f"({sql_in_list((DocVersionIndexStatus.PENDING_DELETE, DocVersionIndexStatus.DELETED))})",
                            (req.doc_id, cur_ver))
                write_audit(doc_id=req.doc_id, version_no=cur_ver, action_type="RESTORE_REQUEST",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"owner={owner_dept} perm={perm} reason={(req.reason or '')[:200]}",
                            cursor=cur)   # 同事务审计（B1）
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_restore 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"恢复失败 (trace: {trace_id})")
    return KbRestoreResponse(
        doc_id=req.doc_id, restored=True,
        note="已恢复上线：重新激活并标记待重索引；若退役后 HA3 仍在则即时可检索，否则下次维护重索引后恢复")


@router.post("/api/kb/set-visibility", response_model=KbSetVisibilityResponse)
def kb_set_visibility(req: KbSetVisibilityRequest, request: Request,
                      identity: Optional[Identity] = Depends(current_identity)):
    """重设【已上线】文档的基础可见范围（dept_internal / public / restricted），无需重新上传。

    被动申请流/主动共享改的是【跨部门授权】(allowed_depts)；本端点改的是文档【自身的基础级别】——
    两者正交，均汇入同一 Phase D 投影。授权（与 retire/restore 同款不对称）：
      - _kb_can_manage(owner_dept)（dept_admin 限 managed，kb_admin 全权）；
      - **涉及 public 需 kb_admin**：目标=public（放宽到全公司）或当前=public（收窄影响全公司可见的文档）
        时，dept_admin 一律 403——与"上传公开需审批"同一不对称。dept_admin 只能在
        dept_internal ↔ restricted 之间调本部门文档。

    各方向语义（与 spot_checker 收紧 / retire 下线 / restore 重推 同款机制，绝不新造）：
      - → restricted（归档下线）：停用【全部活跃版本】chunk（与 retire 同款，收掉双版本残留）+
        全版本喂 PENDING_DELETE outbox（stage-3 每轮 reconcile_pending_deletes 自动删 HA3）。
      - restricted → dept_internal/public（重新上线）：重新激活当前版本 chunk + 标脏 NOT_INDEXED
        （stage-3 重推），并撤销当前版本挂着的 PENDING_DELETE/DELETED（防 reconcile 撤销重上线）。
      - public ↔ dept_internal（均在检索内）：仅改级别 + 标脏重推，让 HA3 chunk 带上新 permission_level。
    并同步 chunk_meta.permission_level（检索/gate 读的去规范化副本），再经 materialize 重算 allowed_depts
    （dept_internal→投影 approved 授权；public/restricted→gate 清空）。RAG_ALLOWED_DEPTS_ACL 关时投影为
    no-op（仅改 RDS 元数据，dev/test 安全）；提交后失效 deny 缓存。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    if not req.doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id")
    # 显式管理动作：严格校验原值（别名 internal/private→dept_internal），未知/垃圾直接 400——不走
    # normalize_permission_level 的 fail-closed→restricted（那是 ingest 启发式路径），避免一个笔误静默归档文档。
    target = (req.permission_level or "").strip().lower()
    target = {"internal": "dept_internal", "private": "dept_internal"}.get(target, target)
    if target not in ("dept_internal", "public", "restricted"):
        raise HTTPException(status_code=400, detail="非法可见范围（须为 仅本部门 / 全公司 / 受限）")
    assert_metadata_write_allowed("kb_set_visibility", get_config().rds.host, kind="rds")
    trace_id = get_request_id()
    owner_dept = cur_perm = ""
    cur_ver = 1
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT owner_dept, permission_level, status, current_version_no "
                            f"FROM {_kb_db()}.document_meta WHERE doc_id=%s FOR UPDATE", (req.doc_id,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="文档不存在")
                owner_dept, cur_perm = (row[0] or ""), (row[1] or "")
                status, cur_ver = (row[2] or "active"), int(row[3] or 1)
                if not _kb_can_manage(kb, owner_dept):
                    raise HTTPException(status_code=403, detail="无权修改该文档（owner_dept 不在管理范围）")
                # public 涉及全公司可见 → 收窄/放宽均需 kb_admin（与 retire/restore/上传同款不对称）
                if (target == "public" or cur_perm == "public") and kb.role != ROLE_KB_ADMIN:
                    raise HTTPException(status_code=403, detail="涉及全公司公开的可见范围变更需知识库管理员操作")
                if str(status).lower() != "active":
                    raise HTTPException(status_code=409, detail="该文档非在线状态，请先恢复上线后再改可见范围")
                # 隔离文档（status 仍 'active'、级别被隔离流程收紧为 restricted）绝不能经本端点
                # "改回 dept_internal/public" 复活——那会把未脱敏 chunk 重新送进 stage-3 → HA3。
                _assert_version_not_quarantined(cur, req.doc_id, cur_ver)
                if cur_perm == target:
                    conn.commit()       # 幂等：级别未变
                    return KbSetVisibilityResponse(doc_id=req.doc_id, permission_level=target,
                                                   changed=False, already=True, note="可见范围未变化")
                # 1) 基础级别：document_meta + chunk_meta 去规范化副本（检索/gate 读后者）
                cur.execute(f"UPDATE {_kb_db()}.document_meta SET permission_level=%s, updated_at=NOW() "
                            "WHERE doc_id=%s", (target, req.doc_id))
                cur.execute(f"UPDATE {_kb_db()}.chunk_meta SET permission_level=%s "
                            "WHERE doc_id=%s AND version_no=%s", (target, req.doc_id, cur_ver))
                # 2) 检索存续：restricted=离开检索（停用 chunk）；重新上线=激活 chunk + 标脏重推
                if target == "restricted":
                    # 与 retire 同款【全部活跃版本】停用（不限当前版本）：双版本 gap 残留的旧版
                    # is_active=1 行只停当前版本会继续被检索/邻居拼接，且 HA3 清除漏删无限期滞留。
                    cur.execute(f"UPDATE {_kb_db()}.chunk_meta SET is_active=0 "
                                "WHERE doc_id=%s AND is_active=1", (req.doc_id,))
                    # 真实检索下线不能只靠 RDS：HA3 行仍带旧 permission_level（原 public 的对全员放行，
                    # deny gate 跳过 public 行）。喂 PENDING_DELETE outbox——stage-3 每轮
                    # reconcile_pending_deletes 自动 drain，删 HA3 后落 DELETED（全版本入队，顺带
                    # 清掉双版本残留）。
                    cur.execute(f"UPDATE {_kb_db()}.document_version "
                                f"SET index_status='{DocVersionIndexStatus.PENDING_DELETE}' "
                                f"WHERE doc_id=%s AND index_status NOT IN "
                                f"({sql_in_list((DocVersionIndexStatus.DELETED, DocVersionIndexStatus.PENDING_DELETE))})",
                                (req.doc_id,))
                else:
                    cur.execute(f"UPDATE {_kb_db()}.chunk_meta "
                                f"SET is_active=1, index_status='{ChunkIndexStatus.NOT_INDEXED}' "
                                "WHERE doc_id=%s AND version_no=%s AND is_active=0", (req.doc_id, cur_ver))
                    # 撤销可能在挂的 PENDING_DELETE（restricted→改回 未跑维护的窗口）：否则下轮
                    # reconcile 会删 HA3 后把 chunk 打回 is_active=0/DELETED，正好撤销这次重新上线。
                    # DELETED（已删过）也拨回 NOT_INDEXED，交 stage-3 重推。仅当前版本；旧版本照删。
                    cur.execute(f"UPDATE {_kb_db()}.document_version "
                                f"SET index_status='{DocVersionIndexStatus.NOT_INDEXED}' "
                                f"WHERE doc_id=%s AND version_no=%s AND index_status IN "
                                f"({sql_in_list((DocVersionIndexStatus.PENDING_DELETE, DocVersionIndexStatus.DELETED))})",
                                (req.doc_id, cur_ver))
                    if cur_perm != "restricted":
                        # 双向 public↔dept_internal（本就 is_active=1）：显式标脏，让 stage-3 重推带新级别的 chunk
                        cur.execute(f"UPDATE {_kb_db()}.chunk_meta "
                                    f"SET index_status='{ChunkIndexStatus.NOT_INDEXED}' "
                                    "WHERE doc_id=%s AND version_no=%s AND is_active=1", (req.doc_id, cur_ver))
                # 3) ACL 重投影（flag 门控，与主动共享/decide 同一注入点）：读己写的新 permission_level →
                #    dept_internal 投影 approved 授权 / public·restricted 清空 allowed_depts。
                if get_config().rag.allowed_depts_acl:
                    from opensearch_pipeline.access_grants import (
                        enqueue_acl_projection, materialize_doc_allowed_depts,
                    )
                    enqueue_acl_projection(cur, req.doc_id, reason="set_visibility")
                    try:
                        materialize_doc_allowed_depts(cur, req.doc_id)
                    except Exception as _pe:
                        logger.warning("set_visibility allowed_depts 内联标脏失败（outbox+reconciler 兜底）doc=%s: %s",
                                       req.doc_id, _pe)
                write_audit(doc_id=req.doc_id, version_no=cur_ver, action_type="SET_VISIBILITY",
                            operator_type="user", operator_id=kb.user_id, trace_id=trace_id,
                            message=f"owner={owner_dept} {cur_perm}->{target} reason={(req.reason or '')[:200]}",
                            cursor=cur)
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_set_visibility 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"修改可见范围失败 (trace: {trace_id})")
    try:
        from opensearch_pipeline.retriever import invalidate_deny_cache
        invalidate_deny_cache(req.doc_id or None)
    except Exception as _ie:   # noqa: BLE001
        logger.warning("deny 缓存失效失败（忽略，TTL 兜底）doc=%s: %s", req.doc_id, _ie)
    _note = {"restricted": "已归档受限：已停止新收录并排队索引清除；从检索彻底移除将在下次维护完成（可再改回恢复）",
             "public": "已改为全公司公开：下次维护重推后全员可检索",
             "dept_internal": "已改为仅本部门：下次维护重推后按部门权限过滤"}.get(target, "")
    return KbSetVisibilityResponse(doc_id=req.doc_id, permission_level=target, changed=True,
                                   already=False, note=_note)


class KbPendingItem(BaseModel):
    doc_id: str
    version_no: int = 1
    title: str = ""
    original_filename: str = ""
    owner_dept: str = ""
    permission_level: str = "public"
    owner_name: str = ""
    created_at: str = ""


class KbPendingResponse(BaseModel):
    items: List[KbPendingItem] = Field(default_factory=list)


@router.get("/api/kb/pending-approvals", response_model=KbPendingResponse)
def kb_pending_approvals(request: Request,
                         identity: Optional[Identity] = Depends(current_identity)):
    """kb_admin 待审批队列：列出 content_process_status='PENDING_APPROVAL' 的版本。仅 kb_admin。只读。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    if kb.role != ROLE_KB_ADMIN:
        raise HTTPException(status_code=403, detail="仅知识库管理员可查看审批队列")
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT m.doc_id, v.version_no, m.title, m.original_filename, m.owner_dept,
                           m.permission_level, m.owner_name, v.received_at
                    FROM {_kb_db()}.document_version v
                    JOIN {_kb_db()}.document_meta m ON m.doc_id = v.doc_id
                    WHERE v.content_process_status = 'PENDING_APPROVAL'
                    ORDER BY v.received_at DESC
                    LIMIT 100
                    """
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        trace_id = get_request_id()
        logger.error("kb_pending_approvals 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"待审批队列查询失败 (trace: {trace_id})")
    items = [
        KbPendingItem(
            doc_id=r[0] or "", version_no=int(r[1] or 1), title=r[2] or "",
            original_filename=r[3] or "", owner_dept=r[4] or "",
            permission_level=r[5] or "public", owner_name=r[6] or "",
            created_at=str(r[7]) if r[7] else "",
        )
        for r in rows
    ]
    return KbPendingResponse(items=items)
