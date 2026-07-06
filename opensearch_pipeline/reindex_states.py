"""Canonical document_version state transitions for re-chunk / re-index resets.

Single source of truth for the laptop push-then-purge / DataWorks re-chunk flow, so an ad-hoc reset
can never again leave a row in a state the stage-3 lock can't claim.

THE BUG THIS PREVENTS (2026-06-15 canary): a reset set ``document_version.index_status='NOT_STARTED'``.
Stage-2's chunk loader keys on ``chunk_meta.index_status='NOT_INDEXED'`` (so chunking ran), but the
stage-3 lock node (``pipeline_nodes.node_acquire_index_lock``) only preempts rows whose
``document_version.index_status IN ('NOT_INDEXED','FAILED')`` (+ a SUCCESS-relock fallback). With the
column left at 'NOT_STARTED' the lock matched nothing → stage 3 skipped every doc (0 work, 0 writes).

``index_status`` for ``document_version`` is an indexing-lifecycle column whose initial/ready value is
**NOT_INDEXED**, never 'NOT_STARTED' ('NOT_STARTED' is the content/chunk lifecycle's initial value).
"""

import hashlib


def docset_hash(doc_ids) -> str:
    """Stable 12-char hash binding an unfrozen-rechunk override token to a specific doc-set.

    The ``RAG_ALLOW_UNFROZEN_RECHUNK`` token is ``<op>:<YYYY-MM-DD>:<docset_hash>``; the hash pins the
    authorization to exactly the doc_ids it was minted for, so a same-day token cannot be silently
    reused for a *different* re-chunk batch (the failure this guards: ack minted for doc-set A
    accidentally authorizing an unfrozen re-chunk of doc-set B later the same day).

    Order-independent + deduped (sorted set), so a re-ordered/duplicated doc list yields the same hash.
    ``reset_for_rechunk.py`` prints this for the docs it resets; the stage-2 guard
    (``pipeline_nodes._unfrozen_rechunk_acked``) recomputes it for the flagged doc-set and requires an
    exact match. Single source of truth so the script and the node can never drift.
    """
    ids = sorted({str(d) for d in doc_ids})
    return hashlib.sha256(",".join(ids).encode("utf-8")).hexdigest()[:12]

class ChunkIndexStatus:
    """``chunk_meta.index_status`` 词表——成功值是 **INDEXED**，绝不是 'SUCCESS'。

    与 ``DocVersionIndexStatus`` 是两套不相交的词表（同名列、不同表、不同值集）：把一边的
    成功值写到另一边是静默 no-op（WHERE 匹配不到任何行），正是 2026-06-15 canary 的事故类。
    生产代码写/查该列时必须引用这里的常量（f-string 插值进 SQL 即可），不要再写裸字符串——
    tests/test_index_status_vocab.py 对关键模块做扫描耦合。
    """
    NOT_INDEXED = "NOT_INDEXED"      # 初始/标脏值（待 stage-3 推 HA3；ACL outbox 也用它标脏）
    INDEXED = "INDEXED"              # HA3 push 成功（chunk 级成功终态）
    FAILED = "FAILED"                # push / embedding / parity 校验失败（下轮 loader 重选）
    DELETED = "DELETED"              # 旧版本停用 / 隔离清除（伴随 is_active=0）
    # G9 死信终态：index_retry_count 达上限（默认 3，RAG_STAGE3_CHUNK_MAX_RETRIES）的
    # 毒 chunk。不在 loader 重选集内（消除队头阻塞整轮 drain），所属 document_version
    # 会被置 chunk_status='NEEDS_REVIEW' 供人工处理；修复后把该 chunk 复位
    # NOT_INDEXED + index_retry_count=0 即重新进队（schema/019）。
    DEAD = "DEAD"
    ALL = (NOT_INDEXED, INDEXED, FAILED, DELETED, DEAD)


class DocVersionIndexStatus:
    """``document_version.index_status`` 词表——成功值是 **SUCCESS**，绝不是 'INDEXED'。

    'INDEXED' 从未被任何代码写入本列（只存在于历史 schema 注释里）；按 'INDEXED' 查本列
    永远空集。同样，'NOT_STARTED' 属于 content/chunk 生命周期，绝不属于本列（见模块 docstring
    的 canary 事故）。
    """
    NOT_INDEXED = "NOT_INDEXED"      # 初始/复位值（stage-3 锁可抢占）
    PROCESSING = "PROCESSING"        # stage-3 持锁中（2h 失效接管窗口）
    SUCCESS = "SUCCESS"              # 索引完成 + 旧版本已停用（文档版本级成功终态）
    FAILED = "FAILED"                # 本轮索引失败（stage-3 锁可抢占重试）
    PENDING_DELETE = "PENDING_DELETE"  # HA3 删除失败排队重试（spot_checker 对账 drain）
    DELETED = "DELETED"              # 索引删除完成 / 隔离清除
    ALL = (NOT_INDEXED, PROCESSING, SUCCESS, FAILED, PENDING_DELETE, DELETED)


def sql_in_list(statuses) -> str:
    """把状态元组渲染成 SQL IN (...) 的字面量体："'A', 'B'"（含空格分隔，与既有 SQL 逐字一致）。

    只接受本模块的受信常量（[A-Z_]），绝不接受用户输入——调用方直接 f-string 进 SQL。
    """
    return ", ".join(f"'{s}'" for s in statuses)


# The exact set node_acquire_index_lock (pipeline_nodes.py) preempts on its primary UPDATE.
# Keep in sync with that node — tests/test_reset_for_rechunk.py asserts the coupling.
STAGE3_CLAIMABLE_INDEX_STATUS = (DocVersionIndexStatus.NOT_INDEXED, DocVersionIndexStatus.FAILED)

# The chunk_meta re-select set the stage-3 loader (dataworks_orchestrator) keys on —
# same spelling as the doc-version claimable set but a DIFFERENT table/vocabulary.
STAGE3_CHUNK_RESELECT_INDEX_STATUS = (ChunkIndexStatus.NOT_INDEXED, ChunkIndexStatus.FAILED)


def rechunk_reset_state() -> dict:
    """The document_version field values a re-chunk (stage 2 -> 3) reset MUST write.

    - content_process_status / chunk_status = 'NOT_STARTED'  -> stage-2 claim predicate selects it
    - index_status = 'NOT_INDEXED'                            -> stage-3 lock can preempt it (the fix)
    - retry_count = 0                                          -> fresh attempt budget

    Canonical KEEP-CANONICAL reset: callers must NOT clear canonical_json_key (extraction was fine;
    only chunking changed) and must scope to version_no = current_version_no.
    """
    return {
        "content_process_status": "NOT_STARTED",
        "chunk_status": "NOT_STARTED",
        "index_status": DocVersionIndexStatus.NOT_INDEXED,
        "retry_count": 0,
    }
