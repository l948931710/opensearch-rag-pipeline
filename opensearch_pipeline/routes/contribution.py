# -*- coding: utf-8 -*-
"""
routes/contribution.py — 知识贡献域：员工众包问答提交/审核/采纳/入库重试、
贡献英雄榜、知识缺口清单（NO_RESULT/REFUSAL 归并）。见 schema/010。

F-A2 结构债拆分（2026-07-01）：从 api.py 机械搬移，行为不变。api.py 底部
include_router 并 re-export 全部端点函数/模型（tests 直接调用 api.<endpoint> /
引用 api.Kb* 模型）。本模块**不得**定义或遮蔽任何被 tests monkeypatch 的
api 属性（规则见 routes/__init__.py）。
"""

import os
import threading
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from opensearch_pipeline.qa_logger import _op_db
from opensearch_pipeline.request_context import get_request_id

# api 驻留共享件（模型/助手/依赖）。from-import 拷贝绑定在这里是安全的：
# 这些名字均不在 tests 的 api monkeypatch 清单内（见 routes/__init__.py）。
from opensearch_pipeline.api import (
    Identity,
    _enforce_rate_limit,
    _kb_can_manage,
    _kb_db,
    _kb_owner_scope_sql,
    _require_kb_console,
    current_identity,
    logger,
)

router = APIRouter()



# ═══════════════════════════════════════════════════════════════
# 知识贡献（员工众包问答 → 部门管理员采纳 → 走管线入库）
#   设计稿 Atlas Chat.dc.html「知识贡献」屏；数据=缺口（qa NO_RESULT/REFUSAL）+ kb_contribution。
#   ⚠️ review_status / ingestion_status 双生命周期解耦；采纳=幂等可恢复状态机（不假设 OSS+MySQL 原子）；
#      缺口仅在 ingestion_status='searchable' 后关闭；合成 .md 正文不含提交人姓名。见 schema/010。
# ═══════════════════════════════════════════════════════════════
_CONTRIB_COLS = ("contribution_id, question, content, category_dept, author_id, author_name, "
                 "review_status, ingestion_status, doc_id, review_note, created_at, reviewed_at")
_CONTRIB_WINDOW_DAYS = 30
_CONTRIB_CANDIDATE_CAP = 400   # 每源（NO_RESULT / REFUSAL）拉取的原始候选行上限，再在 py 内归一去重

# 缺口清单 TTL 缓存（perf#16）：kb_gaps 每次打开贡献页都全量重算两条重查询 + Python 聚合归并，
# 而可见范围只由用户的 depts 集决定 → 按 sorted(depts) 键缓存分页前的 open_gaps 全集 + summary，
# 同部门员工翻页/刷新直接内存切片（PII 脱敏留在渲染层，仅每页 ≤100 条）。缺口数据天级演化，
# 60s staleness 无感；提交/采纳物化两条主写路径主动清空（「等待入库」徽标即时可见），
# 驳回/重试由 TTL 兜底。部分子查询失败的降级结果不缓存。RAG_KB_GAPS_CACHE_TTL=0 关闭；conftest 每测清空。
_gaps_cache: dict = {}
_gaps_cache_lock = threading.Lock()

# reconcile 写-on-read 降噪（perf#84）：4 个 GET 端点读前对账原本每请求无条件发
# 2 条跨库 UPDATE + 1 commit。两道闸：① 进程内 60s 节流——稳态（无 registered 行）
# 下把后续请求降为零额外 DB 往返（--workers 1 单进程即权威）；② EXISTS 短路——
# 有 registered 行才发 UPDATE/commit。有贡献待对账时行为不变（EXISTS 命中不武装
# 节流，每请求照常对账直到 registered 清空）。节流状态并入 _gaps_cache_clear 统一
# 清理（conftest 每测清空复用同一钩子；提交/采纳写路径调用后即时解除节流）。
_RECONCILE_THROTTLE_S = 60.0
_reconcile_state = {"ts": 0.0}
_reconcile_lock = threading.Lock()


def _gaps_cache_ttl() -> float:
    try:
        return float(os.environ.get("RAG_KB_GAPS_CACHE_TTL", "60"))
    except ValueError:
        return 60.0


def _gaps_cache_clear() -> None:
    with _gaps_cache_lock:
        _gaps_cache.clear()
    # perf#84：连带解除 reconcile 节流（写路径产生的 registered 即时可对账）
    with _reconcile_lock:
        _reconcile_state["ts"] = 0.0


def _gaps_cache_get(key):
    if _gaps_cache_ttl() <= 0:
        return None
    with _gaps_cache_lock:
        ent = _gaps_cache.get(key)
        if ent is not None and ent[0] > time.time():
            return ent[1]
    return None


def _gaps_cache_put(key, value) -> None:
    ttl = _gaps_cache_ttl()
    if ttl <= 0:
        return
    with _gaps_cache_lock:
        _gaps_cache[key] = (time.time() + ttl, value)


class KbGapItem(BaseModel):
    question: str = ""             # 已脱敏的提问原文（展示用）
    asks: int = 0                  # COUNT(DISTINCT message_id)
    last_days: int = 0             # 距最近一次提问的天数
    dept: str = ""                 # 建议归属（NO_RESULT=提问部门 / REFUSAL=命中文档部门），仅展示
    kind: str = ""                 # 'no_result'（缺文档）| 'refusal'（有文档没答好）
    question_hash: str = ""
    source_message_id: str = ""    # 代表性 message_id（「回答」预填溯源）
    has_pending_contribution: bool = False   # 已有贡献待入库（缺口仍开放，标「等待入库」）


class KbGapsSummary(BaseModel):
    unanswered: int = 0
    answered: int = 0              # 已入库（searchable）贡献数
    this_month: int = 0           # 本月贡献数（含待审核）
    contributors: int = 0         # 本季活跃贡献者


class KbGapsResponse(BaseModel):
    items: List[KbGapItem] = Field(default_factory=list)
    summary: KbGapsSummary = Field(default_factory=KbGapsSummary)
    has_more: bool = False


class KbContributionItem(BaseModel):
    contribution_id: str = ""
    question: str = ""
    content: str = ""
    category_dept: str = ""
    author_id: str = ""
    author_name: str = ""
    review_status: str = "pending"
    ingestion_status: str = "none"
    state: str = "pending"         # 前端徽章码：pending|registering|searchable|failed|rejected
    doc_id: Optional[str] = None
    review_note: str = ""
    created_at: str = ""
    reviewed_at: Optional[str] = None


class KbContributionListResponse(BaseModel):
    items: List[KbContributionItem] = Field(default_factory=list)
    has_more: bool = False


class KbContributionSubmitRequest(BaseModel):
    question: str
    content: str
    category_dept: str
    source_message_id: Optional[str] = None
    gap_query: Optional[str] = None


class KbContributionAcceptRequest(BaseModel):
    # 采纳前可选修订（改 category_dept 必按新部门重做写授权）
    question: Optional[str] = None
    content: Optional[str] = None
    category_dept: Optional[str] = None
    # 部门领导采纳时决定可见范围：dept_internal=部门公开（默认）/ public=全员公开。
    # P2-16（2026-07-04 拍板「kb_admin 只管入库」，取代 2026-06-29「public 直通」裁决）：
    # dept_admin 选 public → 登记为 PENDING_APPROVAL 进 kb_admin 待审批队列，放行后才入库；
    # kb_admin 自己采纳 public 照旧直通（其即终审）。dept_internal 完全不变。
    permission_level: Optional[str] = None
    note: Optional[str] = None


class KbContributionRejectRequest(BaseModel):
    note: Optional[str] = None


class KbContributionActionResponse(BaseModel):
    contribution_id: str = ""
    review_status: str = "pending"
    ingestion_status: str = "none"
    state: str = "pending"
    doc_id: Optional[str] = None
    idempotent: bool = False
    ok: bool = True
    error: str = ""
    # P2-16：本次采纳/续跑的文档是否已进 kb_admin 待审批队列（前端据此提示"放行后才入库"）
    requires_kb_admin_approval: bool = False


class KbHeroItem(BaseModel):
    rank: int = 0
    author_id: str = ""
    author_name: str = ""
    count: int = 0


class KbHeroesResponse(BaseModel):
    items: List[KbHeroItem] = Field(default_factory=list)


def _contrib_item(row) -> "KbContributionItem":
    """把 _CONTRIB_COLS 顺序的 DB 行映射为响应项（state 由两条生命周期折叠）。"""
    from opensearch_pipeline import contribution as C
    cid, q, content, dept, aid, aname, rs, ing, did, note, created, reviewed = row
    return KbContributionItem(
        contribution_id=cid or "", question=q or "", content=content or "",
        category_dept=dept or "", author_id=aid or "", author_name=aname or "",
        review_status=rs or "pending", ingestion_status=ing or "none",
        state=C.contribution_state(rs, ing), doc_id=did, review_note=note or "",
        created_at=(created.isoformat() if created else ""),
        reviewed_at=(reviewed.isoformat() if reviewed else None),
    )


def _reconcile_contributions_searchable(conn) -> None:
    """懒式对账：registered 的贡献文档若 DAG 已索引成功→searchable，索引失败→failed。

    跨库 UPDATE...JOIN document_version。best-effort、非致命——辅助治理绝不拖垮读端点
    （任何异常只记 info 并放过；读端点仍按持久态展示）。

    ⚠️ doc_id 跨库 JOIN【必须】collation-cast：kb_contribution(fuling_operation, unicode_ci) ⋈
       document_version(fuling_knowledge)——后者若 _0900_ai_ci（staging _stg / 未显式 COLLATE 建库
       即漂移，与 kb_access_request 同坑：staging 实测 1267）直接 JOIN 报 1267 → 被本函数 try/except
       吞掉 → reconcile 静默永不 flip searchable。显式 COLLATE 强制统一比较；prod 两侧 unicode_ci 时为 no-op。

    perf#84 两道降噪闸（见模块级 _RECONCILE_THROTTLE_S 注释）：60s 节流 + EXISTS 短路。
    仅在 EXISTS 干净地返回「无 registered 行」时武装节流；EXISTS 命中或结果异常
    （驱动/桩未返回行）一律 fail open 走原对账路径，有贡献待对账时行为不变。
    """
    now = time.time()
    with _reconcile_lock:
        if now - _reconcile_state["ts"] < _RECONCILE_THROTTLE_S:
            return
    from opensearch_pipeline import contribution as C
    ok_in = ",".join("'%s'" % s for s in C.INDEX_OK_STATUSES)
    fail_in = ",".join("'%s'" % s for s in C.INDEX_FAIL_STATUSES)
    _doc_join = ("dv.doc_id = CONVERT(c.doc_id USING utf8mb4) COLLATE utf8mb4_unicode_ci"
                 " AND dv.version_no=1")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT EXISTS(SELECT 1 FROM {_op_db()}.kb_contribution"
                " WHERE ingestion_status='registered' LIMIT 1)")
            row = cur.fetchone()
            if row is not None and not row[0]:
                # 稳态（无待对账行）→ 武装节流，60s 内后续读请求零额外 DB 往返
                with _reconcile_lock:
                    _reconcile_state["ts"] = now
                return
            cur.execute(
                f"UPDATE {_op_db()}.kb_contribution c"
                f" JOIN {_kb_db()}.document_version dv ON {_doc_join}"
                " SET c.ingestion_status='searchable', c.searchable_at=NOW()"
                f" WHERE c.ingestion_status='registered' AND dv.index_status IN ({ok_in})")
            cur.execute(
                f"UPDATE {_op_db()}.kb_contribution c"
                f" JOIN {_kb_db()}.document_version dv ON {_doc_join}"
                " SET c.ingestion_status='failed', c.ingestion_error='索引失败（管线 index_status 异常）'"
                f" WHERE c.ingestion_status='registered' AND dv.index_status IN ({fail_in})")
            # P2-16 闭环：dept_admin 采纳 public 的贡献登记为 PENDING_APPROVAL 后，若被
            # kb_admin 在既有审批入口驳回（content_process_status='REJECTED'，永不入库），
            # 把贡献同步翻 failed——否则会永远停在「等待入库」（index_status 永不写入）。
            cur.execute(
                f"UPDATE {_op_db()}.kb_contribution c"
                f" JOIN {_kb_db()}.document_version dv ON {_doc_join}"
                " SET c.ingestion_status='failed',"
                " c.ingestion_error='全员公开发布被知识库管理员驳回'"
                " WHERE c.ingestion_status='registered'"
                "   AND dv.content_process_status='REJECTED'")
        conn.commit()
    except Exception as e:
        logger.info("contribution reconcile 跳过 (non-fatal): %s", e)


def _materialize_contribution(conn, *, doc_id: str, owner_dept: str, raw_key: str, bucket: str,
                              title: str, reviewer_id: str, reviewer_name: str, md_text: str,
                              permission_level: str = "dept_internal",
                              requires_approval: bool = False) -> None:
    """把合成 .md 写入 OSS + 登记 document_meta/version（NOT_STARTED，等下一批 DAG 入库）。

    全部以【固定 doc_id/raw_key】幂等执行：已登记（raw_key 命中）直接返回；document_version 唯一键
    1062（并发续跑）按幂等放过。失败上抛由调用方记 ingestion_error。

    P2-16：requires_approval=True（dept_admin 采纳 public）时不直通管线——与自助上传同一纪律
    （kb_console kb_register 同款 cps/appr 写法）：content_process_status='PENDING_APPROVAL'
    （stage-1 scanner 不认领、永不入索引）+ approval_status='PENDING'，自动进 kb_admin 既有
    待审批队列（/api/kb/pending-approvals）；kb_admin 在既有入口放行（→NOT_STARTED）后才随
    下一批 DAG 入库，驳回（→REJECTED）则永不入库（reconcile 把贡献翻 failed）。
    """
    import hashlib

    from opensearch_pipeline.oss_url import put_object

    data = md_text.encode("utf-8")
    size = len(data)
    etag = hashlib.sha256(data).hexdigest()[:32].upper()
    raw_key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    # original_filename 取 raw_key 真实 basename（= contribution-<contribution_id>.md），
    # 与 OSS 对象名严格一致——别另拼 doc_id，否则台账显示名与实际对象对不上。
    oss_filename = raw_key.rsplit("/", 1)[-1]
    kb_type = "public" if permission_level == "public" else "private"
    cps = "PENDING_APPROVAL" if requires_approval else "NOT_STARTED"
    appr = "PENDING" if requires_approval else "APPROVED"

    if not put_object(raw_key, data, "text/markdown; charset=utf-8"):
        raise RuntimeError("OSS 写入合成文档失败")

    with conn.cursor() as cur:
        # 幂等：固定 raw_key 已登记 → 直接返回（续跑/竞态安全）。谓词走 raw_key_hash 索引
        # （perf#5，schema/014；OR IS NULL 兜住回填前存量行，见 kb_register 同款注释）。
        cur.execute(f"SELECT doc_id, version_no FROM {_kb_db()}.document_version "
                    "WHERE raw_key=%s AND (raw_key_hash=%s OR raw_key_hash IS NULL) LIMIT 1",
                    (raw_key, raw_key_hash))
        if cur.fetchone():
            return
        cur.execute(
            f"""
            INSERT INTO {_kb_db()}.document_meta
              (doc_id, title, original_filename, owner_dept, owner_user_id, owner_name,
               category_l1, category_l2, permission_level, kb_type, status, current_version_no)
            VALUES (%s,%s,%s,%s,%s,%s,'reference','others',%s,%s,'active',1)
            ON DUPLICATE KEY UPDATE current_version_no=GREATEST(current_version_no,1), updated_at=NOW()
            """,
            (doc_id, (title or "")[:200], oss_filename, owner_dept,
             reviewer_id, reviewer_name or "", permission_level, kb_type),
        )
        try:
            # cps/appr 是本函数内的二值常量（非用户输入），直接内插进 SQL 文本无注入风险，
            # 且保持状态字面量在 SQL 里可见（tests 按 SQL 关键字断言）。
            cur.execute(
                f"""
                INSERT INTO {_kb_db()}.document_version
                  (doc_id, version_no, bucket_name, raw_key, raw_key_hash, etag, file_ext, mime_type,
                   file_size_bytes, content_process_status, approval_status, status, received_at)
                VALUES (%s,1,%s,%s,%s,%s,'md','text/markdown',%s,'{cps}','{appr}','active',NOW())
                """,
                (doc_id, bucket, raw_key, raw_key_hash, etag, size),
            )
        except Exception as ins_err:
            # uk_doc_version 1062：并发续跑撞键 → 赢家已登记，按幂等放过（不重复出文档）。
            if (getattr(ins_err, "args", None) or (None,))[0] != 1062:
                raise
            logger.info("contribution 物化并发幂等命中：raw_key=%s", raw_key)
    conn.commit()


def _finish_contribution_ingestion(cid: str, *, doc_id: str, raw_key: str, owner_dept: str,
                                   question: str, content: str, reviewer_id: str,
                                   reviewer_name: str, trace_id: str,
                                   requires_kb_admin_approval: bool = False):
    """采纳后的物化+登记（独立事务，幂等可重试）：成功→registered，失败→failed+ingestion_error。

    返回 (ingestion_status, error_or_None)。绝不假设跨系统原子——OSS 与 RDS 任一失败都记 failed，
    固定键留存，retry-ingestion 用同键续跑。
    P2-16：requires_kb_admin_approval=True → 登记为 PENDING_APPROVAL（见 _materialize_contribution），
    并 best-effort 通知 kb_admin（notify_upload_approval，no-raise）。
    """
    from opensearch_pipeline import contribution as C
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    from opensearch_pipeline.audit_log import write_audit
    from opensearch_pipeline.db import _get_db_conn

    from opensearch_pipeline import kb_upload
    cfg = get_config()
    md = C.synthesize_markdown(question, content)
    # 权限以【已固定的 raw_key 路径】为权威（accept 时按部门领导选择编码进路径；retry 续跑沿用同键）。
    permission_level = kb_upload.perm_from_raw_key(raw_key)
    try:
        assert_metadata_write_allowed("kb_contribution_materialize", cfg.rds.host, kind="rds")
        conn = _get_db_conn()
        try:
            _materialize_contribution(
                conn, doc_id=doc_id, owner_dept=owner_dept, raw_key=raw_key,
                bucket=cfg.oss.bucket_name, title=question, reviewer_id=reviewer_id,
                reviewer_name=reviewer_name, md_text=md, permission_level=permission_level,
                requires_approval=requires_kb_admin_approval)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {_op_db()}.kb_contribution SET ingestion_status='registered', "
                    "registered_at=NOW(), ingestion_error=NULL WHERE contribution_id=%s", (cid,))
            conn.commit()
        finally:
            conn.close()
        write_audit(doc_id=doc_id, version_no=1, action_type="CONTRIB_ADOPT",
                    operator_type="user", operator_id=reviewer_id, oss_key=raw_key,
                    trace_id=trace_id,
                    message=f"contribution={cid} owner={owner_dept}"
                            + (" pending_kb_approval" if requires_kb_admin_approval else ""))
        if requires_kb_admin_approval:
            # P2-16：进待审批队列后即时告知 kb_admin（既有挂点，内部 no-raise，失败只记 warning）
            from opensearch_pipeline.admin_notify import notify_upload_approval
            notify_upload_approval(owner_dept=owner_dept, title=question)
        _gaps_cache_clear()   # 采纳物化完成 → 缺口徽标状态变化即时可见
        return C.INGEST_REGISTERED, None
    except Exception as e:
        err = str(e)[:480]
        logger.error("contribution 物化失败 [trace=%s] cid=%s: %s", trace_id, cid, e, exc_info=True)
        try:
            conn2 = _get_db_conn()
            try:
                with conn2.cursor() as cur:
                    cur.execute(
                        f"UPDATE {_op_db()}.kb_contribution SET ingestion_status='failed', "
                        "ingestion_error=%s WHERE contribution_id=%s", (err, cid))
                conn2.commit()
            finally:
                conn2.close()
        except Exception as e2:
            logger.error("contribution 置 failed 也失败 cid=%s: %s", cid, e2)
        return C.INGEST_FAILED, err


@router.post("/api/kb/contributions", response_model=KbContributionItem)
def kb_contribution_submit(req: KbContributionSubmitRequest, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    """员工提交知识贡献（问答文本）。仅要求登录（员工即可）；status=pending 待部门管理员采纳。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    from opensearch_pipeline import contribution as C, kb_authz
    # P2-17：先剥离字面 <<IMG:N>> 图片占位符（防视觉引用伪造，见 contribution.strip_img_markers），
    # 再校验（剥完为空 → 正常走 400）。accept 侧对最终文本再过同一道（防先提交干净、采纳前改回）。
    q_clean = C.strip_img_markers(req.question).strip()
    c_clean = C.strip_img_markers(req.content).strip()
    verr = C.validate_contribution_text(q_clean, c_clean)
    if verr:
        raise HTTPException(status_code=400, detail=verr)
    depts = kb_authz.sanitize_owner_depts(req.category_dept)
    if not depts:
        raise HTTPException(status_code=400, detail="归属分类无效")
    category_dept = depts[0]
    cid = C.new_contribution_id()
    qhash = C.question_hash(q_clean)
    nq = C.normalize_question(q_clean)
    gq = (req.gap_query or "").strip() or None
    gqhash = C.question_hash(gq) if gq else None
    ngq = C.normalize_question(gq) if gq else None
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {_op_db()}.kb_contribution
                      (contribution_id, question, content, normalized_question, question_hash,
                       category_dept, suggested_dept, author_id, author_name,
                       review_status, ingestion_status, source_message_id, gap_query,
                       normalized_gap_query, gap_query_hash)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending','none',%s,%s,%s,%s)
                    """,
                    (cid, q_clean, c_clean, nq, qhash,
                     category_dept, category_dept, identity.user_id, identity.name or "",
                     (req.source_message_id or None), gq, ngq, gqhash),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.error("kb_contribution_submit 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"提交失败 (trace: {trace_id})")
    _gaps_cache_clear()   # 缺口的「等待入库」徽标即时可见，不等 TTL
    # 钉钉工作通知（RAG_ADMIN_NOTIFY 门控，best-effort no-raise）：归属部门管理员即时知晓待审核贡献
    from opensearch_pipeline.admin_notify import notify_contribution
    notify_contribution(category_dept=category_dept, question=q_clean)
    return KbContributionItem(
        contribution_id=cid, question=q_clean, content=c_clean,
        category_dept=category_dept, author_id=identity.user_id, author_name=identity.name or "",
        review_status="pending", ingestion_status="none", state="pending")


@router.get("/api/kb/contributions/mine", response_model=KbContributionListResponse)
def kb_contributions_mine(request: Request, limit: int = 20, offset: int = 0,
                          identity: Optional[Identity] = Depends(current_identity)):
    """我的贡献（按 author_id；含实时 4 态——读前先 reconcile registered→searchable）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    limit = max(1, min(int(limit or 20), 100)); offset = max(0, int(offset or 0))
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_contributions_mine 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"查询失败 (trace: {trace_id})")
    try:
        _reconcile_contributions_searchable(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_CONTRIB_COLS} FROM {_op_db()}.kb_contribution WHERE author_id=%s"
                " ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (identity.user_id, limit + 1, offset))
            rows = cur.fetchall() or []
    finally:
        conn.close()
    has_more = len(rows) > limit
    return KbContributionListResponse(items=[_contrib_item(r) for r in rows[:limit]], has_more=has_more)


@router.get("/api/kb/contributions/pending", response_model=KbContributionListResponse)
def kb_contributions_pending(request: Request, limit: int = 20, offset: int = 0,
                             identity: Optional[Identity] = Depends(current_identity)):
    """贡献审核队列（部门管理员：本部门 category_dept ∈ managed；kb_admin 全量）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    limit = max(1, min(int(limit or 20), 100)); offset = max(0, int(offset or 0))
    scope_clause, scope_params = _kb_owner_scope_sql(kb, "category_dept")
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_contributions_pending 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"查询失败 (trace: {trace_id})")
    try:
        _reconcile_contributions_searchable(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_CONTRIB_COLS} FROM {_op_db()}.kb_contribution"
                " WHERE review_status='pending' " + scope_clause
                + " ORDER BY created_at ASC LIMIT %s OFFSET %s",
                tuple(scope_params + [limit + 1, offset]))
            rows = cur.fetchall() or []
    finally:
        conn.close()
    has_more = len(rows) > limit
    return KbContributionListResponse(items=[_contrib_item(r) for r in rows[:limit]], has_more=has_more)


@router.post("/api/kb/contributions/{cid}/accept", response_model=KbContributionActionResponse)
def kb_contribution_accept(cid: str, req: KbContributionAcceptRequest, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    """采纳贡献（幂等可恢复状态机）：pending→accepted/registering（原子认领+固定键），再物化入库。

    可在采纳前修订 question/content/category_dept；改 category_dept 则按【新部门】重做写授权。
    已采纳→幂等返回（补跑物化交给 retry-ingestion）；已驳回→409。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline import contribution as C, kb_upload, kb_authz
    trace_id = get_request_id()
    # ── 阶段1：行锁认领（独立事务，commit 后才物化）──
    claim = None
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_contribution_accept 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"采纳失败 (trace: {trace_id})")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT review_status, ingestion_status, doc_id, upload_id, raw_key,"
                " question, content, category_dept"
                f" FROM {_op_db()}.kb_contribution WHERE contribution_id=%s FOR UPDATE", (cid,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=404, detail="贡献不存在")
            review_status = row[0] or "pending"
            ingestion_status = row[1] or "none"
            cur_doc_id, cur_upload_id, cur_raw_key = row[2], row[3], row[4]
            cur_q, cur_c, cur_dept = row[5], row[6], row[7]
            # 采纳前必须能管理贡献【原始】所属部门（与 reject/retry 一致）——否则 A 部门管理员
            # 可凭 cid 把 B 部门贡献改 category_dept 抢入 A 部门（authorize_upload 只校验目标部门）。
            if not _kb_can_manage(kb, cur_dept or ""):
                conn.rollback()
                raise HTTPException(status_code=403, detail="无权审核该部门的贡献")
            if review_status == C.REVIEW_REJECTED:
                conn.rollback()
                raise HTTPException(status_code=409, detail="该贡献已被驳回，不能采纳")
            if review_status == C.REVIEW_ACCEPTED:
                # 幂等：已采纳 → 直接返回当前态（物化补跑走 retry-ingestion）
                conn.rollback()
                return KbContributionActionResponse(
                    contribution_id=cid, review_status=review_status, ingestion_status=ingestion_status,
                    state=C.contribution_state(review_status, ingestion_status), doc_id=cur_doc_id,
                    idempotent=True, ok=(ingestion_status != C.INGEST_FAILED))
            # pending → 采纳（可修订）
            final_q = (req.question if req.question is not None else cur_q) or ""
            final_c = (req.content if req.content is not None else cur_c) or ""
            # P2-17：采纳侧对【最终】文本再剥一次字面 <<IMG:N>>（submit 侧已剥，但采纳前
            # 可修订 / 存量行可能带标记——防「先提交干净、采纳前改回」的绕过）。
            final_q = C.strip_img_markers(final_q).strip()
            final_c = C.strip_img_markers(final_c).strip()
            final_dept = (req.category_dept or cur_dept or "").strip()
            verr = C.validate_contribution_text(final_q, final_c)
            if verr:
                conn.rollback()
                raise HTTPException(status_code=400, detail=verr)
            # 部门领导采纳时定可见范围：dept_internal（部门公开，默认）/ public（全员公开）。
            chosen_perm = (req.permission_level or "dept_internal").strip().lower()
            chosen_perm = {"internal": "dept_internal", "private": "dept_internal"}.get(chosen_perm, chosen_perm)
            if chosen_perm not in ("dept_internal", "public"):
                conn.rollback()
                raise HTTPException(status_code=400, detail="可见范围只能是 部门公开 或 全员公开")
            # 按【最终】目标部门 + 选定可见范围做写授权（DB 现查的 kb；改部门即按新部门裁决）。
            # P2-16（2026-07-04 拍板「kb_admin 只管入库」，取代 2026-06-29「public 直通」裁决）：
            # 不再忽略 requires_kb_admin_approval——dept_admin 采纳 public 时物化为
            # PENDING_APPROVAL 进 kb_admin 既有待审批队列（与自助上传同一纪律），放行后才入库；
            # kb_admin 自己采纳 public 的 decision 本就不带该标记 → 照旧直通（其即终审）。
            decision = kb_authz.authorize_upload(kb, final_dept, chosen_perm)
            if not decision.allowed:
                conn.rollback()
                raise HTTPException(status_code=403, detail=f"无权采纳到部门「{final_dept}」：{decision.reason}")
            requires_kb_approval = bool(decision.requires_kb_admin_approval)
            # 一次性固定键（raw_key 把可见范围编码进路径段，防管线 stage-2 重解析升/降权）
            doc_id = cur_doc_id or kb_upload.new_doc_id()
            upload_id = cur_upload_id or kb_upload.new_ulid()
            raw_key = cur_raw_key or kb_upload.build_raw_key(
                final_dept, doc_id, upload_id, f"contribution-{cid}.md", permission_level=chosen_perm)
            cur.execute(
                f"UPDATE {_op_db()}.kb_contribution SET review_status='accepted',"
                " ingestion_status='registering', reviewed_by=%s, reviewed_at=NOW(),"
                " review_note=%s, doc_id=%s, upload_id=%s, raw_key=%s,"
                " question=%s, content=%s, category_dept=%s, normalized_question=%s, question_hash=%s"
                " WHERE contribution_id=%s AND review_status='pending'",
                (kb.user_id, (req.note or None), doc_id, upload_id, raw_key, final_q, final_c,
                 final_dept, C.normalize_question(final_q), C.question_hash(final_q), cid))
            claimed = getattr(cur, "rowcount", 1)
        conn.commit()
        if not claimed:
            # 竞态：他人已抢先推进 → 重读返回幂等，绝不二次物化
            with conn.cursor() as c2:
                c2.execute("SELECT review_status, ingestion_status, doc_id"
                           f" FROM {_op_db()}.kb_contribution WHERE contribution_id=%s", (cid,))
                r2 = c2.fetchone() or ("accepted", "registering", doc_id)
            return KbContributionActionResponse(
                contribution_id=cid, review_status=r2[0] or "accepted",
                ingestion_status=r2[1] or "registering",
                state=C.contribution_state(r2[0], r2[1]), doc_id=r2[2], idempotent=True, ok=True)
        claim = dict(doc_id=doc_id, raw_key=raw_key, owner_dept=final_dept,
                     question=final_q, content=final_c,
                     requires_kb_approval=requires_kb_approval)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_contribution_accept 认领失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"采纳失败 (trace: {trace_id})")
    finally:
        conn.close()
    # ── 阶段2：物化入库（独立事务，失败→failed+可重试，不回滚已 accepted 的审核决定）──
    ing, err = _finish_contribution_ingestion(
        cid, doc_id=claim["doc_id"], raw_key=claim["raw_key"], owner_dept=claim["owner_dept"],
        question=claim["question"], content=claim["content"],
        reviewer_id=kb.user_id, reviewer_name=kb.name or "", trace_id=trace_id,
        requires_kb_admin_approval=claim["requires_kb_approval"])
    return KbContributionActionResponse(
        contribution_id=cid, review_status="accepted", ingestion_status=ing,
        state=C.contribution_state("accepted", ing), doc_id=claim["doc_id"],
        ok=(ing != C.INGEST_FAILED), error=(err or ""),
        requires_kb_admin_approval=claim["requires_kb_approval"])


@router.post("/api/kb/contributions/{cid}/reject", response_model=KbContributionActionResponse)
def kb_contribution_reject(cid: str, req: KbContributionRejectRequest, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    """驳回贡献（部门管理员/kb_admin，按 category_dept 鉴权）。仅 pending 可驳；已驳回→幂等。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline import contribution as C
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_contribution_reject 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"驳回失败 (trace: {trace_id})")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT review_status, ingestion_status, doc_id, category_dept"
                        f" FROM {_op_db()}.kb_contribution WHERE contribution_id=%s FOR UPDATE", (cid,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=404, detail="贡献不存在")
            review_status, ingestion_status, doc_id, dept = (row[0] or "pending"), (row[1] or "none"), row[2], (row[3] or "")
            if not _kb_can_manage(kb, dept):
                conn.rollback()
                raise HTTPException(status_code=403, detail="无权审核该部门的贡献")
            if review_status == C.REVIEW_REJECTED:
                conn.rollback()
                return KbContributionActionResponse(contribution_id=cid, review_status="rejected",
                    ingestion_status=ingestion_status, state="rejected", doc_id=doc_id, idempotent=True, ok=True)
            if review_status == C.REVIEW_ACCEPTED:
                conn.rollback()
                raise HTTPException(status_code=409, detail="该贡献已采纳，不能驳回")
            cur.execute(f"UPDATE {_op_db()}.kb_contribution SET review_status='rejected',"
                        " reviewed_by=%s, reviewed_at=NOW(), review_note=%s"
                        " WHERE contribution_id=%s AND review_status='pending'",
                        (kb.user_id, (req.note or None), cid))
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_contribution_reject 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"驳回失败 (trace: {trace_id})")
    finally:
        conn.close()
    return KbContributionActionResponse(contribution_id=cid, review_status="rejected",
        ingestion_status="none", state="rejected", ok=True)


@router.post("/api/kb/contributions/{cid}/retry-ingestion", response_model=KbContributionActionResponse)
def kb_contribution_retry(cid: str, request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """重试入库（registering/failed → 用【固定键】续跑物化，绝不新建文档）。仅已采纳行。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline import contribution as C, kb_authz, kb_upload
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_contribution_retry 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"重试失败 (trace: {trace_id})")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT review_status, ingestion_status, doc_id, raw_key, category_dept,"
                        " question, content"
                        f" FROM {_op_db()}.kb_contribution WHERE contribution_id=%s FOR UPDATE", (cid,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=404, detail="贡献不存在")
            review_status, ingestion_status = (row[0] or "pending"), (row[1] or "none")
            doc_id, raw_key, dept, q, content = row[2], row[3], (row[4] or ""), row[5], row[6]
            if not _kb_can_manage(kb, dept):
                conn.rollback()
                raise HTTPException(status_code=403, detail="无权操作该部门的贡献")
            if review_status != C.REVIEW_ACCEPTED:
                conn.rollback()
                raise HTTPException(status_code=400, detail="仅已采纳的贡献可重试入库")
            if ingestion_status == C.INGEST_SEARCHABLE:
                conn.rollback()
                return KbContributionActionResponse(contribution_id=cid, review_status="accepted",
                    ingestion_status="searchable", state="searchable", doc_id=doc_id, idempotent=True, ok=True)
            if not doc_id or not raw_key:
                conn.rollback()
                raise HTTPException(status_code=409, detail="缺少固定键，无法续跑（数据异常）")
        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_contribution_retry 读取失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"重试失败 (trace: {trace_id})")
    finally:
        conn.close()
    # P2-16：续跑按「谁在完成物化」重裁 public 审批门（perm 以固定 raw_key 为权威；
    # kb_admin 续跑=其即终审→直通，dept_admin 续跑 public→仍进待审批）。裁决异常/被拒时
    # fail-closed 取「需审批」——宁可多过一道 kb_admin，绝不静默直通。已登记行（raw_key
    # 命中）物化幂等早退，该标记不生效，不会改写既有审批状态。
    try:
        _perm = kb_upload.perm_from_raw_key(raw_key)
        _d = kb_authz.authorize_upload(kb, dept, _perm)
        _requires_approval = (not _d.allowed) or bool(_d.requires_kb_admin_approval)
    except Exception:  # noqa: BLE001 — 裁决不可用 → 保守转审批（fail-closed）
        _requires_approval = True
    ing, err = _finish_contribution_ingestion(
        cid, doc_id=doc_id, raw_key=raw_key, owner_dept=dept, question=q, content=content,
        reviewer_id=kb.user_id, reviewer_name=kb.name or "", trace_id=trace_id,
        requires_kb_admin_approval=_requires_approval)
    return KbContributionActionResponse(contribution_id=cid, review_status="accepted",
        ingestion_status=ing, state=C.contribution_state("accepted", ing), doc_id=doc_id,
        ok=(ing != C.INGEST_FAILED), error=(err or ""),
        requires_kb_admin_approval=_requires_approval)


@router.get("/api/kb/contributions/heroes", response_model=KbHeroesResponse)
def kb_contribution_heroes(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """知识贡献英雄榜：按【已入库(searchable)】贡献数排名（真正闭环才计入）。全公司前 10。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception:
        return KbHeroesResponse(items=[])
    items: List[KbHeroItem] = []
    try:
        _reconcile_contributions_searchable(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT author_id, MAX(author_name), COUNT(*) c FROM {_op_db()}.kb_contribution"
                " WHERE ingestion_status='searchable' GROUP BY author_id ORDER BY c DESC LIMIT 10")
            for i, r in enumerate(cur.fetchall() or []):
                items.append(KbHeroItem(rank=i + 1, author_id=r[0] or "",
                                        author_name=r[1] or "", count=int(r[2] or 0)))
    except Exception as e:
        logger.info("heroes 查询失败（fail-open 空榜）: %s", e)
    finally:
        conn.close()
    return KbHeroesResponse(items=items)


@router.get("/api/kb/gaps", response_model=KbGapsResponse)
def kb_gaps(request: Request, limit: int = 20, offset: int = 0,
            identity: Optional[Identity] = Depends(current_identity)):
    """缺失知识（员工面向）：未答出的提问（NO_RESULT 缺文档 + REFUSAL 有文档没答好）。

    可见范围 = 本部门 + 全公司公开（最保守：混合命中 public/private 时，仅当【全部】命中文档为
    public 才进公开池）。query_text 展示前【无条件 PII 脱敏】。按归一化 question_hash 去重；已有
    searchable 贡献覆盖的缺口【关闭】（不再展示），accepted 未 searchable 的标「等待入库」。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    from opensearch_pipeline import contribution as C, kb_authz
    limit = max(1, min(int(limit or 20), 100)); offset = max(0, int(offset or 0))
    win = _CONTRIB_WINDOW_DAYS
    depts = kb_authz.sanitize_owner_depts(identity.acl_groups)
    trace_id = get_request_id()

    def _page_response(open_gaps, summary):
        """分页前全集 → 本页响应（PII 脱敏只做本页，供缓存命中/未命中两路共用）。"""
        page = open_gaps[offset:offset + limit]
        items = [KbGapItem(
            question=C.redact_query_text(g["raw"]), asks=g["asks"], last_days=g["days"],
            dept=g["dept"], kind=g["kind"], question_hash=g["hash"],
            source_message_id=g["msg"], has_pending_contribution=g["pending"]) for g in page]
        return KbGapsResponse(items=items, summary=summary, has_more=(offset + limit) < len(open_gaps))

    # 可见范围只由 depts 决定 → 按 sorted(depts) 键取缓存（同部门用户共享；权限判定在键构造前已完成）
    cache_key = tuple(sorted(depts))
    cached = _gaps_cache_get(cache_key)
    if cached is not None:
        return _page_response(cached[0], cached[1])
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_gaps 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"缺口查询失败 (trace: {trace_id})")
    # hash → 聚合体
    agg: Dict[str, Dict[str, Any]] = {}
    fails = 0

    def _accumulate(qtext, msg_id, days_ago, dept, kind):
        h = C.question_hash(qtext)
        if not h:
            return
        e = agg.get(h)
        if e is None:
            e = {"hash": h, "raw": qtext or "", "msgs": set(), "days": int(days_ago or 0),
                 "dept": dept or "", "kind": kind}
            agg[h] = e
        if msg_id:
            e["msgs"].add(msg_id)
        e["days"] = min(e["days"], int(days_ago or 0))
        if not e["dept"] and dept:
            e["dept"] = dept
        # REFUSAL（有文档没答好）信号优先于纯 NO_RESULT 展示 kind
        if kind == "refusal":
            e["kind"] = "refusal"

    summary = KbGapsSummary()
    try:
        _reconcile_contributions_searchable(conn)
        with conn.cursor() as cur:
            # 1) NO_RESULT（缺文档）：按提问部门归属（仅建议）→ 本部门可见
            if depts:
                try:
                    ph = ",".join(["%s"] * len(depts))
                    cur.execute(
                        "SELECT q.query_text, q.message_id, DATEDIFF(NOW(), q.created_at), q.user_dept"
                        f" FROM {_op_db()}.qa_session_log q"
                        " WHERE q.answer_status='NO_RESULT'"
                        "   AND q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                        f"   AND q.user_dept IN ({ph})"
                        " ORDER BY q.created_at DESC LIMIT %s",
                        tuple([win] + depts + [_CONTRIB_CANDIDATE_CAP]))
                    for r in cur.fetchall() or []:
                        _accumulate(r[0], r[1], r[2], r[3], "no_result")
                except Exception as e:
                    fails += 1; logger.warning("kb_gaps NO_RESULT 失败: %s", e)
            # 2) REFUSAL（有文档没答好）：本部门命中 OR 全部命中为 public（最保守）
            try:
                mine_expr = "0"
                # F-kb-gaps-sql 占位符须按 SQL 文本顺序绑定：depts(mine_expr@SELECT) → win(WHERE) → cap(LIMIT)
                params: List[Any] = []
                if depts:
                    ph = ",".join(["%s"] * len(depts))
                    mine_expr = f"MAX(CASE WHEN m.owner_dept IN ({ph}) THEN 1 ELSE 0 END)"
                    params.extend(depts)
                params.append(win)
                from opensearch_pipeline.qa_facts import qa_docs_join_sql
                cur.execute(
                    "SELECT t.query_text, t.message_id, t.days_ago, t.any_dept FROM ("
                    " SELECT q.message_id,"
                    "   MAX(q.query_text) query_text, DATEDIFF(NOW(), MAX(q.created_at)) days_ago,"
                    f"   {mine_expr} hit_mine,"
                    "   MIN(CASE WHEN m.permission_level='public' THEN 1 ELSE 0 END) all_public,"
                    "   MIN(m.owner_dept) any_dept"
                    f" FROM {_op_db()}.qa_session_log q"
                    + qa_docs_join_sql()
                    + " WHERE q.answer_status='REFUSAL' AND q.retrieved_docs_json IS NOT NULL"
                    "   AND q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    " GROUP BY q.message_id"
                    ") t WHERE t.hit_mine=1 OR t.all_public=1"
                    " ORDER BY t.days_ago ASC LIMIT %s",
                    tuple(params + [_CONTRIB_CANDIDATE_CAP]))
                for r in cur.fetchall() or []:
                    _accumulate(r[0], r[1], r[2], r[3], "refusal")
            except Exception as e:
                fails += 1; logger.warning("kb_gaps REFUSAL 失败: %s", e)
            # 3) 贡献覆盖：同 hash 已 searchable→关闭；pending/accepted-未searchable→标等待入库
            covered_closed: Set[str] = set()
            covered_pending: Set[str] = set()
            if agg:
                try:
                    hl = list(agg.keys())
                    ph = ",".join(["%s"] * len(hl))
                    cur.execute(
                        "SELECT question_hash, review_status, ingestion_status"
                        f" FROM {_op_db()}.kb_contribution WHERE question_hash IN ({ph})", tuple(hl))
                    for hh, rs, ing in cur.fetchall() or []:
                        if ing == C.INGEST_SEARCHABLE:
                            covered_closed.add(hh)
                        elif rs == C.REVIEW_PENDING or (rs == C.REVIEW_ACCEPTED and ing != C.INGEST_SEARCHABLE):
                            covered_pending.add(hh)
                except Exception as e:
                    fails += 1; logger.warning("kb_gaps 覆盖查询失败: %s", e)
            # 4) summary（各自独立降级）
            try:
                cur.execute(f"SELECT COUNT(*) FROM {_op_db()}.kb_contribution WHERE ingestion_status='searchable'")
                summary.answered = int((cur.fetchone() or (0,))[0] or 0)
                cur.execute(f"SELECT COUNT(*) FROM {_op_db()}.kb_contribution"
                            " WHERE YEAR(created_at)=YEAR(NOW()) AND MONTH(created_at)=MONTH(NOW())")
                summary.this_month = int((cur.fetchone() or (0,))[0] or 0)
                cur.execute(f"SELECT COUNT(DISTINCT author_id) FROM {_op_db()}.kb_contribution"
                            " WHERE created_at >= DATE_SUB(NOW(), INTERVAL 90 DAY)")
                summary.contributors = int((cur.fetchone() or (0,))[0] or 0)
            except Exception as e:
                fails += 1; logger.warning("kb_gaps summary 失败: %s", e)
    finally:
        conn.close()
    if fails >= 4:
        raise HTTPException(status_code=500, detail=f"缺口查询失败 (trace: {trace_id})")
    # 组装：去掉已 searchable 覆盖的缺口；排序 asks desc, days asc；脱敏 + 分页
    open_gaps = []
    for h, e in agg.items():
        if h in covered_closed:
            continue
        open_gaps.append({
            "hash": h, "raw": e["raw"], "asks": len(e["msgs"]) or 1, "days": e["days"],
            "dept": e["dept"], "kind": e["kind"], "msg": next(iter(e["msgs"]), ""),
            "pending": h in covered_pending,
        })
    open_gaps.sort(key=lambda g: (-g["asks"], g["days"]))
    summary.unanswered = len(open_gaps)
    if fails == 0:   # 降级结果（部分子查询失败）不缓存——下一请求重试取全量
        _gaps_cache_put(cache_key, (open_gaps, summary))
    return _page_response(open_gaps, summary)
