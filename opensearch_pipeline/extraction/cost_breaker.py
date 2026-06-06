# -*- coding: utf-8 -*-
"""
extraction/cost_breaker.py — 单文档成本熔断器 (Cost-Ceiling Breaker).

在 VLM layout-rebuild (Increment 1) 升级之前做前置成本预估与熔断。这是开启任何 VLM
版面重建之前必须先落地的硬前提：它替代了今天用 `max_ocr_pages=5` 兜住成本的临时上限，
防止"4000 页扫描 PDF 一次性过 VLM"导致数百~数万元的失控开销 (见 work_report.md)。

设计要点：
  - 估算只对"未命中缓存"的计费单元计费 (cache-aware)；全缓存命中的文档成本为 0。
  - 三道闸：
      1. 单文档硬单元上限 (max_pages)        — 原始计费单元数超过即拒绝 (与缓存无关)。
      2. 单文档预算 (doc_budget_rmb)          — 预估 RMB 超过即拒绝。
      3. 单次运行累计预算 (run_budget_rmb)    — 进程内累计花费越过即熔断，后续文档一律拒绝。
  - 拒绝 (DENY) 后由调用方就地封存 (quarantine) 文档并回退到确定性规则输出，绝不丢弃文档。
  - 总开关 `cfg.rebuild.enabled` 默认关闭 → 整个熔断器为 no-op (永远放行)，
    所以它可以先于 VLM rebuilder 安全落地而不改变现有行为。

复用参考：spot_checker.py:377-429 (封存事务形态)、pipeline_nodes.py:62-73 (_get_db_conn)。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# 计费单元类型
UNIT_OCR_PAGE = "ocr_page"     # PDF OCR-fallback：每页一次 OCR 调用
UNIT_VLM_IMAGE = "vlm_image"   # 嵌入式图片：每张一次 VLM (+OCR) 调用

# 预算比较容差：RMB 浮点累加会漂移 (0.4+0.4+0.4=1.2000…0666)，
# 恰好等于上限的预留不应被误拒。
_BUDGET_EPS = 1e-6


@dataclass
class CostEstimate:
    """单文档成本预估结果 (纯数据，无副作用)。"""
    file_ext: str
    billable_units: int        # 实际计费单元 (已扣除缓存命中)
    raw_units: int             # 未扣缓存的原始单元数 (用于 max_pages 硬闸)
    est_cost_rmb: float
    breakdown: dict            # {"vlm_image": n, "ocr_page": m}


def estimate_doc_cost(
    file_ext: str,
    unit_count: int,
    cached_count: int,
    cfg,
    *,
    ocr_page_count: int = 0,
    ocr_cached_count: int = 0,
) -> CostEstimate:
    """纯函数：预估单文档 VLM-rebuild 成本 (RMB)。无 DB / 无 API，可零基础设施单测。

    Args:
        file_ext:        小写扩展名，不含点 ("pdf"/"xlsx"/"pptx"/"png"...)。
        unit_count:      VLM 计费单元总数 (Funnel-1 幸存的嵌入式图片去重后数量)。
        cached_count:    其中已命中 vlm_cache 的数量 (cost 0)。
        cfg:             PipelineConfig (使用 cfg.rebuild)。
        ocr_page_count:  PDF OCR-fallback 页数 = min(page_count, cfg.ocr.max_ocr_pages)；否则 0。
        ocr_cached_count:OCR 页缓存命中数 (当前实现恒为 0 — OCR 无页级缓存)。
    """
    ext = (file_ext or "").lower().lstrip(".")
    rb = cfg.rebuild

    billable_vlm = max(0, unit_count - cached_count)
    billable_ocr = max(0, ocr_page_count - ocr_cached_count)

    cost = billable_vlm * rb.vlm_image_rmb + billable_ocr * rb.ocr_page_rmb
    # raw_units 不扣缓存：防止"本次全缓存命中但单元极多"绕过 max_pages 硬闸
    raw_units = unit_count + ocr_page_count
    return CostEstimate(
        file_ext=ext,
        billable_units=billable_vlm + billable_ocr,
        raw_units=raw_units,
        est_cost_rmb=round(cost, 4),
        breakdown={UNIT_VLM_IMAGE: billable_vlm, UNIT_OCR_PAGE: billable_ocr},
    )


class CostBreaker:
    """成本熔断器 (单 orchestrator 进程内的运行级单例)。

    线程安全：record() 在 ThreadPoolExecutor (RAG_VLM_CONCURRENCY) 下可能并发调用，
    用 Lock 保护累计计数器。

    ⚠️ 限制：累计预算是 *进程内* 计数器。多个 orchestrator 实例并发运行时各自独立计数，
    无法跨进程聚合 (无 DB/Redis 成本账本)。单实例 DataWorks 调度下足够；多实例需后续接入共享计数。
    """

    def __init__(self, cfg, *, enabled: Optional[bool] = None):
        self.cfg = cfg
        self.enabled = cfg.rebuild.enabled if enabled is None else enabled
        self._lock = threading.Lock()
        self._run_total_rmb = 0.0
        self._run_tripped = False
        self._run_alert_sent = False
        self._doc_denied = 0
        self._doc_allowed = 0
        # 单文档累计预留 (doc_id -> reserved rmb)：让同一文档的 rebuild + refine 两次
        # 调用共用同一份 per-doc 预算，避免一个文档花费突破 doc_budget 的两倍。
        self._doc_reserved: dict = {}

    def _gate_estimate(self, est: CostEstimate) -> Optional[str]:
        """纯估算闸 (闸 1/2，无共享状态)。返回拒绝理由，或 None 表示通过。"""
        rb = self.cfg.rebuild
        if est.raw_units > rb.max_pages:
            return (
                f"unit count {est.raw_units} exceeds per-doc hard cap {rb.max_pages} "
                f"(file_ext={est.file_ext}, breakdown={est.breakdown})"
            )
        if est.est_cost_rmb > rb.doc_budget_rmb + _BUDGET_EPS:
            return (
                f"VLM rebuild est {est.est_cost_rmb:.2f} RMB > per-doc budget "
                f"{rb.doc_budget_rmb:.2f} RMB (billable_units={est.billable_units}, "
                f"breakdown={est.breakdown})"
            )
        return None

    def try_reserve(self, doc_id: str, est: CostEstimate) -> Tuple[bool, Optional[str]]:
        """原子"检查并预留"：在单次加锁内完成全部闸判定并预留预算，杜绝 check→record
        之间的竞态 (并发线程同时通过预判却各自记账导致越界)。

        - 同一 doc_id 多次调用 (rebuild + refine) 共用 per-doc 预算 (累计计入 doc_budget)。
        - 单个文档放不下"剩余"运行预算时只拒绝该文档，**不**永久熔断 —— 后续更小的文档仍可通过。
        - 仅当累计实际预留达到 run 上限才置 _run_tripped。
        若放行后该文档实际未产生 VLM 调用 (渲染失败/无重建块/表格全被拒)，调用方应 refund()。

        Returns (allowed, reason)。
        """
        if not self.enabled:
            return True, None  # 标志关闭 → 永远放行

        # 闸 1/2：纯估算，无需锁
        gate_reason = self._gate_estimate(est)
        if gate_reason is not None:
            with self._lock:
                self._doc_denied += 1
            return False, gate_reason

        rb = self.cfg.rebuild
        with self._lock:
            if self._run_tripped:
                self._doc_denied += 1
                return False, (
                    f"RUN budget exhausted: cumulative {self._run_total_rmb:.2f} RMB "
                    f">= run cap {rb.run_budget_rmb:.2f} RMB; VLM rebuild disabled for remainder of run"
                )
            # 闸 2b：同一文档累计 (rebuild + refine) 不得突破 per-doc 预算
            doc_prev = self._doc_reserved.get(doc_id, 0.0)
            if doc_prev + est.est_cost_rmb > rb.doc_budget_rmb + _BUDGET_EPS:
                self._doc_denied += 1
                return False, (
                    f"per-doc budget exceeded for {doc_id}: reserved {doc_prev:.2f} "
                    f"+ {est.est_cost_rmb:.2f} > {rb.doc_budget_rmb:.2f} RMB"
                )
            # 闸 3：放不下剩余运行预算 → 只拒本文档，不永久熔断 (避免一个大文档误杀后续小文档)
            if self._run_total_rmb + est.est_cost_rmb > rb.run_budget_rmb + _BUDGET_EPS:
                self._doc_denied += 1
                return False, (
                    f"would exceed RUN budget: cumulative {self._run_total_rmb:.2f} "
                    f"+ {est.est_cost_rmb:.2f} > run cap {rb.run_budget_rmb:.2f} RMB"
                )
            # 预留 (与上述判定原子)
            self._run_total_rmb += est.est_cost_rmb
            self._doc_reserved[doc_id] = doc_prev + est.est_cost_rmb
            self._doc_allowed += 1
            if self._run_total_rmb >= rb.run_budget_rmb:
                self._run_tripped = True
            return True, None

    def refund(self, doc_id: str, est: CostEstimate) -> None:
        """退还一笔已预留但未实际花费的预算 (渲染失败/无重建块/表格全被拒)。
        让 run 预算不被空跑的工作消耗 (修复 charge-before-work)。线程安全。

        只退还该 doc_id 实际预留过的额度 (min(est, 已预留))：避免误退一个未预留的
        doc_id 把共享 run 计数器扣穿、连带抹掉其它文档的预留。
        """
        if not self.enabled:
            return
        with self._lock:
            reserved = self._doc_reserved.get(doc_id, 0.0)
            amount = min(est.est_cost_rmb, reserved)
            if amount <= 0:
                return
            self._run_total_rmb = max(0.0, self._run_total_rmb - amount)
            self._doc_reserved[doc_id] = reserved - amount
            # 退还后回落到上限之下 → 解除熔断 (反映真实可用预算)
            if self._run_tripped and self._run_total_rmb < self.cfg.rebuild.run_budget_rmb:
                self._run_tripped = False

    def check(self, doc_id: str, est: CostEstimate) -> Tuple[bool, Optional[str]]:
        """只读预判 (不修改任何计数器)：判断该文档此刻是否会被放行。

        ⚠️ 非原子：check() 与 record() 之间存在竞态，生产路径请用 try_reserve()。
        保留此方法仅供只读探测与向后兼容。
        """
        if not self.enabled:
            return True, None

        rb = self.cfg.rebuild
        with self._lock:
            tripped = self._run_tripped
            total = self._run_total_rmb
        if tripped:
            return False, (
                f"RUN budget exhausted: cumulative {total:.2f} RMB "
                f">= run cap {rb.run_budget_rmb:.2f} RMB; VLM rebuild disabled for remainder of run"
            )
        gate_reason = self._gate_estimate(est)
        if gate_reason is not None:
            return False, gate_reason
        if total + est.est_cost_rmb > rb.run_budget_rmb + _BUDGET_EPS:
            return False, (
                f"would exceed RUN budget: cumulative {total:.2f} "
                f"+ {est.est_cost_rmb:.2f} > run cap {rb.run_budget_rmb:.2f} RMB"
            )
        return True, None

    def record(self, doc_id: str, est: CostEstimate, allowed: bool) -> None:
        """记录一次决策，累加运行级花费 (仅 allowed=True 时累计)。线程安全。

        ⚠️ 非原子：与 check() 配对使用存在竞态 (见 check())。生产路径请用 try_reserve()。
        """
        if not self.enabled:
            return
        with self._lock:
            if allowed:
                self._doc_allowed += 1
                self._run_total_rmb += est.est_cost_rmb
                self._doc_reserved[doc_id] = self._doc_reserved.get(doc_id, 0.0) + est.est_cost_rmb
                if self._run_total_rmb >= self.cfg.rebuild.run_budget_rmb:
                    self._run_tripped = True
            else:
                self._doc_denied += 1

    def maybe_alert_run_tripped(self) -> bool:
        """运行级熔断首次触发时返回 True (供调用方发一次告警)；之后恒 False。"""
        with self._lock:
            if self._run_tripped and not self._run_alert_sent:
                self._run_alert_sent = True
                logger.warning(
                    "[CostBreaker] RUN budget tripped: cumulative=%.2f RMB cap=%.2f RMB "
                    "denied=%d allowed=%d — VLM rebuild disabled for remainder of run",
                    self._run_total_rmb, self.cfg.rebuild.run_budget_rmb,
                    self._doc_denied, self._doc_allowed,
                )
                print(
                    f"🚨 [CostBreaker] RUN budget tripped "
                    f"({self._run_total_rmb:.2f}/{self.cfg.rebuild.run_budget_rmb:.2f} RMB). "
                    f"VLM rebuild OFF for rest of run.",
                    flush=True,
                )
                return True
            return False

    @property
    def run_total_rmb(self) -> float:
        with self._lock:
            return self._run_total_rmb

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._run_tripped


def quarantine_for_cost(
    doc_id: str,
    version_no: int,
    owner_dept: str,
    reason: str,
    *,
    simulate_db: bool = True,
) -> bool:
    """成本超限就地封存。复用 spot_checker 的封存事务形态。

    熔断在 VLM-rebuild *之前* 触发 → 无已索引 chunk，不需要删索引。
    terminal: retry_count=3 防止 DAG-1 (dataworks_orchestrator.py:117) 重新认领。

    Returns: True=封存成功；False=失败/跳过 (非致命，调用方继续规则回退)。
    """
    logger.warning("[CostBreaker] DENY %s v%s: %s", doc_id, version_no, reason)
    print(f"    🚨 [CostBreaker] QUARANTINE {doc_id} v{version_no}: {reason}", flush=True)

    if simulate_db:
        print(f"    [SIMULATED] would quarantine {doc_id} v{version_no} (cost_ceiling_exceeded)",
              flush=True)
        return True

    from opensearch_pipeline.pipeline_nodes import _get_db_conn

    review_reason = reason if len(reason) <= 490 else reason[:490] + "..."
    conn = None
    try:
        conn = _get_db_conn(select_db=True)
        conn.begin()
        with conn.cursor() as cur:
            # a. 封存 flip (terminal)
            cur.execute("""
                UPDATE document_version
                SET risk_level             = 'high',
                    publish_status         = 'QUARANTINED',
                    gate_status            = 'quarantined',
                    content_process_status = 'FAILED',
                    retry_count            = 3,
                    content_process_error  = %s
                WHERE doc_id = %s AND version_no = %s
            """, (review_reason, doc_id, version_no))

            # b. 从公共 KB 撤出
            cur.execute(
                "UPDATE document_meta SET kb_type = 'private' WHERE doc_id = %s",
                (doc_id,),
            )

            # c. 人工审核任务 (新 review_type='cost_ceiling_exceeded'，幂等)
            task_id = f"cost_brk_{doc_id}_v{version_no}"
            cur.execute("""
                INSERT INTO review_task (
                    task_id, doc_id, version_no, review_key, review_type, review_reason,
                    review_status, owner_dept, suggested_category_l1, suggested_category_l2,
                    suggested_permission_level, confidence_score
                ) VALUES (
                    %s, %s, %s, %s, 'cost_ceiling_exceeded', %s, 'PENDING',
                    %s, 'reference', 'unknown', 'restricted', 0.5
                ) ON DUPLICATE KEY UPDATE
                    review_reason = VALUES(review_reason),
                    review_status = 'PENDING'
            """, (
                task_id, doc_id, version_no,
                f"processing/canonical/{doc_id}/v{version_no}/content.md",
                review_reason, owner_dept or "unknown",
            ))
        conn.commit()
        return True
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.error("[CostBreaker] quarantine DB write failed for %s v%s: %s",
                     doc_id, version_no, e)
        print(f"    ⚠️ [CostBreaker] quarantine insert skipped (non-fatal): {e}", flush=True)
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def gate_vlm_rebuild(breaker: CostBreaker, doc: dict, simulate_db: bool = True,
                     *, quarantine_on_deny: bool = True) -> Tuple[bool, CostEstimate]:
    """VLM-rebuild 升级前置闸 —— 未来的 rebuilder 在做任何 OCR/VLM 之前调用此函数。

    doc 需含: doc_id, version_no, file_ext, owner_dept, 及预先统计好的
              unit_count / cached_count / ocr_page_count(= min(page_count, cfg.ocr.max_ocr_pages))。

    quarantine_on_deny:
      True  (默认，用于 rebuild 升级) → 拒绝即封存文档 (该文档不可提取，回退规则输出近乎为空)。
      False (用于可选的表格精修) → 拒绝只是跳过这次"锦上添花"的精修、保留原生表格，
            **不**封存文档 (文档本身可用，绝不因可选精修被否决而丢弃)。

    Returns (allowed, est)。allowed=False 时调用方必须回退到确定性规则输出 (绝不丢弃文档)。
    """
    est = estimate_doc_cost(
        file_ext=doc.get("file_ext", ""),
        unit_count=int(doc.get("unit_count", 0)),
        cached_count=int(doc.get("cached_count", 0)),
        cfg=breaker.cfg,
        ocr_page_count=int(doc.get("ocr_page_count", 0)),
        ocr_cached_count=0,
    )
    # 原子"检查并预留"：避免 check→record 竞态与重复记账。放行后若实际未发生 VLM 调用，
    # 调用方 (vlm_rebuilder) 须 breaker.refund(doc_id, est) 退还本次预留。
    allowed, reason = breaker.try_reserve(doc["doc_id"], est)
    if not allowed:
        if quarantine_on_deny:
            quarantine_for_cost(
                doc["doc_id"], int(doc.get("version_no", 1)),
                doc.get("owner_dept", "unknown"), reason or "cost ceiling exceeded",
                simulate_db=simulate_db,
            )
        breaker.maybe_alert_run_tripped()
    return allowed, est
