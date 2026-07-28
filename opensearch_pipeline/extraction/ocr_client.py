# -*- coding: utf-8 -*-
"""
ocr_client.py — Qwen-VL OCR 统一客户端

单一 OCR 来源，消除 scan_pending_clean.py 和 faq_extract.py 的分叉。
支持 page-level 粒度：每页独立返回 OCR 文本。

生产依赖：dashscope API, fitz (PyMuPDF), oss2
模拟模式：返回模拟 OCR 结果
"""

import os
import re
import threading
from dataclasses import dataclass, field
from typing import List, Optional

from opensearch_pipeline.extraction.schema import ExtractedBlock

# DashScope / Gemini 两个分支共用的 OCR 指令
_OCR_PROMPT = "请识别图片中的所有文字，保持原文顺序输出。只输出识别文本，不要解释。"


def sanitize_ocr_text(text, *, width=None, height=None):
    """OCR 输出反幻觉清洗 → (clean_text, meta)。

    Qwen-VL OCR 对低文本/照片类输入会凭空编造内容（典型形态：同一表格行
    重复几十次、尾部短语死循环），编造文本曾直接进入索引 chunk。规则
    （全部为修剪而非整体丢弃，唯一例外是规则 4）：
      1. 连续重复行：同一行（去空白比较）连续 >2 次 → 只留前 2 次；
      2. 主导行：≥10 行里同一行占比 >40%（穿插重复）→ 只留前 2 次；
      3. 尾部短语循环：≥6 字符短语在结尾连续 ≥4 次 → 截到 2 次
         （只扫描末尾 2000 字符，避免回溯爆炸；≥6 字符下限保护
         检验表里"合格 合格 合格"这类合法短单元格重复）；
      4. 像素密度上界（仅传入尺寸的嵌入图触发；整页渲染不传尺寸）：
         len(text) > max(120, w*h/40) 在物理上不可能 → 返回 ""。
    永不抛异常：任何内部错误 → 原文原样返回（fail-open，与全管线
    "辅助环节失败不破坏主流程"约定一致）。
    """
    meta = {"sanitized": False, "reason": ""}
    try:
        if not text or not isinstance(text, str):
            return text, meta
        original_len = len(text)
        reasons = []

        # 规则 4：像素密度上界（先于修剪——编造表格在小图上直接整体拒绝）
        if width and height:
            allowance = max(120, int(width * height / 40))
            if len(text) > allowance:
                meta["sanitized"] = True
                meta["reason"] = f"density:{len(text)}>{allowance}@{width}x{height}"
                print(f"      [ocr-sanitize] {meta['reason']}: dropped fabricated text")
                return "", meta

        def _norm(s):
            return re.sub(r"\s+", "", s)

        lines = text.splitlines()
        dropped = 0
        if len(lines) >= 3:
            # 规则 1：连续重复行 collapse
            out = []
            run_norm, run_count = None, 0
            for ln in lines:
                n = _norm(ln)
                if n and n == run_norm:
                    run_count += 1
                    if run_count > 2:
                        dropped += 1
                        continue
                else:
                    run_norm, run_count = n, 1
                out.append(ln)

            # 规则 2：主导行（穿插重复，规则 1 抓不到）
            if len(out) >= 10:
                from collections import Counter
                norms = [_norm(ln) for ln in out if _norm(ln)]
                if norms:
                    top, cnt = Counter(norms).most_common(1)[0]
                    if cnt > 2 and cnt / len(norms) > 0.4:
                        kept, out2 = 0, []
                        for ln in out:
                            if _norm(ln) == top:
                                kept += 1
                                if kept > 2:
                                    dropped += 1
                                    continue
                            out2.append(ln)
                        out = out2
            if dropped:
                reasons.append(f"lines:{dropped}")
            text = "\n".join(out)

        # 规则 3：尾部短语循环（只看末尾 2000 字符，限定回溯成本）
        tail = text[-2000:]
        m = re.search(r"(.{6,80}?)(?:\1){3,}\s*$", tail, re.DOTALL)
        if m:
            cut = len(text) - len(tail) + m.start()
            text = text[:cut] + m.group(1) * 2
            reasons.append("tail-loop")

        if reasons:
            meta["sanitized"] = True
            meta["reason"] = "+".join(reasons)
            print(f"      [ocr-sanitize] {meta['reason']}: "
                  f"{original_len} → {len(text)} chars")
        return text, meta
    except Exception:
        return text, meta


@dataclass
class OCRPageResult:
    """单页 OCR 结果。"""
    page_num: int
    text: str
    status: str = "DONE"     # DONE / FAILED / SIMULATED
    error: Optional[str] = None


@dataclass
class OCRResult:
    """完整 OCR 结果（按页保留粒度）。"""
    pages: List[OCRPageResult] = field(default_factory=list)
    combined_text: str = ""
    status: str = "DONE"     # DONE / FAILED / SIMULATED / SKIPPED
    error: Optional[str] = None
    # B5（2026-07-25）：被 max_ocr_pages 上限砍掉、从未 OCR 的页数。此前只打一行 stdout ——
    # 被砍的页不产生 OCRPageResult，所以既有的 partial_loss_notes（只从 pages 的 FAILED 派生）
    # 看不见它们：长文档尾部内容不进索引，而 document_version 照常 DONE+INDEXED，
    # 运维没有任何一条 SQL 能圈出"需要重扫"的对象。
    skipped_pages: int = 0

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def to_blocks(self) -> List[ExtractedBlock]:
        """将 OCR 结果转为 ExtractedBlock 列表（带 page_num）。"""
        blocks = []
        for page in self.pages:
            if page.text.strip():
                blocks.append(ExtractedBlock(
                    block_type="ocr_text",
                    text=page.text.strip(),
                    page_num=page.page_num,
                    source="ocr",
                ))
        return blocks


def _ocr_page_cache_path() -> str:
    """A5：绝对路径（对齐 vlm_cache.default_cache_path / embedding_cache）。

    此前是相对路径 `scratch/ocr_page_cache.sqlite3` —— 依赖进程当前工作目录，
    DataWorks 节点在临时解压目录里跑，缓存会落到一个每次都不同的位置。
    """
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(project_root, "scratch", "ocr_page_cache.sqlite3")


def _make_ocr_page_cache():
    """构造页级缓存 store（接 OSS 镜像 → 新 pod 冷启动也能暖起来）。

    A5（2026-07-25）：此前 `OSS_MIRROR_KEY = None`（"本地缓存即可；OSS 镜像留给后续需要时开启"），
    而 DataWorks 每次都是全新 serverless pod、`scratch/` 不随代码包走（build_dataworks_zip 只打
    opensearch_pipeline/），于是跨运行命中率恒 0 —— 缓存只在单次 run 内有效。三个持久缓存里
    只有它没接镜像。

    镜像键**不按模型分文件**：缓存键 `_page_cache_key` 已含 ocr_model，无需第二层隔离
    （这与 embedding_cache 必须按模型+维度分对象不同——那边的键里没有模型）。
    """
    from opensearch_pipeline.embedding_cache import SqliteKVStore

    class _OcrPageCache(SqliteKVStore):
        TABLE = "ocr_page_cache"
        LABEL = "ocr page cache"
        OSS_MIRROR_KEY = "processing/cache/ocr_page_cache.sqlite3"
        MIRROR_ENV = "RAG_OCR_PAGE_CACHE_OSS_MIRROR"

    return _OcrPageCache(_ocr_page_cache_path())


class OCRClient:
    """
    OCR 客户端。

    生产模式：调用 Qwen-VL 做图片 OCR。
    模拟模式：返回模拟结果。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base_url: Optional[str] = None,
        ocr_model: Optional[str] = None,
        max_ocr_pages: Optional[int] = None,
        simulate: bool = True,
    ):
        """省略的参数从 `config.ocr` 取；**显式传值恒优先**。

        2026-07-25：此前 api_key 的回退只读 `DASHSCOPE_API_KEY` **环境变量**，而本仓的密钥
        在 `RAG_DASHSCOPE_API_KEY`、由 config.py 解析进 `config.ocr.api_key` —— 构造器不看它。
        于是 `OCRClient(simulate=False)` 会静默拿到**空 key**（全量 401）**外加**错误的默认
        模型 `qwen-vl-max`（而非配置的 `qwen-vl-ocr-latest`）。两个生产构造点
        （image_funnel_processor / unified_extractor）都显式传了 config 所以没踩到，
        但任何临时脚本/诊断都会踩，且失败表现为"密钥无效"，极易误判成密钥过期。

        边界（刻意）：
          · `api_key=""` 仍表示**显式无 key**，不回退 —— tests/test_real_extractors 等
            用例正是靠它验"无 key 时的优雅降级"路径；只有 `None`（未传）才回退。
          · config 不可用时退回原字面量默认值（fail-open，不让配置故障变成构造失败）。
        """
        _cfg_ocr = None
        try:
            from opensearch_pipeline.config import get_config
            _cfg_ocr = get_config().ocr
        except Exception:      # noqa: BLE001 — 配置不可用不该让构造失败
            _cfg_ocr = None

        def _pick(explicit, attr, literal_default):
            if explicit is not None:
                return explicit
            return getattr(_cfg_ocr, attr, literal_default) if _cfg_ocr else literal_default

        self.api_key = _pick(api_key, "api_key", "") or os.environ.get("DASHSCOPE_API_KEY", "")
        self.api_base_url = _pick(api_base_url, "api_base_url",
                                  "https://dashscope.aliyuncs.com/api/v1")
        self.ocr_model = _pick(ocr_model, "model", "qwen-vl-max")
        self.max_ocr_pages = _pick(max_ocr_pages, "max_ocr_pages", 20)
        self.simulate = simulate
        # 真实模式 + 无 key = 必然全量 401。响一声，别让调用方去猜"密钥是不是过期了"。
        if not simulate and not self.api_key:
            print("    ⚠️ [ocr] OCRClient 无 API key（simulate=False）——所有调用都会 401。"
                  "检查 RAG_DASHSCOPE_API_KEY / config.ocr.api_key。", flush=True)

    def ocr_pdf(
        self,
        local_path: str,
        doc_id: str,
        oss_bucket=None,
        page_nums: Optional[List[int]] = None,
    ) -> OCRResult:
        """
        对 PDF 做 OCR（按页）。

        page_nums: 仅 OCR 这些页（1-based）；None = 前 max_ocr_pages 页（旧行为）。
                   用于 per-page OCR gate——只 OCR 扫描页/坏字体页，不浪费在有文本的页上。

        生产模式：PDF → 图片 → 上传 OSS → Qwen-VL OCR
        模拟模式：返回按页模拟文本
        """
        if self.simulate:
            return self._simulate_pdf_ocr(doc_id, page_nums)

        return self._real_pdf_ocr(local_path, doc_id, oss_bucket, page_nums)

    def ocr_image(
        self,
        local_path: str,
        doc_id: str,
        oss_bucket=None,
    ) -> OCRResult:
        """
        对单张图片做 OCR。

        生产模式：上传 → Qwen-VL OCR
        模拟模式：返回模拟文本
        """
        if self.simulate:
            return OCRResult(
                pages=[OCRPageResult(page_num=1, text="[OCR: image content recognized]", status="SIMULATED")],
                combined_text="[OCR: image content recognized]",
                status="SIMULATED",
            )

        return self._real_image_ocr(local_path, doc_id, oss_bucket)

    # ── 模拟实现 ──

    def _simulate_pdf_ocr(self, doc_id: str, page_nums: Optional[List[int]] = None) -> OCRResult:
        """模拟 PDF OCR。page_nums 指定时按这些页模拟（反映 per-page 选择），否则默认 2 页。"""
        nums = list(page_nums) if page_nums else [1, 2]
        nums = nums[:self.max_ocr_pages]
        pages = [
            OCRPageResult(
                page_num=p,
                text=f"[OCR page {p}: scanned content for {doc_id}]",
                status="SIMULATED",
            )
            for p in nums
        ]
        combined = "\n\n".join(p.text for p in pages)
        return OCRResult(pages=pages, combined_text=combined, status="SIMULATED")

    # ── 生产实现（基于 Gemini Vision） ──

    def _real_pdf_ocr(self, local_path: str, doc_id: str, oss_bucket=None,
                      page_nums: Optional[List[int]] = None) -> OCRResult:
        """
        真实 PDF OCR (Qwen-VL Vision)。
        流程：
        1. PDF → 图片（fitz，串行——pymupdf 同一 doc 句柄的 page 对象非线程安全）
        2. Base64 编码
        3. Qwen-VL API 提取文本（有界并发，见 _ocr_rendered_pages）

        page_nums: 仅 OCR 这些页（1-based）；None = 前 max_ocr_pages 页。
        """
        if not self.api_key:
            return OCRResult(status="FAILED", error="API KEY not configured")

        try:
            import fitz
        except ImportError:
            return OCRResult(status="FAILED", error="PyMuPDF (fitz) not installed")

        import base64

        try:
            doc = fitz.open(local_path)
            n = len(doc)
            # 选页：指定则只 OCR 这些页（去重、有序、限页内），否则前 N 页（旧行为）
            if page_nums:
                page_idxs = sorted({p - 1 for p in page_nums if 1 <= p <= n})
            else:
                page_idxs = list(range(n))
            _skipped_by_cap = 0
            if len(page_idxs) > self.max_ocr_pages:
                dropped = len(page_idxs) - self.max_ocr_pages
                page_idxs = page_idxs[:self.max_ocr_pages]
                _skipped_by_cap = dropped     # B5：回传给调用方 → partial_loss_notes
                print(f"    ⚠️ [ocr] capped at max_ocr_pages={self.max_ocr_pages}; "
                      f"{dropped} low-text page(s) left un-OCR'd", flush=True)

            # Phase 1（串行）：渲染 + 压缩 + base64。pymupdf 同一 doc 句柄的 page
            # 对象非线程安全，渲染必须留在单线程；相对 API 调用（秒级）渲染是快路径，
            # 串行代价可忽略。渲染失败沿用旧语义：异常直接落到外层 except → 整体 FAILED。
            rendered = []  # [(page_idx, b64_data, mime_type)]，按页序
            for page_idx in page_idxs:
                page = doc[page_idx]
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)

                # 直接获取图像二进制数据
                img_data = pix.tobytes("png")
                # perf#64：2x 渲染的 A4 中文页 PNG 常 1-3MB，直接 base64 上传曾触发
                # write timeout（vlm_retry docstring 记载 381KB 即出过事）。复用
                # vlm_rebuilder 生产验证过的压缩（JPEG q78 + 限边 1568px，OCR 精度
                # 已在版面重建路径验证），base64 体积降 ~5x；helper 内部 fail-open，
                # 任何失败原样回退 PNG 字节，绝不阻断 OCR。
                mime_type = "image/png"
                try:
                    from opensearch_pipeline.extraction.vlm_rebuilder import compress_page_png
                    img_data, mime_type = compress_page_png(img_data)
                except Exception:
                    pass
                b64_data = base64.b64encode(img_data).decode('utf-8')
                rendered.append((page_idx, b64_data, mime_type))

            doc.close()

            # Phase 2：API 调用（页间独立）有界并发，结果按页序 keyed 重组。
            pages = self._ocr_rendered_pages(rendered)
            combined = "\n\n".join(p.text for p in pages if p.text)
            # 聚合状态：之前恒返回 DONE，即使每页都失败也掩盖为成功（扫描件内容全丢却报成功，
            # 下游 node_write_chunk_meta 把它当"真空"以 DONE 收尾）。仅当所有页都 FAILED 时翻成
            # FAILED（最小改动，不引入 PARTIAL 枚举，避免改动有文本的部分失败语义）。
            agg_status = "FAILED" if (pages and all(p.status == "FAILED" for p in pages)) else "DONE"
            # 批次6（ultra ocr_client:298）：部分页失败此前零信号——失败页 text="" 被
            # to_blocks 丢弃、状态又是 DONE，缺页永久静默。这里 loud log；消费方
            # （unified_extractor）按 pages 派生失败页清单落 partial_loss_notes →
            # 文档收尾 NEEDS_REVIEW（可服务、重灌自愈），不再假装完整。
            _failed_pgs = [p.page_num for p in pages if p.status == "FAILED"]
            if _failed_pgs and agg_status == "DONE":
                print(f"    ⚠️ [ocr] partial page failure: {len(_failed_pgs)}/{len(pages)} "
                      f"page(s) FAILED (pages {_failed_pgs[:10]}) — kept successful pages; "
                      f"doc will close NEEDS_REVIEW", flush=True)
            return OCRResult(pages=pages, combined_text=combined, status=agg_status,
                             skipped_pages=_skipped_by_cap)

        except Exception as e:
            return OCRResult(status="FAILED", error=repr(e))

    # ── G8：OCR 页级缓存（SqliteKVStore 底座，键=模型+渲染字节 sha256）──────────
    # 此前 OCR 无任何缓存（cost_breaker 注释自认 ocr_cached_count 恒 0）：同一文档
    # 重扫/重灌时每个扫描页都重新付费。键含模型名（换 OCR 模型即天然失效）与压缩后
    # 渲染字节哈希（渲染参数变化 → 字节变化 → 自动 miss，无需手工 bump）。
    # RAG_OCR_PAGE_CACHE=false 关闭；缓存层任何异常都 fail-open 直接调 API。

    _page_cache = None
    # A5（2026-07-25）：此前是 `_page_cache_lock = None` 且全文件从未使用 —— 文档级抽取并发
    # （RAG_EXTRACT_CONCURRENCY>1）下多线程会各造一个 store，最终 flush 的那个实例不持有其他
    # 实例的 _dirty_puts，镜像根本推不出去。改成真锁 + 双检。
    _page_cache_lock = threading.Lock()

    @classmethod
    def _get_page_cache(cls):
        if os.environ.get("RAG_OCR_PAGE_CACHE", "true").lower() in ("0", "false", "no"):
            return None
        try:
            if cls._page_cache is None:
                with cls._page_cache_lock:
                    if cls._page_cache is None:      # 双检：锁外快路径 + 锁内唯一构造
                        cls._page_cache = _make_ocr_page_cache()
            return cls._page_cache
        except Exception:
            return None

    @classmethod
    def flush_page_cache_to_oss(cls, max_entries: Optional[int] = None) -> None:
        """A5：run 收尾把页级缓存裁剪并推 OSS 镜像。fail-open（缓存是 advisory）。

        没有这个调用点时 finalize 永不发生 → _push_oss_mirror 永不触发（底座只从 finalize
        推），镜像形同虚设。调用点在 node_extract 的 finally，紧挨 flush_vlm_cache_to_oss。
        """
        store = cls._page_cache
        if store is None:
            return
        if max_entries is None:
            try:
                max_entries = int(os.environ.get("RAG_OCR_PAGE_CACHE_MAX_ENTRIES", "50000"))
            except ValueError:
                max_entries = 50000
        try:
            store.finalize(max_entries)
        except Exception as e:
            print(f"    ⚠️ OCR page cache flush failed (non-fatal): {e}", flush=True)

    def _page_cache_key(self, b64_data: str) -> str:
        import hashlib as _h
        return f"{self.ocr_model}:{_h.sha256(b64_data.encode('ascii')).hexdigest()}"

    def _ocr_rendered_pages(self, rendered) -> List[OCRPageResult]:
        """对已渲染的页面批量做 OCR API 调用 → 按输入页序返回 OCRPageResult 列表。

        rendered: [(page_idx, b64_data, mime_type)]（page_idx 0-based，已按页序排好）。

        并发模型（对齐 unified_extractor 的 VLM 漏斗 RAG_VLM_CONCURRENCY 模式）：
          - 每页一个任务进有界 ThreadPoolExecutor；单页失败 → 该页 FAILED（text=""、
            error=repr(e)），不影响邻页——语义与旧串行 try/except 完全一致，
            全页 FAILED → 聚合 FAILED 的收口逻辑仍在调用方。
          - 结果按 page_idx keyed 收集，最后按输入顺序重组：page_num = page_idx+1
            （1-based），与旧串行输出逐字节一致，不受完成顺序影响。
          - 429/5xx 由 _call_ocr_api 内的 post_json_with_retry 兜底（honors
            Retry-After）；退避 sleep 发生在各 worker 线程内，天然限制了重试
            重发速率 ≤ 并发度，不会放大成 stampede。
        """
        # RAG_OCR_PAGE_CONCURRENCY：扫描页 OCR 并发度，默认 4（保守）。qwen-vl OCR
        # 与嵌入图漏斗（RAG_VLM_CONCURRENCY，默认 8）共享同一 DashScope 账号的
        # QPS 配额，且该账号有已记录的 429 脆弱性；stage-1 里两条路径可能同时在
        # 跑，默认必须给漏斗留余量——调大前先确认漏斗侧负载。=1 时走纯串行路径
        # （零线程池，行为与历史串行实现完全一致）。
        try:
            max_workers = int(os.environ.get("RAG_OCR_PAGE_CONCURRENCY", "4"))
        except ValueError:
            max_workers = 4
        max_workers = max(1, max_workers)

        # G8：页级缓存前置批量点查——命中页零 API 调用（sanitize 后的文本已在缓存里）
        cache = self._get_page_cache()
        cached_texts: dict = {}
        if cache is not None and rendered:
            try:
                key_by_idx = {item[0]: self._page_cache_key(item[1]) for item in rendered}
                hits = cache.get_many(key_by_idx.values())
                cached_texts = {idx: hits[k] for idx, k in key_by_idx.items() if k in hits}
                if cached_texts:
                    print(f"    [ocr-cache] {len(cached_texts)}/{len(rendered)} page(s) served "
                          f"from cache (0 API calls)", flush=True)
            except Exception:
                cached_texts = {}

        def _ocr_one(page_idx, b64_data, mime_type):
            if page_idx in cached_texts:
                return OCRPageResult(
                    page_num=page_idx + 1,
                    text=str(cached_texts[page_idx]),
                    status="DONE",
                )
            try:
                page_text = self._call_ocr_api(b64_data, mime_type)
                # 反幻觉修剪（不传尺寸：整页渲染只修剪重复，绝不整体丢弃）
                page_text, _ = sanitize_ocr_text(page_text)
                # 批次6：空结果不入缓存——空文本可能是瞬态异常残留（或 sanitize 全剪），
                # 缓存它会把一次性问题固化成该页跨重灌的永久空文本；真空白页重付一次
                # OCR 的代价可接受（罕见且单页）。
                if cache is not None and page_text.strip():
                    try:  # 存 sanitize 后文本；缓存写失败绝不影响 OCR 结果
                        cache.put_many({self._page_cache_key(b64_data): page_text})
                    except Exception:
                        pass
                return OCRPageResult(
                    page_num=page_idx + 1,
                    text=page_text,
                    status="DONE",
                )
            except Exception as e:
                return OCRPageResult(
                    page_num=page_idx + 1,
                    text="",
                    status="FAILED",
                    error=repr(e),
                )

        if max_workers <= 1 or len(rendered) <= 1:
            return [_ocr_one(*item) for item in rendered]

        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = {}  # page_idx -> OCRPageResult
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_idx = {pool.submit(_ocr_one, *item): item[0] for item in rendered}
            for future in as_completed(future_to_idx):
                results[future_to_idx[future]] = future.result()  # _ocr_one 不抛异常
        return [results[item[0]] for item in rendered]

    def _real_image_ocr(self, local_path: str, doc_id: str, oss_bucket=None) -> OCRResult:
        """真实图片 OCR (Gemini Vision)。"""
        if not self.api_key:
            return OCRResult(status="FAILED", error="API KEY not configured")

        import base64
        try:
            with open(local_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode('utf-8')

            ext = os.path.splitext(local_path)[1].lower()
            mime_type = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"

            text = self._call_ocr_api(b64_data, mime_type)

            # 反幻觉清洗：嵌入图自测像素尺寸 → 密度上界可触发（整页渲染不传）
            w = h = None
            try:
                from PIL import Image
                with Image.open(local_path) as im:
                    w, h = im.size
            except Exception:
                pass
            text, _ = sanitize_ocr_text(text, width=w, height=h)

            return OCRResult(
                pages=[OCRPageResult(page_num=1, text=text, status="DONE")],
                combined_text=text,
                status="DONE",
            )
        except Exception as e:
            return OCRResult(status="FAILED", error=repr(e))

    def _call_ocr_api(self, base64_image: str, mime_type: str) -> str:
        """根据配置调用相应的 OCR API (Gemini or DashScope)。"""
        import requests

        from opensearch_pipeline.vlm_endpoint import (
            auth_headers, build_image_chat_payload, extract_vlm_text,
            resolve_vlm_url, use_compat_mode,
        )

        is_dashscope = "dashscope.aliyuncs.com" in self.api_base_url

        if is_dashscope:
            # qwen3 系列只在 compatible-mode 端点提供；qwen-vl-ocr 等旧系列走原生端点。
            # （此前这里只有原生分支：配置 qwen3-vl-* 做 OCR 会打到错误端点直接报错。）
            use_compat = use_compat_mode(self.ocr_model, self.api_base_url)
            url = resolve_vlm_url(self.api_base_url, use_compat)
            payload = build_image_chat_payload(
                self.ocr_model, _OCR_PROMPT, base64_image, mime_type, use_compat,
            )
            from opensearch_pipeline.vlm_retry import post_json_with_retry
            # 重试瞬时 429/5xx：单次抖动会让该图/页 OCR 文本变空（被静默吞成 ""），降低 chunk 质量。
            resp = post_json_with_retry(url, json=payload, headers=auth_headers(self.api_key),
                                        timeout=60, label="OCR(DashScope)", post_fn=requests.post)
            if resp.status_code != 200:
                raise RuntimeError(f"DashScope OCR HTTP {resp.status_code}: {resp.text[:500]}")

            try:
                return extract_vlm_text(resp.json(), use_compat).strip()
            except (KeyError, IndexError) as e:
                # 批次6（ultra ocr_client:477）：200 但响应体缺预期内容路径（错误/拒答载荷
                # 形态）≠ 空页成功。此前返 "" 被当 DONE 并永久写入页缓存——一次响应形态
                # 异常变成该页永久静默丢失（后续重灌全部命中空缓存、零 API 调用、无重试
                # 路径）。raise → 该页 FAILED 且不入缓存，下次运行重试。
                raise RuntimeError(f"DashScope OCR 200 with unparseable body: {e!r}")
        else:
            # Default: Gemini API format
            # P0-2 Fix: API key 通过 header 传递，避免暴露在 URL 中被代理/日志记录
            if "/models/" in self.api_base_url:
                url = f"{self.api_base_url}:generateContent"
            else:
                url = f"{self.api_base_url}/models/{self.ocr_model}:generateContent"
            
            payload = {
                "contents": [{
                    "parts": [
                        {"text": _OCR_PROMPT},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64_image
                            }
                        }
                    ]
                }],
                "generationConfig": {
                    "temperature": 0.0
                }
            }
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini OCR HTTP {resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError):
                return ""
