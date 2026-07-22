# -*- coding: utf-8 -*-
"""
qa_logger.py — RAG 问答日志写入模块

每次 RAG 问答完成后，将完整的问答上下文写入 qa_session_log 表。
所有写入操作均用 try/except 包裹，失败只记日志不阻断回复。

供 dingtalk_bot.py 和 api.py 共用。
"""

import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 评审F11（2026-07-21）：qa_session_log 强制写契约列——单一事实源，三方共同消费：
#   ① log_qa_session（serving 主写方）② insert_qa_row_tx（Agent 原子事务写方）
#   ③ /api/ready 的 operation_db 列漂移探针（api.py::_compute_readiness）
# 顺序即 INSERT 列序（两写方的 vals 列表按位对应）；改列必须三处同步。
QA_SESSION_MANDATORY_COLS = (
    "session_id", "message_id", "user_id", "user_name", "user_dept",
    "query_text", "answer_text", "intent_type", "risk_level", "risk_blocked",
    "retrieved_docs_json", "cited_docs_json",
    "latency_ms", "retrieval_latency_ms", "llm_latency_ms",
    "answer_status", "model_name", "error_message",
    "opensearch_hit_count", "top_score", "conversation_type",
    "content_blocks_json",
)

# 评审F11：表结构漂移（1054/1146）ops 告警——进程内至多一次【尝试】（检查即置位：
# 并发首失只发一枚；发送失败不重试——logger.critical 每次照记、alerting 自带 fail-open）。
_SCHEMA_DRIFT_ALERTED = False
_SCHEMA_DRIFT_ALERT_LOCK = threading.Lock()


def _alert_schema_drift_once(errno, exc) -> None:
    """qa_session_log 表结构漂移 → send_ops_alert(critical)（至多一次；绝不抛）。"""
    global _SCHEMA_DRIFT_ALERTED
    try:
        with _SCHEMA_DRIFT_ALERT_LOCK:
            if _SCHEMA_DRIFT_ALERTED:
                return
            _SCHEMA_DRIFT_ALERTED = True
        from opensearch_pipeline.alerting import send_ops_alert
        send_ops_alert(
            "qa_session_log 表结构漂移——问答日志静默丢失中",
            f"errno={errno}: {exc}。修复前每一条问答日志都在丢、反馈无法按 message_id "
            "关联（readiness 默认不因此翻红）。请立即 apply 对应 schema 迁移。",
            severity="critical",
        )
    except Exception:  # noqa: BLE001 — 告警失败绝不外溢到答案主路径
        logger.warning("schema-drift ops 告警发送失败（fail-open）", exc_info=True)


def _op_db() -> str:
    """问答运营库名（qa_session_log/user_feedback/escalation_ticket 所在库）。
    经 RAG_RDS_OPERATION_DATABASE 配置（STAGING 用 fuling_operation_stg）。"""
    from opensearch_pipeline.config import get_config
    return get_config().rds.operation_database


def generate_message_id() -> str:
    """生成唯一的 message_id，作为反馈系统的核心关联键。"""
    return str(uuid.uuid4())


def fetch_answer_by_message_id(message_id: str) -> Optional[Dict[str, Any]]:
    """按 message_id 取回最终答案（U1 答案读回，重审计 §5：审批续跑/断线后发起人经
    run 详情拿到答案文本——agent_run.message_id(schema/036) → 本函数）。

    只取 SUCCESS 行（AGENT_ERROR 行同 id 复用，读回只要答案）；同 id 多行取最新
    （message_id 无 UNIQUE 约束）。读失败/simulate 无库 → None（fail-open，调用方
    引导用户走会话历史）。"""
    if not message_id:
        return None
    try:
        from opensearch_pipeline.db import _get_db_conn
        conn = _get_db_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT answer_text, created_at FROM {_op_db()}.qa_session_log "
                    "WHERE message_id=%s AND answer_status='SUCCESS' "
                    "ORDER BY id DESC LIMIT 1",
                    (message_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        return {"message_id": message_id, "answer_text": row[0],
                "answered_at": str(row[1]) if row[1] is not None else None}
    except Exception:   # noqa: BLE001 — 读回是辅助路径，绝不外泄异常
        logger.warning("按 message_id 读回答案失败（忽略）", exc_info=True)
        return None


def _qa_log_pii_redact_on() -> bool:
    """RAG_QA_LOG_PII_REDACT 开关（懒读 config；异常退回 True=安全方向）。"""
    try:
        from opensearch_pipeline.config import get_config
        return bool(get_config().rag.qa_log_pii_redact)
    except Exception:
        return True


def _redact_for_log(text: Optional[str]) -> Optional[str]:
    """写 qa_session_log 前对 query_text/answer_text 做查询侧 PII 不可逆掩码（OBS-qa-pii）。

    复用入库侧 redaction.redact_text（纯本地正则，不传 name_llm_fn → 无 LLM/网络/延迟），
    把身份证/手机号/邮箱/银行卡/地址/密钥及标注式姓名替换为占位符。flag 关或空文本时原样
    返回。

    B1 P1-09（生产级外审 2026-07-17，行为更替）：掩码异常**不再退回原文**——待掩码文本
    恰是可能含 PII 的那份，「掩码坏了」的窗口期把明文写库与同文件 content_blocks 的
    丢弃策略（_redact_content_blocks_for_log 异常→None）相悖。改为写不可逆占位
    （sha256 前 16 位 + 长度：可对账/去重，不可还原），error 级留痕不阻断主写入；
    逃生口 RAG_QA_LOG_REDACT_FAILOPEN=true 还原旧「退回原文」行为（默认 fail-closed）。"""
    if not text or not _qa_log_pii_redact_on():
        return text
    try:
        from opensearch_pipeline.redaction import redact_text
        masked, _counts = redact_text(text)
        return masked
    except Exception as e:
        import hashlib as _hl
        import os as _os
        if _os.environ.get("RAG_QA_LOG_REDACT_FAILOPEN",
                           "").strip().lower() in ("1", "true", "yes", "on"):
            logger.error("qa_session_log PII 掩码失败——FAILOPEN 逃生口开启，退回原文: %s", e)
            return text
        digest = _hl.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        logger.error("qa_session_log PII 掩码失败——正文以不可逆占位落库 "
                     "(sha256:%s len:%d): %s", digest, len(text), e, exc_info=True)
        return f"[PII_REDACT_FAILED sha256:{digest} len:{len(text)}]"


def _redact_content_blocks_for_log(cbj: Optional[str]) -> Optional[str]:
    """写 qa_session_log 前对 content_blocks_json 做【结构感知】PII 掩码（OBS-qa-pii 的补口）。

    query_text/answer_text 已在别处掩码，但同一行的 content_blocks_json（图文卡片序列化）
    此前旁路了掩码 —— markdown/文本块里复述的身份证/手机号会明文驻留、且经 /api/history
    原样回渲。这里 json.loads 后只对【文本载荷】跑 redact_text：type=text 的 text、image 的
    caption/alt/title、以及 legacy markdown 的 content；url/oss_key 一律不动，故卡片回调重签
    （content_blocks_builder.refresh_image_block_urls 只改 url 字段）完全不受影响。
    flag 关或空时原样返回；解析/掩码异常时返回 None（丢弃图文块，绝不把未脱敏 PII 落库）。"""
    if not cbj or not _qa_log_pii_redact_on():
        return cbj
    try:
        from opensearch_pipeline.redaction import redact_text
        blocks = json.loads(cbj)
        if not isinstance(blocks, list):
            return cbj
        changed = False
        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") in ("text", "markdown"):
                keys = ("text", "content")
            elif b.get("type") == "image":
                keys = ("caption", "alt", "title")
            else:
                continue
            for k in keys:
                if b.get(k):
                    masked, counts = redact_text(b[k])
                    if counts:
                        b[k] = masked
                        changed = True
        # 无命中时原样返回（字节级不变，避免无谓的 JSON 空白归一化 / 回调重签面）。
        return json.dumps(blocks, ensure_ascii=False) if changed else cbj
    except Exception as e:
        logger.warning("qa_session_log content_blocks PII 掩码失败，丢弃图文块 (non-fatal): %s", e)
        return None


def _conversation_history_on() -> bool:
    """RAG_CONVERSATION_HISTORY 开关（懒读 config；异常退回 False）。"""
    try:
        from opensearch_pipeline.config import get_config
        return bool(get_config().rag.conversation_history)
    except Exception:
        return False


def _exec_conversation_upsert(conn, user_id: str, conversation_id: str, title) -> None:
    """执行 qa_conversation 幂等 upsert 语句（**不 commit**，事务边界由调用方掌控）。

    标题仅首次落、后续只更新时间。隐藏状态由删除接口单独管理，本 upsert 不触碰
    hidden_at —— 故对已隐藏会话继续写入不会令其自动重现。
    perf E#47：拆出纯执行体，主路径把它并入审计行 INSERT 的同一事务（一次 commit）；
    降级路径 _upsert_conversation 仍以独立小事务包装（现状两段式）。
    """
    with conn.cursor() as c2:
        c2.execute(
            f"""
            INSERT INTO {_op_db()}.qa_conversation
                (user_id, conversation_id, title, created_at, updated_at, last_message_at)
            VALUES (%s, %s, %s, NOW(3), NOW(3), NOW(3))
            ON DUPLICATE KEY UPDATE updated_at = NOW(3), last_message_at = NOW(3)
            """,
            (user_id, conversation_id, (title or "")[:255]),
        )


def _upsert_conversation(conn, user_id: str, conversation_id: str, title) -> None:
    """会话元数据幂等 upsert（独立小事务降级路径）。

    失败仅 warning、绝不回滚已落库的审计行。合并事务（E#47 主路径）失败时回退到本函数，
    行为与历史两段式完全一致。
    """
    try:
        _exec_conversation_upsert(conn, user_id, conversation_id, title)
        conn.commit()
    except Exception as ce:
        logger.warning(
            "qa_conversation upsert 失败 (non-fatal): conversation_id=%s, %s",
            conversation_id, ce,
        )


# schema/018 未 apply 的进程内负缓存（P2-20/21/22）：首次 1054(gen_meta_json) 后本进程
# 不再尝试携带该列 —— qa_session_log 每问一行，逐行「试写失败再回退」会翻倍 RDS 往返、
# 刷屏 warning（qa_rollup._upsert_daily 的同款 1054 回退无缓存是因为它每天只跑一次）。
# apply 迁移后重启 serving 进程即恢复携带。
def _serialize_retrieved(retrieved_docs: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """retrieved_docs → 瘦身 JSON（只保留溯源关键字段，避免存储过大）。

    答案血缘：chunk_id(内嵌 version) + version_no,使一条回答可溯源到精确 chunk/版本。
    不带它们时,re-chunk 后 chunk_index 漂移 → 历史答案无法定位到原始来源(L7-01/INC-6)。"""
    if not retrieved_docs:
        return None
    return json.dumps(
        [
            {
                "doc_id": d.get("doc_id", ""),
                "chunk_id": d.get("chunk_id", ""),
                "version_no": d.get("version_no"),
                "title": d.get("title", ""),
                "section_title": d.get("section_title", ""),
                "score": d.get("score", 0),
                "chunk_index": d.get("chunk_index", 0),
            }
            for d in retrieved_docs
        ],
        ensure_ascii=False,
    )


def insert_qa_row_tx(
    cur,
    *,
    session_id: str,
    message_id: str,
    query_text: str,
    user_id: Optional[str] = None,
    user_dept: Optional[str] = None,
    answer_text: Optional[str] = None,
    conversation_id: Optional[str] = None,
    answer_status: str = "SUCCESS",
    model_name: Optional[str] = None,
    error_message: Optional[str] = None,
    retrieved_docs: Optional[List[Dict[str, Any]]] = None,
    latency_ms: int = 0,
) -> None:
    """qa_session_log 行的**事务内**写入变体（P0-01，unknown-unknowns 批次1）。

    executor.complete_run_atomic 在 running→succeeded 同一事务里回调本函数——答案与
    succeeded 原子共存亡。与 log_qa_session 的关键差异：
    - **会抛异常**（fail-closed）：写不进 → 调用方回滚整个完成事务、run 落 failed，
      绝不发 done。log_qa_session 的「绝不抛出」契约只适用于辅助性落库，真值写必须响。
    - 不 commit（骑调用方事务）；不做 conversation upsert / qa_retrieved_doc 物化——
      那些是增强，commit 后由 qa_answer_post_commit best-effort 补。
    - 可选列（conversation_id / question_hash）沿用 1054 摘除重试阶梯（语句级错误不
      终止 InnoDB 事务，行锁与已写语句保持）——列缺失绝不让整个 run 失败。
    - latency_ms（γ1/M9.1，Majors 批次 γ，codex 共识 2026-07-21）：agent 完成事务
      传入累计活跃执行耗时（active_latency_ms 口径，不含审批等待）——此前恒 0，
      agent 行在通用延迟 p95 里全体隐形。既有调用不传维持 0。
    PII 掩码与主路径同口径（_redact_for_log）。"""
    query_text = _redact_for_log(query_text)
    answer_text = _redact_for_log(answer_text)
    base_cols = list(QA_SESSION_MANDATORY_COLS)   # 单一事实源（评审F11）
    base_vals: List[Any] = [
        session_id, message_id, user_id or "", None, user_dept,
        query_text, answer_text, None, None, 0,
        _serialize_retrieved(retrieved_docs), None,
        int(latency_ms or 0), None, None,
        answer_status, model_name, error_message,
        None, None, None,
        None,
    ]
    global _QUESTION_HASH_COL_MISSING
    opt_cols: List[str] = []
    opt_vals: List[Any] = []
    if conversation_id and _conversation_history_on():
        opt_cols.append("conversation_id")
        opt_vals.append(conversation_id)
    if query_text and not _QUESTION_HASH_COL_MISSING:
        try:
            from opensearch_pipeline.contribution import question_hash as _qhash_fn
            opt_cols.append("question_hash")
            opt_vals.append(_qhash_fn(query_text))
        except Exception:   # noqa: BLE001 — 哈希派生绝不拖垮真值写
            pass
    while True:
        cols, vals = base_cols + opt_cols, base_vals + opt_vals
        try:
            cur.execute(
                f"INSERT INTO {_op_db()}.qa_session_log ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})",
                tuple(vals),
            )
            return
        except Exception as ge:
            gerr = ge.args[0] if getattr(ge, "args", None) and isinstance(ge.args[0], int) else None
            if not opt_cols or gerr != 1054:
                raise
            msg = str(ge)
            idx = next((i for i, c in enumerate(opt_cols) if c in msg), len(opt_cols) - 1)
            cname = opt_cols.pop(idx)
            opt_vals.pop(idx)
            if cname == "question_hash" and cname in msg:
                _QUESTION_HASH_COL_MISSING = True
            logger.warning(
                "insert_qa_row_tx：%s 列缺失，摘除后事务内重试（请应用对应 schema）: message_id=%s",
                cname, message_id,
            )


def qa_answer_post_commit(
    message_id: str,
    *,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    query_text: Optional[str] = None,
    retrieved_docs: Optional[List[Dict[str, Any]]] = None,
    cited_docs: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """P0-01 配套：qa 行已随 complete_run_atomic 事务落库之后的 best-effort 增强——
    qa_retrieved_doc 物化（perf#3）+ conversation upsert（schema/006）。全程吞异常：
    增强绝不影响已成真的 run（与 log_qa_session 内联时的容错语义一致）。"""
    try:
        from opensearch_pipeline.db import _get_db_conn

        conn = _get_db_conn()
        try:
            try:
                from opensearch_pipeline.qa_facts import insert_qa_doc_facts
                insert_qa_doc_facts(conn, message_id, retrieved_docs, cited_docs)
            except Exception as fe:   # noqa: BLE001
                logger.warning("qa_retrieved_doc 物化异常 (non-fatal): %s", fe)
            if conversation_id and _conversation_history_on():
                _upsert_conversation(conn, user_id or "", conversation_id,
                                     _redact_for_log(query_text))
        finally:
            conn.close()
    except Exception:   # noqa: BLE001
        logger.warning("qa 完成后增强失败 (non-fatal): message_id=%s", message_id, exc_info=True)


_GEN_META_COL_MISSING = False
# schema/039 未 apply 时的进程内负缓存（question_hash 可选列，与 gen_meta 同款纪律）
_QUESTION_HASH_COL_MISSING = False
# schema/050 未 apply 时的 **TTL** 负缓存（rewritten_query 可选列）：与上两个永久布尔
# 不同——到期自动重试探测，schema apply 后无须重启进程即恢复携带（约束 10）。
_REWRITTEN_COL_MISSING_UNTIL = 0.0
_REWRITTEN_COL_RETRY_SECONDS = 600.0


def log_qa_session(
    *,
    session_id: str,
    message_id: str,
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    user_dept: Optional[str] = None,
    query_text: str,
    answer_text: Optional[str] = None,
    intent_type: Optional[str] = None,
    risk_level: Optional[str] = None,
    risk_blocked: bool = False,
    retrieved_docs: Optional[List[Dict[str, Any]]] = None,
    cited_docs: Optional[List[Dict[str, Any]]] = None,
    latency_ms: int = 0,
    retrieval_latency_ms: Optional[int] = None,
    llm_latency_ms: Optional[int] = None,
    answer_status: str = "SUCCESS",
    model_name: Optional[str] = None,
    error_message: Optional[str] = None,
    opensearch_hit_count: Optional[int] = None,
    top_score: Optional[float] = None,
    conversation_type: Optional[str] = None,
    content_blocks_json: Optional[str] = None,
    conversation_id: Optional[str] = None,
    gen_meta_json: Optional[str] = None,
    rewritten_query: Optional[str] = None,
) -> None:
    """
    写入一条 qa_session_log 记录。

    所有异常均被捕获并记录日志，绝不向调用方抛出异常。
    问答回复是核心功能，落库是辅助功能。

    Args:
        session_id: 会话 ID（钉钉 conversationId:staffId 或 API session）
        message_id: 本次回答的唯一 ID，后续反馈通过此 ID 关联
        user_id: 钉钉 staffId 或 API 调用方 ID
        user_name: 用户昵称
        user_dept: 用户部门代码
        query_text: 用户原始问题
        answer_text: 机器人回答快照
        retrieved_docs: OpenSearch 原始召回结果（topK chunks）
        cited_docs: 最终引用的来源文档
        latency_ms: 总耗时(ms)
        retrieval_latency_ms: 检索阶段耗时(ms)，第一版暂不填
        llm_latency_ms: LLM 生成阶段耗时(ms)，第一版暂不填
        answer_status: SUCCESS / REFUSAL / NO_RESULT / LLM_ERROR / RETRIEVAL_ERROR /
            BLOCKED / CLIENT_DISCONNECTED（SSE 客户端中途断开，回答截断，仅 /api/ask/stream）
        model_name: 使用的 LLM 模型名称
        error_message: 失败时的错误信息
        opensearch_hit_count: 检索命中数
        top_score: 最高检索得分
        conversation_type: '1'=单聊, '2'=群聊
        gen_meta_json: 生成元数据 JSON 快照（P2-20/21/22，schema/018 可选列；
            未 apply 时 1054 自动降级不携带，审计行绝不丢）
        rewritten_query: 追问改写后的独立问题（RAG_FOLLOWUP_REWRITE；schema/050 可选列，
            未 apply 时 1054 TTL 负缓存降级）。非空时 question_hash 改按它计算（脱敏后）
            ——该行「实际所问」是改写后的独立形式；原始 query_text 原样保留。
    """
    try:
        from opensearch_pipeline.db import _get_db_conn

        # 查询侧 PII 掩码（OBS-qa-pii）：用户问题与机器人回答在落盘前做不可逆掩码，
        # 避免身份证/手机号/受限文档 PII 明文驻留 qa_session_log。conversation 标题取
        # 自掩码后的 query_text（见下方 _upsert_conversation），故标题同样不含 PII。
        query_text = _redact_for_log(query_text)
        answer_text = _redact_for_log(answer_text)
        rewritten_query = _redact_for_log(rewritten_query) if rewritten_query else None
        # content_blocks_json 同为 PII sink（图文块里可能复述号码），结构感知掩码后再落库。
        content_blocks_json = _redact_content_blocks_for_log(content_blocks_json)

        # 序列化 JSON 字段（瘦身逻辑提为 _serialize_retrieved——insert_qa_row_tx 同口径复用）
        retrieved_json = _serialize_retrieved(retrieved_docs)

        cited_json = None
        if cited_docs:
            cited_json = json.dumps(cited_docs, ensure_ascii=False)

        conn = _get_db_conn()
        try:
            base_cols = list(QA_SESSION_MANDATORY_COLS)   # 单一事实源（评审F11）
            base_vals = [
                session_id, message_id, user_id or "", user_name, user_dept,
                query_text, answer_text, intent_type, risk_level,
                1 if risk_blocked else 0,
                retrieved_json, cited_json,
                latency_ms, retrieval_latency_ms, llm_latency_ms,
                answer_status, model_name, error_message,
                opensearch_hit_count, top_score, conversation_type,
                content_blocks_json,
            ]

            def _execute_insert(cols, vals):
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO {_op_db()}.qa_session_log ({', '.join(cols)}) "
                        f"VALUES ({', '.join(['%s'] * len(cols))})",
                        tuple(vals),
                    )

            def _insert(cols, vals):
                _execute_insert(cols, vals)
                conn.commit()

            # 正常路径：开关开 + 有 conversation_id → conversation_id 直接进主 INSERT（原子，无 post-commit 空窗）。
            # 兼容降级：库未迁移（unknown column 1054）→ 回滚后改 legacy INSERT，核心审计行恒落库、绝不丢。
            enrich = bool(conversation_id) and _conversation_history_on()
            conversation_upserted = False

            def _write_row(cols, vals):
                """一整次落行尝试（列集参数化；cols/vals 不含 conversation_id 增强列）。

                内含既有的 conversation_id 合并事务/两段式/1054-legacy 三级降级，逻辑与
                历史逐字节等价 —— 仅把 base_cols/base_vals 换成形参，供 gen_meta_json
                可选列（schema/018）在外层做同款 1054 回退。
                """
                nonlocal conversation_upserted
                try:
                    if enrich:
                        # perf E#47：qa_conversation upsert 并入主事务、同一 commit（省一次
                        # RDS 往返+fsync）。合并事务任何失败 → 回滚后降级为现状两段式：先单独
                        # 重试主 INSERT（含 conversation_id，1054 由外层 except 继续降级 legacy），
                        # upsert 交回下方独立小事务 best-effort——核心审计行绝不因 upsert 丢失。
                        try:
                            _execute_insert(cols + ["conversation_id"],
                                            vals + [conversation_id])
                            _exec_conversation_upsert(conn, user_id or "", conversation_id, query_text)
                            conn.commit()
                            conversation_upserted = True
                        except Exception as me:
                            logger.warning(
                                "qa_session_log 合并事务失败，降级两段式 (non-fatal): "
                                "message_id=%s, %s", message_id, me,
                            )
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                            _insert(cols + ["conversation_id"], vals + [conversation_id])
                    else:
                        _insert(cols, vals)
                except Exception as ie:
                    ierr = ie.args[0] if getattr(ie, "args", None) and isinstance(ie.args[0], int) else None
                    if enrich and ierr == 1054:
                        logger.warning(
                            "conversation_id 列缺失，降级 legacy INSERT（请应用 schema/006）: message_id=%s, %s",
                            message_id, ie,
                        )
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        _insert(cols, vals)
                    else:
                        raise

            # 可选列阶梯（不进 base_cols —— legacy 回退路径绝不携带，test_qa_logger 的
            # base_cols↔001/002 DDL 契约不变）。未 apply 时 1054 → 摘除该列重试并置进程内
            # 负缓存，审计行绝不因新列丢失：
            #   gen_meta_json（P2-20/21/22，schema/018）
            #   question_hash（缺口语义去重 Layer-1，schema/039）——对【脱敏后落库文本】
            #     计算 contribution.question_hash（与 kb_gaps Python 归并/asks 平查同口径，
            #     绝不能挪到 _redact_for_log 之前）。改写发生时（rewritten_query 非空）
            #     改按脱敏后的 rewritten 计算——该行实际所问是改写后的独立形式。
            #   rewritten_query（追问改写，schema/050）——TTL 负缓存（非永久布尔）。
            global _GEN_META_COL_MISSING, _QUESTION_HASH_COL_MISSING, _REWRITTEN_COL_MISSING_UNTIL
            _OPT_SCHEMA = {"gen_meta_json": "018", "question_hash": "039",
                           "rewritten_query": "050"}
            opt_cols: List[str] = []
            opt_vals: List[Any] = []
            if gen_meta_json and not _GEN_META_COL_MISSING:
                opt_cols.append("gen_meta_json")
                opt_vals.append(gen_meta_json)
            if query_text and not _QUESTION_HASH_COL_MISSING:
                try:
                    from opensearch_pipeline.contribution import question_hash as _qhash_fn
                    opt_cols.append("question_hash")
                    opt_vals.append(_qhash_fn(rewritten_query or query_text))
                except Exception:   # noqa: BLE001 — 哈希派生绝不拖垮审计行
                    pass
            if rewritten_query and time.time() >= _REWRITTEN_COL_MISSING_UNTIL:
                opt_cols.append("rewritten_query")
                opt_vals.append(rewritten_query)
            while True:
                try:
                    _write_row(base_cols + opt_cols, base_vals + opt_vals)
                    break
                except Exception as ge:
                    gerr = ge.args[0] if getattr(ge, "args", None) and isinstance(ge.args[0], int) else None
                    if not opt_cols or gerr != 1054:
                        raise
                    # 报错文本点名则精确摘除并置负缓存；未点名（驱动/桩差异）→ 摘除
                    # 末位可选列重试、不置负缓存（下条日志再探测）——与旧 gen_meta 行为
                    # 一致（旧代码任意 1054 均回退 base，仅点名时置缓存）。
                    msg = str(ge)
                    idx = next((i for i, c in enumerate(opt_cols) if c in msg), len(opt_cols) - 1)
                    cname = opt_cols.pop(idx)
                    opt_vals.pop(idx)
                    if cname in msg:
                        if cname == "gen_meta_json":
                            _GEN_META_COL_MISSING = True
                        elif cname == "rewritten_query":
                            # TTL 负缓存：到期自动重试，schema/050 apply 后无须重启恢复携带
                            _REWRITTEN_COL_MISSING_UNTIL = time.time() + _REWRITTEN_COL_RETRY_SECONDS
                        else:
                            _QUESTION_HASH_COL_MISSING = True
                    logger.warning(
                        "%s 列缺失，回退旧列集（请应用 schema/%s%s）: message_id=%s, %s",
                        cname, _OPT_SCHEMA.get(cname, "?"),
                        "；本进程不再携带" if cname in msg else "",
                        message_id, ge,
                    )
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            logger.info(
                "qa_session_log 写入成功: message_id=%s, status=%s",
                message_id, answer_status,
            )
            # perf#3：审计行已落库，顺手物化 (message_id, doc_id, cited) 瘦事实行——
            # 看板归属链从 JSON_TABLE 现场展开变普通索引 JOIN（schema/013 + RAG_QA_FACT_JOIN）。
            # fail-open，表缺失自动熔断；见 qa_facts.insert_qa_doc_facts。
            try:
                from opensearch_pipeline.qa_facts import insert_qa_doc_facts
                insert_qa_doc_facts(conn, message_id, retrieved_docs, cited_docs)
            except Exception as _fe:
                logger.warning("qa_retrieved_doc 物化异常 (non-fatal): %s", _fe)
            # best-effort 幂等 upsert 会话元数据（独立小事务，失败仅 warning）——
            # 仅在 E#47 合并事务未覆盖时（降级两段式 / legacy 回退）才补跑。
            if enrich and not conversation_upserted:
                _upsert_conversation(conn, user_id or "", conversation_id, query_text)
        finally:
            conn.close()

    except Exception as e:
        # 绝不阻断主流程；但表结构漂移（列/表不存在）意味着**每一条**问答日志都在静默丢失、
        # 反馈再也找不到 message_id —— 必须比普通写入失败喊得更响。pymysql 错误的 args[0] 是 errno。
        errno = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], int) else None
        if errno in (1054, 1146):  # 1054=Unknown column / 1146=表不存在
            logger.critical(
                "qa_session_log 表结构落后于代码 (errno=%s)：请在 RDS 重跑 "
                "schema/002_feedback_system.sql（幂等）。修复前所有问答日志静默丢失、"
                "反馈无法按 message_id 关联。message_id=%s, error=%s",
                errno, message_id, e,
            )
            _alert_schema_drift_once(errno, e)   # 评审F11：光 log 不 page 没人看见
        else:
            logger.error(
                "qa_session_log 写入失败 (non-fatal): message_id=%s, error=%s",
                message_id, e, exc_info=True,
            )
