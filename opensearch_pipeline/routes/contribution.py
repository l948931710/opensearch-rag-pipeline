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
    _kb_managed_descendants,
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
                 "review_status, ingestion_status, doc_id, review_note, created_at, reviewed_at, "
                 "source_message_id, gap_query, "   # 缺口溯源透出（批次ε-1，写侧一直在存）
                 "ingestion_error")                 # 失败原因透出（批次ε-2——作者不再瞎重试）
# node 轴列清单（schema/067 已 apply 时才用）：**独立常量**而非直接扩 _CONTRIB_COLS——
# 067 未 apply 的环境引用该列会 1054 打挂全部贡献列表。行长度差异由 _contrib_item 吸收。
_CONTRIB_COLS_NODE = _CONTRIB_COLS + ", category_dept_id"
# 批次ε-4 拍板解耦：缺口列表与 asks 热度是两套口径，绝不共享常量——
#   缺口窗 365 天（去「老缺口静默过期」；当下=系统 QA 全量历史，长期保留为增长边界；
#   真·无窗需 qa_session_log 加 hash 列/缺口物化表=远期立项）；cap 随窗放大。
#   asks（审核队列「近 30 天被问 N 次」chip）语义=近期热度，必须钉 30 天/400。
_GAP_WINDOW_DAYS = 365
_GAP_CANDIDATE_CAP = 2000      # 每源（NO_RESULT / REFUSAL）拉取的原始候选行上限，再在 py 内归一去重
_CONTRIB_WINDOW_DAYS = 30      # asks 专属（_pending_asks）；勿再喂 kb_gaps
_CONTRIB_CANDIDATE_CAP = 400   # asks 专属候选上限

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

# 内容阶段自动重试上限——与 dataworks_orchestrator 的 stage-2 认领谓词
# （content_process_status='FAILED' AND retry_count < 3，两处 SQL）同值同义：
# retry_count < 3 的 FAILED 行下一批 DAG 还会自动续跑（毒文档机制，非死链），
# reconcile 只对「重试已用尽」的行判死。改那边的上限必须同步改这里，否则
# 「还会自愈的行被提前判死」（提前翻 failed 后即使 DAG 自愈也不会再翻 searchable）
# 或「真死行永不翻」。
_CS_RETRY_EXHAUSTED = 3


def _gaps_cache_ttl() -> float:
    try:
        return float(os.environ.get("RAG_KB_GAPS_CACHE_TTL", "60"))
    except ValueError:
        return 60.0


# ── 无效问题分层过滤（2026-07-18 拍板）：判定纯函数与 flag 单一来源都在
# opensearch_pipeline/contribution.py（junk_filter_on / hide_incomplete_on /
# is_junk_question / is_incomplete_question）；本模块只做读侧挂点。
from opensearch_pipeline.contribution import (   # noqa: E402
    hide_incomplete_on as _hide_incomplete_on,
    junk_filter_on as _junk_filter_on,
)


# schema/050（rewritten_query 列）未 apply 时的 TTL 负缓存：到期自动重试探测，
# apply 后无须重启即恢复带列查询；**仅 errno 1054 走降级**，其他 SQL 错误照抛。
_GAP_REWRITTEN_MISSING_UNTIL = 0.0
_GAP_REWRITTEN_RETRY_SECONDS = 600.0


def _exec_gap_sql(cur, sql_with_rw: str, sql_without_rw: str, params) -> bool:
    """执行缺口候选 SQL：优先带 rewritten_query 列的版本，1054（列缺失）→ TTL 负缓存
    并回退无列版本。返回 True=行尾带 rewritten 列。"""
    global _GAP_REWRITTEN_MISSING_UNTIL
    if time.time() >= _GAP_REWRITTEN_MISSING_UNTIL:
        try:
            cur.execute(sql_with_rw, params)
            return True
        except Exception as e:   # noqa: BLE001 — 仅 1054 降级，其余照抛
            errno = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
            if errno != 1054:
                raise
            _GAP_REWRITTEN_MISSING_UNTIL = time.time() + _GAP_REWRITTEN_RETRY_SECONDS
            logger.info("kb_gaps rewritten_query 列缺失（schema/050 未 apply），"
                        "%.0fs 内回退无列查询", _GAP_REWRITTEN_RETRY_SECONDS)
    cur.execute(sql_without_rw, params)
    return False


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
    question: str = ""             # 已脱敏的提问（展示用；追问改写发生时优先展示改写后的独立问题）
    asks: int = 0                  # COUNT(DISTINCT message_id)
    last_days: int = 0             # 距最近一次提问的天数
    dept: str = ""                 # 建议归属（NO_RESULT=提问部门 / REFUSAL=命中文档部门），仅展示
    kind: str = ""                 # 'no_result'（缺文档）| 'refusal'（有文档没答好）
    question_hash: str = ""
    source_message_id: str = ""    # 代表性 message_id（「回答」预填溯源）
    has_pending_contribution: bool = False   # 已有贡献待入库（缺口仍开放，标「等待入库」）
    phrasings: int = 1             # 语义组归并后的成员问法数（RAG_QA_GAP_SEMANTIC 关时恒 1；additive）
    # 缺口卡上下文展开（2026-07-18；additive）：representative_message_id 与
    # source_message_id 同值（语义显名，前端上下文展开用它）；has_context=该代表提问
    # 所在会话有前序问答（前端仅 true 时渲染「查看上下文」）。
    representative_message_id: str = ""
    has_context: bool = False


class KbGapsSummary(BaseModel):
    unanswered: int = 0
    answered: int = 0              # 已入库（searchable）贡献数
    this_month: int = 0           # 本月提交数（含待审核/已驳回——UI hint 如实标注，批次ε-3 R3）
    contributors: int = 0         # 近 90 天有提交的贡献者数
    # 审核漏斗（批次ε-3 R3；2026-07-15 拍板收敛：**API 层只给管理员**，员工响应恒 None）：
    # 近 30 天按 reviewed_at，列现成纯聚合；None=算不出/无样本/非管理员。按请求身份在
    # _page_response 补注，**绝不进共享缓存**（gaps 缓存按 sorted(depts) 跨用户共享，
    # 进缓存=管理员填的数字漏给同部门员工）。
    review_accept_rate_30d: Optional[float] = None    # accepted/(accepted+rejected)，分母 0→None
    review_avg_hours_30d: Optional[float] = None      # AVG(created_at→reviewed_at)，小时


class KbGapsResponse(BaseModel):
    items: List[KbGapItem] = Field(default_factory=list)
    summary: KbGapsSummary = Field(default_factory=KbGapsSummary)
    has_more: bool = False
    # 缺口滚动窗（批次ε-3 R3 下发防漂移；ε-4 拍板 365）：默认值必须与查询侧同源同常量——
    # 只改查询不改这里=「实际查 365 天、响应报 30」的静默失真（ε-4 审计点名的地雷）。
    window_days: int = _GAP_WINDOW_DAYS


class KbContributionItem(BaseModel):
    contribution_id: str = ""
    question: str = ""
    content: str = ""
    category_dept: str = ""
    # 归属组织节点（node 轴权威，schema/067）：None=组码轴历史行 ⇒ 判定回落 category_dept。
    # ⚠️ 前端**不得**由 category_dept 反推本字段（组码→dept_id 一对多，见方案 M8/M11）。
    category_dept_id: Optional[int] = None
    author_id: str = ""
    author_name: str = ""
    review_status: str = "pending"
    ingestion_status: str = "none"
    state: str = "pending"         # 前端徽章码：pending|registering|searchable|failed|rejected
    doc_id: Optional[str] = None
    review_note: str = ""
    created_at: str = ""
    reviewed_at: Optional[str] = None
    # 缺口溯源（批次ε-1）：来自「待回答」缺口的贡献带原提问上下文，审核队列据此显「来自缺口」。
    source_message_id: Optional[str] = None
    gap_query: Optional[str] = None
    # 失败原因（批次ε-2）：failed 行透出 ingestion_error（DB 自始有列此前不透出，作者只能瞎重试）
    ingestion_error: Optional[str] = None
    # 被引用次数（批次ε-2 R2，仅 mine 端点回填；语义同 KbHeroItem.hits：None=算不出，0=真零）
    hits: Optional[int] = None
    # 管线徽章（批次ε-3 R1，仅 mine 端点对 registering 行回填）：复用台账 _kb_status_badge
    # 词表（待审核=卡 kb_admin 待放行 / 已隔离·未入索引=管线死链 / 排队中·处理中=正常）。
    # 作者据此分辨「等人放行」「死局需重投」「正常排队」——此前三者同显「已采纳·待入库」。
    # None=算不出/无对应版本行（P2-16 时序竞态），前端回落默认徽标。纯读侧派生，不动状态机。
    doc_badge: Optional[str] = None
    # 被问次数（批次ε-3 R2，仅 pending 队列回填）：近 30 天 NO_RESULT+REFUSAL 提问里与本贡献
    # 同 hash（COALESCE(gap_query_hash, question_hash)）的去重 message 数——审核人据此判优先级。
    # 有意不按部门过滤（真实提问热度；纯计数不回传原文，无泄露面）。None=算不出，0=真零。
    asks: Optional[int] = None


class KbContributionListResponse(BaseModel):
    items: List[KbContributionItem] = Field(default_factory=list)
    has_more: bool = False


class KbContributionSubmitRequest(BaseModel):
    question: str
    content: str
    # 组码轴归属（node 轴提交时不传/忽略）。node 轴提交给 category_dept_id。
    category_dept: str = ""
    # 归属组织节点（Sam 裁决 A：提交人选，默认=其所在部门；来源只能是 /api/kb/my-depts）。
    # 给了它就走 node 轴——服务端不信客户端传值，另查 dept_dim is_active（方案 M5 第 1 处）。
    category_dept_id: Optional[int] = None
    source_message_id: Optional[str] = None
    gap_query: Optional[str] = None


class KbContributionAcceptRequest(BaseModel):
    # 采纳前可选修订（改归属必按新归属重做写授权；node 轴改归属须**双端管辖**，方案 M5）
    question: Optional[str] = None
    content: Optional[str] = None
    category_dept: Optional[str] = None
    category_dept_id: Optional[int] = None
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


# ── 总积分榜预览权重（Sam 2026-08-01 拍板 10/1/3）──────────────────────────────
# 口径=docs/ai_reward_metrics_2026-07-27.md：采纳=⑦(searchable) / 被引用=cited 事实表 /
# 有效反馈=④⑤(downvote+RESOLVED+首报按 message_id 去重)且按①限渠道(console+小程序)。
# 活跃/提问量**刻意不进**个人分（07-27 结论：量化消耗≠价值）。
# ⚠️ 这是【预览口径】：正式积分换算以《AI参与奖励管理办法》定稿为准（F1 试行稿在等
# 综合管理中心二稿）；权重单一来源在此，改动须同步口径文件留修订记录。
HERO_SCORE_W_ADOPTED = 10
HERO_SCORE_W_HIT = 1
HERO_SCORE_W_FEEDBACK = 3


class KbHeroItem(BaseModel):
    rank: int = 0
    author_id: str = ""
    author_name: str = ""
    count: int = 0                       # =adopted（旧字段名保留兼容）
    # 被引用次数（批次ε-2 R2）：cited=True 口径（与 Phase E 价值类看板同源）、全期窗口。
    # None=算不出（事实表路径不可用/查询失败——诚实 NULL 纪律，绝不用 0 顶替）；0=真零引用。
    hits: Optional[int] = None
    # 总积分（2026-08-01）：10×采纳 + 1×引用 + 3×有效反馈。hits=None 时引用项按 0 计
    # （分数如实少算，不伪造）；构成三项随行回传供 UI 副行展示（可审计：任何人质疑
    # 都能指到构成与口径定义）。
    adopted: int = 0
    feedback: int = 0
    score: int = 0


class KbDeptScoreItem(BaseModel):
    """部门总积分行（2026-08-01 三榜切换）：同一套 10/1/3 个人分按**一级部门（中心）**求和
    ——不引入新指标，不依赖奖励办法的部门口径（周活跃/有效问答占比那套等办法二稿）。
    取总和不取人均：人均会奖励「少参与保均值」（口径⑧同类顾虑），总和 + 参与人数并示。"""
    rank: int = 0
    dept_id: int = 0
    dept_name: str = ""
    members: int = 0                     # 有积分的参与人数（非在册总人数）
    score: int = 0


class KbHeroesResponse(BaseModel):
    items: List[KbHeroItem] = Field(default_factory=list)          # 全公司个人 TOP10（兼容字段）
    # 三榜切换（2026-08-01）：归属来自 staff_dim.primary_dept → dept_dim 上溯到一级部门。
    # 组织快照缺失/账号未在册 → 对应榜为空（全公司榜不受影响，优雅降级）。
    dept_items: List[KbDeptScoreItem] = Field(default_factory=list)   # 部门榜 TOP10
    my_dept_items: List[KbHeroItem] = Field(default_factory=list)     # 本部门个人 TOP10
    my_dept_name: str = ""                                            # 请求者的一级部门名（空=未在册）


def _contrib_item(row) -> "KbContributionItem":
    """把 _CONTRIB_COLS（或 _CONTRIB_COLS_NODE）顺序的 DB 行映射为响应项（state 由两条生命周期折叠）。

    多出的第 16 列 = category_dept_id（067 已 apply 时才被 SELECT）；缺席 ⇒ None ⇒ 组码轴行。
    """
    from opensearch_pipeline import contribution as C
    (cid, q, content, dept, aid, aname, rs, ing, did, note, created, reviewed,
     src_msg, gapq, ing_err) = row[:15]
    dept_id = row[15] if len(row) > 15 else None
    return KbContributionItem(
        contribution_id=cid or "", question=q or "", content=content or "",
        category_dept=dept or "", category_dept_id=(int(dept_id) if dept_id else None),
        author_id=aid or "", author_name=aname or "",
        review_status=rs or "pending", ingestion_status=ing or "none",
        state=C.contribution_state(rs, ing), doc_id=did, review_note=note or "",
        created_at=(created.isoformat() if created else ""),
        reviewed_at=(reviewed.isoformat() if reviewed else None),
        source_message_id=(src_msg or None), gap_query=(gapq or None),
        ingestion_error=(ing_err or None),
    )


def _reconcile_contributions_searchable(conn) -> None:
    """懒式对账：registered 的贡献文档若 DAG 已索引成功→searchable；索引失败 /
    kb_admin 驳回 / 内容阶段失败且自动重试用尽（ε-5 审计 P0 谓词对齐）→failed。

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
            # ε-5 审计 P0 根治（cs=FAILED 谓词对齐）：内容阶段失败且自动重试已用尽的行是
            # 真死链（orchestrator 认领谓词 retry_count<3 之外的 FAILED 行永不再被拾起，此前
            # 永停 registered）→ 翻显式 failed 终态。retry_count 守卫是认领谓词的镜像补集：
            # <3 的行明天还会自愈，绝不提前判死（NULL 不满足 <3、同样不会被认领 → 一并判死）。
            # 出路：员工「修改重交」自助；管理员「重试入库」（retry 端点对 FAILED 文档同步
            # 重置 NOT_STARTED+retry_count=0 重新排队，见 _requeue_failed_doc）。不通知、
            # 不自动重跑——与本函数其余谓词同为读侧诚实化。
            cur.execute(
                f"UPDATE {_op_db()}.kb_contribution c"
                f" JOIN {_kb_db()}.document_version dv ON {_doc_join}"
                " SET c.ingestion_status='failed',"
                " c.ingestion_error='内容处理失败且自动重试已用尽——可修改后重新提交，或由管理员重试入库'"
                " WHERE c.ingestion_status='registered'"
                "   AND dv.content_process_status='FAILED'"
                f"   AND (dv.retry_count >= {_CS_RETRY_EXHAUSTED} OR dv.retry_count IS NULL)")
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


def _materialize_contribution_node(conn, *, doc_id: str, owner_dept_id: int, raw_key: str,
                                   bucket: str, title: str, reviewer_id: str, reviewer_name: str,
                                   md_text: str, permission_level: str = "dept_internal",
                                   requires_approval: bool = False) -> None:
    """**node 归属**的物化+登记（方案 M4，Codex C7 BLOCKER）。

    与上面的 legacy 版**并列**，后者逐字不动 —— legacy 物化只写 `owner_dept`，对 node 归属
    的贡献而言那是「登记了一篇谁都搜不到、且组码残值可能命中别的管理员」的文档。

    同一事务内原子完成 5 件事（任一失败 ⇒ **数据库侧整笔回滚**，`ingestion_status` 保持可重试态）：
      1. `document_meta`：`owner_dept=NULL` + `acl_mode='node'` + `owner_dept_id` + active/v1
      2. `document_version`：与 legacy 版同形（cps/appr 沿用现有 public 审批语义）
      3. `kb_doc_node_grant (doc_id, owner_dept_id, scope='subtree')` —— **Sam 裁决 D**。
         🔴 缺这条则可见集为空 ⇒ 文档谁都搜不到（node 文档的可见性**完全**来自这张表）
      4. `record_acl_projection_invalidation`（acl_epoch bump + 投影 outbox 入队）
      5. raw_key 由调用方按 `kb_upload.node_storage_segment` 编好（防 stage-1 按路径段重派归属）

    ⚠️ **这不是跨 OSS+DB 的原子事务**（Codex 三轮 NEW CONCERN）：与 legacy 版一样先写 OSS 再开
    DB 写入，DB 回滚撤不回已落的对象。清理沿用既有的「同 raw_key 重试覆盖」策略——raw_key 由
    `(doc_id, upload_id)` 固定，重试写同一对象，不产生孤儿。
    """
    import hashlib

    from opensearch_pipeline.access_grants import record_acl_projection_invalidation
    from opensearch_pipeline.api import _kb_node_capability
    from opensearch_pipeline.oss_url import put_object

    data = md_text.encode("utf-8")
    size = len(data)
    etag = hashlib.sha256(data).hexdigest()[:32].upper()
    raw_key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    oss_filename = raw_key.rsplit("/", 1)[-1]
    kb_type = "public" if permission_level == "public" else "private"
    cps = "PENDING_APPROVAL" if requires_approval else "NOT_STARTED"
    appr = "PENDING" if requires_approval else "APPROVED"
    oid = int(owner_dept_id)

    # ⚠️ capability 检查**必须早于 put_object**（评审 R2）：060 未 apply 时这条路径是
    # **确定性**失败（capability 不会自愈），而 docstring 那句「raw_key 固定、重试写同一对象、
    # 不产生孤儿」的免责只对**可恢复**失败成立——永远等不到成功的那一次覆盖，每次 accept/retry
    # 都留一个再也不会被写第二次的 OSS 对象。检查前移是纯顺序调整，正常路径语义零变化。
    with conn.cursor() as cur:
        if _kb_node_capability(cur) != "present":
            # 060 未 apply ⇒ 写不出合法的 node 文档；宁可失败可重试，也不退化成 legacy
            # （退化 = owner_dept 落 NULL/空 + 无 grant，文档谁都搜不到且归属不可判）
            raise RuntimeError("node-ACL schema 未就绪（060 未 apply），拒绝以 node 归属登记")

    if not put_object(raw_key, data, "text/markdown; charset=utf-8"):
        raise RuntimeError("OSS 写入合成文档失败")

    try:
        with conn.cursor() as cur:
            # 幂等：固定 raw_key 已登记 → 直接返回（续跑/竞态安全；谓词同 legacy 版走 raw_key_hash 索引）
            cur.execute(f"SELECT doc_id, version_no FROM {_kb_db()}.document_version "
                        "WHERE raw_key=%s AND (raw_key_hash=%s OR raw_key_hash IS NULL) LIMIT 1",
                        (raw_key, raw_key_hash))
            if cur.fetchone():
                conn.rollback()
                return
            cur.execute(
                f"""
                INSERT INTO {_kb_db()}.document_meta
                  (doc_id, title, original_filename, owner_dept, owner_user_id, owner_name,
                   category_l1, category_l2, permission_level, kb_type, status,
                   current_version_no, acl_mode, owner_dept_id)
                VALUES (%s,%s,%s,NULL,%s,%s,'reference','others',%s,%s,'active',1,'node',%s)
                ON DUPLICATE KEY UPDATE current_version_no=GREATEST(current_version_no,1),
                                        updated_at=NOW()
                """,
                (doc_id, (title or "")[:200], oss_filename,
                 reviewer_id, reviewer_name or "", permission_level, kb_type, oid),
            )
            try:
                # cps/appr 是本函数内的二值常量（非用户输入），直接内插无注入风险（同 legacy 版）
                cur.execute(
                    f"""
                    INSERT INTO {_kb_db()}.document_version
                      (doc_id, version_no, bucket_name, raw_key, raw_key_hash, etag, file_ext,
                       mime_type, file_size_bytes, content_process_status, approval_status,
                       status, received_at)
                    VALUES (%s,1,%s,%s,%s,%s,'md','text/markdown',%s,'{cps}','{appr}','active',NOW())
                    """,
                    (doc_id, bucket, raw_key, raw_key_hash, etag, size),
                )
            except Exception as ins_err:
                if (getattr(ins_err, "args", None) or (None,))[0] != 1062:
                    raise
                logger.info("contribution[node] 物化并发幂等命中：raw_key=%s", raw_key)
            # Sam 裁决 D：默认可见范围 = 归属节点 subtree（与摄取侧 node 默认授权一致）
            cur.execute(
                f"INSERT INTO {_kb_db()}.kb_doc_node_grant "
                "(doc_id, dept_id, scope, granted_by, note) VALUES (%s,%s,'subtree',%s,%s) "
                "ON DUPLICATE KEY UPDATE revoked_at=NULL, revoked_by=NULL, "
                "granted_by=VALUES(granted_by), granted_at=NOW(), note=VALUES(note)",
                (doc_id, oid, reviewer_id, "contribution_adopt"))
            record_acl_projection_invalidation(cur, doc_id, reason="contribution_node_adopt")
        conn.commit()
    except Exception:
        # 「整笔回滚」是本函数的契约：半截 node 文档（有 meta 无 grant）= 谁都搜不到且不可判归属
        try:
            conn.rollback()
        except Exception as rb:   # noqa: BLE001 — 回滚失败只记日志，异常仍按原样上抛
            logger.warning("contribution[node] 物化回滚失败 doc=%s: %s", doc_id, rb)
        raise


def _finish_contribution_ingestion(cid: str, *, doc_id: str, raw_key: str, owner_dept: str,
                                   question: str, content: str, reviewer_id: str,
                                   reviewer_name: str, trace_id: str,
                                   requires_kb_admin_approval: bool = False,
                                   owner_dept_id: Optional[int] = None):
    """采纳后的物化+登记（独立事务，幂等可重试）：成功→registered，失败→failed+ingestion_error。

    返回 (ingestion_status, error_or_None)。绝不假设跨系统原子——OSS 与 RDS 任一失败都记 failed，
    固定键留存，retry-ingestion 用同键续跑。
    P2-16：requires_kb_admin_approval=True → 登记为 PENDING_APPROVAL（见 _materialize_contribution），
    并 best-effort 通知 kb_admin（notify_upload_approval，no-raise）。
    方案 M4：`owner_dept_id` 非空 ⇒ 走 node materializer（组码 materializer 逐字不动）。
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
            if owner_dept_id is not None:
                _materialize_contribution_node(
                    conn, doc_id=doc_id, owner_dept_id=int(owner_dept_id), raw_key=raw_key,
                    bucket=cfg.oss.bucket_name, title=question, reviewer_id=reviewer_id,
                    reviewer_name=reviewer_name, md_text=md, permission_level=permission_level,
                    requires_approval=requires_kb_admin_approval)
            else:
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
        _owner_label = f"node:{int(owner_dept_id)}" if owner_dept_id is not None else owner_dept
        write_audit(doc_id=doc_id, version_no=1, action_type="CONTRIB_ADOPT",
                    operator_type="user", operator_id=reviewer_id, oss_key=raw_key,
                    trace_id=trace_id,
                    message=f"contribution={cid} owner={_owner_label}"
                            + (" pending_kb_approval" if requires_kb_admin_approval else ""))
        if requires_kb_admin_approval:
            # P2-16：进待审批队列后即时告知 kb_admin（既有挂点，内部 no-raise，失败只记 warning）
            from opensearch_pipeline.admin_notify import notify_upload_approval
            notify_upload_approval(owner_dept=owner_dept, title=question,
                                   owner_dept_id=owner_dept_id)
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


def _requeue_failed_doc(doc_id: str, *, trace_id: str) -> None:
    """管理员重试入库时，把内容阶段 FAILED 的贡献文档重新排队（ε-5 审计 P0 出路侧）。

    _materialize_contribution 对已登记 raw_key 幂等早退、绝不碰 document_version 状态 →
    没有这一步，对 cs=FAILED 且重试用尽的文档点「重试」只是把贡献翻回 registered，下次
    reconcile 又诚实翻回 failed（空转）。重置 NOT_STARTED + retry_count=0 后，下一批 DAG
    stage-2 按既有认领谓词重新拾起（kb 放行端点已有 serving 侧写 content_process_status
    的先例）。谓词锁死 content_process_status='FAILED'：物化失败重试（文档行不存在或
    NOT_STARTED）、待放行（PENDING_APPROVAL）等一概不碰。

    best-effort：失败只记 warning——贡献停在 registered，下次 reconcile 会诚实翻回 failed，
    不会留下「假 registered」终态（自洽，无需回滚）。人工触发（管理员点按钮），非自动重跑。
    """
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.db import _get_db_conn
    from opensearch_pipeline.env_guard import assert_metadata_write_allowed
    try:
        assert_metadata_write_allowed("kb_contribution_requeue", get_config().rds.host, kind="rds")
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {_kb_db()}.document_version"
                    " SET content_process_status='NOT_STARTED', retry_count=0"
                    " WHERE doc_id=%s AND version_no=1"
                    "   AND content_process_status='FAILED'",
                    (doc_id,))
                requeued = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        if requeued:
            logger.info("contribution retry 重新排队 FAILED 文档 [trace=%s] doc=%s", trace_id, doc_id)
    except Exception as e:  # noqa: BLE001 — 排队失败不拖垮重试主流程（见 docstring 自洽性）
        logger.warning("contribution retry 重新排队失败 (non-fatal) [trace=%s] doc=%s: %s",
                       trace_id, doc_id, e)


class KbMyDeptItem(BaseModel):
    dept_id: int = 0
    name: str = ""


class KbMyDeptsResponse(BaseModel):
    """提交端归属来源（Sam 裁决 F/G）。**极小只读面**：不返回整棵树，不返回任何管辖/授权字段。"""
    items: List[KbMyDeptItem] = Field(default_factory=list)
    # 三态纪律（「缺键=unknown≠空集」，同 asset-diff 教训）：available=False = 算不出
    # （flag 关 / 未在册 / 快照不可用），**不是**「你没有可选部门」。前端据此回落组码轴。
    available: bool = True


_MY_DEPTS_CAP = 200
# 单人直属部门数上限。超过即判「算不出」而**不是**取前 N 个（评审 R3，Codex 建议）：
# 静默取前 8 会让第 9 个部门的人**永远选不到自己的部门**且无任何痕迹，排查时看到的现象是
# 「下拉里就是没有」。现网实测最大 5（1164 名在册员工，>8 者 0 人）⇒ 这是潜伏闸，不是活跃截断。
_MY_DEPTS_DIRECT_MAX = 8


def _resolve_my_dept_ids(cur, user_id: str) -> Optional[Dict[int, str]]:
    """调用者可选的贡献归属节点 → `{dept_id: name}`，或 **None**（= 算不出，≠ 没得选）。

    **提交端与查询端的唯一真相**（评审 C1）：`/api/kb/my-depts` 渲染它，
    `kb_contribution_submit` 用它**拒绝**不在集合内的 `category_dept_id`。
    两边必须同源——否则前端收窄了、后端没收窄，等于只在 UI 上"限制"，
    任一登录员工构造 POST 即可把贡献投进任意部门的待审队列并触发钉钉通知给对方管理员。
    （legacy 组码轴历来同样不校验成员身份，故这是**平权修复**；Sam 2026-08-07 裁决补校验。）

    返回规则（裁决 G —— 直挂上级节点者可选下级）。对调用者所在的每个节点 N：
      · N **无**活跃子节点（叶子）⇒ 收 `N` 自己
      · N **有**活跃子节点       ⇒ 收 **N 的直接子节点集**（不含 N 自己）
    ⚠️ 有意**不做**任何向上/向下的多级推导——只看一层，与 `_contrib_can_manage` 的
    「代码不做归属推导」同一纪律。

    None 的四种来源（一律 fail-closed，调用方**绝不**当成"随便选"）：flag 关 / 不在册 /
    直属部门数超 `_MY_DEPTS_DIRECT_MAX` / 快照读失败。
    """
    if not _node_acl_grant_on():
        return None
    from opensearch_pipeline.dingtalk_identity import _load_direct_dept_ids
    raw = _load_direct_dept_ids(user_id)
    if not raw:
        return None            # staff_dim 不可用 / 该人不在册 ⇒ 算不出
    # dept_id<=1 = 虚拟根，全仓既有约定即排除（dept_ancestry.py:222-224 的祖先链、
    # kb_authz.authorize_upload_node 的 oid<=1 门槛都是同一门槛），不是本函数的静默丢弃。
    direct = [d for d in dict.fromkeys(int(x) for x in raw) if d > 1]
    if not direct:
        return None
    if len(direct) > _MY_DEPTS_DIRECT_MAX:
        logger.warning("user=%s 直属部门 %d 个，超过 %d ⇒ 贡献归属判为「算不出」"
                       "（不截断取前 N：静默取前 N 会让其余部门永远选不到且无痕迹）",
                       user_id, len(direct), _MY_DEPTS_DIRECT_MAX)
        return None
    try:
        ph = ",".join(["%s"] * len(direct))
        cur.execute(
            f"SELECT dept_id, parent_id, name FROM {_kb_db()}.dept_dim"
            f" WHERE is_active=1 AND (dept_id IN ({ph}) OR parent_id IN ({ph}))",
            tuple(direct) * 2)
        rows = cur.fetchall() or []
    except Exception as e:   # noqa: BLE001 — 快照读失败 ⇒ 算不出（fail-closed）
        logger.warning("_resolve_my_dept_ids 组织快照读取失败（算不出）user=%s: %s", user_id, e)
        return None
    direct_set = set(direct)
    self_by_id = {int(r[0]): (r[2] or "") for r in rows if int(r[0]) in direct_set}
    children: Dict[int, List[tuple]] = {}
    for r in rows:
        pid = int(r[1] or 0)
        if pid in direct_set and int(r[0]) != pid:
            children.setdefault(pid, []).append((int(r[0]), r[2] or ""))
    picked: Dict[int, str] = {}
    for d in direct:
        kids = children.get(d)
        if kids:
            for kid_id, kid_name in kids:
                picked.setdefault(kid_id, kid_name)
        elif d in self_by_id:
            picked.setdefault(d, self_by_id[d])
    if not picked:
        return None            # 直属部门全不在活跃快照里 ⇒ 同样是「算不出」
    if len(picked) > _MY_DEPTS_CAP:
        # 同上：宁可判「算不出」也不静默截断（这里是子节点数爆炸，同样不该悄悄少给）
        logger.warning("user=%s 可选归属节点 %d 个，超过 %d ⇒ 判为「算不出」",
                       user_id, len(picked), _MY_DEPTS_CAP)
        return None
    return picked


@router.get("/api/kb/my-depts", response_model=KbMyDeptsResponse)
def kb_my_depts(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """调用者可选的贡献归属节点（Sam 裁决 F + 补充裁决 G）。

    **为什么不能复用 `/api/kb/org-tree`**（Codex 三轮 B1，已核验）：贡献提交只要求登录
    （`employee` 可提交），而 org-tree 挂 `_require_kb_console` ⇒ employee 恒 403；且该响应含
    `my_managed_owner_depts` / `my_managed_node_roots`，放开会泄露管理结构。

    返回规则（裁决 G —— 直挂上级节点者可选下级）。对调用者所在的每个节点 N：
      · N **无**活跃子节点（叶子）⇒ 返回 `N` 自己
      · N **有**活跃子节点       ⇒ 返回 **N 的直接子节点集**（不含 N 自己）
    这条直接消解 L1 盲区的主体：直挂 depth1 的人不再被迫把贡献挂在无人管辖的中心节点上。
    ⚠️ 有意**不做**任何向上/向下的多级推导——只看一层，与 `_contrib_can_manage` 的
    「代码不做归属推导」同一纪律。
    """
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    if not _node_acl_grant_on():
        return KbMyDeptsResponse(items=[], available=False)
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                picked = _resolve_my_dept_ids(cur, identity.user_id)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("kb_my_depts 组织快照读取失败（算不出）: %s", e)
        return KbMyDeptsResponse(items=[], available=False)
    if not picked:
        return KbMyDeptsResponse(items=[], available=False)
    items = [KbMyDeptItem(dept_id=i, name=(n or f"节点 {i}"))
             for i, n in sorted(picked.items())]
    return KbMyDeptsResponse(items=items, available=True)


@router.post("/api/kb/contributions", response_model=KbContributionItem)
def kb_contribution_submit(req: KbContributionSubmitRequest, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    """员工提交知识贡献（问答文本）。仅要求登录（员工即可）；status=pending 待部门管理员采纳。

    两根归属轴（方案 M9/M10，Sam 裁决 A/E）：
      · `category_dept_id` 给出 ⇒ **node 轴**。要求 `RAG_NODE_ACL_GRANT` 开 + schema/067 已
        apply + 该节点在 `dept_dim` 里 active（M5 第 1 处）+ **该节点在提交人自己的可选集内**
        （评审 C1，Sam 2026-08-07 裁决）。前两道挡的是脏数据落库（否则存下指向死节点的
        pending 行，要等 accept 才失败）；第三道挡的是**越界投稿**——`my-depts` 的收窄若只做在
        前端，任一登录员工构造 POST 即可把贡献投进任意部门的待审队列并触发钉钉通知给对方管理员。
        可选集与 `/api/kb/my-depts` **同源**（`_resolve_my_dept_ids`），杜绝两处口径分裂。
        任一不满足 ⇒ **400/403 拒绝**，
        **绝不静默回落组码轴**（裁决 E；对齐 `kb_console.py:2658-2663` 的上传行为）。
        组码列写空串哨兵——理由同 060 给 node 文档写 `owner_dept=NULL`：留组码残值 =
        node 行会被旧组码管理员的作用域命中（越权），见 `_contrib_axis_present` 头注。
      · 否则 ⇒ 组码轴，逐字节保持既有语义。
    """
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
    node_id = int(req.category_dept_id or 0) or None
    category_dept = ""
    if node_id is None:
        depts = kb_authz.sanitize_owner_depts(req.category_dept)
        if not depts:
            raise HTTPException(status_code=400, detail="归属分类无效")
        category_dept = depts[0]
    elif not _node_acl_grant_on():
        raise HTTPException(status_code=400, detail="组织树授权通道未开启（RAG_NODE_ACL_GRANT）")
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
                # 两条**字面** INSERT（而非拼列名变量）：列清单要能被
                # tests/test_schema_ddl_parity.py 静态扫到——F-35 的 010 漂移就是靠这条守住的。
                params = [cid, q_clean, c_clean, nq, qhash,
                          category_dept, (category_dept or None), identity.user_id,
                          identity.name or "",
                          (req.source_message_id or None), gq, ngq, gqhash]
                if node_id is None:
                    cur.execute(
                        f"""
                        INSERT INTO {_op_db()}.kb_contribution
                          (contribution_id, question, content, normalized_question, question_hash,
                           category_dept, suggested_dept, author_id, author_name,
                           review_status, ingestion_status, source_message_id, gap_query,
                           normalized_gap_query, gap_query_hash)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending','none',%s,%s,%s,%s)
                        """,
                        tuple(params),
                    )
                else:
                    if not _contrib_axis_present(cur):
                        raise HTTPException(status_code=400,
                                            detail="贡献归属节点通道未就绪（schema/067 未 apply）")
                    _assert_node_active(cur, node_id)
                    # 评审 C1：归属必须落在提交人自己的可选集内（与 /api/kb/my-depts 同源）。
                    # 「算不出」（None）同样拒绝——fail-closed：宁可让这个人退回组码轴提交，
                    # 也不能因为查不出他的部门就放行任意节点。
                    _allowed = _resolve_my_dept_ids(cur, identity.user_id)
                    if not _allowed or node_id not in _allowed:
                        logger.warning("越界贡献归属被拒 user=%s node=%s allowed=%s",
                                       identity.user_id, node_id,
                                       sorted(_allowed) if _allowed else None)
                        raise HTTPException(
                            status_code=403,
                            detail="只能把贡献挂在自己所在的部门下（归属节点不在你的可选范围内）")
                    cur.execute(
                        f"""
                        INSERT INTO {_op_db()}.kb_contribution
                          (contribution_id, question, content, normalized_question, question_hash,
                           category_dept, suggested_dept, author_id, author_name,
                           review_status, ingestion_status, source_message_id, gap_query,
                           normalized_gap_query, gap_query_hash, category_dept_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending','none',%s,%s,%s,%s,%s)
                        """,
                        tuple(params + [node_id]),
                    )
            conn.commit()
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_contribution_submit 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"提交失败 (trace: {trace_id})")
    _gaps_cache_clear()   # 缺口的「等待入库」徽标即时可见，不等 TTL
    # 钉钉工作通知（RAG_ADMIN_NOTIFY 门控，best-effort no-raise）：归属管理员即时知晓待审核贡献
    from opensearch_pipeline.admin_notify import notify_contribution
    notify_contribution(category_dept=category_dept, question=q_clean, category_dept_id=node_id)
    return KbContributionItem(
        contribution_id=cid, question=q_clean, content=c_clean,
        category_dept=category_dept, category_dept_id=node_id,
        author_id=identity.user_id, author_name=identity.name or "",
        review_status="pending", ingestion_status="none", state="pending",
        source_message_id=(req.source_message_id or None), gap_query=gq)


def _doc_cited_hits(cur, doc_ids) -> Optional[dict]:
    """doc_id → 被引用的回答数（批次ε-2 R2）。口径=cited=True（与 Phase E 价值类看板同源，
    「被引用进答案」才是帮到人的信号；retrieved 只是曝光）、全期窗口（与英雄榜 searchable
    COUNT 同窗，避免同榜两套时间口径）。COUNT(DISTINCT message_id)=同一回答引用同作者多
    chunk/多文档只计一次回答。

    仅事实表路径（qa_retrieved_doc 与 kb_contribution 同库同 collation，索引 JOIN 零 cast）；
    fact 路径不可用/查询失败 → None=「算不出」（诚实 NULL 纪律，绝不用 0 顶替——0 是真零）。
    JSON_TABLE 回退**有意不做**：qa_session_log 无界增长，heroes/mine 只有 aux 限流保护，
    不值得为次要激励信号冒全表 JSON 解析的成本。"""
    ids = [d for d in (doc_ids or []) if d]
    if not ids:
        return {}
    from opensearch_pipeline.qa_facts import FACT_TABLE, fact_join_enabled
    if not fact_join_enabled():
        return None
    try:
        ph = ",".join(["%s"] * len(ids))
        cur.execute(
            f"SELECT jt.doc_id, COUNT(DISTINCT jt.message_id)"
            f" FROM {_op_db()}.{FACT_TABLE} jt"
            f" WHERE jt.cited=1 AND jt.doc_id IN ({ph}) GROUP BY jt.doc_id",
            tuple(ids))
        return {r[0]: int(r[1] or 0) for r in (cur.fetchall() or [])}
    except Exception as e:   # noqa: BLE001 — 激励信号绝不拖垮主列表
        logger.info("贡献引用数聚合失败（诚实 None）: %s", e)
        return None


def _contrib_doc_badges(cur, doc_ids) -> Optional[dict]:
    """doc_id → 台账管线徽章（批次ε-3 R1）。复用 _kb_status_badge（与「文档管理」台账同词表
    同判定，勿重造窄谓词——PENDING_APPROVAL 之外还有 QUARANTINED/EMPTY/SKIPPED_* 等 reconcile
    不碰的卡死终态，窄谓词会漏）。document_version⋈document_meta 同库 JOIN + doc_id IN 绑定
    参数（非跨库列对列 JOIN，天然无 1267 collation 陷阱）。失败→None（诚实「算不出」）。"""
    ids = [d for d in (doc_ids or []) if d]
    if not ids:
        return {}
    try:
        from opensearch_pipeline.api import _kb_status_badge
        ph = ",".join(["%s"] * len(ids))
        cur.execute(
            f"SELECT dv.doc_id, dv.content_process_status, dv.index_status,"
            f" dv.publish_status, dv.chunk_status, dm.status, dv.gate_status, dv.status"
            f" FROM {_kb_db()}.document_version dv"
            f" LEFT JOIN {_kb_db()}.document_meta dm ON dm.doc_id = dv.doc_id"
            f" WHERE dv.version_no=1 AND dv.doc_id IN ({ph})",
            tuple(ids))
        # gate_status 随行传入（渲染侧 gate 轴，2026-08-04 独立核验 B2）：缺了它，
        # gate-only 隔离件在贡献列表会显「已上线」，与台账筛选口径自相矛盾。
        # ⚠️ 全部改**关键字实参**（2026-08-06）：本处原是位置调用，而 helper 删掉第 4 位的
        # `chunk_active` 后，第 6 个位置实参会落到 `gate_status`、与其关键字实参撞成
        # `TypeError` —— 又被下方「徽章派生绝不拖垮主列表」的 except 吞掉，
        # 表现为**贡献列表徽章整片静默消失**。helper 已改 keyword-only 从根上封死这条。
        # dv.status：本函数固定读 v1，而 v1 在文档升版后会变成 superseded ⇒「历史版本」可达。
        return {r[0]: _kb_status_badge(r[1], r[2], r[5], version_status=r[7],
                                       publish_status=r[3], chunk_status=r[4],
                                       gate_status=r[6])
                for r in (cur.fetchall() or [])}
    except Exception as e:   # noqa: BLE001 — 徽章派生绝不拖垮主列表
        logger.info("贡献管线徽章派生失败（诚实 None）: %s", e)
        return None


@router.get("/api/kb/contributions/mine", response_model=KbContributionListResponse)
def kb_contributions_mine(request: Request, limit: int = 20, offset: int = 0,
                          identity: Optional[Identity] = Depends(current_identity)):
    """我的贡献（按 author_id；含实时 4 态——读前先 reconcile registered→searchable）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    # ⚠️ offset **无上界**，与 kb_console 的 `_KB_MAX_OFFSET=10000` 静默钳位口径相反
    # （B7 补评审 2026-08-06 记录的接口策略不对称）。这里**有意不加**上界：本列表按
    # author_id 限定，深 offset 不构成全表扫风险，而加上限会把原本可达的数据变成不可达
    # —— 与本仓 "no silent caps" 反向。cap 的最终协议见 backlog §F 第 2 项（待裁决）。
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
            # 067 已 apply 才带 category_dept_id（「修改重交」预填原归属要用；M11）
            _cols = _CONTRIB_COLS_NODE if _contrib_axis_present(cur) else _CONTRIB_COLS
            cur.execute(
                f"SELECT {_cols} FROM {_op_db()}.kb_contribution WHERE author_id=%s"
                " ORDER BY created_at DESC, contribution_id DESC LIMIT %s OFFSET %s",
                (identity.user_id, limit + 1, offset))
            rows = cur.fetchall() or []
            items = [_contrib_item(r) for r in rows[:limit]]
            # 批次ε-2 R2：已入库行回填被引用数（None=算不出→前端自隐；无 doc_id 行保持 None）
            hits = _doc_cited_hits(cur, [i.doc_id for i in items if i.doc_id
                                         and i.ingestion_status == "searchable"])
            if hits is not None:
                for i in items:
                    if i.doc_id and i.ingestion_status == "searchable":
                        i.hits = hits.get(i.doc_id, 0)
            # 批次ε-3 R1：registering（已采纳未入库）行回填管线徽章——分辨 待放行/死链/正常排队。
            # 仅对该态查询（searchable/failed/pending 行零成本）；行缺失（P2-16 竞态）保持 None。
            badges = _contrib_doc_badges(cur, [i.doc_id for i in items if i.doc_id
                                               and i.state == "registering"])
            if badges:
                for i in items:
                    if i.doc_id and i.state == "registering":
                        i.doc_badge = badges.get(i.doc_id)
    finally:
        conn.close()
    has_more = len(rows) > limit
    return KbContributionListResponse(items=items, has_more=has_more)


# ═══════════════════════════════════════════════════════════════════════════
# 贡献域 node 轴（schema/067；方案 docs/contribution_node_axis_migration_2026-08-06.md）
#
# 背景：贡献域的八个消费点原本只认组码 `category_dept`（dept_admin_grant），而
# 2026-07-27 起的节点授权（dept_admin_node_grant）是**另一根轴** ⇒ 只被 node 授权的
# 管理员在贡献域完全失明。本组函数是贡献域**专属**的单一判定入口。
#
# 🔴 **绝不改** `api.py:_kb_owner_scope_sql/_kb_can_manage`（组码域 generic 入口，被上传/
#    退役/共享/Agent/本体 20+ 处复用）——它们仍是本组函数 legacy 分支的实现。
# 🔴 **轴隔离**（Codex C3 + `api.py::_kb_doc_owner_scope_sql` 范式）：
#    `category_dept_id IS NOT NULL` ⇒ **只**走 node；`IS NULL` ⇒ **只**走组码。
#    两支互斥、**不 OR 交叉**——交叉即 node 行的 `category_dept` 残值继续命中旧组码
#    管理员（与 060 阶段 A 的 owner_dept 残值越权同型）。
# ═══════════════════════════════════════════════════════════════════════════

# 067 capability 正向缓存（同 access_grants._NODE_SCHEMA_PRESENT 形态）：一旦探到列在册
# 就不再探；未 apply/探测失败 ⇒ False ⇒ 全域**逐字节回到组码行为**（先部署后 apply 安全）。
_CONTRIB_AXIS_PRESENT: Set[str] = set()
_contrib_axis_lock = threading.Lock()


def _contrib_axis_present(cur) -> bool:
    """schema/067 是否已 apply（`kb_contribution.category_dept_id` 列在册）。

    探测失败按「未 apply」处理是**安全**的：node 贡献行的 `category_dept` 恒为空串哨兵
    （见 `kb_contribution_submit` 的 node 分支——理由同 060 给 node 文档写 owner_dept=NULL），
    组码腿的 `IN (...)` 永不命中空串 ⇒ 降级方向是「落 kb_admin 孤儿兜底」，绝不会把 node 行
    的组码残值喂给旧组码管理员。M8 迁移的 4 行是例外（保留 category_dept='hr'），其降级
    受众恰是它们今天的管理员，无扩权。
    """
    key = _op_db()
    if key in _CONTRIB_AXIS_PRESENT:
        return True
    try:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='kb_contribution' "
            "  AND COLUMN_NAME='category_dept_id'", (key,))
        row = cur.fetchone()
        present = bool(row and row[0])
    except Exception as e:   # noqa: BLE001 — 探测失败 ⇒ 组码语义（收紧方向，见 docstring）
        logger.warning("贡献 node 轴列探测失败，按 067 未 apply 处理（全域组码）: %s", e)
        return False
    if present:
        with _contrib_axis_lock:
            _CONTRIB_AXIS_PRESENT.add(key)
    return present


def _node_acl_grant_on() -> bool:
    """`RAG_NODE_ACL_GRANT` 现值。单一来源=`kb_console._node_acl_grant_enabled`（惰性 import
    避免两个路由模块的导入序耦合）。取不到一律 False —— fail-safe 到组码轴（现状可用）。"""
    try:
        from opensearch_pipeline.routes.kb_console import _node_acl_grant_enabled
        return _node_acl_grant_enabled()
    except Exception as e:   # noqa: BLE001
        logger.warning("读取 node_acl_grant 失败，按未开启处理: %s", e)
        return False


def _assert_node_active(cur, dept_id: int, *, status: int = 400) -> None:
    """归属节点必须在 `dept_dim` 里 active（方案 M5；提交 / accept / retry **三处共用**）。

    ⚠️ `kb_authz.can_manage_doc` **不校验节点 active**（只做 `owner_dept_id ∈ descendant_ids`），
    active 校验历来是 `kb_console.py:2681` 在上传路径上**单独**做的。贡献域三处一个都不能省：
    提交入口挡的是脏数据落库（否则存下指向死节点的 pending 行，要等 accept 才失败），
    accept/retry 挡的是「提交之后节点被停用」的时间差。
    读失败 ⇒ **抛 503**（fail-closed），绝不把「查不出来」当成 active 放过。
    """
    try:
        cur.execute(f"SELECT 1 FROM {_kb_db()}.dept_dim WHERE dept_id=%s AND is_active=1 LIMIT 1",
                    (int(dept_id),))
        ok = bool(cur.fetchone())
    except Exception as e:   # noqa: BLE001
        logger.warning("dept_dim active 校验失败（fail-closed）dept_id=%s: %s", dept_id, e)
        raise HTTPException(status_code=503, detail="组织节点校验暂不可用，请稍后重试") from e
    if not ok:
        raise HTTPException(status_code=status, detail=f"归属节点不存在或已停用：{int(dept_id)}")


def _all_managed_node_descendants(cur) -> Optional[Set[int]]:
    """全局「被**任何** active `dept_admin_node_grant` 覆盖」的节点集 → set[int] 或 None。

    Codex C4/C9：`api._kb_managed_descendants` 是 **per-identity**（按调用者自己的
    granted_node_roots 展开），不可复用——kb_admin 的孤儿判定问的是「有没有【任何人】
    管这个节点」。

    None = 不可得（组织快照过期/读失败/展开超限）。调用方的处置**按轴不同**：
      · node 分支（kb_admin 孤儿兜底）→ fail-**open** 取全集（宁多看不丢件）；
      · legacy 分支 → 原组码孤儿规则**分毫不变**；
      · dept_admin 一律 fail-closed（`_contrib_scope_sql` / `_contrib_can_manage`）。
    """
    try:
        cur.execute(f"SELECT DISTINCT managed_dept_id FROM {_kb_db()}.dept_admin_node_grant"
                    " WHERE is_active=1")
        roots = [int(r[0]) for r in (cur.fetchall() or []) if r and r[0]]
    except Exception as e:   # noqa: BLE001 — 060 未 apply / 读失败
        logger.warning("dept_admin_node_grant 读取失败，node 管辖全集不可得: %s", e)
        return None
    if not roots:
        return set()          # 无人持 node 管辖根：合法状态，全部 node 行都是孤儿
    try:
        from opensearch_pipeline.dept_ancestry import resolve_descendant_ids
        from opensearch_pipeline.org_sync import load_children_index
        _rev, fresh, children = load_children_index()
        if not fresh:
            logger.warning("组织快照过期 ⇒ node 管辖全集不可得（贡献孤儿判定降级）")
            return None
        got, ok = resolve_descendant_ids(children, roots)
        return got if ok else None
    except Exception as e:   # noqa: BLE001 — OrgSnapshotUnavailable 等
        logger.warning("node 管辖全集展开失败（贡献孤儿判定降级）: %s", e)
        return None


def _contrib_can_manage(kb, category_dept_id, category_dept, *, cur) -> bool:
    """单条贡献的管理判定（队列可见 / accept / reject / retry 共用**唯一**入口）。

    kb_admin → True（救急兜底通道，与批次δ-3 一致：队列收窄到孤儿，但权限面不收紧）。
    dept_admin：
      · node 行（category_dept_id 非空）→ `category_dept_id ∈ 管辖后代集`；后代集不可得
        （快照过期/读失败）⇒ **False**（kb_authz.can_manage_doc 对 descendant_ids=None
        fail-closed，绝不回退组码残值判定）；
      · legacy 行（category_dept_id 为空）→ 组码语义**逐字节不变**（_kb_can_manage）。
    `cur` 用于 067 capability 探测：未 apply 的环境即便调用方传了 id 也一律按组码判，
    与 SQL 侧（同样探测后才敢引用该列）保持**同一根轴**，杜绝「列表按组码、accept 按 node」
    这类两口径分裂。
    """
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN, can_manage_doc
    if kb.role == ROLE_KB_ADMIN:
        return True
    if category_dept_id is not None and _contrib_axis_present(cur):
        return can_manage_doc(kb, "node", None, int(category_dept_id),
                              _kb_managed_descendants(kb))
    return _kb_can_manage(kb, category_dept or "")


def _contrib_scope_sql(kb) -> tuple:
    """dept_admin 的贡献队列作用域（两支互斥）→ (clause, params)。

    node 腿在后代集不可得/为空时**整腿去掉**（fail-closed：node 贡献对该管理员隐身），
    组码腿在无 managed 时同样去掉；两腿都没有 ⇒ `AND 1=0`。
    """
    from opensearch_pipeline.kb_authz import expand_managed_owner_depts, managed_owner_depts
    owners = expand_managed_owner_depts(managed_owner_depts(kb))
    descendants = _kb_managed_descendants(kb)
    if descendants is None:
        logger.warning("管辖后代集不可得（快照过期/读失败）——贡献队列 node 腿失效（fail-closed）")
    legs, params = [], []
    if descendants:
        ph = ",".join(["%s"] * len(descendants))
        legs.append(f"(category_dept_id IS NOT NULL AND category_dept_id IN ({ph}))")
        params.extend(sorted(descendants))
    if owners:
        ph = ",".join(["%s"] * len(owners))
        legs.append(f"(category_dept_id IS NULL AND category_dept IN ({ph}))")
        params.extend(owners)
    if not legs:
        return " AND 1=0", []
    return " AND (" + " OR ".join(legs) + ")", params


def _contrib_orphan_sql(cur) -> tuple:
    """kb_admin 的**孤儿兜底**作用域（两支互斥）→ (clause, params)。

    · legacy 支：`category_dept` 无任何 active dept_admin 管辖 —— 谓词与改动前**逐字节一致**
      （含跨库 COLLATE 钉死：category_dept 在 fuling_operation / dept_admin_grant 在
      fuling_knowledge，2026-07-15 现网因 collation 漂移让 kb_admin 队列全 500）。
    · node 支：`category_dept_id` 不在「全局被管辖节点集」内。该集不可得（None）时
      **fail-open 取全集**（Codex C9：kb_admin 宁多看不丢件），并记 WARN——注意
      fail-open **只作用于 node 支**，legacy 支的孤儿规则分毫不动。
    """
    legacy = ("(category_dept_id IS NULL AND category_dept NOT IN"
              " (SELECT DISTINCT managed_owner_dept COLLATE utf8mb4_unicode_ci"
              f" FROM {_kb_db()}.dept_admin_grant WHERE is_active=1))")
    all_managed = _all_managed_node_descendants(cur)
    params: List[Any] = []
    if all_managed is None:
        logger.warning("node 管辖全集不可得 ⇒ kb_admin 贡献队列 node 支 fail-open（全部 node 行可见）")
        node = "(category_dept_id IS NOT NULL)"
    elif not all_managed:
        node = "(category_dept_id IS NOT NULL)"
    else:
        ph = ",".join(["%s"] * len(all_managed))
        node = f"(category_dept_id IS NOT NULL AND category_dept_id NOT IN ({ph}))"
        params.extend(sorted(all_managed))
    return f" AND ({legacy} OR {node})", params


def _contrib_pending_scope_sql(kb, cur=None) -> tuple:
    """贡献审核队列作用域（批次δ-3 拍板：业务采纳/驳回归 dept_admin——业务标准在部门）。

    dept_admin：本部门（组码 managed ∪ node 管辖后代集，两轴互斥）。
    kb_admin：**仅孤儿**（无任何 active 管理员管辖的归属）——日常动线上裁决权移交对应
    dept_admin；accept/reject 的权限面**不收紧**（kb_admin 保留经 API 的救急兜底通道，与
    2026-07-04 access-request「kb_admin 只管入库、后端留救急通道」拍板同构）。

    `cur=None`（或 067 未 apply）⇒ 走**与改动前逐字节相同**的组码语句：列不存在时引用
    `category_dept_id` 会 1054 打挂整个队列。
    category_dept 恒为伞组之一（authorize_upload 白名单校验，子线进不来）——与
    dept_admin_grant.managed_owner_dept 同枚举域，精确匹配即可，无需伞形展开。
    """
    from opensearch_pipeline.kb_authz import ROLE_KB_ADMIN
    axis = bool(cur is not None and _contrib_axis_present(cur))
    if kb.role == ROLE_KB_ADMIN:
        if axis:
            return _contrib_orphan_sql(cur)
        return (" AND category_dept NOT IN (SELECT DISTINCT managed_owner_dept COLLATE utf8mb4_unicode_ci"
                f" FROM {_kb_db()}.dept_admin_grant WHERE is_active=1)"), []
    if axis:
        return _contrib_scope_sql(kb)
    return _kb_owner_scope_sql(kb, "category_dept")


def _pending_asks(cur, cids) -> Optional[dict]:
    """contribution_id → 近 30 天被问次数（批次ε-3 R2「asks 口径归并」）。

    此前 asks 只存在于 /api/kb/gaps 的实时聚合，且按【提问者 acl_groups】限定可见范围——与
    审核队列的【managed_owner_depts】不是同一口径（ε-1 审计裁定弱关联路线有口径分裂）。这里
    按 COALESCE(gap_query_hash, question_hash)（审核人修订过 question 时 gap_query_hash 仍指
    向原始提问）归并统计 NO_RESULT+REFUSAL 两源、_CONTRIB_WINDOW_DAYS 窗口、每源
    _CONTRIB_CANDIDATE_CAP 上限（asks 专属 30 天/400——ε-4 起与 kb_gaps 的 365 天缺口窗解耦，
    近期热度语义必须钉 30）。qa_session_log 无 hash 列，
    Python 侧 question_hash 归并；REFUSAL 走平查（idx_status_created）——不需要 kb_gaps 的
    qa_docs_join（那个 JOIN 只为算 dept 可见性，计数场景有意不按部门过滤：审核人要的是真实
    热度，纯计数不回传原文无泄露面）。失败→None（诚实「算不出」）；同 hash 同 message 去重。"""
    ids = [c for c in (cids or []) if c]
    if not ids:
        return {}
    from opensearch_pipeline import contribution as C
    try:
        ph = ",".join(["%s"] * len(ids))
        cur.execute(
            f"SELECT contribution_id, COALESCE(gap_query_hash, question_hash)"
            f" FROM {_op_db()}.kb_contribution WHERE contribution_id IN ({ph})",
            tuple(ids))
        hash_by_cid = {r[0]: r[1] for r in (cur.fetchall() or []) if r and r[1]}
        counts: dict = {}
        if hash_by_cid:
            for status in ("NO_RESULT", "REFUSAL"):
                cur.execute(
                    f"SELECT query_text, message_id FROM {_op_db()}.qa_session_log"
                    f" WHERE answer_status='{status}'"
                    "   AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    " ORDER BY created_at DESC LIMIT %s",
                    (_CONTRIB_WINDOW_DAYS, _CONTRIB_CANDIDATE_CAP))
                for row in cur.fetchall() or []:
                    qt, mid = row[0], row[1]
                    h = C.question_hash(qt)
                    if h:
                        counts.setdefault(h, set()).add(mid or qt)
        # hash 缺失的行（历史空 hash）不进 hash_by_cid → 调用方 .get(cid, 0) 归 0
        return {cid: len(counts.get(h, ())) for cid, h in hash_by_cid.items()}
    except Exception as e:   # noqa: BLE001 — 热度信号绝不拖垮审核队列
        logger.info("pending asks 聚合失败（诚实 None）: %s", e)
        return None


@router.get("/api/kb/contributions/pending", response_model=KbContributionListResponse)
def kb_contributions_pending(request: Request, limit: int = 20, offset: int = 0,
                             identity: Optional[Identity] = Depends(current_identity)):
    """贡献审核队列（dept_admin：本部门；kb_admin：仅孤儿部门兜底——批次δ-3）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    limit = max(1, min(int(limit or 20), 100)); offset = max(0, int(offset or 0))
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
            # 作用域与列清单都在**同一个游标**上定 axis（先探测再引用列）：分开定会出现
            # 「列清单按 067 已 apply、作用域按未 apply」的半截状态。
            _axis = _contrib_axis_present(cur)
            _cols = _CONTRIB_COLS_NODE if _axis else _CONTRIB_COLS
            scope_clause, scope_params = _contrib_pending_scope_sql(kb, cur)
            cur.execute(
                f"SELECT {_cols} FROM {_op_db()}.kb_contribution"
                " WHERE review_status='pending' " + scope_clause
                + " ORDER BY created_at ASC, contribution_id ASC LIMIT %s OFFSET %s",
                tuple(list(scope_params) + [limit + 1, offset]))
            rows = cur.fetchall() or []
            items = [_contrib_item(r) for r in rows[:limit]]
            # 批次ε-3 R2：回填被问次数（None=算不出→前端自隐；随手动加载返回，不引入轮询）
            asks = _pending_asks(cur, [i.contribution_id for i in items])
            if asks is not None:
                for i in items:
                    i.asks = asks.get(i.contribution_id, 0)
    finally:
        conn.close()
    has_more = len(rows) > limit
    return KbContributionListResponse(items=items, has_more=has_more)


@router.post("/api/kb/contributions/{cid}/accept", response_model=KbContributionActionResponse)
def kb_contribution_accept(cid: str, req: KbContributionAcceptRequest, request: Request,
                           identity: Optional[Identity] = Depends(current_identity)):
    """采纳贡献（幂等可恢复状态机）：pending→accepted/registering（原子认领+固定键），再物化入库。

    可在采纳前修订 question/content/归属；改归属则按【新归属】重做写授权。
    已采纳→幂等返回（补跑物化交给 retry-ingestion）；已驳回→409。

    两轴（方案 M2/M4/M5）：
      · 源侧管辖用 `_contrib_can_manage`（node 行只看后代集，legacy 行只看组码）；
      · 目标侧 node 走 `authorize_upload_node` + `dept_dim` active 现查 ⇒ 源与目标**双端管辖**
        （kb_admin 除外），对齐 `kb_console.py:4220` 的 D6：防「把贡献挪进别人子树」；
      · 跨轴改归属（node 行改回组码分类）**不支持**，显式 400 —— 静默保留旧轴会让审核人
        以为改成功了。
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
            axis = _contrib_axis_present(cur)
            cur.execute(
                "SELECT review_status, ingestion_status, doc_id, upload_id, raw_key,"
                " question, content, category_dept"
                + (", category_dept_id" if axis else "")
                + f" FROM {_op_db()}.kb_contribution WHERE contribution_id=%s FOR UPDATE", (cid,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=404, detail="贡献不存在")
            review_status = row[0] or "pending"
            ingestion_status = row[1] or "none"
            cur_doc_id, cur_upload_id, cur_raw_key = row[2], row[3], row[4]
            cur_q, cur_c, cur_dept = row[5], row[6], row[7]
            cur_dept_id = (row[8] if (axis and len(row) > 8) else None) or None
            # 采纳前必须能管理贡献【原始】归属（与 reject/retry 一致）——否则 A 部门管理员
            # 可凭 cid 把 B 部门贡献改归属抢入 A 部门（授权函数只校验目标侧）。
            if not _contrib_can_manage(kb, cur_dept_id, cur_dept, cur=cur):
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
            # ── 最终归属（两轴互斥，绝不交叉推导）────────────────────────────
            req_dept_id = int(req.category_dept_id or 0) or None
            if req_dept_id is not None and not axis:
                conn.rollback()
                raise HTTPException(status_code=400,
                                    detail="贡献归属节点通道未就绪（schema/067 未 apply）")
            # 📌 **跨轴迁移契约是刻意单向的**（评审 R5，Sam 2026-08-07 确认为设计意图）：
            #   · legacy 行 + 传 `category_dept_id` ⇒ **升轴**为 node 归属，**允许**——
            #     这是存量 legacy 贡献逐条迁到 node 轴的唯一在线入口（M8 那种批量脚本
            #     只处理已知白名单，日常行只能靠审核人在采纳时指定）。
            #     升轴后该行同时留有 `category_dept` 旧值与 `category_dept_id`（M8 同形），
            #     判定上安全：轴隔离规定 id 非空即**只**看 node，组码腿永不命中它。
            #   · node 行改回组码 ⇒ **拒绝**（下方显式 400）——降轴会让已按 node 授权过的
            #     归属悄悄回到组码受众，且审核人以为改成功了。
            # 两个方向都要走下面的双端管辖（源 `_contrib_can_manage` + 目标 authorize_*），
            # 所以「允许升轴」不等于「谁都能把别人的贡献捞进自己子树」。
            final_dept_id = req_dept_id if req_dept_id is not None else cur_dept_id
            if final_dept_id is not None and req.category_dept:
                conn.rollback()
                raise HTTPException(status_code=400,
                                    detail="该贡献归属组织节点，不支持在采纳时改回组码分类")
            # P0(2026-07-17)：必须净化——尾斜杠 'marketing/' 会通过 authorize_upload（其校验
            # 净化副本）却把原值编进 raw_key，权限段错位 → perm_from_raw_key 读回 public，
            # dept_admin 绕过 kb_admin 审批直发全员。授权/raw_key/落库全用同一净值。
            final_dept = "" if final_dept_id is not None else \
                kb_authz.sanitize_owner_dept(req.category_dept or cur_dept or "")
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
            # 按【最终】目标归属 + 选定可见范围做写授权（DB 现查的 kb；改归属即按新归属裁决）。
            # P2-16（2026-07-04 拍板「kb_admin 只管入库」，取代 2026-06-29「public 直通」裁决）：
            # 不再忽略 requires_kb_admin_approval——dept_admin 采纳 public 时物化为
            # PENDING_APPROVAL 进 kb_admin 既有待审批队列（与自助上传同一纪律），放行后才入库；
            # kb_admin 自己采纳 public 的 decision 本就不带该标记 → 照旧直通（其即终审）。
            if final_dept_id is not None:
                # M5 第 2 处：节点可能在提交之后被停用 —— can_manage_doc 只判后代集、不判 active
                # 显式 rollback 后再抛（评审 R4）：本块内其余每个出口都显式回滚，只有这里
                # 曾靠 `finally: conn.close()` 的隐式回滚兜底。403/503 都会带着 FOR UPDATE
                # 的行锁一路持到 close，与相邻分支的写法也不一致。
                try:
                    _assert_node_active(cur, final_dept_id, status=403)
                except HTTPException:
                    conn.rollback()
                    raise
                decision = kb_authz.authorize_upload_node(
                    kb, final_dept_id, chosen_perm, descendants=_kb_managed_descendants(kb))
                _target_label = f"节点 {final_dept_id}"
            else:
                decision = kb_authz.authorize_upload(kb, final_dept, chosen_perm)
                _target_label = f"部门「{final_dept}」"
            if not decision.allowed:
                conn.rollback()
                raise HTTPException(status_code=403,
                                    detail=f"无权采纳到{_target_label}：{decision.reason}")
            requires_kb_approval = bool(decision.requires_kb_admin_approval)
            # 一次性固定键（raw_key 把可见范围编码进路径段，防管线 stage-2 重解析升/降权）；
            # node 归属的第 2 段走 node_storage_segment（防 stage-1 按路径段重派归属）。
            doc_id = cur_doc_id or kb_upload.new_doc_id()
            upload_id = cur_upload_id or kb_upload.new_ulid()
            _seg = (kb_upload.node_storage_segment(final_dept_id)
                    if final_dept_id is not None else final_dept)
            raw_key = cur_raw_key or kb_upload.build_raw_key(
                _seg, doc_id, upload_id, f"contribution-{cid}.md", permission_level=chosen_perm)
            # 归属列**按轴分别写**：node 轴不碰 category_dept（M8 迁移行的组码留痕保持原样），
            # legacy 轴不碰 category_dept_id（恒 NULL）。
            _owner_set = ("category_dept_id=%s" if final_dept_id is not None else "category_dept=%s")
            _owner_val = final_dept_id if final_dept_id is not None else final_dept
            cur.execute(
                f"UPDATE {_op_db()}.kb_contribution SET review_status='accepted',"
                " ingestion_status='registering', reviewed_by=%s, reviewed_at=NOW(),"
                " review_note=%s, doc_id=%s, upload_id=%s, raw_key=%s,"
                f" question=%s, content=%s, {_owner_set}, normalized_question=%s, question_hash=%s"
                " WHERE contribution_id=%s AND review_status='pending'",
                (kb.user_id, (req.note or None), doc_id, upload_id, raw_key, final_q, final_c,
                 _owner_val, C.normalize_question(final_q), C.question_hash(final_q), cid))
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
                     owner_dept_id=final_dept_id,
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
        requires_kb_admin_approval=claim["requires_kb_approval"],
        owner_dept_id=claim["owner_dept_id"])
    # 批次ε-2：审核结果通知提交人（commit+物化之后；模块内 best-effort no-raise，绝不反噬主流程）
    from opensearch_pipeline.admin_notify import notify_contribution_result
    notify_contribution_result(
        cid,
        ("pending_approval" if claim["requires_kb_approval"]
         else "failed" if ing == C.INGEST_FAILED else "accepted"),
        error=(err or ""), actor_id=kb.user_id)
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
            axis = _contrib_axis_present(cur)
            cur.execute("SELECT review_status, ingestion_status, doc_id, category_dept"
                        + (", category_dept_id" if axis else "")
                        + f" FROM {_op_db()}.kb_contribution WHERE contribution_id=%s FOR UPDATE", (cid,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=404, detail="贡献不存在")
            review_status, ingestion_status, doc_id, dept = (row[0] or "pending"), (row[1] or "none"), row[2], (row[3] or "")
            dept_id = (row[4] if (axis and len(row) > 4) else None) or None
            if not _contrib_can_manage(kb, dept_id, dept, cur=cur):
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
    # 批次ε-2：驳回结果通知提交人（commit 之后；文案含理由，空理由有「未填写理由」兜底）
    from opensearch_pipeline.admin_notify import notify_contribution_result
    notify_contribution_result(cid, "rejected", note=(req.note or ""), actor_id=kb.user_id)
    return KbContributionActionResponse(contribution_id=cid, review_status="rejected",
        ingestion_status="none", state="rejected", ok=True)


@router.post("/api/kb/contributions/{cid}/retry-ingestion", response_model=KbContributionActionResponse)
def kb_contribution_retry(cid: str, request: Request,
                          identity: Optional[Identity] = Depends(current_identity)):
    """重试入库（registering/failed → 用【固定键】续跑物化，绝不新建文档）。仅已采纳行。

    ε-5 审计 P0 出路侧：物化续跑成功后，若文档停在内容阶段 FAILED（自动重试已用尽的
    死链，reconcile 已把贡献翻 failed），同步重置为 NOT_STARTED 重新排队（_requeue_failed_doc，
    谓词锁死 FAILED、其余状态零触碰）——否则对这类行「重试」只是 registered↔failed 空转。
    """
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
            axis = _contrib_axis_present(cur)
            cur.execute("SELECT review_status, ingestion_status, doc_id, raw_key, category_dept,"
                        " question, content"
                        + (", category_dept_id" if axis else "")
                        + f" FROM {_op_db()}.kb_contribution WHERE contribution_id=%s FOR UPDATE", (cid,))
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise HTTPException(status_code=404, detail="贡献不存在")
            review_status, ingestion_status = (row[0] or "pending"), (row[1] or "none")
            doc_id, raw_key, dept, q, content = row[2], row[3], (row[4] or ""), row[5], row[6]
            dept_id = (row[7] if (axis and len(row) > 7) else None) or None
            if not _contrib_can_manage(kb, dept_id, dept, cur=cur):
                conn.rollback()
                raise HTTPException(status_code=403, detail="无权操作该部门的贡献")
            if dept_id is not None:
                # M5 第 3 处：与 accept 同款——节点可能在采纳之后被停用；在**锁内同一快照**上查，
                # 比放到连接关闭后再查更强（不给中间态留窗口）。
                _assert_node_active(cur, dept_id, status=403)
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
    # Codex C2：本处此前**整条路径遗漏 node 轴** —— node 贡献续跑会走组码 authorize_upload
    # 并按 dept（node 行恒空串）恒被拒 ⇒ fail-closed 成「永远待 kb_admin 审批」。
    try:
        _perm = kb_upload.perm_from_raw_key(raw_key)
        if dept_id is not None:
            _d = kb_authz.authorize_upload_node(kb, dept_id, _perm,
                                                descendants=_kb_managed_descendants(kb))
        else:
            _d = kb_authz.authorize_upload(kb, dept, _perm)
        _requires_approval = (not _d.allowed) or bool(_d.requires_kb_admin_approval)
    except Exception:  # noqa: BLE001 — 裁决不可用 → 保守转审批（fail-closed）
        _requires_approval = True
    ing, err = _finish_contribution_ingestion(
        cid, doc_id=doc_id, raw_key=raw_key, owner_dept=dept, question=q, content=content,
        reviewer_id=kb.user_id, reviewer_name=kb.name or "", trace_id=trace_id,
        requires_kb_admin_approval=_requires_approval, owner_dept_id=dept_id)
    if ing == C.INGEST_REGISTERED:
        # 内容阶段 FAILED 的死链文档重新排队（谓词内锁死 FAILED，其余状态 no-op）
        _requeue_failed_doc(doc_id, trace_id=trace_id)
    # 批次ε-2：续跑结果同样通知提交人（作者自己点的重试 actor==author → 模块内跳过不自扰）
    from opensearch_pipeline.admin_notify import notify_contribution_result
    notify_contribution_result(
        cid,
        ("pending_approval" if _requires_approval
         else "failed" if ing == C.INGEST_FAILED else "accepted"),
        error=(err or ""), actor_id=kb.user_id)
    return KbContributionActionResponse(contribution_id=cid, review_status="accepted",
        ingestion_status=ing, state=C.contribution_state("accepted", ing), doc_id=doc_id,
        ok=(ing != C.INGEST_FAILED), error=(err or ""),
        requires_kb_admin_approval=_requires_approval)


@router.get("/api/kb/contributions/heroes", response_model=KbHeroesResponse)
def kb_contribution_heroes(request: Request, identity: Optional[Identity] = Depends(current_identity)):
    """总积分榜（2026-08-01，Sam 拍板权重 10/1/3，预览口径）：
    score = 10×采纳入库 + 1×被引用 + 3×有效反馈，全公司前 10。

    三项构成（口径=docs/ai_reward_metrics_2026-07-27.md）：
      · 采纳=ingestion_status='searchable'（⑦：采纳但入库失败不计分）；
      · 被引用=cited 事实表全期口径（fact 路径关 → hits=None，分数按 0 如实少算）；
      · 有效反馈=downvote+RESOLVED+按 message_id 首报去重（④⑤，防乱踩/组团），
        且限渠道 console+小程序（①：conversation_type IS NULL）。
    候选池 = 采纳作者 ∪ 有效反馈者（只反馈没贡献也上榜——反馈是办法认可的参与项）。
    """
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
            # ① 采纳数（kb_contribution 表规模小，全量聚合后在 py 内合成排名）
            cur.execute(
                f"SELECT author_id, MAX(author_name), COUNT(*) c FROM {_op_db()}.kb_contribution"
                " WHERE ingestion_status='searchable' GROUP BY author_id")
            adopted: Dict[str, int] = {}
            names: Dict[str, str] = {}
            for r in cur.fetchall() or []:
                if r and r[0]:
                    adopted[r[0]] = int(r[2] or 0)
                    names[r[0]] = r[1] or ""
            # ② 有效反馈数（④⑤⑥①）：每个 downvote 消息只记首报者；处置是按 message_id
            # 批量置 RESOLVED，故必须按 message_id 去重。GROUP_CONCAT 取 (created_at, id)
            # 最早行（staffId 无逗号，SUBSTRING_INDEX 安全）。失败 → 该项按空（榜单不倒）。
            feedback: Dict[str, int] = {}
            try:
                cur.execute(
                    "SELECT t.uid, COUNT(*) FROM ("
                    " SELECT SUBSTRING_INDEX(GROUP_CONCAT(f.user_id ORDER BY f.created_at ASC, f.id ASC), ',', 1) uid"
                    f" FROM {_op_db()}.user_feedback f"
                    f" JOIN {_op_db()}.qa_session_log q"
                    "   ON q.message_id = f.message_id AND q.conversation_type IS NULL"
                    " WHERE f.feedback_type='downvote' AND f.handled_status='RESOLVED'"
                    " GROUP BY f.message_id) t WHERE t.uid IS NOT NULL AND t.uid<>'' GROUP BY t.uid")
                feedback = {r[0]: int(r[1] or 0) for r in (cur.fetchall() or []) if r and r[0]}
            except Exception as e:   # noqa: BLE001 — 单项失败绝不拖垮榜单
                logger.info("heroes 有效反馈聚合失败（该项按 0）: %s", e)
            # ③ 被引用数：覆盖全部采纳作者（不再只回填 TOP10——引用现在进分）。
            hits: Optional[Dict[str, int]] = None
            if adopted:
                from opensearch_pipeline.qa_facts import FACT_TABLE, fact_join_enabled
                if fact_join_enabled():
                    try:
                        ph = ",".join(["%s"] * len(adopted))
                        cur.execute(
                            f"SELECT c.author_id, COUNT(DISTINCT jt.message_id)"
                            f" FROM {_op_db()}.kb_contribution c"
                            f" JOIN {_op_db()}.{FACT_TABLE} jt"
                            f"   ON jt.doc_id = c.doc_id AND jt.cited=1"
                            f" WHERE c.ingestion_status='searchable' AND c.author_id IN ({ph})"
                            " GROUP BY c.author_id",
                            tuple(adopted))
                        hits = {r[0]: int(r[1] or 0) for r in (cur.fetchall() or [])}
                    except Exception as e:   # noqa: BLE001 — 激励信号绝不拖垮榜单
                        logger.info("heroes 引用数聚合失败（诚实 None）: %s", e)
            # 合成 + 排名（分同 → 采纳多者前 → uid 字典序，确定性）
            pool = sorted(set(adopted) | set(feedback))
            scored = []
            for uid in pool:
                a = adopted.get(uid, 0)
                fb = feedback.get(uid, 0)
                h = (hits or {}).get(uid, 0) if hits is not None else 0
                scored.append((HERO_SCORE_W_ADOPTED * a + HERO_SCORE_W_HIT * h
                               + HERO_SCORE_W_FEEDBACK * fb, a, uid, fb))
            scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
            top = scored[:10]
            # ── 三榜切换（2026-08-01）：组织归属聚合 ─────────────────────────────
            # 归属 = staff_dim.primary_dept（⚠️ 现实现=min(dept_ids)，多部门者取最小 id——
            # 已知近似，见 ai_reward_metrics §3）→ dept_dim 上溯到一级部门（中心，depth==1）。
            # 快照缺失/查询失败 → 部门两榜为空、全公司榜不受影响（榜单绝不被组织数据拖垮）。
            dept_items: List[KbDeptScoreItem] = []
            my_dept_items: List[KbHeroItem] = []
            my_dept_name = ""
            my_l1 = None
            l1_of: Dict[str, tuple] = {}           # uid → (l1_dept_id, l1_name)
            try:
                if scored:
                    ph = ",".join(["%s"] * len(scored))
                    cur.execute(
                        f"SELECT staff_id, primary_dept FROM {_kb_db()}.staff_dim"
                        f" WHERE is_active=1 AND primary_dept IS NOT NULL AND staff_id IN ({ph})",
                        tuple(uid for _, _, uid, _ in scored))
                    prim = {r[0]: int(r[1]) for r in (cur.fetchall() or []) if r and r[1]}
                    cur.execute(f"SELECT dept_id, parent_id, name, depth FROM {_kb_db()}.dept_dim"
                                " WHERE is_active=1")
                    dmap = {int(r[0]): (int(r[1]), r[2] or "", int(r[3] or 0))
                            for r in (cur.fetchall() or [])}

                    def _l1(did):
                        hops = 0
                        while did in dmap and hops < 15:
                            parent, name, depth = dmap[did]
                            if depth <= 1:
                                return did, name
                            did = parent
                            hops += 1
                        return None, ""

                    for uid, did in prim.items():
                        l1_id, l1_name = _l1(did)
                        if l1_id:
                            l1_of[uid] = (l1_id, l1_name)
                    # 请求者本部门（可能不在积分池里，单查一次）
                    cur.execute(f"SELECT primary_dept FROM {_kb_db()}.staff_dim"
                                " WHERE staff_id=%s AND is_active=1", (identity.user_id,))
                    mrow = cur.fetchone()
                    if mrow and mrow[0]:
                        my_l1, my_dept_name = _l1(int(mrow[0]))
                    # 部门榜：同分求和 + 参与人数（未在册者不计入部门榜，个人榜照常）
                    agg: Dict[int, List] = {}
                    for score, a, uid, fb in scored:
                        got = l1_of.get(uid)
                        if not got:
                            continue
                        cell = agg.setdefault(got[0], [got[1], 0, 0])
                        cell[1] += score
                        cell[2] += 1
                    ranked = sorted(agg.items(), key=lambda kv: (-kv[1][1], kv[1][0]))[:10]
                    dept_items = [KbDeptScoreItem(rank=i + 1, dept_id=did, dept_name=v[0],
                                                  members=v[2], score=v[1])
                                  for i, (did, v) in enumerate(ranked)]
            except Exception as e:   # noqa: BLE001 — 组织数据绝不拖垮榜单
                logger.info("heroes 组织归属聚合失败（部门榜为空）: %s", e)
                my_l1 = None
            my_top = ([t for t in scored if l1_of.get(t[2], (None,))[0] == my_l1][:10]
                      if my_l1 else [])
            # 展示名回填覆盖 全公司 TOP ∪ 本部门 TOP（只反馈没贡献的人没有 author_name）
            _need = {uid for _, _, uid, _ in top} | {uid for _, _, uid, _ in my_top}
            missing = [uid for uid in sorted(_need) if not names.get(uid)]
            if missing:
                try:
                    ph = ",".join(["%s"] * len(missing))
                    cur.execute(f"SELECT user_id, user_name FROM {_kb_db()}.user_role"
                                f" WHERE user_id IN ({ph})", tuple(missing))
                    for r in cur.fetchall() or []:
                        if r and r[0] and (r[1] or ""):
                            names[r[0]] = r[1]
                except Exception as e:   # noqa: BLE001
                    logger.info("heroes 展示名回填失败（回退 uid）: %s", e)

            def _item(i, t):
                score, a, uid, fb = t
                return KbHeroItem(
                    rank=i + 1, author_id=uid, author_name=names.get(uid, ""),
                    count=a, adopted=a, feedback=fb, score=score,
                    hits=((hits or {}).get(uid, 0) if hits is not None else None))

            items = [_item(i, t) for i, t in enumerate(top)]
            my_dept_items = [_item(i, t) for i, t in enumerate(my_top)]
    except Exception as e:
        logger.info("heroes 查询失败（fail-open 空榜）: %s", e)
        return KbHeroesResponse(items=items)
    finally:
        conn.close()
    return KbHeroesResponse(items=items, dept_items=dept_items,
                            my_dept_items=my_dept_items, my_dept_name=my_dept_name)


def _gaps_review_funnel(identity) -> dict:
    """审核漏斗字段（2026-07-15 拍板：API 层只给管理员）。非管理员/算不出 → {}。

    独立小连接、按请求计算——绝不写进 gaps 共享缓存（cache_key 只按 depts，同部门
    管理员与员工共享缓存条目，进缓存必跨角色泄漏）。失败静默 {}，绝不拖垮 gaps 主路径。"""
    try:
        from opensearch_pipeline.api import _require_kb_console
        _require_kb_console(identity)          # 非管理员 → HTTPException → 走 except 返回 {}
    except Exception:   # noqa: BLE001
        return {}
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT SUM(review_status='accepted'), SUM(review_status='rejected'),"
                    " AVG(TIMESTAMPDIFF(MINUTE, created_at, reviewed_at))"
                    f" FROM {_op_db()}.kb_contribution"
                    " WHERE reviewed_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)")
                row = cur.fetchone() or (None, None, None)
        finally:
            conn.close()
        acc, rej = int(row[0] or 0), int(row[1] or 0)
        out: dict = {}
        if acc + rej > 0:
            out["review_accept_rate_30d"] = acc / (acc + rej)
        if row[2] is not None:
            out["review_avg_hours_30d"] = round(float(row[2]) / 60.0, 1)
        return out
    except Exception as e:   # noqa: BLE001
        logger.info("kb_gaps 审核漏斗聚合失败（诚实缺省）: %s", e)
        return {}


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
    depts = kb_authz.sanitize_owner_depts(identity.acl_groups)
    trace_id = get_request_id()

    def _page_response(open_gaps, summary):
        """分页前全集 → 本页响应（PII 脱敏只做本页，供缓存命中/未命中两路共用）。
        审核漏斗在这里按【请求身份】补注（model_copy 不改缓存对象）——缓存里的 summary
        永远不带漏斗字段。"""
        page = open_gaps[offset:offset + limit]
        items = [KbGapItem(
            question=C.redact_query_text(g["raw"]), asks=g["asks"], last_days=g["days"],
            dept=g["dept"], kind=g["kind"], question_hash=g["hash"],
            source_message_id=g["msg"], has_pending_contribution=g["pending"],
            phrasings=int(g.get("phrasings") or 1),
            representative_message_id=g["msg"],
            has_context=bool(g.get("has_context"))) for g in page]
        funnel = _gaps_review_funnel(identity)
        if funnel:
            summary = summary.model_copy(update=funnel)
        return KbGapsResponse(items=items, summary=summary, has_more=(offset + limit) < len(open_gaps))

    # 可见范围只由 depts 决定 → 按 sorted(depts) 键取缓存（同部门用户共享；权限判定在键构造前已完成）
    cache_key = tuple(sorted(depts))
    cached = _gaps_cache_get(cache_key)
    if cached is not None:
        return _page_response(cached[0], cached[1])
    open_gaps, summary = _compute_open_gaps(depts, trace_id)
    return _page_response(open_gaps, summary)


def _compute_open_gaps(depts: List[str], trace_id: str):
    """开放缺口全集 (open_gaps, summary)——kb_gaps 列表与 kb_gap_dismiss 可见性裁权共用
    （批次9，ultra P3 contribution:1422：单一实现防两处谓词漂移）。fails==0 才写缓存
    （降级结果不缓存，语义与抽取前一致）；聚合面 ≥4 路失败抛 HTTPException(500)。"""
    from opensearch_pipeline import contribution as C
    win = _GAP_WINDOW_DAYS
    cache_key = tuple(sorted(depts))
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_gaps 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"缺口查询失败 (trace: {trace_id})")
    # hash → 聚合体
    agg: Dict[str, Dict[str, Any]] = {}
    fails = 0
    _junk_on = _junk_filter_on()

    def _accumulate(qtext, msg_id, days_ago, dept, kind, sid=None, rid=None, rewritten=None):
        # 展示/归并口径：改写发生的行按改写后的独立问题（与写侧 question_hash 语义一致）
        display = rewritten or qtext
        # 分层第 1 层：确定性垃圾（纯标点/单字/纯数字）读出侧直接过滤（逃生阀 flag）
        if _junk_on and C.is_junk_question(display):
            return
        h = C.question_hash(display)
        if not h:
            return
        try:
            rid_i = int(rid) if rid is not None else None
        except (TypeError, ValueError):
            rid_i = None
        # 行级完整性：有改写 = 已是独立问题；否则按「短且缺主体」判定
        row_complete = bool(rewritten) or not C.is_incomplete_question(display)
        # 追问型（2026-07-18 Sam：上下文只对追问型有意义）：原文短缺主体或发生过改写——
        # 独立完整问题的"前几轮"往往是别的主题，不给上下文按钮
        row_followup = bool(rewritten) or C.is_incomplete_question(qtext)
        e = agg.get(h)
        if e is None:
            e = {"hash": h, "raw": display or "", "msgs": set(), "days": int(days_ago or 0),
                 "dept": dept or "", "kind": kind,
                 # 代表行 = 首见行（NO_RESULT 源按 created_at DESC，首见即最新）
                 "msg": msg_id or "", "rep": (sid or "", rid_i),
                 "complete": row_complete, "followup": row_followup, "ctx": []}
            agg[h] = e
        if msg_id:
            e["msgs"].add(msg_id)
        e["days"] = min(e["days"], int(days_ago or 0))
        if not e["dept"] and dept:
            e["dept"] = dept
        e["complete"] = e["complete"] or row_complete
        e["followup"] = e["followup"] or row_followup
        if sid and rid_i is not None and len(e["ctx"]) < 5:
            e["ctx"].append((sid, rid_i))
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
                    _nr_base = (
                        "SELECT q.query_text, q.message_id, DATEDIFF(NOW(), q.created_at),"
                        " q.user_dept, q.session_id, q.id{rw}"
                        f" FROM {_op_db()}.qa_session_log q"
                        " WHERE q.answer_status='NO_RESULT'"
                        "   AND q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                        f"   AND q.user_dept IN ({ph})"
                        # 全序（2026-08-06 codex 补评审）：created_at 是**秒精度** DATETIME，
                        # 同秒行在 LIMIT 边界上由优化器任选 ⇒ 两次重算可选中不同候选，
                        # 而下游 hash 终排序补不回**上游已漏选**的行。
                        # tiebreaker 取 `q.id`（PK）——`message_id` 只有普通索引
                        # `idx_message_id`，DDL 注释「消息唯一ID」**不是**唯一约束。
                        # ⚠️ 补全序只稳定「选哪 2000 条」，**不解决 2000 硬 cap 本身的
                        # 不完整**（第 2001 条起永不可见）——后者记为设计题，见 backlog。
                        " ORDER BY q.created_at DESC, q.id DESC LIMIT %s")
                    _params = tuple([win] + depts + [_GAP_CANDIDATE_CAP])
                    _has_rw = _exec_gap_sql(cur, _nr_base.format(rw=", q.rewritten_query"),
                                            _nr_base.format(rw=""), _params)
                    for r in cur.fetchall() or []:
                        # len 护栏：真实 SQL 恒 ≥6 列；只兜测试桩/驱动差异的短行
                        _accumulate(r[0], r[1], r[2], r[3], "no_result",
                                    sid=(r[4] if len(r) > 4 else None),
                                    rid=(r[5] if len(r) > 5 else None),
                                    rewritten=(r[6] if (_has_rw and len(r) > 6) else None))
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
                _rf_base = (
                    "SELECT t.query_text, t.message_id, t.days_ago, t.any_dept, t.sid, t.rid{rw_o}"
                    " FROM ("
                    " SELECT q.message_id,"
                    "   MAX(q.query_text) query_text, DATEDIFF(NOW(), MAX(q.created_at)) days_ago,"
                    f"   {mine_expr} hit_mine,"
                    "   MIN(CASE WHEN m.permission_level='public' THEN 1 ELSE 0 END) all_public,"
                    "   MIN(m.owner_dept) any_dept,"
                    "   MAX(q.session_id) sid, MAX(q.id) rid{rw_i}"
                    f" FROM {_op_db()}.qa_session_log q"
                    + qa_docs_join_sql()
                    + " WHERE q.answer_status='REFUSAL' AND q.retrieved_docs_json IS NOT NULL"
                    "   AND q.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
                    " GROUP BY q.message_id"
                    ") t WHERE t.hit_mine=1 OR t.all_public=1"
                    # 全序（同上）：days_ago 只有**整天**粒度，同日行在 LIMIT 边界不确定。
                    # tiebreaker 取 `t.rid`（子查询的 `MAX(q.id)`）：q.id 是全局 PK，
                    # 而 `GROUP BY q.message_id` 的分组彼此不相交 ⇒ 各组 MAX(id) 必不相同。
                    # DESC 让同一天内较新的日志优先，与「最近优先」的展示语义一致。
                    # （方向与前项 ASC 不同是**有意**的：该查询经 GROUP BY/派生表，本就要排序，
                    #  不存在可被单次索引扫描满足的计划——方向跟随规则不适用于此站点。）
                    " ORDER BY t.days_ago ASC, t.rid DESC LIMIT %s")
                _rf_params = tuple(params + [_GAP_CANDIDATE_CAP])
                _has_rw = _exec_gap_sql(
                    cur,
                    _rf_base.format(rw_o=", t.rw", rw_i=", MAX(q.rewritten_query) rw"),
                    _rf_base.format(rw_o="", rw_i=""), _rf_params)
                for r in cur.fetchall() or []:
                    _accumulate(r[0], r[1], r[2], r[3], "refusal",
                                sid=(r[4] if len(r) > 4 else None),
                                rid=(r[5] if len(r) > 5 else None),
                                rewritten=(r[6] if (_has_rw and len(r) > 6) else None))
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
            # 3.6) 忽略台账（schema/041）：dept_admin「忽略此缺口」的 active 行排除出列表。
            # 独立降级不进 fails（041 未 apply 时 fail-open 不排除——宁可噪音回来不丢真缺口）。
            dismissed: Set[str] = set()
            if agg:
                try:
                    hl = list(agg.keys())
                    ph = ",".join(["%s"] * len(hl))
                    cur.execute(
                        f"SELECT question_hash FROM {_op_db()}.qa_gap_dismissal"
                        f" WHERE revoked_at IS NULL AND question_hash IN ({ph})", tuple(hl))
                    dismissed = {r[0] for r in (cur.fetchall() or []) if r[0]}
                except Exception as e:
                    logger.info("kb_gaps 忽略台账不可用（不排除，non-fatal）: %s", e)
            # 3.5) 语义组映射（schema/040，RAG_QA_GAP_SEMANTIC 默认关）：相似问法展示层
            # 归并的输入。独立 try、不进 fails 500 阈值（漏斗同款先例）；表缺失/查询失败
            # → 空映射=不归并原样展示（fail-open）。归并本身在组装段（纯函数）。
            group_map: Dict[str, str] = {}
            if agg:
                from opensearch_pipeline.qa_gap_groups import load_group_map, semantic_groups_on
                if semantic_groups_on():
                    try:
                        group_map = load_group_map(cur, _op_db(), list(agg.keys()))
                    except Exception as e:
                        logger.info("kb_gaps 语义组映射不可用（不归并，non-fatal）: %s", e)
            # 3.7) 会话上下文批查（2026-07-18）：每 session 一次 MIN(id)——某行 id > 该
            # session 最小 id 即「有前序问答」。供 ① has_context（前端「查看上下文」门）
            # ② incomplete 低质量隐藏判定（无任何上下文才隐藏）。独立降级不进 fails：
            # 查询失败 → session_min 缺项按「未知」处理（不隐藏、不显上下文按钮，fail-open）。
            session_min: Dict[str, int] = {}
            if agg:
                need_sessions = {s for e in agg.values()
                                 for (s, r) in ([e["rep"]] + e["ctx"]) if s and r is not None}
                try:
                    sess_list = sorted(need_sessions)[:1500]
                    for i in range(0, len(sess_list), 500):
                        chunk = sess_list[i:i + 500]
                        ph = ",".join(["%s"] * len(chunk))
                        cur.execute(
                            f"SELECT session_id, MIN(id) FROM {_op_db()}.qa_session_log"
                            f" WHERE session_id IN ({ph}) GROUP BY session_id", tuple(chunk))
                        for sid, mid in cur.fetchall() or []:
                            if sid and mid is not None:
                                session_min[str(sid)] = int(mid)
                except Exception as e:
                    logger.info("kb_gaps 会话上下文批查不可用（fail-open）: %s", e)
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
    def _has_ctx(sid: str, rid) -> bool:
        return bool(sid) and rid is not None and sid in session_min and int(rid) > session_min[sid]

    _hide_on = _hide_incomplete_on()
    open_gaps = []
    for h, e in agg.items():
        if h in covered_closed or h in dismissed:
            continue
        # 分层第 3 层：短缺主体（全部成员 incomplete、无改写）且【毫无会话上下文】
        # → 低质量隐藏（独立 flag；session_min 缺项=未知，按有上下文处理，绝不误隐）
        if _hide_on and not e["complete"]:
            known = [(s, r) for (s, r) in e["ctx"] if s in session_min]
            if known and not any(_has_ctx(s, r) for (s, r) in known):
                continue
        rep_sid, rep_rid = e["rep"]
        open_gaps.append({
            "hash": h, "raw": e["raw"], "asks": len(e["msgs"]) or 1, "days": e["days"],
            "dept": e["dept"], "kind": e["kind"], "msg": e["msg"],
            "pending": h in covered_pending,
            # 收窄（2026-07-18）：仅追问型缺口给上下文按钮——独立问题的前几轮多为无关主题
            "has_context": e["followup"] and _has_ctx(rep_sid, rep_rid),
        })
    # 语义组归并（纯函数；关闭判定已按 exact hash 在上方逐成员完成，归并只影响展示）：
    # 同组开放成员并为一张卡（asks 求和/days 取 min/refusal 优先），卡片 hash 恒为真实
    # 成员 hash——贡献提交仍只关闭该成员，绝不跨问法自动关闭（schema/040 预注册边界）。
    if group_map:
        from opensearch_pipeline.qa_gap_groups import merge_semantic_gaps
        open_gaps = merge_semantic_gaps(open_gaps, group_map)
    # hash 兜底 tiebreaker（2026-08-04 独立核验 B3）：(-asks, days) 非全序——同热度同龄的
    # 缺口相对次序逐跑可漂，Python 切片分页（offset 参数）跨缓存 TTL 会漏/重。console
    # 现只发单页（limit=30 无 offset），此为 API 层潜伏面的封口，非现网 bug。
    open_gaps.sort(key=lambda g: (-g["asks"], g["days"], g.get("hash") or ""))
    summary.unanswered = len(open_gaps)
    if fails == 0:   # 降级结果（部分子查询失败）不缓存——下一请求重试取全量
        _gaps_cache_put(cache_key, (open_gaps, summary))
    return open_gaps, summary


# ── 缺口卡会话上下文展开（2026-07-18）：多轮追问沉淀的缺口单看是无上下文短问题，
#    认领者展开该提问所在会话的前几轮问答才能看懂。可见性与 /api/kb/gaps 同源
#    （轻量单 message EXISTS 谓词，不重跑聚合）；他人问答全程脱敏。────────────────
class KbGapContextTurn(BaseModel):
    question: str = ""             # 前序用户提问（redact_query_text 脱敏）
    answer_status: str = ""        # 该轮回答状态（SUCCESS/REFUSAL/NO_RESULT/…）
    answer_excerpt: str = ""       # 回答节选（仅 SUCCESS 轮给；审计级脱敏后截断 200 字）
    created_at: str = ""


class KbGapContextResponse(BaseModel):
    message_id: str = ""
    items: List[KbGapContextTurn] = Field(default_factory=list)   # 时间正序（旧→新）


def _gap_message_visible(cur, row, depts: List[str]) -> bool:
    """单 message 的缺口可见性谓词——与 _compute_open_gaps 两条候选 SQL 的部门口径
    同源：NO_RESULT=提问部门∈depts；REFUSAL=命中文档归属∈depts 或全部命中为 public。
    row=(id, session_id, created_at, answer_status, user_dept, message_id)。"""
    status, user_dept = row[3], row[4]
    if status == "NO_RESULT":
        return bool(depts) and (user_dept or "") in depts
    # REFUSAL：轻量单 message 聚合（与列表 SQL 同一 hit_mine/all_public 口径）
    from opensearch_pipeline.qa_facts import qa_docs_join_sql
    mine_expr = "0"
    params: List[Any] = []
    if depts:
        ph = ",".join(["%s"] * len(depts))
        mine_expr = f"MAX(CASE WHEN m.owner_dept IN ({ph}) THEN 1 ELSE 0 END)"
        params.extend(depts)
    cur.execute(
        f"SELECT {mine_expr} hit_mine,"
        "   MIN(CASE WHEN m.permission_level='public' THEN 1 ELSE 0 END) all_public"
        f" FROM {_op_db()}.qa_session_log q"
        + qa_docs_join_sql()
        + " WHERE q.message_id=%s AND q.retrieved_docs_json IS NOT NULL"
        " GROUP BY q.message_id",
        tuple(params + [row[5]]))
    vis = cur.fetchone()
    return bool(vis) and (int(vis[0] or 0) == 1 or int(vis[1] or 0) == 1)


@router.get("/api/kb/gaps/context", response_model=KbGapContextResponse)
def kb_gap_context(request: Request, message_id: str,
                   identity: Optional[Identity] = Depends(current_identity)):
    """缺口代表提问的会话上文（前 ≤3 轮）。message 不存在/非缺口行 → 404；
    存在但不在调用者可见缺口池 → 403。(created_at, id) 复合游标——同时间戳不漏不乱。"""
    _enforce_rate_limit(request, identity, scope="aux")
    if not identity or not identity.user_id:
        raise HTTPException(status_code=401, detail="需要登录")
    from opensearch_pipeline import contribution as C, kb_authz
    depts = kb_authz.sanitize_owner_depts(identity.acl_groups)
    trace_id = get_request_id()
    if not message_id or len(message_id) > 128:
        raise HTTPException(status_code=404, detail="记录不存在")
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, session_id, created_at, answer_status, user_dept, message_id"
                    f" FROM {_op_db()}.qa_session_log WHERE message_id=%s LIMIT 1",
                    (message_id,))
                row = cur.fetchone()
                if not row or row[3] not in ("NO_RESULT", "REFUSAL"):
                    raise HTTPException(status_code=404, detail="记录不存在")
                if not _gap_message_visible(cur, row, depts):
                    raise HTTPException(status_code=403, detail="无权查看该缺口上下文")
                rid, sid, created = int(row[0]), row[1], row[2]
                # 多取（8）再滤噪音轮（垃圾/寒暄，2026-07-18）后取最近 3——「问：5」「问：hi」
                # 这类轮对理解追问语境零价值，不展示
                cur.execute(
                    "SELECT query_text, answer_status, answer_text, created_at, id"
                    f" FROM {_op_db()}.qa_session_log"
                    " WHERE session_id=%s AND (created_at < %s OR (created_at = %s AND id < %s))"
                    " ORDER BY created_at DESC, id DESC LIMIT 8",
                    (sid, created, created, rid))
                prior = [r for r in (cur.fetchall() or [])
                         if not C.is_noise_turn(r[0])][:3]
        finally:
            conn.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.error("kb_gap_context 查询失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"上下文查询失败 (trace: {trace_id})")
    items: List[KbGapContextTurn] = []
    for (qtext, status, ans, cat, _pid) in reversed(prior):   # 时间正序（旧→新）
        excerpt = ""
        if (status or "") == "SUCCESS" and ans:
            # 约束 8：回答是审计级文本，redact_query_text（面向短查询）不够——用
            # redaction.redact_text（feedback_comment 同款），**脱敏后**再截断。
            try:
                from opensearch_pipeline.redaction import redact_text
                masked, _cnt = redact_text(str(ans))
                excerpt = masked[:200]
            except Exception:   # noqa: BLE001 — 脱敏失败宁可不给节选
                excerpt = ""
        items.append(KbGapContextTurn(
            # 长度防线：脱敏后截 120 字（用户可能贴一大段）；答案节选已在上方截 200
            question=C.redact_query_text(str(qtext or ""))[:120],
            answer_status=str(status or ""), answer_excerpt=excerpt,
            created_at=str(cat or "")))
    return KbGapContextResponse(message_id=message_id, items=items)


# ── 「忽略此缺口」（schema/041，ε-4 遗留；2026-07-15 用户拍板：交给 dept_admin）────
class KbGapDismissRequest(BaseModel):
    question_hash: str = ""
    question: str = ""     # 展示快照（可选；落 preview 供审计知道忽略了什么）
    reason: str = ""       # 忽略原因（可选）


class KbGapDismissResponse(BaseModel):
    ok: bool = True
    affected: int = 0      # 实际落行/恢复的成员 hash 数（语义组联动时可 >1）


def _valid_gap_hash(h: str) -> bool:
    return len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def _expand_gap_targets(conn, h: str) -> Set[str]:
    """语义组联动（RAG_QA_GAP_SEMANTIC 开时）：把动作扩展到同组全部成员——否则归并卡片
    摘头后其余成员下轮换头复现（打地鼠）。联动查询失败 → 只动单条（fail-open）。
    新问法（新 hash）不受既往忽略牵连（语义连坐永久静默有意不做，见 schema/041 头注）。"""
    targets = {h}
    from opensearch_pipeline.qa_gap_groups import semantic_groups_on
    if not semantic_groups_on():
        return targets
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT group_id FROM {_op_db()}.qa_gap_semantic_group"
                        " WHERE question_hash=%s", (h,))
            row = cur.fetchone()
            if row and row[0]:
                cur.execute(f"SELECT question_hash FROM {_op_db()}.qa_gap_semantic_group"
                            " WHERE group_id=%s", (row[0],))
                targets |= {r[0] for r in (cur.fetchall() or []) if r[0]}
    except Exception as e:   # noqa: BLE001
        logger.info("忽略缺口语义联动不可用（只动单条，non-fatal）: %s", e)
    return targets


class KbGapDismissedItem(BaseModel):
    question_hash: str = ""
    question_preview: str = ""     # 忽略时的问法快照（已脱敏；老行可能为空）
    reason: str = ""
    dismissed_by_name: str = ""
    dismissed_at: str = ""         # 最近一次忽略时间（updated_at）


class KbGapDismissedResponse(BaseModel):
    items: List[KbGapDismissedItem] = Field(default_factory=list)
    # P3-3（2026-08-04）：硬 LIMIT 100，此前截断不外露 —— 已忽略缺口超过 100 条时，
    # 更早的忽略无法在 UI 里寻回（本端点是 restore 的**唯一入口**）⇒ 撤销路径实际断掉。
    truncated: bool = False


@router.get("/api/kb/gaps/dismissed", response_model=KbGapDismissedResponse)
def kb_gaps_dismissed(request: Request,
                      identity: Optional[Identity] = Depends(current_identity)):
    """已忽略缺口列表（「已忽略」折叠区，2026-07-15 拍板补齐）：刷新后撤销的唯一 UI 入口
    ——此前 restore 端点在而无处可点（忽略行被读侧排除后不可寻回）。仅 active 行
    （revoked_at IS NULL）；console 管理员专属；最近忽略在前，LIMIT 100 兜底。"""
    _enforce_rate_limit(request, identity, scope="aux")
    _require_kb_console(identity)
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_gaps_dismissed 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"查询失败 (trace: {trace_id})")
    items: List[KbGapDismissedItem] = []
    _truncated = False   # P3-3：与 items 同初始化——fail-open 降级路径下也要有确定值
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT question_hash, question_preview, reason, dismissed_by_name, updated_at"
                f" FROM {_op_db()}.qa_gap_dismissal WHERE revoked_at IS NULL"
                " ORDER BY updated_at DESC LIMIT 101")   # 100+1 探针行（P3-3）
            _rows = cur.fetchall() or []
            _truncated = len(_rows) > 100
            for h, prev, reason, by_name, at in _rows[:100]:
                items.append(KbGapDismissedItem(
                    question_hash=h or "", question_preview=prev or "", reason=reason or "",
                    dismissed_by_name=by_name or "",
                    dismissed_at=(at.isoformat() if at else "")))
    except Exception as e:   # noqa: BLE001 — 041 未 apply 的环境诚实空列表（fail-open）
        logger.info("kb_gaps_dismissed 查询失败（空列表，non-fatal）: %s", e)
    finally:
        conn.close()
    return KbGapDismissedResponse(items=items, truncated=_truncated)


@router.post("/api/kb/gaps/dismiss", response_model=KbGapDismissResponse)
def kb_gap_dismiss(req: KbGapDismissRequest, request: Request,
                   identity: Optional[Identity] = Depends(current_identity)):
    """忽略「待回答」缺口（dept_admin/kb_admin；员工 403）。可撤销留痕（schema/041），
    重复忽略幂等复活同一行（last action wins）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    from opensearch_pipeline import contribution as C, kb_authz
    h = (req.question_hash or "").strip().lower()
    if not _valid_gap_hash(h):
        raise HTTPException(status_code=400, detail="question_hash 非法")
    trace_id = get_request_id()
    # 批次9（ultra P3 contribution:1422）：忽略动作按可见范围裁权——忽略台账是【全局排除】
    # （列表 3.6 段按 hash 排除，不分部门），而此前 dismiss 不校归属：任何 dept_admin 可对
    # 任意问题文本离线算 hash、全局静默压掉别部门的缺口。现 dept_admin 只能忽略自己缺口
    # 视图（本部门归属 + 全公开池，与列表同一 _compute_open_gaps 谓词）内的 hash；
    # kb_admin 不受限。重试幂等：已处 active 忽略态的 hash 放行（重复忽略=复活同一行）。
    if kb.role != kb_authz.ROLE_KB_ADMIN:
        _depts = kb_authz.sanitize_owner_depts(identity.acl_groups)
        _cached = _gaps_cache_get(tuple(sorted(_depts)))
        _visible = _cached[0] if _cached is not None else _compute_open_gaps(_depts, trace_id)[0]
        if not any(g["hash"] == h for g in _visible):
            _already = False
            try:
                from opensearch_pipeline.db import _get_db_conn as _gdc
                _vc = _gdc()
                try:
                    with _vc.cursor() as _cur:
                        _cur.execute(
                            f"SELECT 1 FROM {_op_db()}.qa_gap_dismissal"
                            " WHERE question_hash=%s AND revoked_at IS NULL LIMIT 1", (h,))
                        _already = _cur.fetchone() is not None
                finally:
                    _vc.close()
            except Exception:   # noqa: BLE001 — 幂等豁免查不出按不可见处理（fail-closed）
                _already = False
            if not _already:
                raise HTTPException(status_code=403,
                                    detail="该缺口不在你的可见范围（本部门+公开池），无法忽略")
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_gap_dismiss 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"忽略失败 (trace: {trace_id})")
    try:
        targets = _expand_gap_targets(conn, h)
        preview = (C.redact_query_text(req.question) or "")[:512] or None
        reason = (req.reason or "").strip()[:255] or None
        with conn.cursor() as cur:
            for t in sorted(targets):
                cur.execute(
                    f"INSERT INTO {_op_db()}.qa_gap_dismissal"
                    " (question_hash, question_preview, reason, dismissed_by, dismissed_by_name)"
                    " VALUES (%s,%s,%s,%s,%s)"
                    " ON DUPLICATE KEY UPDATE revoked_at=NULL, revoked_by=NULL,"
                    "   reason=VALUES(reason), dismissed_by=VALUES(dismissed_by),"
                    "   dismissed_by_name=VALUES(dismissed_by_name),"
                    "   question_preview=COALESCE(VALUES(question_preview), question_preview)",
                    (t, preview if t == h else None, reason, kb.user_id, kb.name or ""))
        conn.commit()
    except Exception as e:
        logger.error("kb_gap_dismiss 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"忽略失败 (trace: {trace_id})")
    finally:
        conn.close()
    _gaps_cache_clear()   # 忽略即时生效（写路径清缓存惯例）
    return KbGapDismissResponse(ok=True, affected=len(targets))


@router.post("/api/kb/gaps/restore", response_model=KbGapDismissResponse)
def kb_gap_restore(req: KbGapDismissRequest, request: Request,
                   identity: Optional[Identity] = Depends(current_identity)):
    """恢复被忽略的缺口（撤销动作同权限；语义联动与 dismiss 对称——否则撤销只回半组）。"""
    _enforce_rate_limit(request, identity, scope="aux")
    kb = _require_kb_console(identity)
    h = (req.question_hash or "").strip().lower()
    if not _valid_gap_hash(h):
        raise HTTPException(status_code=400, detail="question_hash 非法")
    trace_id = get_request_id()
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
    except Exception as e:
        logger.error("kb_gap_restore 连接失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"恢复失败 (trace: {trace_id})")
    try:
        targets = sorted(_expand_gap_targets(conn, h))
        with conn.cursor() as cur:
            ph = ",".join(["%s"] * len(targets))
            cur.execute(
                f"UPDATE {_op_db()}.qa_gap_dismissal"
                " SET revoked_at=NOW(), revoked_by=%s"
                f" WHERE question_hash IN ({ph}) AND revoked_at IS NULL",
                tuple([kb.user_id] + targets))
            affected = int(cur.rowcount or 0)
        conn.commit()
    except Exception as e:
        logger.error("kb_gap_restore 失败 [trace=%s]: %s", trace_id, e, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"恢复失败 (trace: {trace_id})")
    finally:
        conn.close()
    _gaps_cache_clear()
    return KbGapDismissResponse(ok=True, affected=affected)
