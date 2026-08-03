# -*- coding: utf-8 -*-
"""raw_inventory.py — B10 + B11（2026-07-25）：`raw/` 对象的只读盘点探针。

两条合并成一个作业，**共享一次 `raw/` LIST**（B10 与 B11 的对象集合完全重叠，分开跑等于
把同一份全量 LIST 付两遍，还会对同一个对象重复报警）。

为什么需要：
  · B10 —— 自助上传（console 两段式直传）在 register 失败时会留下 OSS 对象但无
    `document_version` 行。`register_new_files.py` 对这类孤儿是 `continue` + 注释
    「孤儿存储由 GC 清理」，而全仓**没有**这个 GC；`reconcile.run_raw_parity_check` 又是
    单向的（RDS→OSS，查"注册了但对象没了"），管不了反方向。
  · B11 —— `scan_oss_sync_keys.py` 那行"OSS 新文件（未注册）"计数既不过 `ingest_policy`
    准入策略（归档/临时文件/旧格式/不支持格式全算进去），`db_raw_keys` 又只从
    `status='active'` 构建（退役/被取代版本的 raw_key 会被算成"新文件"）。而且那份报告
    没有任何消费者 —— 脚本 DRY_RUN=True，产物只是节点日志里的一行 print。

四个桶（口径写死在这里，避免各处再长出第五份计数）：
  self_serve_orphan   —— 自助上传路径形状、超过 upload token TTL 仍未登记
  expected_reject     —— B14 的"同 ETag 升版"被有意拒绝：对象在、但那是正确拦截而非丢失
  unregistered        —— 过了准入策略、确实该注册却没注册的
  policy_rejected     —— 被准入策略挡掉的（归档 / 临时文件 / 旧格式 / 非文档格式）

⚠️ **只读、只报数、绝不删**。而且本作业**尚未接任何调度** —— `ops_monitor` 自注 DataWorks
调度未建立，现网节点只跑 `--only reconcile_ha3 reconcile_oss`。接调度与告警阈值属 C1
（先配 webhook 再加探针），本文件不宣称任何"监控闭环"。
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

RAW_PREFIX = "raw/"


def _doc_id_from_self_serve_key(key: str) -> Optional[str]:
    """自助上传 raw_key 的形状：raw/<dept>[/<perm>]/<doc_id>/<upload_id>/<filename>。

    与 kb_upload.build_raw_key 同一契约；doc_id 恒以 `DOC_` 起头（kb_upload.new_doc_id）。
    取不出来 → 不是自助上传路径（可能是运维批量拷贝/历史 cohort）。
    """
    parts = (key or "").split("/")
    for p in parts:
        if p.startswith("DOC_"):
            return p
    return None


def run_raw_inventory(*, alert: bool = True) -> dict:
    """一次 LIST，四桶分类。fail-open：任何异常都返回 ok=False + errors，绝不抛。"""
    result = {
        "ok": True,
        "counts": {"total": 0, "self_serve_orphan": 0, "expected_reject": 0,
                   "unregistered": 0, "policy_rejected": 0},
        "oldest_orphan_age_days": None,
        "samples": {},
        "errors": [],
    }
    try:
        import datetime as _dt

        from opensearch_pipeline.clients import _get_oss_bucket
        from opensearch_pipeline.ingest_policy import should_ingest_raw_key
        from opensearch_pipeline.kb_upload import UPLOAD_TOKEN_TTL
        from opensearch_pipeline.pipeline_nodes import _get_db_conn

        # ⚠️ `_get_oss_bucket()` 返回的是 **二元组** `(bucket_or_None, is_simulated)`——此前这里
        # 漏解包：元组恒 truthy 使 `is None` 恒 False，`ObjectIterator(元组)` 迭代时抛
        # AttributeError 被下方 broad except 吞掉 ⇒ 本探针在【任何】环境恒返回 ok=False、四桶计数
        # 恒 0。全仓其余 8 处调用方均正确解包，唯此一处笔误（C1，2026-08-03）。
        bucket, is_sim = _get_oss_bucket()
        if is_sim:
            # simulate 无真实对象可盘 —— 明确 skipped，别伪装成"盘点成功且零孤儿"（假绿）
            result["skipped"] = "simulate"
            return result
        if bucket is None:
            result["ok"] = False
            result["errors"].append("OSS bucket unavailable")
            return result

        import oss2
        objects = {}
        for obj in oss2.ObjectIterator(bucket, prefix=RAW_PREFIX, max_keys=1000):
            if obj.key.endswith("/"):
                continue
            objects[obj.key] = obj
        result["counts"]["total"] = len(objects)
        if not objects:
            return result

        # RDS 侧取 **全集**（不是只取 active）—— 退役/被取代版本的 raw_key 同样是"已注册"，
        # 只减 active 会把它们误报成"新文件"（B11 的口径缺陷之一）。
        conn = _get_db_conn(select_db=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT raw_key, doc_id, etag FROM document_version")
                rows = cur.fetchall() or []
        finally:
            conn.close()
        registered = {r[0] for r in rows if r[0]}
        # B14 的"同 ETag 升版被拒"证据：doc_id → 该文档已登记过的 etag 集合。
        # 判据必须**可复算**：光看"对象在但没有 document_version 行"分不出
        # 「正确拦截」与「register 真的丢了」。
        etags_by_doc = {}
        for _rk, _doc, _etag in rows:
            if _doc and _etag:
                etags_by_doc.setdefault(_doc, set()).add(str(_etag))

        now = _dt.datetime.now(_dt.timezone.utc)
        oldest_days = None
        samples = {k: [] for k in ("self_serve_orphan", "expected_reject",
                                   "unregistered", "policy_rejected")}
        for key, obj in objects.items():
            if key in registered:
                continue
            ingestable, _reason = should_ingest_raw_key(key)
            doc_id = _doc_id_from_self_serve_key(key)
            age_s = None
            try:
                age_s = (now - _dt.datetime.fromtimestamp(
                    obj.last_modified, _dt.timezone.utc)).total_seconds()
            except Exception:      # noqa: BLE001 — 时间戳形态异常不该毁掉整次盘点
                age_s = None

            if doc_id and etags_by_doc.get(doc_id) and str(
                    getattr(obj, "etag", "") or "").strip('"') in etags_by_doc[doc_id]:
                bucket_name = "expected_reject"      # B14 有意拒绝：同 ETag 升版
            elif doc_id and age_s is not None and age_s > UPLOAD_TOKEN_TTL:
                bucket_name = "self_serve_orphan"    # 自助上传后未登记，且 token 已过期
            elif ingestable:
                bucket_name = "unregistered"
            else:
                bucket_name = "policy_rejected"

            result["counts"][bucket_name] += 1
            if len(samples[bucket_name]) < 5:
                samples[bucket_name].append(key)
            if bucket_name == "self_serve_orphan" and age_s is not None:
                d = age_s / 86400.0
                oldest_days = d if oldest_days is None else max(oldest_days, d)

        result["samples"] = {k: v for k, v in samples.items() if v}
        result["oldest_orphan_age_days"] = (
            round(oldest_days, 1) if oldest_days is not None else None)
        logger.info("[RAW-INVENTORY] %s", result["counts"])
    except Exception as e:      # noqa: BLE001 — 只读盘点，绝不打断调用方
        result["ok"] = False
        result["errors"].append(f"raw inventory failed: {e}")
        logger.warning("[RAW-INVENTORY] failed (fail-open): %s", e)
    return result
