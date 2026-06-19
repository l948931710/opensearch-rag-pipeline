# -*- coding: utf-8 -*-
"""
unified_extractor.py — 统一文档提取入口

单一入口 UnifiedExtractor.extract()，根据 file_ext 分发到对应的提取器。
支持 mock 模式（接收 mock_text）和生产模式（读取真实文件）。

DAG 层不需要知道文件类型，只调用 extract() 即可。
"""

import copy
import os
import tempfile
from typing import Dict, List, Optional

from opensearch_pipeline.extraction.schema import ExtractionResult, ExtractedBlock
from opensearch_pipeline.extraction.text_extractor import (
    extract_text_file,
    blocks_to_text,
    extract_title_from_blocks,
)
from opensearch_pipeline.extraction.ocr_client import OCRClient, sanitize_ocr_text


_DEFAULT_IMG_DIR: Optional[str] = None


def _safe_image_output_dir(task: dict) -> str:
    """解析图片导出目录。绝不返回 ""——空串会被 os.path.join("", filename) 落到 cwd,
    污染工作区(历史上往仓库根目录散落 *_img*.png / *_slide*.png)。调用方未传 `_tmp_dir`
    时,回退到一个稳定的系统临时目录(进程级复用,不落仓库)。"""
    d = (task or {}).get("_tmp_dir")
    if d:
        return d
    global _DEFAULT_IMG_DIR
    if _DEFAULT_IMG_DIR is None:
        _DEFAULT_IMG_DIR = tempfile.mkdtemp(prefix="rag_extract_images_")
    return _DEFAULT_IMG_DIR


def _html_to_text(raw_html: str) -> str:
    """HTML → 纯文本：剥标签、跳过 script/style、块级标签转换行；失败回退原文。"""
    import re
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        _BLOCK_TAGS = {
            "p", "div", "br", "li", "tr", "table", "section", "article",
            "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
        }

        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
            self._skip_depth = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self._skip_depth += 1
            elif tag in self._BLOCK_TAGS:
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self._skip_depth = max(0, self._skip_depth - 1)
            elif tag in self._BLOCK_TAGS:
                self.parts.append("\n")

        def handle_data(self, data):
            if not self._skip_depth:
                self.parts.append(data)

    try:
        parser = _TextExtractor()
        parser.feed(raw_html)
        parser.close()
        text = "\n".join(ln.strip() for ln in "".join(parser.parts).splitlines())
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text or raw_html
    except Exception:
        return raw_html


def _csv_to_text(raw_csv: str) -> str:
    """CSV → 行式文本：csv 模块处理引号/转义，单元格以 " | " 连接；失败回退原文。"""
    import csv
    import io

    try:
        try:
            dialect = csv.Sniffer().sniff(raw_csv[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = csv.reader(io.StringIO(raw_csv), dialect)
        lines = [
            " | ".join(cell.strip() for cell in row)
            for row in rows if any((cell or "").strip() for cell in row)
        ]
        return "\n".join(lines) or raw_csv
    except Exception:
        return raw_csv


_VLM_CACHE_VALID_STATUSES = {"DISCARD_DECORATIVE", "ROUTE_TO_TEXT", "ROUTE_TO_VECTOR"}


def _vlm_cache_lookup(vlm_cache, file_hash, is_public):
    """带命名空间的 VLM 缓存查询 + 遗留裸 MD5 key 的只读回退。

    遗留条目（命名空间化之前写入，OSS 上 ~1500 条）用现行带后缀 key 永远
    查不到 → 回灌时全量重打 VLM 白花钱。回退仅限 public 命名空间（裸 key
    全部产生于 public-bypass 时代；quarantine 的 :sec 绝不回退，避免跳过
    敏感审计），且要求 status 合法、非 QUARANTINE_SENSITIVE、无 Simulated
    污染。命中即迁移到带后缀 key（内存中；真实运行随 _save_vlm_cache
    持久化，simulate 不落盘），裸 key 自然老化。只读新写仍只用带后缀 key。
    """
    cache_key = f"{file_hash}:{'pub' if is_public else 'sec'}"
    entry = vlm_cache.get(cache_key)
    if entry is not None:
        return entry
    if not is_public:
        return None
    legacy = vlm_cache.get(file_hash)
    if not isinstance(legacy, dict):
        return None
    if legacy.get("status") not in _VLM_CACHE_VALID_STATUSES:
        return None
    blob = (f"{legacy.get('visual_summary', '')}|{legacy.get('ocr_text', '')}"
            f"|{legacy.get('reason', '')}")
    if "simulat" in blob.lower():
        return None
    vlm_cache[cache_key] = legacy
    return legacy


def _stitch_strip_runs(blocks, image_assets, min_run=4, max_slice_height=80):
    """缝合 Word 条带切片：一张照片被存成 N 条同宽窄条时，逐条都会被
    Funnel-1 丢弃（h<50 或 aspect>8），整图彻底丢失。

    判据（每条都 load-bearing，全部满足才缝合，否则原样返回）：
      - blocks 中 ≥min_run 个连续 image_ref（之间无任何其他块——图片独占段落
        不产生文本块，所以"连续"是唯一可观测信号）；
      - 资产像素宽完全一致（纵向堆叠的前提）；
      - 逐条 height<max_slice_height 且条状（w≥3h 或 w≥100）——方形图标行
        （工具栏/①②③枚举）不会被误缝；
      - 逐条都是今日 Funnel-1 的丢弃对象（w<50/h<50/<3KB/aspect>8）——
        只"救回"，绝不改变现状能存活的图；
      - 缝合总高在 [100, 6000] 内。
    网格切片（如 23×31 小方块）因不满足条状判据而不缝——纵向堆叠会产生
    无意义的细长柱，留待网格重建（future work）。
    任何 PIL/IO 失败 → 该 run 原样保留（fail-open）。
    """
    if not blocks or not image_assets:
        return blocks, image_assets
    try:
        from PIL import Image
    except ImportError:
        return blocks, image_assets

    def _btype(b):
        return b.block_type if hasattr(b, "block_type") else b.get("block_type", "")

    def _bextra(b):
        e = b.extra if hasattr(b, "extra") else b.get("extra")
        return e if isinstance(e, dict) else {}

    asset_by_index = {}
    for a in image_assets:
        asset_by_index.setdefault(getattr(a, "image_index", None), a)

    runs = []  # [run_blocks, ...]
    cur = []
    for b in blocks:
        if _btype(b) == "image_ref":
            cur.append(b)
        else:
            if len(cur) >= min_run:
                runs.append(list(cur))
            cur = []
    if len(cur) >= min_run:
        runs.append(list(cur))
    if not runs:
        return blocks, image_assets

    def _discard_bound(w, h, kb):
        aspect = max(w / max(h, 1), h / max(w, 1))
        return w < 50 or h < 50 or kb < 3.0 or aspect > 8.0

    drop_block_ids = set()
    drop_asset_idx = set()
    new_assets = []

    for run_blocks in runs:
        infos = []  # (block, asset, w, h, kb, image_index)
        ok = True
        for b in run_blocks:
            idx = _bextra(b).get("image_index")
            a = asset_by_index.get(idx)
            lp = getattr(a, "local_path", "") if a else ""
            if not a or not lp or not os.path.exists(lp):
                ok = False
                break
            try:
                with Image.open(lp) as im:
                    w, h = im.size
                kb = os.path.getsize(lp) / 1024.0
            except Exception:
                ok = False
                break
            infos.append((b, a, w, h, kb, idx))
        if not ok or not infos:
            continue

        if len({w for _, _, w, _, _, _ in infos}) != 1:
            continue
        if not all(
            h < max_slice_height and (w >= 3 * h or w >= 100) and _discard_bound(w, h, kb)
            for _, _, w, h, kb, _ in infos
        ):
            continue
        w0 = infos[0][2]
        total_h = sum(h for _, _, _, h, _, _ in infos)
        if not (100 <= total_h <= 6000):
            continue

        try:
            canvas = Image.new("RGB", (w0, total_h), (255, 255, 255))
            y = 0
            for _, a, _, h, _, _ in infos:
                with Image.open(a.local_path) as im:
                    canvas.paste(im.convert("RGB"), (0, y))
                y += h
            first_a = infos[0][1]
            out_path = f"{os.path.splitext(first_a.local_path)[0]}_stitched{len(infos)}.png"
            canvas.save(out_path)
        except Exception as e:
            print(f"      ⚠️ [strip-stitch] PIL stitch failed (run kept as-is): {e}")
            continue

        first_b = infos[0][0]
        first_idx = infos[0][5]
        for _, _, _, _, _, idx in infos:
            drop_asset_idx.add(idx)
        comp = copy.copy(first_a)
        comp.local_path = out_path
        comp.image_index = first_idx
        comp.original_name = f"stitched:{len(infos)}slices"
        new_assets.append(comp)
        extra0 = first_b.extra if hasattr(first_b, "extra") else None
        if isinstance(extra0, dict):
            extra0["stitched_from"] = len(infos)
        for b in run_blocks[1:]:
            drop_block_ids.add(id(b))
        print(f"      [strip-stitch] {len(infos)} slices ({w0}x{total_h}) → "
              f"{os.path.basename(out_path)}")

    if not new_assets:
        return blocks, image_assets

    blocks = [b for b in blocks if id(b) not in drop_block_ids]
    image_assets = [
        a for a in image_assets
        if getattr(a, "image_index", None) not in drop_asset_idx
    ] + new_assets
    return blocks, image_assets


class UnifiedExtractor:
    """
    统一文档提取器。

    用法：
        extractor = UnifiedExtractor(simulate=True)
        result = extractor.extract(task)

    task dict 需包含：
        doc_id, version_no, file_ext, raw_key
        mock_text (可选，模拟模式)
        local_path (可选，生产模式)
    """

    # 原生提取文本低于此阈值 → OCR fallback（图片类型用整体阈值）
    OCR_THRESHOLD_CHARS = 100
    # Increment 0b: PDF 逐页 OCR 门槛——原生文本少于此字符数的页视为扫描页/坏字体页，
    # 单独送 OCR；取代旧的"按整文档 text_length 一刀切"（封面有字则整文档跳过 OCR）。
    PER_PAGE_OCR_THRESHOLD = 50

    def __init__(
        self,
        oss_client=None,
        ocr_client: Optional[OCRClient] = None,
        simulate: bool = None,
    ):
        from opensearch_pipeline.config import get_config
        cfg = get_config()
        if simulate is None:
            simulate = cfg.simulate
        self.oss_client = oss_client
        if not ocr_client:
            self.ocr_client = OCRClient(
                api_key=cfg.ocr.api_key,
                api_base_url=cfg.ocr.api_base_url,
                ocr_model=cfg.ocr.model,
                max_ocr_pages=cfg.ocr.max_ocr_pages,
                simulate=simulate,
            )
        else:
            self.ocr_client = ocr_client
        self.simulate = simulate
        self.config = cfg
        # 可选：运行级成本熔断器（由 orchestrator 注入，跨文档累计运行预算）。
        # 为空时 vlm_rebuilder 会按 cfg 现造一个做单文档闸。
        self.cost_breaker = None

    def extract(self, task: dict) -> ExtractionResult:
        """
        统一提取入口。

        根据 file_ext 分发，自动处理 OCR fallback。
        """
        file_ext = task.get("file_ext", "txt").lower().strip().lstrip(".")
        doc_id = task["doc_id"]
        version_no = task["version_no"]
        source_key = task.get("raw_key", "")

        # ── Mock 模式：直接解析注入的文本 ──
        if "mock_text" in task:
            return self._extract_mock(task, file_ext)

        # ── 生产模式：按文件类型分发 ──
        if file_ext == "pdf":
            return self._extract_pdf(task)
        elif file_ext == "docx":
            return self._extract_docx(task)
        elif file_ext == "xlsx":
            return self._extract_xlsx(task)
        elif file_ext == "xls":
            # 旧版二进制 Excel：按用户决策不在管线内支持 —— .xls/.doc 走一次性
            # 转换（xlsx/docx）后回灌；ingest_policy 已把 xls 排除在扫描之外。
            # 显式 unsupported 保证错误可见（勿误路由进 _extract_xlsx 静默吞掉）。
            return self._unsupported(task, file_ext)
        elif file_ext == "pptx":
            return self._extract_pptx(task)
        elif file_ext in ("txt", "md", "csv", "html", "htm"):
            return self._extract_text(task)
        elif file_ext in ("png", "jpg", "jpeg", "webp", "tif", "tiff", "gif", "bmp"):
            return self._extract_image(task)
        else:
            return self._unsupported(task, file_ext)

    # ── Mock 模式 ──

    def _extract_mock(self, task: dict, file_ext: str) -> ExtractionResult:
        """解析 mock_text 为结构化 blocks。"""
        text = task["mock_text"]
        blocks = extract_text_file(text, source="mock")
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))
        flat_text = blocks_to_text(blocks)

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=file_ext,
            extract_method="mock_injection",
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            page_count=None,
            ocr_required=False,
            ocr_status="NOT_REQUIRED",
        )

    # ── PDF ──

    def _extract_pdf(self, task: dict) -> ExtractionResult:
        """PDF 提取（文本 + 嵌入图片 + OCR fallback）。"""
        from opensearch_pipeline.extraction.pdf_extractor import extract_pdf
        from opensearch_pipeline.extraction.image_extraction_utils import extract_images_from_pdf

        local_path = task.get("local_path", "")
        blocks, page_count, warnings = extract_pdf(local_path)
        flat_text = blocks_to_text(blocks)
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))

        # 提取嵌入图片 → 三阶段过滤漏斗
        assets, img_blocks = self._process_embedded_images(
            extract_images_from_pdf(local_path, _safe_image_output_dir(task), max_pages=20),
            task,
        )
        if img_blocks:
            blocks.extend(img_blocks)
            flat_text = blocks_to_text(blocks)

        # 判断实际使用的提取方法
        has_layout_blocks = any(
            b.extra.get("detected_by") in ("font_size", "bold_font", "pdfplumber_lines", "layout")
            for b in blocks if hasattr(b, "extra") and b.extra
        )
        extract_method = "pdfplumber_layout" if has_layout_blocks else "pypdf"

        result = ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext="pdf",
            extract_method=extract_method,
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            page_count=page_count,
            warnings=warnings,
            assets=assets,
        )
        # Stash 本地路径，供 _pages_needing_ocr 在 page_count<=0 时
        # 走保守 fallback 重拿真实页数（不暴露成 dataclass field 以避免序列化噪声）
        result._local_path = local_path  # type: ignore[attr-defined]


        # OCR fallback 判断
        if self._needs_ocr(result):
            result = self._apply_ocr_fallback(task, result)

        # ── Increment 1: VLM 版面重建（逐页升级不可提取页）──
        # 默认关闭（cfg.rebuild.enabled=False）→ no-op；开启后受成本熔断器约束。
        try:
            from opensearch_pipeline.extraction.vlm_rebuilder import maybe_rebuild_pdf
            result = maybe_rebuild_pdf(task, result, self.config,
                                       breaker=getattr(self, "cost_breaker", None))
        except Exception as _e:
            print(f"    ⚠️ [vlm_rebuilder] skipped (non-fatal): {_e}", flush=True)

        # ── Increment 2: VLM 表格精修（结构错乱的 PDF 表格；数字保真闸把关）──
        # 默认关闭（cfg.rebuild.refine_tables=False）→ 完全 no-op（零回归）。
        try:
            from opensearch_pipeline.extraction.vlm_rebuilder import maybe_refine_tables
            result = maybe_refine_tables(task, result, self.config,
                                         breaker=getattr(self, "cost_breaker", None))
        except Exception as _e:
            print(f"    ⚠️ [table_refine] skipped (non-fatal): {_e}", flush=True)

        return result

    # ── DOCX ──

    def _extract_docx(self, task: dict) -> ExtractionResult:
        """DOCX 提取（文本 + 嵌入图片，段落级图片位置追踪）。"""
        from opensearch_pipeline.extraction.docx_extractor import extract_docx_with_images
        from opensearch_pipeline.extraction.image_extraction_utils import extract_images_from_docx

        local_path = task.get("local_path", "")

        # ── 方案 B：用 extract_docx_with_images 获得段落级 image_ref 位置 ──
        # 这让 _inject_image_ref_blocks 走精确匹配路径而非启发式均匀分配
        blocks, inline_image_assets = extract_docx_with_images(local_path)
        warnings = []
        flat_text = blocks_to_text(blocks)
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))

        # ── 提取嵌入图片到磁盘 → 三阶段过滤漏斗 ──
        exported_images = extract_images_from_docx(
            local_path, _safe_image_output_dir(task)
        )

        # ── 对齐 image_index：inline_image_assets 按文档顺序，
        #    exported_images 按 rels 遍历顺序，需要通过 target_ref 匹配 ──
        if inline_image_assets and exported_images:
            # 构建 target_ref → exported asset 映射
            export_by_ref = {}
            for ea in exported_images:
                ref = getattr(ea, "original_name", "") or ""
                if ref:
                    export_by_ref[ref] = ea

            # 用 inline 顺序重建 exported 列表，确保 image_index 一致。
            # 同一 media 被正文引用多次时（同 rId 多处出现），第二次起必须
            # 复制资产对象——直接复用会让 image_index 互相覆盖（last-write-wins），
            # 先出现的步骤永远绑不到图。copy.copy 保留 XLSX 等动态附加属性。
            aligned_exports = []
            consumed_ids = set()
            for ia in inline_image_assets:
                ref = getattr(ia, "original_name", "") or ""
                matched = export_by_ref.get(ref)
                if matched:
                    if id(matched) in consumed_ids:
                        matched = copy.copy(matched)
                    else:
                        consumed_ids.add(id(matched))
                    # 用 inline 的 image_index 覆盖 export 的 image_index
                    matched.image_index = ia.image_index
                    aligned_exports.append(matched)

            # 如果对齐成功（大部分都能匹配），用对齐后的列表
            if len(aligned_exports) >= len(exported_images) * 0.5:
                exported_images = aligned_exports

        # ── 条带切片缝合（须在对齐之后：依赖 ref↔asset 的 image_index 对应）──
        try:
            blocks, exported_images = _stitch_strip_runs(blocks, exported_images)
        except Exception as _e:
            print(f"      ⚠️ [strip-stitch] skipped (non-fatal): {_e}")

        assets, img_blocks = self._process_embedded_images(
            exported_images, task,
        )
        # 不再 extend img_blocks — image_ref 已内联在 blocks 中
        # img_blocks 是 _process_embedded_images 生成的冗余 image_ref，
        # 只有当 blocks 中完全没有 image_ref 时才 fallback 追加
        has_inline_refs = any(
            (b.block_type if hasattr(b, "block_type") else b.get("block_type", ""))
            == "image_ref"
            for b in blocks
        )
        if not has_inline_refs and img_blocks:
            blocks.extend(img_blocks)
            flat_text = blocks_to_text(blocks)

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext="docx",
            extract_method="python_docx",
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            warnings=warnings,
            assets=assets,
        )

    # ── XLSX / XLS ──

    # 设备清扫基准书：通用部位关键词 fallback 白名单
    _PART_KEYWORDS_FALLBACK = {
        "进片口", "外侧", "电刷", "轴轮", "链条", "齿轮", "丝杆", "油位",
        "温度", "剥离条", "电机", "烘箱", "油壸", "管路", "底盘", "配电箱",
        "变频器", "液压站", "三辊", "涂油辊", "硅油", "粉碎机", "油缸",
        "主机", "仪表", "标识", "外观", "送片组件", "拉伸总成", "出杯口",
        "机顶盖", "活动件", "油路",
    }

    # 表头关键词：命中 ≥2 个则认为是表头行
    _HEADER_KEYWORDS = {"清扫部位名称", "点检部位", "部位名称", "清扫基准",
                        "清扫方法", "清扫工具", "清扫周期", "点检项目",
                        "点检方法", "判定标准", "序号", "类别",
                        "运转中", "停机时", "安全注意事项", "责任人",
                        "所需时间", "频次", "异常处理", "点检人"}

    # 部位名称列：优先匹配这些列名
    _PART_COL_NAMES = {"清扫部位名称", "点检部位", "部位名称"}

    def _extract_xlsx(self, task: dict) -> ExtractionResult:
        """Excel 提取（文本 + 嵌入图片）：逐 sheet 逐行读取单元格文本。

        增强功能（设备清扫基准书类文档）：
        - 子 section 检测："清扫时要点检的项目" 插入 heading 分隔清扫区和点检区
        - 表头行识别 + 部位名称列自动提取 part_candidates
        - 表头行和设备信息行标记 row_role="metadata"，不进入 row card chunk
        - 图片 part_labels 提取（优先匹配 part_candidates，fallback 白名单）
        """
        import re
        from opensearch_pipeline.extraction.image_extraction_utils import extract_images_from_xlsx

        local_path = task.get("local_path", "")
        file_ext = task.get("file_ext", "xlsx").lower()
        blocks = []
        warnings = []
        all_part_candidates: set = set()  # 跨 sheet 收集所有部位名称

        try:
            import openpyxl
            # read_only=False 才能读到合并单元格几何（merged_cells），而合并单元格常承载
            # 分组键（清扫基准书的"类别"列、规格书的分区）。
            # 兜底按"行数"而非文件大小判断：图文型 xlsx（清扫基准书/规格书）文件可达数十 MB，
            # 但只有几十行，应当展开合并；真正的数据导出表（数十万~百万行）才保持 read_only 防止内存暴涨。
            _use_ro = True
            try:
                _fsz = os.path.getsize(local_path)
                _probe = openpyxl.load_workbook(local_path, read_only=True, data_only=True)
                _maxr = max((ws.max_row or 0) for ws in _probe.worksheets) if _probe.worksheets else 0
                _probe.close()
                _use_ro = (_maxr > 50000) or (_fsz > 100 * 1024 * 1024)
            except Exception:
                _use_ro = True
            wb = openpyxl.load_workbook(local_path, read_only=_use_ro, data_only=True)
            for sheet_idx, sheet_name in enumerate(wb.sheetnames):
                ws = wb[sheet_name]

                # 合并单元格"向下填充"：纵向合并的首格值（如"类别"列）传播到该列下方各行，
                # 避免下游丢失分组键。横向合并不填充（标题只出现一次）。
                merge_fill = {}
                if not _use_ro:
                    try:
                        for mr in ws.merged_cells.ranges:
                            tl = ws.cell(mr.min_row, mr.min_col).value
                            if tl is None:
                                continue
                            # 只向下传播"短标签"型合并值（分组键，如类别名"设备本体"）。
                            # 跨行合并的长正文/步骤说明若被复制，会生成重复数据行，
                            # 破坏 step 检测与图文绑定（如过程检验 SOP 的步骤被复制两次）。
                            if len(str(tl).strip()) > 20:
                                continue
                            for rr in range(mr.min_row + 1, mr.max_row + 1):
                                merge_fill[(rr, mr.min_col)] = tl
                    except Exception:
                        pass

                # sheet 标题作为 heading block（默认 section_type=cleaning_items）
                sheet_heading = ExtractedBlock(
                    block_type="heading",
                    text=sheet_name,
                    page_num=sheet_idx + 1,
                    section_path=sheet_name,
                    source="openpyxl",
                )
                sheet_heading.extra = {"section_type": "cleaning_items"}
                blocks.append(sheet_heading)

                # ── Pass 1: 扫描所有行，识别表头 + 收集 part_candidates ──
                all_rows = []
                header_row_idx = None
                part_col_idx = None

                for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    cells = []
                    for ci0, c in enumerate(row):
                        if c is None and (row_idx, ci0 + 1) in merge_fill:
                            c = merge_fill[(row_idx, ci0 + 1)]
                        cells.append(str(c) if c is not None else "")
                    all_rows.append((row_idx, cells))

                    # 检测表头行（命中 ≥2 个关键词）
                    if header_row_idx is None:
                        stripped_cells = {c.strip() for c in cells if c.strip()}
                        hits = len(stripped_cells & self._HEADER_KEYWORDS)
                        if hits >= 2:
                            header_row_idx = row_idx
                            # 找部位名称列
                            for ci, c in enumerate(cells):
                                if c.strip() in self._PART_COL_NAMES:
                                    part_col_idx = ci
                                    break

                # 从数据行收集 part_candidates
                if header_row_idx is not None and part_col_idx is not None:
                    for row_idx, cells in all_rows:
                        if row_idx <= header_row_idx:
                            continue
                        if part_col_idx < len(cells):
                            part_name = cells[part_col_idx].strip()
                            if part_name and len(part_name) >= 2 and not part_name.isdigit():
                                all_part_candidates.add(part_name)

                # ── Pass 2: 生成 blocks ──
                # 签字行/备注分界正则
                _RE_SIGNATURE = re.compile(r"编写[：:]?\s*(.*\s+)?审核")
                _RE_SECTION_BOUNDARY = re.compile(r"清扫时[要需]点检|^点检项目$")

                in_inspection_section = False
                for row_idx, cells in all_rows:
                    line = "\t".join(cells).strip()
                    if not line:
                        continue

                    clean_text = line.replace("\t", "").strip()
                    stripped_cells = {c.strip() for c in cells if c.strip()}

                    # ── Bug fix 1: 子区域分界行 → 只生成 heading，跳过 paragraph ──
                    if _RE_SECTION_BOUNDARY.match(clean_text):
                        if not in_inspection_section:
                            in_inspection_section = True
                            sub_heading = ExtractedBlock(
                                block_type="heading",
                                text=f"{sheet_name} — 点检项目",
                                page_num=sheet_idx + 1,
                                section_path=f"{sheet_name} — 点检项目",
                                source="openpyxl",
                            )
                            sub_heading.extra = {"section_type": "inspection_items"}
                            blocks.append(sub_heading)
                        continue  # 不生成 paragraph block

                    # ── Bug fix 3: 签字行 → 跳过 ──
                    if _RE_SIGNATURE.search(clean_text):
                        continue

                    # ── Bug fix 2: 二级表头检测（点检区表头等） ──
                    header_hits = len(stripped_cells & self._HEADER_KEYWORDS)
                    is_secondary_header = (
                        header_row_idx is not None
                        and row_idx > header_row_idx
                        and header_hits >= 2
                    )

                    # ── Bug fix 4: 稀疏表头碎片行（只含表头关键词，无实质数据） ──
                    non_empty_cells = [c.strip() for c in cells if c.strip()]
                    is_sparse_header_fragment = (
                        len(non_empty_cells) <= 3
                        and header_hits >= 1
                        and all(c in self._HEADER_KEYWORDS or len(c) <= 2 for c in non_empty_cells)
                    )

                    # ── v2 fix 2: 纯序号空行（只有 1-2 个 cell 且全是数字/空） ──
                    is_empty_number_row = (
                        len(non_empty_cells) <= 2
                        and all(c.isdigit() for c in non_empty_cells)
                    )

                    # 生成 paragraph block
                    blk = ExtractedBlock(
                        block_type="paragraph",
                        text=line,
                        page_num=sheet_idx + 1,
                        source="openpyxl",
                    )
                    extra = {"row_num": row_idx, "sheet_idx": sheet_idx}

                    # 标记 row_role
                    if header_row_idx is not None and row_idx <= header_row_idx:
                        extra["row_role"] = "metadata"
                    elif is_secondary_header or is_sparse_header_fragment:
                        extra["row_role"] = "metadata"
                    elif is_empty_number_row:
                        extra["row_role"] = "metadata"  # 纯序号空行
                    else:
                        extra["row_role"] = "data"
                        # 提取 part_name（如果有部位列）
                        if part_col_idx is not None and part_col_idx < len(cells):
                            pn = cells[part_col_idx].strip()
                            if pn and len(pn) >= 2 and not pn.isdigit():
                                extra["part_name"] = pn
                        # v2 fix 5: 标记稀疏数据行（非空数据 cell ≤3）
                        data_cells = [c for c in non_empty_cells if not c.isdigit()]
                        if len(data_cells) <= 2 and not is_empty_number_row:
                            extra["sparse_row"] = True

                    blk.extra = extra
                    blocks.append(blk)

            wb.close()
        except Exception as e:
            warnings.append(f"Failed to extract Excel file: {e}")

        # 提取嵌入图片 → 三阶段过滤漏斗
        assets, img_blocks = self._process_embedded_images(
            extract_images_from_xlsx(local_path, _safe_image_output_dir(task)),
            task,
        )

        # ── part_labels 提取（混合策略：part_candidates 优先 + 白名单 fallback）──
        if all_part_candidates or self._PART_KEYWORDS_FALLBACK:
            for asset in assets:
                ocr = asset.get("ocr_text", "")
                vs = asset.get("visual_summary", "")
                search_text = f"{ocr} {vs}"
                if not search_text.strip():
                    continue
                # 优先匹配表格中实际出现的部位名称
                labels = [p for p in all_part_candidates if p in search_text]
                # Fallback：通用白名单
                if not labels:
                    labels = [kw for kw in self._PART_KEYWORDS_FALLBACK if kw in search_text]
                if labels:
                    asset["part_labels"] = sorted(set(labels))

        if img_blocks:
            blocks.extend(img_blocks)

        # ── procedure_image_guide 后处理：步骤标注 + 图号映射 ──
        from opensearch_pipeline.extraction.xlsx_classifier import classify_xlsx_layout
        _sheet_names = list(dict.fromkeys(
            b.text for b in blocks
            if b.block_type == "heading" and b.extra.get("section_type") in ("cleaning_items", "")
        ))
        _layout_type, _ = classify_xlsx_layout(
            filename=task.get("filename", ""),
            sheet_names=_sheet_names,
            flat_text=blocks_to_text(blocks)[:5000],
        )
        if _layout_type == "procedure_image_guide":
            _RE_STEP_PREFIX = re.compile(r"^(\d+)\t")
            # 图号引用：如图1、见左图3、4、5、如图8-9
            _RE_FIG_REF = re.compile(
                r"(?:如图|见.*?图)\s*(\d+(?:\s*[、,，\-~～]\s*\d+)*)"
            )

            def _parse_figure_refs(text: str) -> list:
                """从文本中提取所有引用的图号列表，如 ['图1','图3','图4','图5']"""
                refs = []
                for m in _RE_FIG_REF.finditer(text):
                    nums_str = m.group(1)
                    # 处理范围（8-9）和列表（3、4、5）
                    parts = re.split(r"[、,，]\s*", nums_str)
                    for part in parts:
                        part = part.strip()
                        range_m = re.match(r"(\d+)\s*[\-~～]\s*(\d+)", part)
                        if range_m:
                            for n in range(int(range_m.group(1)), int(range_m.group(2)) + 1):
                                refs.append(f"图{n}")
                        elif part.isdigit():
                            refs.append(f"图{part}")
                return refs

            # 标记步骤行
            for blk in blocks:
                if blk.block_type != "paragraph":
                    continue
                m = _RE_STEP_PREFIX.match(blk.text)
                if m:
                    step_no = int(m.group(1))
                    blk.extra["step_no"] = step_no
                    blk.extra["row_role"] = "step"
                    fig_refs = _parse_figure_refs(blk.text)
                    if fig_refs:
                        blk.extra["figure_refs"] = fig_refs

            # 建立图号 → asset 映射
            # 关键：跳过 logo/表头装饰图（anchor_row 在图例区之前的图片）
            # 优先用"图例"行号定位图片区起始；其次用 step_row-3；保底 row<5 过滤 logo
            tu_li_rows = [b.extra.get("row_num", 999) for b in blocks
                          if b.block_type == "paragraph" and "图例" in b.text]
            step_rows = [b.extra.get("row_num", 999) for b in blocks if b.extra.get("step_no")]
            if tu_li_rows:
                figure_start_row = min(tu_li_rows)  # "图例" 行即是图片区开始
            elif step_rows:
                figure_start_row = max(0, min(step_rows) - 3)
            else:
                figure_start_row = 5  # 保底：前5行通常是标题/签署区

            sorted_assets = sorted(assets, key=lambda a: a.get("filename", ""))
            figure_map = {}
            fig_counter = 1
            for asset in sorted_assets:
                anchor_row = asset.get("anchor_row")
                # 跳过步骤区之前的图片（logo、表头装饰等）
                if anchor_row is not None and anchor_row < figure_start_row:
                    asset["figure_no"] = None  # 标记为非步骤图片
                    continue
                fig_label = f"图{fig_counter}"
                asset["figure_no"] = fig_label
                figure_map[fig_label] = fig_counter - 1
                fig_counter += 1

        flat_text = blocks_to_text(blocks)
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=file_ext,
            extract_method="openpyxl",
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            warnings=warnings,
            assets=assets,
        )

    # ── PPTX ──

    # 标题占位符 type 值（PP_PLACEHOLDER 枚举的整数值）
    # TITLE=1, CENTER_TITLE=3, VERTICAL_TITLE=5
    _PPTX_TITLE_TYPES = {1, 3, 5}
    # SUBTITLE=4
    _PPTX_SUBTITLE_TYPES = {4}

    def _extract_pptx(self, task: dict) -> ExtractionResult:
        """
        PPTX 提取（Shape 级别结构化 + 表格 + Speaker Notes + 嵌入图片）。

        每个 slide 的 shapes 按类型生成不同 block：
          - Title/Center Title placeholder → heading block (level=1)
          - Subtitle placeholder → heading block (level=2)
          - Body/Content text → paragraph block
          - Table → table block (markdown pipe format)
          - Speaker notes → paragraph block (标记 source="speaker_notes")

        section_path 追踪：slide 标题 → 后续 shapes 继承。
        """
        from opensearch_pipeline.extraction.image_extraction_utils import extract_images_from_pptx

        local_path = task.get("local_path", "")
        blocks = []
        warnings = []
        slide_count = 0

        try:
            from pptx import Presentation
            prs = Presentation(local_path)
            slide_count = len(prs.slides)

            current_section = None  # section_path 追踪

            for slide_idx, slide in enumerate(prs.slides):
                page_num = slide_idx + 1
                slide_title = None  # 当前 slide 的标题

                # ── Phase 1: 按 shape 类型生成 blocks ──
                for shape in slide.shapes:

                    # ── 表格 shape → table block ──
                    if shape.has_table:
                        rows_text = []
                        for row in shape.table.rows:
                            cells = [cell.text.strip().replace("\n", " ") if cell.text else ""
                                     for cell in row.cells]
                            if any(cells):
                                rows_text.append("| " + " | ".join(cells) + " |")
                        if rows_text:
                            blocks.append(ExtractedBlock(
                                block_type="table",
                                text="\n".join(rows_text),
                                page_num=page_num,
                                section_path=current_section,
                                source="python_pptx",
                                extra={
                                    "table_index": 0,
                                    "row_count": len(rows_text),
                                    "slide_num": page_num,
                                },
                            ))
                        continue  # table shape 已处理，跳过文本提取

                    # ── 文本类 shape ──
                    if not hasattr(shape, "text") or not shape.text.strip():
                        continue

                    text = shape.text.strip()

                    # 判断是否是标题占位符
                    is_title = False
                    is_subtitle = False
                    if shape.is_placeholder:
                        try:
                            ph_type = shape.placeholder_format.type
                            # ph_type 是 PP_PLACEHOLDER 枚举，int(ph_type) 获取整数值
                            ph_type_int = int(ph_type) if ph_type is not None else -1
                            if ph_type_int in self._PPTX_TITLE_TYPES:
                                is_title = True
                            elif ph_type_int in self._PPTX_SUBTITLE_TYPES:
                                is_subtitle = True
                        except Exception:
                            pass

                    if is_title:
                        slide_title = text
                        current_section = text
                        blocks.append(ExtractedBlock(
                            block_type="heading",
                            text=text,
                            level=1,
                            page_num=page_num,
                            section_path=current_section,
                            source="python_pptx",
                            extra={"placeholder": "title", "slide_num": page_num},
                        ))
                    elif is_subtitle:
                        blocks.append(ExtractedBlock(
                            block_type="heading",
                            text=text,
                            level=2,
                            page_num=page_num,
                            section_path=current_section,
                            source="python_pptx",
                            extra={"placeholder": "subtitle", "slide_num": page_num},
                        ))
                    else:
                        # 普通文本框 / Body 占位符 → paragraph block
                        blocks.append(ExtractedBlock(
                            block_type="paragraph",
                            text=text,
                            page_num=page_num,
                            section_path=current_section,
                            source="python_pptx",
                        ))

                # ── Phase 2: 如果没有检测到标题占位符，用 slide 序号做 fallback heading ──
                if slide_title is None:
                    # 检查此 slide 是否有任何内容 block
                    slide_blocks = [b for b in blocks if b.page_num == page_num]
                    if slide_blocks:
                        # 找第一个有文本的 block 作为 slide 标题
                        first_text = slide_blocks[0].text[:50] if slide_blocks else ""
                        fallback_title = f"Slide {page_num}" + (f": {first_text}" if first_text else "")
                        current_section = fallback_title

                # ── Phase 3: Speaker notes → paragraph block ──
                try:
                    if slide.has_notes_slide and slide.notes_slide:
                        notes_text = slide.notes_slide.notes_text_frame.text.strip()
                        if notes_text and len(notes_text) > 5:
                            blocks.append(ExtractedBlock(
                                block_type="paragraph",
                                text=notes_text,
                                page_num=page_num,
                                section_path=current_section,
                                source="speaker_notes",
                                extra={"slide_num": page_num, "is_notes": True},
                            ))
                except Exception:
                    pass  # notes 提取失败不影响主流程

        except Exception as e:
            warnings.append(f"Failed to extract PPTX text: {e}")

        # 提取嵌入图片 → 三阶段过滤漏斗
        assets, img_blocks = self._process_embedded_images(
            extract_images_from_pptx(local_path, _safe_image_output_dir(task)),
            task,
        )
        if img_blocks:
            blocks.extend(img_blocks)

        flat_text = blocks_to_text(blocks)
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext="pptx",
            extract_method="python_pptx",
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            page_count=slide_count,
            warnings=warnings,
            assets=assets,
        )

    # ── Plain text / Markdown ──

    def _extract_text(self, task: dict) -> ExtractionResult:
        """纯文本/Markdown/HTML/CSV 提取。"""
        local_path = task.get("local_path", "")
        file_ext = task.get("file_ext", "txt").lower()

        try:
            with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
                raw_text = f.read()
        except Exception as e:
            return ExtractionResult(
                doc_id=task["doc_id"],
                version_no=task["version_no"],
                source_key=task.get("raw_key", ""),
                file_ext=file_ext,
                extract_method="plain_text",
                title=task.get("filename", ""),
                text="",
                text_length=0,
                warnings=[f"Failed to read file: {e}"],
            )

        # HTML/CSV 此前被直读为纯文本：HTML 满屏标签、CSV 引号/转义糊在一起。
        # 先转成可读文本再走统一分块；转换失败回退原文（保持优雅降级约定）。
        extract_method = "plain_text"
        if file_ext == "html":
            raw_text = _html_to_text(raw_text)
            extract_method = "html_text"
        elif file_ext == "csv":
            raw_text = _csv_to_text(raw_text)
            extract_method = "csv_table"

        blocks = extract_text_file(raw_text, source="native")
        flat_text = blocks_to_text(blocks)
        title = extract_title_from_blocks(blocks, fallback=task.get("filename", ""))

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=file_ext,
            extract_method=extract_method,
            title=title,
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
        )

    # ── 嵌入图片通用处理 ──

    # ── 跨文档 VLM 结果持久化缓存 ──
    # 本地存储: scratch/vlm_cache.json（和 embedding_cache.json 同目录）
    # OSS 存储: processing/cache/vlm_cache.json（跨 DataWorks 运行持久化）
    # 缓存 key: 图片文件 MD5 hash
    # 缓存 value: funnel_result dict (status, visual_summary, reason, width, height, ...)
    _vlm_cache = None  # lazy-load，类级别共享
    _vlm_cache_file = None
    _vlm_cache_oss_key = "processing/cache/vlm_cache.json"

    @classmethod
    def _load_vlm_cache(cls) -> dict:
        """
        延迟加载 VLM 缓存文件。

        加载优先级：
          1. 本地 scratch/vlm_cache.json（快速，无网络开销）
          2. OSS processing/cache/vlm_cache.json（跨 DataWorks 运行持久化）
          3. 空缓存（首次运行）
        """
        if cls._vlm_cache is not None:
            return cls._vlm_cache

        import json
        # __file__ = opensearch_pipeline/extraction/unified_extractor.py
        # → dirname x3 = project root (same level as scratch/embedding_cache.json)
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cls._vlm_cache_file = os.path.join(project_root, "scratch", "vlm_cache.json")

        # 优先读本地缓存
        if os.path.exists(cls._vlm_cache_file):
            try:
                with open(cls._vlm_cache_file, "r", encoding="utf-8") as f:
                    cls._vlm_cache = json.load(f)
                print(f"      [VLM Cache] Loaded {len(cls._vlm_cache)} cached entries from local {os.path.basename(cls._vlm_cache_file)}")
                return cls._vlm_cache
            except Exception:
                pass

        # 本地不存在，尝试从 OSS 下载
        cls._vlm_cache = cls._download_vlm_cache_from_oss()
        if cls._vlm_cache:
            # 同步到本地方便后续快速读取
            try:
                os.makedirs(os.path.dirname(cls._vlm_cache_file), exist_ok=True)
                with open(cls._vlm_cache_file, "w", encoding="utf-8") as f:
                    json.dump(cls._vlm_cache, f, ensure_ascii=False)
            except Exception:
                pass
            print(f"      [VLM Cache] Downloaded {len(cls._vlm_cache)} cached entries from OSS {cls._vlm_cache_oss_key}")
        else:
            cls._vlm_cache = {}

        return cls._vlm_cache

    @classmethod
    def _download_vlm_cache_from_oss(cls) -> dict:
        """从 OSS 下载 VLM 缓存，失败返回 None。"""
        import json
        try:
            from opensearch_pipeline.pipeline_nodes import _get_oss_bucket
            bucket, is_sim = _get_oss_bucket()
            if is_sim or bucket is None:
                return None
            result = bucket.get_object(cls._vlm_cache_oss_key)
            data = json.loads(result.read().decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            # OSS 上不存在或读取失败，静默忽略
            return None

    @classmethod
    def _save_vlm_cache(cls):
        """
        持久化 VLM 缓存。

        写入目标：
          1. 本地 scratch/vlm_cache.json（快速读取）
          2. OSS processing/cache/vlm_cache.json（跨运行持久化）
        """
        import json
        if cls._vlm_cache is None or cls._vlm_cache_file is None:
            return

        cache_json = json.dumps(cls._vlm_cache, ensure_ascii=False)

        # 写入本地（DET: 原子写 temp + os.replace，避免崩溃/并发写产生半截 JSON 触发全量 VLM 重判）
        try:
            os.makedirs(os.path.dirname(cls._vlm_cache_file), exist_ok=True)
            _tmp = f"{cls._vlm_cache_file}.tmp.{os.getpid()}"
            with open(_tmp, "w", encoding="utf-8") as f:
                f.write(cache_json)
            os.replace(_tmp, cls._vlm_cache_file)
        except Exception as e:
            print(f"      ⚠️ Failed to save VLM cache locally: {e}")

        # 上传到 OSS
        try:
            from opensearch_pipeline.pipeline_nodes import _get_oss_bucket
            bucket, is_sim = _get_oss_bucket()
            if not is_sim and bucket is not None:
                bucket.put_object(cls._vlm_cache_oss_key, cache_json.encode("utf-8"))
                print(f"      [VLM Cache] Synced {len(cls._vlm_cache)} entries to OSS {cls._vlm_cache_oss_key}")
        except Exception as e:
            print(f"      ⚠️ Failed to sync VLM cache to OSS: {e}")

    def _process_embedded_images(self, image_assets: list, task: dict) -> tuple:
        """
        将提取出的嵌入图片送入 ImageFunnelProcessor 三阶段过滤漏斗（并发 + 去重 + 持久缓存）。

        优化策略：
          1. Funnel 1（静态启发式，<1ms/张）串行预过滤，快速丢弃装饰图
          2. MD5 Hash 去重 + 跨文档持久缓存：
             - 文档内：相同 hash 图片只过一次 VLM
             - 跨文档：命中 scratch/vlm_cache.json 的图片直接复用，无需 VLM 调用
          3. 未命中缓存的唯一图片并发送入 Funnel 2+3（OCR + VLM），
             使用 ThreadPoolExecutor，并发度由 RAG_VLM_CONCURRENCY 控制（默认 8）

        路由结果：
          - DISCARD_DECORATIVE → 丢弃，不记录
          - ROUTE_TO_TEXT → 记录 asset + 追加 OCR 文本块到 blocks
          - ROUTE_TO_VECTOR → 记录 asset（downstream 自动创建 image chunk）
          - QUARANTINE_SENSITIVE → 记录 asset + warning

        Args:
            image_assets: ImageAsset 列表（来自 image_extraction_utils）。
            task: 当前文档的 task dict。

        Returns:
            (assets, ocr_blocks): assets 列表和 ROUTE_TO_TEXT 产生的文本块列表。
        """
        if not image_assets:
            return [], []

        import hashlib
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor

        processor = ImageFunnelProcessor(simulate=self.simulate)
        is_public = "_quarantine/" not in task.get("raw_key", "")
        doc_id = task["doc_id"]

        # ── Phase 1: Funnel 1 串行预过滤（<1ms/张，无需并发） ──
        candidates = []  # 通过 Funnel 1 的图片
        discard_count = 0

        for img_asset in image_assets:
            try:
                w, h, kb = processor._static_heuristics(img_asset.local_path)
                aspect = max(w / max(h, 1), h / max(w, 1))
                if w < 50 or h < 50 or kb < 3.0 or aspect > 8.0:
                    fname = os.path.basename(img_asset.local_path)
                    print(f"    [Funnel 1] Discarded decorative image: {fname} ({w}x{h}, {kb:.1f}KB, ratio={aspect:.1f})")
                    discard_count += 1
                    continue
                candidates.append(img_asset)
            except Exception as e:
                print(f"      ⚠️ Funnel 1 heuristic failed for {img_asset.original_name}: {e}")
                continue

        if discard_count:
            print(f"      [Funnel 1] Pre-filtered: {discard_count} decorative, {len(candidates)} remaining")

        if not candidates:
            return [], []

        # ── Phase 1.5: MD5 Hash 去重 + 跨文档缓存查询 ──
        vlm_cache = self._load_vlm_cache()

        hash_to_candidates = {}   # md5 -> [img_asset, ...]
        hash_to_representative = {}  # md5 -> 第一张图片（代表）
        hash_to_cached_result = {}   # md5 -> funnel_res（来自持久缓存）

        for img_asset in candidates:
            try:
                with open(img_asset.local_path, "rb") as f:
                    file_hash = hashlib.md5(f.read()).hexdigest()
            except Exception:
                file_hash = f"fallback_{id(img_asset)}"

            if file_hash not in hash_to_candidates:
                hash_to_candidates[file_hash] = []
                hash_to_representative[file_hash] = img_asset
                # 查询跨文档持久缓存（按 is_public 分命名空间）：public 文档会 bypass 安全审计，
                # 其 CLEAN 结论绝不能被 quarantine 文档复用（否则跳过敏感审计）；反之亦然。
                # 遗留裸 MD5 key 的回退/迁移逻辑见 _vlm_cache_lookup。
                cached = _vlm_cache_lookup(vlm_cache, file_hash, is_public)
                if cached is not None:
                    # 反幻觉清洗同样覆盖历史缓存里的编造 ocr_text（幂等；
                    # 缓存条目自带原图 width/height，密度上界可触发）
                    if cached.get("ocr_text"):
                        clean, _m = sanitize_ocr_text(
                            cached.get("ocr_text", ""),
                            width=cached.get("width") or None,
                            height=cached.get("height") or None,
                        )
                        if clean != cached.get("ocr_text"):
                            cached = dict(cached)
                            cached["ocr_text"] = clean
                            vlm_cache[f"{file_hash}:{'pub' if is_public else 'sec'}"] = cached
                    hash_to_cached_result[file_hash] = cached
            hash_to_candidates[file_hash].append(img_asset)

        total_unique = len(hash_to_representative)
        dup_count = len(candidates) - total_unique
        cache_hit_count = len(hash_to_cached_result)
        need_vlm_hashes = [h for h in hash_to_representative if h not in hash_to_cached_result]

        if dup_count > 0 or cache_hit_count > 0:
            print(f"      [Hash Dedup] {len(candidates)} candidates → {total_unique} unique, "
                  f"{cache_hit_count} cache hits, {len(need_vlm_hashes)} need VLM")

        # ── Phase 2: Funnel 2+3 并发处理，仅处理未命中缓存的唯一图片 ──
        max_workers = int(os.environ.get("RAG_VLM_CONCURRENCY", "8"))
        assets = []
        ocr_blocks = []
        t0 = time.time()

        def _process_single(img_asset):
            """单张图片的 Funnel 2+3 处理（线程安全）。"""
            return processor.process_image(
                img_asset.local_path, doc_id, is_public=is_public,
                doc_title=task.get("doc_title", ""),
            )

        # 合并结果：缓存命中 + 新处理
        hash_to_result = dict(hash_to_cached_result)  # 先放入缓存命中的

        if need_vlm_hashes:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_to_hash = {}
                for file_hash in need_vlm_hashes:
                    representative = hash_to_representative[file_hash]
                    future = pool.submit(_process_single, representative)
                    future_to_hash[future] = file_hash

                for future in as_completed(future_to_hash):
                    file_hash = future_to_hash[future]
                    try:
                        funnel_res = future.result()
                        hash_to_result[file_hash] = funnel_res
                        # 写入持久缓存：跳过降级结果（VLM 超时/解析失败的兜底，缓存会把一次性
                        # 故障变成跨文档/跨运行的永久错误标签）和无法稳定取哈希的 fallback key
                        # （基于内存地址，跨进程无意义）。缓存键按 is_public 分命名空间。
                        # ⚠️ simulate 模式的 mock 结果绝不能入持久缓存 —— 按真实 MD5 落键后，
                        # 真实运行会命中 mock 描述（缓存投毒）。
                        if (not self.simulate and not funnel_res.get("degraded")
                                and not file_hash.startswith("fallback_")):
                            cache_key = f"{file_hash}:{'pub' if is_public else 'sec'}"
                            vlm_cache[cache_key] = {
                                "status": funnel_res.get("status", ""),
                                "visual_summary": funnel_res.get("visual_summary", ""),
                                "image_category": funnel_res.get("image_category", ""),
                                "vlm_annotation_map": funnel_res.get("vlm_annotation_map", {}),
                                "reason": funnel_res.get("reason", ""),
                                "width": funnel_res.get("width", 0),
                                "height": funnel_res.get("height", 0),
                                "file_size_kb": funnel_res.get("file_size_kb", 0.0),
                                "ocr_text": funnel_res.get("ocr_text", ""),
                            }
                    except Exception as e:
                        rep = hash_to_representative[file_hash]
                        print(f"      ⚠️ Image funnel failed for {rep.original_name}: {e}")

            # 处理完本文档后持久化缓存（simulate 不落盘，防 mock 污染共享缓存）
            if not self.simulate:
                self._save_vlm_cache()

        # ── Phase 3: 扇出结果到所有图片（包括重复项） ──
        all_results = []  # (img_asset, funnel_res)
        for file_hash, img_assets_group in hash_to_candidates.items():
            if file_hash not in hash_to_result:
                continue
            funnel_res = hash_to_result[file_hash]
            for img_asset in img_assets_group:
                all_results.append((img_asset, funnel_res))

        # 按 page_num → anchor_row → image_index 排序，保持文档内图片的原始顺序
        # DOCX 的 page_num 全部是 None，需要用 image_index 作为 fallback。
        # XLSX 在同一 sheet 内允许 image_index 跨 sheet 累加而 anchor_row 才是物理行号 —
        # 把 anchor_row 提到 image_index 之前，确保 xlsx assets 的下游 pool 顺序由
        # 物理行号唯一决定，避免任何 ThreadPoolExecutor / 历史索引顺序的残留影响。
        all_results.sort(key=lambda x: (
            x[0].page_num if x[0].page_num is not None else 999999,
            getattr(x[0], "anchor_row", None) if getattr(x[0], "anchor_row", None) is not None else 999999,
            x[0].image_index if x[0].image_index is not None else 999999,
            x[0].original_name or "",
        ))

        for img_asset, funnel_res in all_results:
            status = funnel_res["status"]

            # Funnel 1 结果在 process_image 内部也会触发（双重保护），跳过
            if status == "DISCARD_DECORATIVE":
                continue

            asset_dict = {
                "filename": os.path.basename(img_asset.local_path),
                "local_path": img_asset.local_path,
                "page_num": img_asset.page_num,
                "image_index": img_asset.image_index,
                "original_index": img_asset.image_index,
                "status": status,
                "width": funnel_res.get("width", 0),
                "height": funnel_res.get("height", 0),
                "file_size_kb": funnel_res.get("file_size_kb", 0.0),
                "ocr_text": funnel_res.get("ocr_text", ""),
                "visual_summary": funnel_res.get("visual_summary", ""),
                "image_category": funnel_res.get("image_category", ""),

                "vlm_annotation_map": funnel_res.get("vlm_annotation_map", {}),
                "reason": funnel_res.get("reason", ""),
            }
            # 透传 PDF 显示 bbox（页坐标、上原点 — 版面位置图片锚定用）
            if getattr(img_asset, "bbox", None):
                asset_dict["bbox"] = list(img_asset.bbox)
            # 透传 XLSX anchor_row（用于行级图片绑定）
            if hasattr(img_asset, "anchor_row") and img_asset.anchor_row is not None:
                asset_dict["anchor_row"] = img_asset.anchor_row
            # 透传 XLSX annotation_num（Drawing XML 分组标注编号，如 ①②③）
            if hasattr(img_asset, "annotation_num") and img_asset.annotation_num is not None:
                asset_dict["annotation_num"] = img_asset.annotation_num
            if hasattr(img_asset, "annotation_label") and img_asset.annotation_label:
                asset_dict["annotation_label"] = img_asset.annotation_label
            assets.append(asset_dict)

            # ROUTE_TO_TEXT：追加 OCR 文本块（与 _extract_image 行为一致）
            if status == "ROUTE_TO_TEXT" and funnel_res.get("ocr_text"):
                ocr_blocks.append(ExtractedBlock(
                    block_type="ocr_text",
                    text=funnel_res["ocr_text"],
                    page_num=img_asset.page_num,
                    source="ocr",
                    extra={
                        "vlm_annotation_map": funnel_res.get("vlm_annotation_map", {}),
                        "visual_summary": funnel_res.get("visual_summary", ""),
                        "image_category": funnel_res.get("image_category", ""),
                        "source_image": os.path.basename(img_asset.local_path),
                        "local_path": img_asset.local_path,
                    },
                ))

        elapsed = time.time() - t0
        routed_counts = {}
        for a in assets:
            s = a["status"]
            routed_counts[s] = routed_counts.get(s, 0) + 1
        if routed_counts:
            summary = ", ".join(f"{k}={v}" for k, v in routed_counts.items())
            vlm_calls = len(need_vlm_hashes)
            avg_ms = (elapsed / vlm_calls * 1000) if vlm_calls else 0
            print(f"      [img-funnel] {len(image_assets)} extracted → {len(assets)} kept ({summary})")
            print(f"      [img-funnel] ⚡ VLM calls={vlm_calls}, cache_hits={cache_hit_count}, "
                  f"dedup={dup_count}, time={elapsed:.1f}s ({avg_ms:.0f}ms/call, workers={max_workers})")

        return assets, ocr_blocks

    # ── Image (direct OCR) ──

    def _extract_image(self, task: dict) -> ExtractionResult:
        """图片直接调用过滤漏斗进行三阶段分析。"""
        from opensearch_pipeline.image_funnel_processor import ImageFunnelProcessor
        
        local_path = task.get("local_path", "")
        is_public = "_quarantine/" not in task.get("raw_key", "")
        
        processor = ImageFunnelProcessor(simulate=self.simulate)
        funnel_res = processor.process_image(
            local_path, task["doc_id"], is_public=is_public,
            doc_title=task.get("doc_title", ""),
        )
        
        status = funnel_res["status"]
        assets = [{
            "filename": os.path.basename(local_path),
            "local_path": local_path,
            # 与嵌入图 asset_dict 字段对齐：image_index/page_num/funnel 决策可溯源，
            # 下游 _enrich_existing_image_refs 按 image_index 注入 source_image
            "image_index": 0,
            "original_index": 0,
            "page_num": 1,
            # 独立图片文档：raw/ 对象本身就是这张图，直接作为可签名的 oss_key ——
            # 资产上传环节只上传 ROUTE_TO_VECTOR，构造出的 processing/assets/ 路径
            # 对 ROUTE_TO_TEXT 永不存在（serving 签出 403 死图，对抗评审证实）。
            "oss_key": task.get("raw_key", ""),
            "status": status,
            "width": funnel_res.get("width", 0),
            "height": funnel_res.get("height", 0),
            "file_size_kb": funnel_res.get("file_size_kb", 0.0),
            "ocr_text": funnel_res.get("ocr_text", ""),
            "visual_summary": funnel_res.get("visual_summary", ""),
            "image_category": funnel_res.get("image_category", ""),

            "vlm_annotation_map": funnel_res.get("vlm_annotation_map", {}),
            "reason": funnel_res.get("reason", "")
        }]

        # 根据漏斗决策构建块和全文
        blocks = []
        warnings = []

        if status == "ROUTE_TO_TEXT" and funnel_res.get("ocr_text"):
            blocks.append(ExtractedBlock(
                block_type="ocr_text",
                text=funnel_res["ocr_text"],
                page_num=1,
                source="ocr",
                extra={
                    "vlm_annotation_map": funnel_res.get("vlm_annotation_map", {}),
                    "visual_summary": funnel_res.get("visual_summary", ""),
                    "image_category": funnel_res.get("image_category", ""),
                    "local_path": local_path,
                },
            ))
            # 独立图片文档的图就是文档本体（磨床操作流程.png 等 SOP 海报）：
            # ROUTE_TO_TEXT 只留 OCR 文本会让原图永远无法在回答里渲染。
            # 附 image_ref 块（仅 image_index 占位），chunk 阶段由
            # _enrich_existing_image_refs 注入 source_image/visual_summary。
            blocks.append(ExtractedBlock(
                block_type="image_ref",
                text="",
                page_num=1,
                source="multimodal",
                extra={"image_index": 0},
            ))
        elif status == "QUARANTINE_SENSITIVE":
            warnings.append(f"🚨 Sensitive content detected in non-public image asset: {funnel_res.get('reason')}")
        
        flat_text = blocks_to_text(blocks)

        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=task.get("file_ext", "png"),
            extract_method="image_funnel",
            title=task.get("filename", ""),
            text=flat_text,
            text_length=len(flat_text),
            blocks=blocks,
            ocr_required=(status == "ROUTE_TO_TEXT"),
            ocr_status="DONE" if status == "ROUTE_TO_TEXT" else "NOT_REQUIRED",
            warnings=warnings,
            assets=assets
        )


    # ── Unsupported ──

    def _unsupported(self, task: dict, file_ext: str) -> ExtractionResult:
        """不支持的文件类型。"""
        return ExtractionResult(
            doc_id=task["doc_id"],
            version_no=task["version_no"],
            source_key=task.get("raw_key", ""),
            file_ext=file_ext,
            extract_method=f"unsupported:{file_ext}",
            title=task.get("filename", ""),
            text="",
            text_length=0,
            warnings=[f"Unsupported file type: {file_ext}"],
        )

    # ── OCR fallback logic ──

    def _pages_needing_ocr(self, result: ExtractionResult) -> List[int]:
        """PDF: 返回原生文本不足的页码（1-based）——扫描页 / 坏字体页 / 空白页。

        旧门槛按整文档 text_length 判断，会让"封面有文字、正文是扫描"的多页 PDF
        一页都不 OCR（#1 语料失败模式）。这里逐页统计原生文本，挑出需要 OCR 的页。
        图片块 / 已有 OCR 块不计入"原生文本"。

        page_count<=0 的两种来源：
          (a) 真的 0 页 PDF（不存在）；
          (b) extraction 失败（pdfplumber 拿不到 + pypdf 也挂）——这才是真情况，
              RD 61D861 就是被旧版静默 return [] 漏过的扫描型 PDF。
        保守策略：尝试用别的 PDF 库重拿 page_count；都拿不到则返回 [1] 占位，
        强制 qwen-vl-ocr 至少看第 1 页（它能自己识别 PDF 真实页数）。
        """
        if result.file_ext != "pdf":
            return []

        page_count = result.page_count or 0
        if page_count <= 0:
            # 保守 fallback：尝试用本地 PDF 库重新拿 page_count（graceful degradation）
            local_path = ""
            try:
                # source_key 不一定是本地路径；这里仅能利用 result 自身已有的信息。
                # 没有 local_path 时只能走 [1] 占位。
                local_path = getattr(result, "_local_path", "") or ""
            except Exception:
                local_path = ""

            recovered = 0
            if local_path:
                # 尝试 pypdf
                try:
                    from pypdf import PdfReader  # type: ignore
                    recovered = len(PdfReader(local_path).pages)
                except Exception:
                    try:
                        from PyPDF2 import PdfReader  # type: ignore
                        recovered = len(PdfReader(local_path).pages)
                    except Exception:
                        recovered = 0
                # 再尝试 pdfplumber
                if recovered <= 0:
                    try:
                        import pdfplumber  # type: ignore
                        with pdfplumber.open(local_path) as pdf:
                            recovered = len(pdf.pages)
                    except Exception:
                        recovered = 0

            warn_msg = (
                f"_pages_needing_ocr: page_count<=0 (extraction likely failed); "
                f"conservative OCR fallback engaged (recovered_page_count={recovered or 'unknown'})"
            )
            if isinstance(getattr(result, "warnings", None), list):
                result.warnings.append(warn_msg)

            if recovered <= 0:
                # 拿不到真实页数 → 至少 OCR 第 1 页占位，让 qwen-vl-ocr 自己看图判断
                return [1]
            # 拿到了真实页数：用真实 page_count 走下面正常逐页路径
            result.page_count = recovered
            page_count = recovered

        per_page: Dict[int, int] = {}
        for b in result.blocks:
            bt = b.get("block_type", "") if isinstance(b, dict) else getattr(b, "block_type", "")
            if bt in ("image_ref", "ocr_text"):
                continue
            pg = b.get("page_num") if isinstance(b, dict) else getattr(b, "page_num", None)
            if pg is None:
                continue
            txt = (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")) or ""
            per_page[pg] = per_page.get(pg, 0) + len(txt.strip())
        return [pg for pg in range(1, page_count + 1)
                if per_page.get(pg, 0) < self.PER_PAGE_OCR_THRESHOLD]

    def _needs_ocr(self, result: ExtractionResult) -> bool:
        """判断是否需要 OCR fallback。"""
        if result.file_ext == "pdf":
            # 逐页判断：任一页原生文本不足即触发（只 OCR 那些页）
            return len(self._pages_needing_ocr(result)) > 0
        if result.file_ext in ("png", "jpg", "jpeg", "webp"):
            return result.text_length < self.OCR_THRESHOLD_CHARS
        return False

    def _apply_ocr_fallback(self, task: dict, result: ExtractionResult) -> ExtractionResult:
        """应用 OCR fallback。PDF 只 OCR 文本不足的页，并按 page_num 合并回正确位置。"""
        local_path = task.get("local_path", "")

        if result.file_ext == "pdf":
            needy = self._pages_needing_ocr(result)
            if not needy:
                return result
            ocr_result = self.ocr_client.ocr_pdf(
                local_path, task["doc_id"], self.oss_client, page_nums=needy,
            )
        else:
            ocr_result = self.ocr_client.ocr_image(
                local_path, task["doc_id"], self.oss_client,
            )

        ocr_blocks = ocr_result.to_blocks()
        if ocr_blocks:
            if result.file_ext == "pdf":
                # OCR 的是空白页（原页几乎无块），按 page_num 稳定排序合并保持文档顺序
                merged = list(result.blocks) + list(ocr_blocks)
                merged.sort(key=lambda b: (getattr(b, "page_num", 0) or 0))
                result.blocks = merged
                result.text = blocks_to_text(result.blocks)
            else:
                result.blocks.extend(ocr_blocks)
                if ocr_result.combined_text:
                    result.text = (result.text + "\n\n" + ocr_result.combined_text
                                   if result.text else ocr_result.combined_text)
            result.text_length = len(result.text)

        result.ocr_required = True
        result.ocr_status = ocr_result.status
        result.extract_method = f"{result.extract_method}+ocr_fallback"

        return result
