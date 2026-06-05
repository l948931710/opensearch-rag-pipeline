# -*- coding: utf-8 -*-
"""
chunker.py — 文档切分器

将 canonical document（提取后的纯文本）切分为结构化 chunk。
支持多种切分策略，每个 chunk 携带完整 metadata。

切分类型：
  text_chunk          — 按段落/固定长度切
  table_chunk         — 表格块
  faq_chunk           — Q&A 对
  section_chunk       — 按标题层级切
  clause_chunk        — 按条款边界切（第X条 / 一、 / 9.1）
  step_card           — SOP 步骤图文绑定 chunk（步骤文字 + 图片引用 + 编号标注）
  procedure_parent    — SOP 完整流程父 chunk（关联所有 step_card 子 chunk）
"""

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Chunk:
    """单个 chunk 的数据结构。"""
    chunk_id: str
    doc_id: str
    version_no: int
    chunk_index: int
    chunk_type: str  # text_chunk / table_chunk / faq_chunk / section_chunk / step_card / procedure_parent
    chunk_text: str
    token_count: int
    raw_text: str = ""
    context_prefix: str = ""
    embedding_text: str = ""

    # 来源定位
    page_num: Optional[int] = None
    section_title: Optional[str] = None
    source_oss_key: Optional[str] = None
    source: str = "native"

    # 继承自文档的 metadata
    title: Optional[str] = None
    owner_dept: Optional[str] = None
    category_l1: Optional[str] = None
    category_l2: Optional[str] = None
    permission_level: Optional[str] = None
    kb_type: Optional[str] = None
    risk_level: Optional[str] = None

    # 处理状态
    is_active: bool = True
    sensitive_redacted: bool = False
    embedding_status: str = "NOT_STARTED"
    index_status: str = "NOT_INDEXED"
    embedding_model: Optional[str] = None
    embedding_vector: Optional[List[float]] = None
    sparse_vector_indices: Optional[List[int]] = None
    sparse_vector_values: Optional[List[float]] = None
    rds_id: Optional[int] = None  # chunk_meta 表的自增 ID，用于 HA3 INT64 主键
    extra: Dict[str, Any] = field(default_factory=dict)

    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "version_no": self.version_no,
            "chunk_index": self.chunk_index,
            "chunk_type": self.chunk_type,
            "chunk_text": self.chunk_text,
            "token_count": self.token_count,
            "raw_text": self.raw_text,
            "context_prefix": self.context_prefix,
            "page_num": self.page_num,
            "section_title": self.section_title,
            "source_oss_key": self.source_oss_key,
            "source": self.source,
            "title": self.title,
            "owner_dept": self.owner_dept,
            "category_l1": self.category_l1,
            "category_l2": self.category_l2,
            "permission_level": self.permission_level,
            "kb_type": self.kb_type,
            "risk_level": self.risk_level,
            "is_active": self.is_active,
            "sensitive_redacted": self.sensitive_redacted,
            "embedding_status": self.embedding_status,
            "index_status": self.index_status,
            "embedding_model": self.embedding_model,
            "created_at": self.created_at,
        }
        # 不序列化 embedding_vector（太大）
        return d

    def to_opensearch_doc(self) -> Dict[str, Any]:
        """转为 OpenSearch 标准版索引文档格式。"""
        doc = {
            "id": self.chunk_id,
            "doc_id": self.doc_id,
            "version_no": self.version_no,
            "chunk_text": self.chunk_text,
            "raw_text": self.raw_text,
            "context_prefix": self.context_prefix,
            "chunk_type": self.chunk_type,
            "title": self.title or "",
            "owner_dept": self.owner_dept or "",
            "permission_level": self.permission_level or "public",
            "category_l1": self.category_l1 or "",
            "category_l2": self.category_l2 or "",
            "kb_type": self.kb_type or "public",
            "risk_level": self.risk_level or "low",
            "page_num": self.page_num or 0,
            "section_title": self.section_title or "",
            "source_url": self.source_oss_key or "",
            "is_active": self.is_active,
            "created_at": self.created_at,
        }
        if self.embedding_vector:
            doc["chunk_vector"] = self.embedding_vector
            
        # 兼容多模态图像字段
        if self.extra:
            if "source_image" in self.extra:
                doc["source_image"] = self.extra["source_image"]
            if "visual_summary" in self.extra:
                doc["visual_summary"] = self.extra["visual_summary"]
                
        return doc

    def to_ha3_doc(self, pk_field: str = "id") -> Dict[str, Any]:
        """转为阿里云 OpenSearch 向量检索版 (HA3 Engine) 文档格式。

        HA3 与标准 OpenSearch 的关键差异:
        - 主键字段名由 HA3 表结构定义决定（通过 pk_field 参数传入）
        - 向量字段不支持 JSON 数组，需要序列化为逗号分隔的浮点字符串
        - 布尔字段使用 int (0/1) 而非 JSON boolean
        - 不支持嵌套对象字段
        """
        doc = {
            pk_field: self.rds_id if self.rds_id is not None else hash(self.chunk_id) & 0x7FFFFFFFFFFFFFFF,
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "version_no": self.version_no,
            "chunk_text": self.chunk_text,
            "chunk_type": self.chunk_type,
            "title": self.title or "",
            "owner_dept": self.owner_dept or "",
            "permission_level": self.permission_level or "public",
            "category_l1": self.category_l1 or "",
            "category_l2": self.category_l2 or "",
            "section_title": self.section_title or "",
            "chunk_index": self.chunk_index,
            "page_num": self.page_num or 0,
            "kb_type": self.kb_type or "public",
            "chunk_text_store": self.chunk_text,
            "source_url": self.source_oss_key or "",
            "is_active": 1 if self.is_active else 0,
        }
        if self.embedding_vector:
            doc["dense_vector"] = list(self.embedding_vector)
        if self.sparse_vector_indices:
            doc["sparse_vector_indices"] = list(self.sparse_vector_indices)
            doc["sparse_vector_values"] = list(self.sparse_vector_values or [])

        # 图片 chunk 的多模态 metadata
        if self.extra:
            if self.extra.get("source_image"):
                doc["source_image"] = self.extra["source_image"]
            if self.extra.get("visual_summary"):
                doc["visual_summary"] = self.extra["visual_summary"]

        return doc


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文约 1.5 字/token，英文约 4 字符/token）。"""
    cn_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    en_chars = len(text) - cn_chars
    return int(cn_chars / 1.5 + en_chars / 4)


def _generate_chunk_id(doc_id: str, version_no: int, chunk_index: int) -> str:
    raw = f"{doc_id}_v{version_no}_c{chunk_index:04d}"
    short = hashlib.sha256(raw.encode()).hexdigest()[:8].upper()
    return f"{doc_id}_v{version_no}_c{chunk_index:04d}_{short}"


class DocumentChunker:
    """文档切分器。"""

    def __init__(
        self,
        max_chunk_chars: int = 800,
        min_chunk_chars: int = 50,
        overlap_chars: int = 100,
        split_mode: str = "text",
        prepend_dept: bool = False,
        prepend_title: bool = False,
        prepend_section: bool = False,
        prepend_for_faq: bool = False,
        max_context_chars: int = 100,
        max_context_ratio: float = 0.3,
        parent_child: bool = False,
        child_max_chars: int = 150,
        child_overlap_chars: int = 40,
        row_card: bool = False,
        xlsx_layout_type: str = "normal_spreadsheet",
    ):
        self.max_chunk_chars = max_chunk_chars
        self.min_chunk_chars = min_chunk_chars
        self.overlap_chars = overlap_chars
        self.split_mode = split_mode
        self.prepend_dept = prepend_dept
        self.prepend_title = prepend_title
        self.prepend_section = prepend_section
        self.prepend_for_faq = prepend_for_faq
        self.max_context_chars = max_context_chars
        self.max_context_ratio = max_context_ratio
        self.parent_child = parent_child
        self.child_max_chars = child_max_chars
        self.child_overlap_chars = child_overlap_chars
        self.row_card_mode = row_card
        self.xlsx_layout_type = xlsx_layout_type
        if row_card:
            self.min_chunk_chars = min(self.min_chunk_chars, 20)

    def _create_chunk(
        self,
        doc_id: str,
        version_no: int,
        chunk_index: int,
        chunk_type: str,
        chunk_text: str,
        page_num: Optional[int] = None,
        section_title: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "native",
    ) -> Chunk:
        meta = metadata or {}
        raw_body = chunk_text.strip()

        # Check if context prepending is allowed for this chunk type
        is_faq = (chunk_type == "faq_chunk")
        allow_prepend = True
        if is_faq and not self.prepend_for_faq:
            allow_prepend = False

        has_any_prepend = (self.prepend_dept or self.prepend_title or self.prepend_section)
        
        prefix = ""
        if allow_prepend and has_any_prepend:
            # 1. Gather prefix parts
            dept_part = ""
            title_part = ""
            sect_part = ""

            if self.prepend_dept and meta.get("owner_dept"):
                dept_part = f"部门:{meta.get('owner_dept')}"
            if self.prepend_title and meta.get("title"):
                import os
                title_part = f"文档:{os.path.splitext(meta.get('title'))[0]}"
            if self.prepend_section and section_title:
                sect_part = f"章节:{section_title}"

            # Limit calculations
            body_len = len(raw_body)
            ratio_limit = int(body_len * self.max_context_ratio)
            
            # Combine the two limits (take minimum, but ensure at least 20 chars so that short chunks still get a basic prefix if allowed)
            limit = self.max_context_chars
            if ratio_limit > 0:
                limit = min(limit, max(20, ratio_limit))

            # Progressive component-based progressive truncation
            def assemble(d, t, s):
                active_parts = [p for p in [d, t, s] if p]
                if not active_parts:
                    return ""
                return f"【{' | '.join(active_parts)}】"

            full_prefix = assemble(dept_part, title_part, sect_part)
            
            if len(full_prefix) > limit:
                active_count = sum(1 for p in [dept_part, title_part, sect_part] if p)
                sep_overhead = 2 + (active_count - 1) * 3 if active_count > 0 else 0
                max_content_len = limit - sep_overhead

                if max_content_len > 0:
                    dept_len = len(dept_part)
                    remaining = max_content_len - dept_len
                    
                    if remaining > 0:
                        # Split remaining between title and section (40% title, 60% section)
                        title_budget = int(remaining * 0.4)
                        sect_budget = remaining - title_budget
                        
                        if len(title_part) < title_budget:
                            sect_budget += (title_budget - len(title_part))
                            title_budget = len(title_part)
                        elif len(sect_part) < sect_budget:
                            title_budget += (sect_budget - len(sect_part))
                            sect_budget = len(sect_part)

                        if title_part and len(title_part) > title_budget:
                            title_part = title_part[:max(5, title_budget - 3)] + "..."
                        if sect_part and len(sect_part) > sect_budget:
                            sect_part = sect_part[:max(5, sect_budget - 3)] + "..."
                    else:
                        dept_part = dept_part[:max(5, max_content_len - 3)] + "..."
                        title_part = ""
                        sect_part = ""

                    full_prefix = assemble(dept_part, title_part, sect_part)

            prefix = full_prefix

        # Combine text
        final_text = f"{prefix}\n{raw_body}" if prefix else raw_body

        return Chunk(
            chunk_id=_generate_chunk_id(doc_id, version_no, chunk_index),
            doc_id=doc_id,
            version_no=version_no,
            chunk_index=chunk_index,
            chunk_type=chunk_type,
            chunk_text=final_text,
            token_count=_estimate_tokens(final_text),
            raw_text=raw_body,
            context_prefix=prefix,
            embedding_text=final_text,
            page_num=page_num,
            section_title=section_title,
            source_oss_key=meta.get("source_oss_key"),
            source=source,
            title=meta.get("title"),
            owner_dept=meta.get("owner_dept"),
            category_l1=meta.get("category_l1"),
            category_l2=meta.get("category_l2"),
            permission_level=meta.get("permission_level"),
            kb_type=meta.get("kb_type"),
            risk_level=meta.get("risk_level"),
        )
    # ── 步骤边界检测正则 ──
    _STEP_BOUNDARY_RE = re.compile(
        r'^(?:'
        r'步骤\s*([一二三四五六七八九十\d]+)|'          # 步骤1 / 步骤三 / 步骤 2
        r'Step\s*(\d+)|'                             # Step 1 / Step2
        r'第\s*([一二三四五六七八九十\d]+)\s*步|'     # 第一步 / 第1步
        r'(\d+)\s*[\.．、]\s*(?![\d])|'              # 1. / 1．/ 2、（排除 1.1 条款编号）
        r'(\d+)\s*[)）]\s*|'                           # 1) / 2）
        r'(\d+\.\d+)\s+\S'                            # 1.2 / 3.2 / 4.2 条款编号步骤（后跟文字）
        r')',
        re.IGNORECASE | re.MULTILINE,
    )

    def _chunk_by_step(
        self,
        blocks: list,
        doc_id: str,
        version_no: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """
        SOP 步骤感知切分器。

        按步骤边界检测（步骤1 / 1. / 第一步 等），将步骤文字与紧随其后的
        image_ref 图片块绑定为 step_card chunk。同时生成一个 procedure_parent
        父 chunk 关联所有子步骤。

        切分策略：
          1. 扫描 blocks 序列，检测步骤边界和 image_ref 块
          2. 每个步骤文字 + 后续图片 = 一个 step_card
          3. 图片的 OCR 关键词和 annotation_map 展开拼入 chunk_text
          4. 生成 procedure_parent 总览 chunk
          5. 设置 prev/next chunk 链表

        未匹配步骤边界的前导/尾部文本退化为 text_chunk。
        """
        meta = metadata or {}
        chunks: List[Chunk] = []
        chunk_index = 0
        current_section: Optional[str] = None

        # 延迟导入 annotation_parser（避免循环依赖）
        try:
            from opensearch_pipeline.extraction.annotation_parser import (
                parse_annotation_map,
                expand_annotation_map,
                clean_ocr_keywords,
                extract_circled_refs,
            )
            from opensearch_pipeline.extraction.image_relation_classifier import (
                classify_image_relation,
            )
        except ImportError:
            # 容灾降级：如果 annotation_parser 不可用，使用空操作
            def parse_annotation_map(s, o): return {}
            def expand_annotation_map(m): return ""
            def clean_ocr_keywords(t, **kw): return ""
            def extract_circled_refs(s): return []
            classify_image_relation = None

        # ── Phase 1: 将 blocks 按步骤边界分组 ──
        # 每个 step_group = {"step_no": N, "title": str, "text_parts": [str],
        #                     "image_refs": [dict], "page_num": int, "section": str}
        step_groups = []
        preamble_texts = []       # 步骤前的前导文本
        postamble_texts = []      # 最后一个步骤后的尾部文本
        current_step = None
        found_any_step = False
        pending_images = []       # orphan images waiting for next step

        for block in blocks:
            # 兼容 dict 和 dataclass
            if isinstance(block, dict):
                block_type = block.get("block_type", "paragraph")
                text = block.get("text", "").strip()
                page_num = block.get("page_num")
                section_path = block.get("section_path")
                source = block.get("source", "native")
                extra = block.get("extra", {})
            else:
                block_type = block.block_type
                text = block.text.strip() if block.text else ""
                page_num = block.page_num
                section_path = block.section_path
                source = block.source
                extra = block.extra if hasattr(block, "extra") else {}

            # 更新 section 跟踪
            if block_type == "heading":
                current_section = section_path or text

                # ── 编号型操作标题也可能是步骤边界 ──
                # 财务手册模式: heading "3.2.4正常单据记账" → table → image
                # 工厂 SOP 模式: heading "1.2  若核对错误…" → 步骤
                # heading 文本匹配编号格式就算步骤开始，不依赖 found_any_step
                heading_step_match = re.match(
                    r'^(\d+(?:\.\d+)*)\s*[\.．、]?\s*\S', text
                )
                if heading_step_match:
                    found_any_step = True  # heading 也可以首次触发
                    if current_step is not None:
                        step_groups.append(current_step)
                    current_step = {
                        "step_no": 0,
                        "section_no": heading_step_match.group(1),
                        "title": text[:80],
                        "text_parts": [text],
                        "image_refs": list(pending_images),
                        "page_num": page_num,
                        "section": current_section,
                        "source": source,
                    }
                    pending_images.clear()
                continue

            # image_ref 块 → 归入当前步骤，或缓存到 pending 等待下一个步骤
            if block_type == "image_ref":
                if current_step is not None:
                    current_step["image_refs"].append(extra)
                else:
                    # 缓存 orphan images，等下一个步骤创建时归入
                    # 典型场景：跨页 table 关闭了步骤，但后续 image_ref 属于下一个步骤
                    pending_images.append(extra)
                continue

            if not text:
                continue

            # 表格 block → 归入当前步骤（同页），或独立 table_chunk（跨页/无步骤）
            if block_type == "table":
                # 跨页表格不应归入上一页的步骤（避免 page 2 分类表被归入 page 1 的 step 1.3）
                same_page = (current_step is not None and
                             current_step.get("page_num") == page_num)
                if current_step is not None and same_page:
                    current_step["text_parts"].append(text)
                else:
                    # 跨页时先结束上一个步骤组
                    if current_step is not None and not same_page:
                        step_groups.append(current_step)
                        current_step = None
                    chunks.append(self._create_chunk(
                        doc_id=doc_id,
                        version_no=version_no,
                        chunk_index=chunk_index,
                        chunk_type="table_chunk",
                        chunk_text=text,
                        page_num=page_num,
                        section_title=current_section,
                        metadata=meta,
                        source=source,
                    ))
                    chunk_index += 1
                continue

            # ocr_text block → 来自图片 OCR，直接跳过
            # 这些文本已经通过 image_ref 的 ocr_text/visual_summary 元数据保留
            # 不应塞入步骤文本（会产生大量垃圾：重复圈号、ERP 菜单项等）
            if block_type == "ocr_text":
                continue

            # ── 检测步骤边界 ──
            # 用 finditer 找到文本中的 *所有* 步骤标记
            # 然后按步骤标记位置拆分成多段，逐段处理
            # 这修复了一个 paragraph 包含多个步骤（如 "步骤2：… 步骤3：…"）时
            # 只识别第一个步骤标记的问题
            all_matches = list(self._STEP_BOUNDARY_RE.finditer(text))

            if not all_matches:
                # 无步骤标记 → 归入当前上下文
                if current_step is not None:
                    current_step["text_parts"].append(text)
                elif found_any_step:
                    postamble_texts.append((text, page_num, current_section, source))
                else:
                    preamble_texts.append((text, page_num, current_section, source))
                continue

            # ── 按步骤标记拆分文本为多段 ──
            segments = []  # [(match, segment_text)]
            for mi, m in enumerate(all_matches):
                seg_start = m.start()
                seg_end = all_matches[mi + 1].start() if mi + 1 < len(all_matches) else len(text)
                segments.append((m, text[seg_start:seg_end].strip()))

            # 第一个步骤标记前的文本 = prefix（前言/尾部）
            prefix_text = text[:all_matches[0].start()].strip()
            if prefix_text:
                if current_step is not None:
                    current_step["text_parts"].append(prefix_text)
                elif found_any_step:
                    postamble_texts.append((prefix_text, page_num, current_section, source))
                else:
                    preamble_texts.append((prefix_text, page_num, current_section, source))

            # ── 逐段创建步骤组 ──
            for match, seg_text in segments:
                if not seg_text:
                    continue

                # 提取步骤编号（6 个捕获组）
                step_no_str = (match.group(1) or match.group(2) or
                               match.group(3) or match.group(4) or
                               match.group(5) or match.group(6))

                # ── 过滤误判：通用编号格式（N. / N、/ N) / X.Y）需要足够长的文本 ──
                # 避免将材料清单 "4. 胶带" 误判为步骤。
                # 明确的步骤标记（步骤N / Step N / 第N步）不受此限制。
                is_generic_numbering = match.group(4) or match.group(5) or match.group(6)
                if is_generic_numbering and len(seg_text) < 15:
                    # 太短，当普通文本处理
                    if current_step is not None:
                        current_step["text_parts"].append(seg_text)
                    elif found_any_step:
                        postamble_texts.append((seg_text, page_num, current_section, source))
                    else:
                        preamble_texts.append((seg_text, page_num, current_section, source))
                    continue

                found_any_step = True
                try:
                    cn_num = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                    if step_no_str in cn_num:
                        step_no = cn_num[step_no_str]
                    else:
                        step_no = int(step_no_str)
                except (ValueError, TypeError):
                    step_no = len(step_groups) + 1

                # 结束上一个步骤组
                if current_step is not None:
                    step_groups.append(current_step)

                # 开始新步骤组，并吸收 pending orphan images
                current_step = {
                    "step_no": step_no,
                    "title": seg_text[:80],
                    "text_parts": [seg_text],
                    "image_refs": list(pending_images),
                    "page_num": page_num,
                    "section": current_section,
                    "source": source,
                }
                pending_images.clear()

        # 结束最后一个步骤组
        if current_step is not None:
            step_groups.append(current_step)

        # ── 如果没有检测到任何步骤边界，fallback 到普通文本切分 ──
        if not step_groups:
            return self.chunk_from_blocks.__wrapped__(self, blocks, doc_id, version_no, metadata) \
                if hasattr(self.chunk_from_blocks, '__wrapped__') \
                else self._chunk_text_fallback(blocks, doc_id, version_no, metadata)

        # ── Phase 2: 处理前导文本（非步骤部分） ──
        for text, pg, sect, src in preamble_texts:
            stripped = text.strip()
            if len(stripped) < self.min_chunk_chars:
                continue
            sub_texts = self._split_long_text(stripped)
            for sub in sub_texts:
                if len(sub.strip()) < self.min_chunk_chars:
                    continue
                chunks.append(self._create_chunk(
                    doc_id=doc_id, version_no=version_no,
                    chunk_index=chunk_index, chunk_type="text_chunk",
                    chunk_text=sub.strip(), page_num=pg,
                    section_title=sect, metadata=meta, source=src,
                ))
                chunk_index += 1

        # ── Phase 2.5: 按 section 分组 step_groups ──
        # 多 section 文档（如财务手册）每个 section 有独立步骤序列，
        # 需要生成多个 procedure_parent，而非一个扁平 parent。
        section_groups: Dict[str, List[dict]] = {}
        for sg in step_groups:
            sec_key = sg.get("section") or "__default__"
            section_groups.setdefault(sec_key, []).append(sg)

        # ── Phase 3 ~ 6: 按 section 批次生成 step_card + procedure_parent ──
        all_step_card_chunks = []  # 跨 section 收集所有 step_card
        all_step_card_ids = []
        all_step_titles = []
        first_step_page = None
        first_step_section = None
        for section_key, sec_steps in section_groups.items():
            step_card_chunks_sec = []
            step_card_ids_sec = []
            vk_images_sec = []   # visual_knowledge 图片（保留引用 + 复制生成独立 chunk）

            for sg in sec_steps:
                step_text = "\n".join(sg["text_parts"])

                # 收集图片的 OCR 文本和资产信息
                image_refs_list = []
                all_ocr_raw = []
                all_visual_summaries = []

                for img_extra in sg["image_refs"]:
                    img_ref_entry = {
                        "image_index": img_extra.get("image_index"),
                        "source_image": img_extra.get("source_image", ""),
                        "oss_key": img_extra.get("oss_key", ""),
                    }
                    # 透传 VLM 结构化字段
                    for vk in ("image_category", "vlm_annotation_map"):
                        if img_extra.get(vk):
                            img_ref_entry[vk] = img_extra[vk]
                    image_refs_list.append(img_ref_entry)

                    ocr_text = img_extra.get("ocr_text", "")
                    if ocr_text:
                        all_ocr_raw.append(ocr_text)
                    visual_summary = img_extra.get("visual_summary", "")
                    # visual_summary 仅用于 annotation_map 解析，不混入 ocr_keywords
                    # 避免与 [图片内容]/[补充图示] 三重冗余
                    if visual_summary:
                        all_visual_summaries.append(visual_summary)

                # 解析 annotation_map（需要 OCR + visual_summary 全量文本）
                combined_all = " ".join(all_ocr_raw + all_visual_summaries)
                annotation_map = parse_annotation_map(step_text, combined_all)
                annotation_text = expand_annotation_map(annotation_map)
                # ocr_keywords 仅从 OCR 原始文本提取，不包含 VLM caption
                ocr_keywords = clean_ocr_keywords(" ".join(all_ocr_raw))

                # ── Phase 3.5: 图片-步骤 relation 分类 ──
                primary_captions = []
                supporting_captions = []
                audit_flags = []

                if classify_image_relation is not None:
                    for img_ref in image_refs_list:
                        # 取该图片的 caption（优先 visual_summary，fallback ocr_text）
                        img_idx = img_ref.get("image_index")
                        img_caption = ""
                        img_ocr = ""
                        for ie in sg["image_refs"]:
                            if ie.get("image_index") == img_idx:
                                img_caption = ie.get("visual_summary", "")
                                img_ocr = ie.get("ocr_text", "")
                                break
                        caption = img_caption or img_ocr

                        rel = classify_image_relation(
                            step_text=step_text,
                            caption=caption,
                            ocr_keywords=img_ocr,
                            has_annotation=bool(annotation_map),
                            position="inline",
                        )
                        img_ref["relation"] = rel.relation
                        img_ref["relation_confidence"] = rel.confidence
                        if caption:
                            img_ref["caption"] = caption

                        # 低置信度标记 audit
                        if rel.audit_flag:
                            audit_flags.append({
                                "image_index": img_idx,
                                "relation": rel.relation,
                                "confidence": rel.confidence,
                                "reason": rel.reason,
                            })

                        # 收集 caption 用于追加到 chunk_text
                        if caption and not annotation_map:
                            if rel.relation == "primary":
                                primary_captions.append(caption)
                            elif rel.relation == "supporting":
                                supporting_captions.append(caption)
                            # visual_knowledge: 保留引用在 step_card，同时记录以生成独立 chunk
                            if rel.relation == "visual_knowledge":
                                supporting_captions.append(caption)  # step_card 内也追加
                                vk_images_sec.append({
                                    "image_index": img_idx,
                                    "source_image": img_ref.get("source_image", ""),
                                    "oss_key": img_ref.get("oss_key", ""),
                                    "caption": caption,
                                    "context_step_no": sg["step_no"],
                                    "context_section": sg["section"],
                                })

                # 组装 chunk_text
                parts = [step_text]
                if annotation_text:
                    parts.append(annotation_text)
                if primary_captions:
                    parts.append("[图片内容] " + "；".join(c[:120] for c in primary_captions))
                if supporting_captions:
                    parts.append("[补充图示] " + "；".join(c[:120] for c in supporting_captions))
                if ocr_keywords:
                    parts.append(f"[图片OCR] {ocr_keywords}")

                final_chunk_text = "\n".join(parts)

                # ── step_card 超长保护：超过 max_chunk_chars 时拆分 ──
                if len(final_chunk_text) > self.max_chunk_chars and len(parts) > 1:
                    # 策略：主 chunk 保留 step_text + annotation，
                    # 补充内容（图片描述、OCR）拆分为 step_card_continued
                    core_parts = [parts[0]]  # step_text 始终保留
                    supplement_parts = []
                    for p in parts[1:]:
                        candidate = "\n".join(core_parts + [p])
                        if len(candidate) <= self.max_chunk_chars:
                            core_parts.append(p)
                        else:
                            supplement_parts.append(p)

                    final_chunk_text = "\n".join(core_parts)

                    # 生成补充 chunks（如有溢出内容）
                    if supplement_parts:
                        supplement_text = "\n".join(supplement_parts)
                        # 进一步拆分超长补充文本
                        supp_sub_texts = self._split_long_text(supplement_text) if len(supplement_text) > self.max_chunk_chars else [supplement_text]
                        for supp_sub in supp_sub_texts:
                            supp_sub = supp_sub.strip()
                            if len(supp_sub) < self.min_chunk_chars:
                                continue
                            supp_chunk = self._create_chunk(
                                doc_id=doc_id, version_no=version_no,
                                chunk_index=chunk_index, chunk_type="step_card",
                                chunk_text=supp_sub, page_num=sg["page_num"],
                                section_title=sg["section"], metadata=meta,
                                source=sg.get("source", "native"),
                            )
                            supp_chunk.extra["step_no"] = sg["step_no"]
                            supp_chunk.extra["is_step_continuation"] = True
                            supp_chunk.extra["image_refs"] = image_refs_list
                            if annotation_map:
                                supp_chunk.extra["annotation_map"] = annotation_map
                            step_card_chunks_sec.append(supp_chunk)
                            step_card_ids_sec.append(supp_chunk.chunk_id)
                            chunk_index += 1

                step_chunk = self._create_chunk(
                    doc_id=doc_id, version_no=version_no,
                    chunk_index=chunk_index, chunk_type="step_card",
                    chunk_text=final_chunk_text, page_num=sg["page_num"],
                    section_title=sg["section"], metadata=meta,
                    source=sg.get("source", "native"),
                )
                step_chunk.extra["step_no"] = sg["step_no"]
                if sg.get("section_no"):
                    step_chunk.extra["section_no"] = sg["section_no"]
                step_chunk.extra["image_refs"] = image_refs_list
                if annotation_map:
                    step_chunk.extra["annotation_map"] = annotation_map
                if all_ocr_raw:
                    step_chunk.extra["image_ocr_raw"] = combined_all[:2000]
                if audit_flags:
                    step_chunk.extra["relation_audit"] = audit_flags
                # 记录步骤中引用的圈数字标注（①②③...）
                circled = extract_circled_refs(step_text)
                if circled:
                    step_chunk.extra["circled_refs"] = circled

                step_card_chunks_sec.append(step_chunk)
                step_card_ids_sec.append(step_chunk.chunk_id)
                chunk_index += 1

            # Phase 4: 设置 prev/next 链表（section 内）
            for i, sc in enumerate(step_card_chunks_sec):
                sc.extra["prev_chunk_id"] = step_card_ids_sec[i - 1] if i > 0 else None
                sc.extra["next_chunk_id"] = step_card_ids_sec[i + 1] if i < len(step_card_ids_sec) - 1 else None

            # _step_label 用于统一 parent 中的步骤标题生成
            def _step_label(sg):
                """生成步骤标签：heading 派生用 section_no，普通步骤用 step_no。"""
                sno = sg.get("section_no")
                if sg["step_no"] == 0 and sno:
                    return f"{sno} {sg['title'][:40]}"
                return f"步骤{sg['step_no']}：{sg['title'][:40]}"

            # Phase 5.5: 生成 visual_knowledge 独立 chunk（保留引用 + 复制）
            doc_title = meta.get("title", "")
            for vk in vk_images_sec:
                vk_text = f"【文档:{doc_title}】\n[参考图] {vk['caption'][:300]}"
                vk_chunk = self._create_chunk(
                    doc_id=doc_id, version_no=version_no,
                    chunk_index=chunk_index, chunk_type="visual_knowledge",
                    chunk_text=vk_text, page_num=None,
                    section_title=vk["context_section"], metadata=meta,
                )
                vk_chunk.extra["source_image"] = vk.get("source_image", "")
                vk_chunk.extra["oss_key"] = vk.get("oss_key", "")
                vk_chunk.extra["caption"] = vk["caption"]
                vk_chunk.extra["context_step_no"] = vk["context_step_no"]
                vk_chunk.extra["context_section"] = vk["context_section"]
                vk_chunk.extra["image_index"] = vk["image_index"]
                chunks.append(vk_chunk)
                chunk_index += 1

            # Phase 6: 追加 step_card 到结果（parent 在循环外统一生成）
            all_step_card_chunks.extend(step_card_chunks_sec)
            all_step_card_ids.extend(step_card_ids_sec)
            all_step_titles.extend(
                [_step_label(sg) for sg in sec_steps]
            )
            # 记录首个 section 的页码和 section 名
            if first_step_page is None and sec_steps:
                first_step_page = sec_steps[0]["page_num"]
                first_step_section = sec_steps[0].get("section") or current_section

        # ── Phase 4.9: 前导文本（步骤前的元信息/目的/职责/检验频率等）→ 独立 text_chunk ──
        # 这些非步骤段落既会被 procedure_parent 摘要引用，也单独成块，
        # 以便"文档编号/职责/检验频率"等查询能精确命中（而非只匹配到 parent）。
        if preamble_texts:
            pre_full = "\n".join(t.strip() for t, _, _, _ in preamble_texts if t and t.strip())
            pg0, sect0, src0 = preamble_texts[0][1], preamble_texts[0][2], preamble_texts[0][3]
            for sub in self._split_long_text(pre_full):
                if len(sub.strip()) < self.min_chunk_chars:
                    continue
                chunks.append(self._create_chunk(
                    doc_id=doc_id, version_no=version_no,
                    chunk_index=chunk_index, chunk_type="text_chunk",
                    chunk_text=sub.strip(), page_num=pg0,
                    section_title=sect0, metadata=meta, source=src0,
                ))
                chunk_index += 1

        # ── Phase 5 (unified): 生成唯一 procedure_parent ──
        # 将所有 section 的步骤合并为一个 parent，避免多 section SOP 产生冗余 parent
        if all_step_card_chunks:
            doc_title = meta.get("title", "")
            parent_text = f"{doc_title}"

            # 从前导文本中提取"目的和范围"/"适用范围"等描述，提升 embedding 质量
            preamble_summary = ""
            for pt_text, _, _, _ in preamble_texts:
                pt_stripped = pt_text.strip()
                if any(kw in pt_stripped for kw in ("目的", "范围", "适用", "概述", "简介", "用于")):
                    preamble_summary = pt_stripped[:200]
                    break
            # 没有关键词命中时，取第一段非空前导文本作为上下文
            if not preamble_summary and preamble_texts:
                for pt_text, _, _, _ in preamble_texts:
                    pt_stripped = pt_text.strip()
                    if len(pt_stripped) >= 20:
                        preamble_summary = pt_stripped[:200]
                        break
            if preamble_summary:
                parent_text += f"\n{preamble_summary}"

            parent_text += "\n" + "\n".join(all_step_titles)

            parent_chunk = self._create_chunk(
                doc_id=doc_id, version_no=version_no,
                chunk_index=chunk_index, chunk_type="procedure_parent",
                chunk_text=parent_text,
                page_num=first_step_page,
                section_title=first_step_section or "",
                metadata=meta,
            )
            parent_chunk.extra["child_chunk_ids"] = all_step_card_ids
            parent_chunk.extra["step_count"] = len(all_step_card_ids)
            parent_chunk_id = parent_chunk.chunk_id
            chunk_index += 1

            # 回填 parent_chunk_id
            for sc in all_step_card_chunks:
                sc.extra["parent_chunk_id"] = parent_chunk_id

            chunks.append(parent_chunk)
            chunks.extend(all_step_card_chunks)

        # ── Phase 7: 处理尾部文本 ──
        for text, pg, sect, src in postamble_texts:
            stripped = text.strip()
            if len(stripped) < self.min_chunk_chars:
                continue
            sub_texts = self._split_long_text(stripped)
            for sub in sub_texts:
                if len(sub.strip()) < self.min_chunk_chars:
                    continue
                chunks.append(self._create_chunk(
                    doc_id=doc_id, version_no=version_no,
                    chunk_index=chunk_index, chunk_type="text_chunk",
                    chunk_text=sub.strip(), page_num=pg,
                    section_title=sect, metadata=meta, source=src,
                ))
                chunk_index += 1

        return chunks

    def _chunk_by_slide(
        self,
        blocks: list,
        doc_id: str,
        version_no: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """按幻灯片切块（PPTX）。每页 slide 合并为一个 chunk，保留 page_num 作来源定位。

        - 以表格为主的 slide → table_chunk
        - 其余 → text_chunk
        含产品图/示意图的 slide 由 node 按 page_num 绑定图片后升级为 visual_knowledge。
        """
        from collections import OrderedDict
        meta = metadata or {}
        chunks: List[Chunk] = []
        chunk_index = 0

        by_slide: "OrderedDict[Any, dict]" = OrderedDict()
        for block in blocks:
            if isinstance(block, dict):
                bt = block.get("block_type", "paragraph")
                txt = (block.get("text") or "").strip()
                pg = block.get("page_num")
                sect = block.get("section_path")
                src = block.get("source", "native")
            else:
                bt = block.block_type
                txt = (block.text or "").strip()
                pg = block.page_num
                sect = block.section_path
                src = block.source
            if bt == "image_ref" or not txt:
                continue
            key = pg if pg is not None else 0
            if key not in by_slide:
                by_slide[key] = {"texts": [], "has_table": False, "section": sect, "src": src, "page": pg}
            if bt == "table":
                by_slide[key]["has_table"] = True
            if bt == "heading" and not by_slide[key]["section"]:
                by_slide[key]["section"] = txt
            by_slide[key]["texts"].append(txt)

        for sl in by_slide.values():
            combined = "\n".join(sl["texts"]).strip()
            if not combined:
                continue
            ctype = "table_chunk" if sl["has_table"] else "text_chunk"
            for sub in self._split_long_text(combined):
                if not sub.strip():
                    continue
                chunks.append(self._create_chunk(
                    doc_id=doc_id, version_no=version_no,
                    chunk_index=chunk_index, chunk_type=ctype,
                    chunk_text=sub.strip(), page_num=sl["page"],
                    section_title=sl["section"], metadata=meta, source=sl["src"],
                ))
                chunk_index += 1
        return chunks

    def _chunk_text_fallback(
        self,
        blocks: list,
        doc_id: str,
        version_no: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """当 step 模式未检测到步骤边界时，fallback 到标准文本切分。"""
        saved = self.split_mode
        self.split_mode = "text"
        try:
            result = self.chunk_from_blocks(blocks, doc_id, version_no, metadata)
        finally:
            self.split_mode = saved
        return result

    def _chunk_faq(
        self,
        blocks: list,
        doc_id: str,
        version_no: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """
        启发式 FAQ 提取器。
        扫描提取出的段落 blocks，根据 Q/A 前缀标志或中文问答标志提取问题与答案对，并封装为 faq_chunk。
        未匹配的头部/尾部文本段落会安全退化为 text_chunk 提交，防止信息丢失。
        """
        meta = metadata or {}
        chunks: List[Chunk] = []
        chunk_index = 0

        # Heuristic patterns for Questions and Answers
        _Q_PATTERNS = [
            r'^(?:Q|q)[:：]\s*(.*)',
            r'^(?:问|问题)[:：]\s*(.*)',
            r'^\[问\]\s*(.*)',
            r'^(?:【问】|\[问\])\s*(.*)',
        ]
        _A_PATTERNS = [
            r'^(?:A|a)[:：]\s*(.*)',
            r'^(?:答|回答)[:：]\s*(.*)',
            r'^\[答\]\s*(.*)',
            r'^(?:【答】|\[答\])\s*(.*)',
        ]

        pending_paragraphs = []
        current_q_text = None
        current_q_page = None
        current_q_section = None
        current_q_source = None

        def commit_pending_text():
            nonlocal chunk_index
            if not pending_paragraphs:
                return
            merged_text = "\n\n".join(pending_paragraphs)
            pending_paragraphs.clear()
            sub_texts = self._split_long_text(merged_text.strip())
            for sub in sub_texts:
                if len(sub.strip()) < self.min_chunk_chars:
                    continue
                chunks.append(self._create_chunk(
                    doc_id=doc_id,
                    version_no=version_no,
                    chunk_index=chunk_index,
                    chunk_type="text_chunk",
                    chunk_text=sub,
                    page_num=current_q_page,
                    section_title=current_q_section,
                    metadata=meta,
                    source=current_q_source or "native",
                ))
                chunk_index += 1

        for block in blocks:
            if isinstance(block, dict):
                block_type = block.get("block_type", "paragraph")
                text = block.get("text", "").strip()
                page_num = block.get("page_num")
                section_path = block.get("section_path")
                source = block.get("source", "native")
            else:
                block_type = block.block_type
                text = block.text.strip()
                page_num = block.page_num
                section_path = block.section_path
                source = block.source

            if not text:
                continue

            # Heading/Table elements break FAQ continuity, flush buffers
            if block_type in ("heading", "table"):
                commit_pending_text()
                if current_q_text:
                    pending_paragraphs.append(current_q_text)
                    commit_pending_text()
                    current_q_text = None

                if block_type == "table":
                    chunks.append(self._create_chunk(
                        doc_id=doc_id,
                        version_no=version_no,
                        chunk_index=chunk_index,
                        chunk_type="table_chunk",
                        chunk_text=text,
                        page_num=page_num,
                        section_title=section_path or current_q_section,
                        metadata=meta,
                        source=source,
                    ))
                    chunk_index += 1
                elif block_type == "heading":
                    current_q_section = section_path or text
                continue

            # Check if Question
            is_q = False
            for p in _Q_PATTERNS:
                if re.match(p, text):
                    is_q = True
                    break

            # Fallback Q: Starts with standard digit prefix and ends with a question mark
            if not is_q:
                if re.match(r'^(?:\d+[\.、\s]|[(（]\d+[)）])', text) and (text.endswith('?') or text.endswith('？')):
                    is_q = True

            # Check if Answer
            is_a = False
            for p in _A_PATTERNS:
                if re.match(p, text):
                    is_a = True
                    break

            if is_q:
                commit_pending_text()
                if current_q_text:
                    pending_paragraphs.append(current_q_text)
                    commit_pending_text()
                current_q_text = text
                current_q_page = page_num
                current_q_section = section_path or current_q_section
                current_q_source = source
            elif is_a:
                if current_q_text:
                    commit_pending_text()
                    faq_text = f"{current_q_text}\n{text}"
                    chunks.append(self._create_chunk(
                        doc_id=doc_id,
                        version_no=version_no,
                        chunk_index=chunk_index,
                        chunk_type="faq_chunk",
                        chunk_text=faq_text,
                        page_num=current_q_page,
                        section_title=current_q_section,
                        metadata=meta,
                        source=current_q_source or "native",
                    ))
                    chunk_index += 1
                    current_q_text = None
                else:
                    pending_paragraphs.append(text)
            else:
                if current_q_text:
                    # Alternating paragraph immediately after Question is treated as Answer
                    faq_text = f"{current_q_text}\n{text}"
                    chunks.append(self._create_chunk(
                        doc_id=doc_id,
                        version_no=version_no,
                        chunk_index=chunk_index,
                        chunk_type="faq_chunk",
                        chunk_text=faq_text,
                        page_num=current_q_page,
                        section_title=current_q_section,
                        metadata=meta,
                        source=current_q_source or "native",
                    ))
                    chunk_index += 1
                    current_q_text = None
                else:
                    pending_paragraphs.append(text)

        commit_pending_text()
        if current_q_text:
            pending_paragraphs.append(current_q_text)
            commit_pending_text()

        return chunks

    def _chunk_by_clause(
        self,
        blocks: list,
        doc_id: str,
        version_no: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """
        条款感知切分器。
        按条款边界（第X条、一、二、、（一）、9.1 等）切分，
        保持法规/制度条款完整性，避免跨条款断裂。
        超长条款仍 fallback 到 _split_long_text。
        """
        meta = metadata or {}
        chunks: List[Chunk] = []
        chunk_index = 0
        current_section: Optional[str] = None

        # 条款边界检测 regex
        _CLAUSE_RE = re.compile(
            r'^(?:'
            r'第[一二三四五六七八九十百零\d]+[章节条款部分编]|'  # 第X章/第X条
            r'[一二三四五六七八九十]+[、\.]|'  # 一、二、
            r'\d+[、]|'  # 1、2、…（阿拉伯数字+顿号，中文制度/规范常用枚举）
            r'（[一二三四五六七八九十\d]+）|'  # （一）（二）
            r'[a-zA-Z][）)]|'  # a）b) 子条款
            r'\d+\.\d+(?:\.\d+)?\s'  # 9.1 9.2 or 2.2.1
            r')',
            re.MULTILINE,
        )

        # 1. Collect all paragraph text and handle tables/headings
        all_para_texts: List[str] = []
        table_chunks: List[Chunk] = []
        pending_image_refs: List[dict] = []  # 暂存 image_ref 块

        for block in blocks:
            if isinstance(block, dict):
                block_type = block.get("block_type", "paragraph")
                text = block.get("text", "").strip()
                page_num = block.get("page_num")
                section_path = block.get("section_path")
                source = block.get("source", "native")
                extra = block.get("extra", {})
            else:
                block_type = block.block_type
                text = block.text.strip()
                page_num = block.page_num
                section_path = block.section_path
                source = block.source
                extra = block.extra if hasattr(block, "extra") else {}

            # image_ref 块 → 暂存图片元数据
            if block_type == "image_ref":
                if extra:
                    pending_image_refs.append(dict(extra))
                continue

            if not text:
                continue

            if block_type == "heading":
                current_section = section_path or text
                continue

            if block_type == "table":
                table_chunks.append(self._create_chunk(
                    doc_id=doc_id,
                    version_no=version_no,
                    chunk_index=chunk_index,
                    chunk_type="table_chunk",
                    chunk_text=text,
                    page_num=page_num,
                    section_title=current_section,
                    metadata=meta,
                    source=source,
                ))
                chunk_index += 1
                continue

            all_para_texts.append(text)

        # 2. Join all paragraphs and split by clause boundaries
        full_text = "\n".join(all_para_texts)
        if not full_text.strip():
            return self._dedup_table_chunks(table_chunks)

        matches = list(_CLAUSE_RE.finditer(full_text))

        if not matches:
            # No clause boundaries found — fallback to standard text splitting
            sub_texts = self._split_long_text(full_text.strip())
            for sub in sub_texts:
                if len(sub.strip()) < self.min_chunk_chars:
                    continue
                chunks.append(self._create_chunk(
                    doc_id=doc_id,
                    version_no=version_no,
                    chunk_index=chunk_index,
                    chunk_type="text_chunk",
                    chunk_text=sub.strip(),
                    section_title=current_section,
                    metadata=meta,
                ))
                chunk_index += 1
            return self._dedup_table_chunks(table_chunks + chunks)

        # 3. Build clause segments
        clause_segments: List[str] = []

        # Preamble before first clause
        if matches[0].start() > 0:
            preamble = full_text[:matches[0].start()].strip()
            if preamble:
                clause_segments.append(preamble)

        for i, match in enumerate(matches):
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
            segment = full_text[start:end].strip()
            if segment:
                clause_segments.append(segment)

        # 4. Merge short clauses and split oversized ones
        merged_segments: List[str] = []
        buffer = ""

        for seg in clause_segments:
            if buffer:
                candidate = buffer + "\n" + seg
            else:
                candidate = seg

            if len(candidate) <= self.max_chunk_chars:
                if len(seg) < self.min_chunk_chars and buffer:
                    # Merge short clause into buffer
                    buffer = candidate
                elif len(seg) < self.min_chunk_chars and not buffer:
                    buffer = seg
                else:
                    if buffer and buffer != candidate:
                        merged_segments.append(buffer)
                    buffer = seg
            else:
                # Current buffer is full, commit it
                if buffer:
                    merged_segments.append(buffer)
                buffer = seg

        if buffer:
            merged_segments.append(buffer)

        # 5. Create chunks from segments with inter-clause context
        # 提取每个条款的标题行（第一行），追加到下一个条款作为语义 overlap
        prev_clause_title = ""  # 上一条款标题，用于跨条款上下文

        for seg_idx, seg in enumerate(merged_segments):
            # 提取当前条款标题（第一行，截断 60 字符）
            first_line = seg.split("\n")[0].strip()[:60]

            if len(seg) > self.max_chunk_chars:
                sub_texts = self._split_long_text(seg)
                for sub in sub_texts:
                    if len(sub.strip()) < self.min_chunk_chars:
                        continue
                    # 仅对该条款的第一个子 chunk 追加上文前缀
                    clause_text = sub.strip()
                    if prev_clause_title and sub == sub_texts[0]:
                        context_line = f"[上文] {prev_clause_title}"
                        clause_text = f"{context_line}\n{clause_text}"
                    chunks.append(self._create_chunk(
                        doc_id=doc_id,
                        version_no=version_no,
                        chunk_index=chunk_index,
                        chunk_type="clause_chunk",
                        chunk_text=clause_text,
                        section_title=current_section,
                        metadata=meta,
                    ))
                    chunk_index += 1
            else:
                if len(seg.strip()) < self.min_chunk_chars:
                    prev_clause_title = first_line
                    continue
                clause_text = seg.strip()
                if prev_clause_title:
                    context_line = f"[上文] {prev_clause_title}"
                    clause_text = f"{context_line}\n{clause_text}"
                chunks.append(self._create_chunk(
                    doc_id=doc_id,
                    version_no=version_no,
                    chunk_index=chunk_index,
                    chunk_type="clause_chunk",
                    chunk_text=clause_text,
                    section_title=current_section,
                    metadata=meta,
                ))
                chunk_index += 1

            prev_clause_title = first_line

        # 将暂存的 image_refs 附加到最后一个 chunk
        all_result = table_chunks + chunks
        if pending_image_refs and all_result:
            last = all_result[-1]
            existing = last.extra.get("image_refs", [])
            last.extra["image_refs"] = existing + pending_image_refs
            captions = [r.get("visual_summary", "") for r in pending_image_refs if r.get("visual_summary")]
            if captions:
                suffix = "\n[图片内容] " + "；".join(c[:120] for c in captions)
                last.chunk_text += suffix
                last.embedding_text = last.chunk_text
                last.token_count = _estimate_tokens(last.chunk_text)

        return self._dedup_table_chunks(all_result)

    @staticmethod
    def _dedup_table_chunks(chunks: List["Chunk"]) -> List["Chunk"]:
        """去除重复的 table_chunk（DOCX 页眉表格重复问题）。
        
        页眉表格通常仅在"页次/总页数"等细节不同，取首行作为签名。
        """
        seen: set = set()
        result: List["Chunk"] = []
        for c in chunks:
            if c.chunk_type == "table_chunk":
                first_line = c.chunk_text.split("\n")[0].strip()
                if first_line in seen:
                    continue
                seen.add(first_line)
            result.append(c)
        return result

    def chunk_from_blocks(
        self,
        blocks: list,
        doc_id: str,
        version_no: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """
        从 ExtractedBlock 列表生成 chunks（推荐入口）。

        与 chunk_document(text=...) 的区别：
        - 不需要 regex 重新检测标题/表格
        - block 的 page_num / section_path / source 直接传递给 chunk
        - 表格 block 整块作为 table_chunk，不会被按段落拆散

        Args:
            blocks: List[ExtractedBlock] 或 List[dict]
            doc_id: 文档 ID
            version_no: 版本号
            metadata: 继承到每个 chunk 的 metadata
        """
        if self.split_mode == "faq":
            return self._chunk_faq(blocks, doc_id, version_no, metadata)
        if self.split_mode == "clause":
            return self._chunk_by_clause(blocks, doc_id, version_no, metadata)
        if self.split_mode == "step":
            return self._chunk_by_step(blocks, doc_id, version_no, metadata)
        if self.split_mode == "slide":
            return self._chunk_by_slide(blocks, doc_id, version_no, metadata)
        if self.xlsx_layout_type == "procedure_image_guide":
            return self._chunk_procedure_steps(blocks, doc_id, version_no, metadata)
        if self.xlsx_layout_type == "product_spec_instruction":
            return self._chunk_product_spec(blocks, doc_id, version_no, metadata)

        meta = metadata or {}
        chunks: List[Chunk] = []
        chunk_index = 0
        current_section: Optional[str] = None
        
        # We need to buffer consecutive text blocks in the same section to merge them
        buffered_texts = []
        buffered_page_num = None
        buffered_source = "native"
        pending_image_refs = []  # 暂存 image_ref 块，等待附加到最近的 chunk

        def _attach_pending_images(target_chunk):
            """将 pending_image_refs 附加到目标 chunk。"""
            nonlocal pending_image_refs
            if not pending_image_refs or target_chunk is None:
                return
            target_chunk.extra["image_refs"] = list(pending_image_refs)
            suffix_parts = []
            for pr in pending_image_refs:
                vs = pr.get("visual_summary", "")
                if vs:
                    suffix_parts.append(f"[图片内容] {vs}")
                ocr_raw = pr.get("ocr_text", "")
                if ocr_raw:
                    try:
                        from opensearch_pipeline.extraction.annotation_parser import clean_ocr_keywords
                        cleaned = clean_ocr_keywords(ocr_raw)
                    except ImportError:
                        cleaned = ocr_raw.strip()
                    if cleaned:
                        suffix_parts.append(f"[图片OCR] {cleaned}")
            if suffix_parts:
                suffix = "\n" + "\n".join(suffix_parts)
                target_chunk.chunk_text += suffix
                target_chunk.embedding_text = target_chunk.chunk_text
                target_chunk.token_count = _estimate_tokens(target_chunk.chunk_text)
            pending_image_refs = []

        def commit_buffer():
            nonlocal chunk_index, buffered_texts, pending_image_refs
            if not buffered_texts:
                return

            # ── Row Card 模式：每行独立成 chunk，不合并 ──
            if self.row_card_mode:
                last_chunk = None
                # 提取设备/分类上下文，追加到短行文本以提升 embedding 区分度
                row_context = ""
                if current_section:
                    row_context = f"【{current_section}】"
                elif meta.get("title"):
                    import os
                    row_context = f"【{os.path.splitext(meta['title'])[0]}】"

                for para in buffered_texts:
                    para_stripped = para.strip()
                    if not para_stripped:
                        continue
                    # 对极短行追加设备上下文前缀，提升 embedding 区分度
                    enriched_text = para_stripped
                    if row_context and len(para_stripped) < 200:
                        enriched_text = f"{row_context}{para_stripped}"
                    chunk_type = "ocr_chunk" if buffered_source == "ocr" else "text_chunk"
                    chunk = self._create_chunk(
                        doc_id=doc_id,
                        version_no=version_no,
                        chunk_index=chunk_index,
                        chunk_type=chunk_type,
                        chunk_text=enriched_text,
                        page_num=buffered_page_num,
                        section_title=current_section,
                        metadata=meta,
                        source=buffered_source,
                    )
                    chunks.append(chunk)
                    last_chunk = chunk
                    chunk_index += 1
                # v2 fix 3: 如果本批次没有创建任何 chunk（全空行），
                # 把 pending images 挂到之前最后一个 chunk
                if last_chunk is None and chunks:
                    last_chunk = chunks[-1]
                _attach_pending_images(last_chunk)
                buffered_texts.clear()
                return

            # ── 原逻辑：合并短段落 ──
            merged_paras = self._merge_short_paragraphs(buffered_texts)
            merged_paras = self._merge_adjacent_short_chunks(merged_paras, min_chars=150)
            last_chunk = None
            for para in merged_paras:
                para_stripped = para.strip()
                if len(para_stripped) < self.min_chunk_chars:
                    continue
                sub_texts = self._split_long_text(para_stripped)
                for sub in sub_texts:
                    if len(sub.strip()) < self.min_chunk_chars:
                        continue
                    chunk_type = "ocr_chunk" if buffered_source == "ocr" else "text_chunk"
                    chunk = self._create_chunk(
                        doc_id=doc_id,
                        version_no=version_no,
                        chunk_index=chunk_index,
                        chunk_type=chunk_type,
                        chunk_text=sub.strip(),
                        page_num=buffered_page_num,
                        section_title=current_section,
                        metadata=meta,
                        source=buffered_source,
                    )
                    chunks.append(chunk)
                    last_chunk = chunk
                    chunk_index += 1
            _attach_pending_images(last_chunk)
            buffered_texts.clear()

        for block in blocks:
            # 兼容 dict 和 dataclass
            if isinstance(block, dict):
                block_type = block.get("block_type", "paragraph")
                text = block.get("text", "")
                page_num = block.get("page_num")
                section_path = block.get("section_path")
                source = block.get("source", "native")
                extra = block.get("extra", {})
            else:
                block_type = block.block_type
                text = block.text
                page_num = block.page_num
                section_path = block.section_path
                source = block.source
                extra = block.extra if hasattr(block, "extra") else {}

            # 更新 section 跟踪
            if block_type == "heading":
                commit_buffer()
                current_section = section_path or text
                continue  # heading 自身不生成 chunk，作为后续 chunk 的 section_title

            # image_ref 块 → flush buffer 后绑定到最近的 chunk
            # 当单个 chunk 图片过多时，溢出到新 chunk
            MAX_IMAGES_PER_CHUNK = 3

            if block_type == "image_ref":
                if extra:
                    img_entry = dict(extra)
                    # 先 flush 当前 buffer（图片前的文本生成 chunk）
                    if buffered_texts:
                        commit_buffer()

                    # 附加到最近的 chunk，但限制每 chunk 最多 MAX_IMAGES_PER_CHUNK 张
                    target_chunk = chunks[-1] if chunks else None
                    if target_chunk:
                        # Row Card 模式：放宽图片上限，不生成空 spillover chunk
                        img_limit = 8 if self.row_card_mode else MAX_IMAGES_PER_CHUNK
                        existing = target_chunk.extra.get("image_refs", [])
                        if len(existing) >= img_limit:
                            # 溢出：创建新的 image-only chunk
                            spillover = self._create_chunk(
                                doc_id=doc_id,
                                version_no=version_no,
                                chunk_index=chunk_index,
                                chunk_type="text_chunk",
                                chunk_text="",
                                page_num=target_chunk.page_num,
                                section_title=current_section,
                                metadata=meta,
                                source=buffered_source,
                            )
                            chunks.append(spillover)
                            chunk_index += 1
                            target_chunk = spillover

                        existing = target_chunk.extra.get("image_refs", [])
                        existing.append(img_entry)
                        target_chunk.extra["image_refs"] = existing

                        suffix_parts = []
                        vs = img_entry.get("visual_summary", "")
                        if vs:
                            suffix_parts.append(f"[图片内容] {vs}")
                        ocr_raw = img_entry.get("ocr_text", "")
                        if ocr_raw:
                            try:
                                from opensearch_pipeline.extraction.annotation_parser import clean_ocr_keywords
                                cleaned = clean_ocr_keywords(ocr_raw)
                            except ImportError:
                                cleaned = ocr_raw.strip()
                            if cleaned:
                                suffix_parts.append(f"[图片OCR] {cleaned}")

                        if suffix_parts:
                            suffix = "\n" + "\n".join(suffix_parts)
                            target_chunk.chunk_text += suffix
                            target_chunk.embedding_text = target_chunk.chunk_text
                            target_chunk.token_count = _estimate_tokens(target_chunk.chunk_text)
                    else:
                        pending_image_refs.append(img_entry)
                continue

            if not text:
                continue

            # Row Card 模式：跳过表头行和设备信息行
            if self.row_card_mode and extra.get("row_role") == "metadata":
                continue

            # v2 fix 4: Row Card 模式下，OCR 文本块（图片 OCR dump）不作为 row card
            # 把 pending images 挂到最近的 chunk
            if self.row_card_mode and source == "ocr":
                if pending_image_refs and chunks:
                    _attach_pending_images(chunks[-1])
                continue

            # 表格 block → 整块作为 table_chunk
            if block_type == "table":
                commit_buffer()
                chunk = self._create_chunk(
                    doc_id=doc_id,
                    version_no=version_no,
                    chunk_index=chunk_index,
                    chunk_type="table_chunk",
                    chunk_text=text.strip(),
                    page_num=page_num,
                    section_title=current_section,
                    metadata=meta,
                    source=source,
                )
                # 将暂存的 image_refs 附加到 table_chunk（XLSX sheet 图片绑定）
                if pending_image_refs:
                    chunk.extra["image_refs"] = list(pending_image_refs)
                    suffix_parts = []
                    for pr in pending_image_refs:
                        vs = pr.get("visual_summary", "")
                        if vs:
                            suffix_parts.append(f"[图片内容] {vs}")
                        ocr_raw = pr.get("ocr_text", "")
                        if ocr_raw:
                            try:
                                from opensearch_pipeline.extraction.annotation_parser import clean_ocr_keywords
                                cleaned = clean_ocr_keywords(ocr_raw)
                            except ImportError:
                                cleaned = ocr_raw.strip()
                            if cleaned:
                                suffix_parts.append(f"[图片OCR] {cleaned}")
                    if suffix_parts:
                        suffix = "\n" + "\n".join(suffix_parts)
                        chunk.chunk_text += suffix
                        chunk.embedding_text = chunk.chunk_text
                        chunk.token_count = _estimate_tokens(chunk.chunk_text)
                    pending_image_refs = []
                chunks.append(chunk)
                chunk_index += 1
                continue

            # 文本/OCR block → 放入缓冲区
            if buffered_texts and page_num != buffered_page_num:
                commit_buffer()

            if not buffered_texts:
                buffered_page_num = page_num
                buffered_source = source
            elif buffered_source != source:
                commit_buffer()
                buffered_page_num = page_num
                buffered_source = source
                
            buffered_texts.append(text.strip())

        commit_buffer()

        # 如果还有未附加的 image_refs（出现在所有文本之后），附加到最后一个 chunk
        if pending_image_refs and chunks:
            last = chunks[-1]
            existing = last.extra.get("image_refs", [])
            last.extra["image_refs"] = existing + pending_image_refs
            suffix_parts = []
            for pr in pending_image_refs:
                vs = pr.get("visual_summary", "")
                if vs:
                    suffix_parts.append(f"[图片内容] {vs}")
                ocr_raw = pr.get("ocr_text", "")
                if ocr_raw:
                    try:
                        from opensearch_pipeline.extraction.annotation_parser import clean_ocr_keywords
                        cleaned = clean_ocr_keywords(ocr_raw)
                    except ImportError:
                        cleaned = ocr_raw.strip()
                    if cleaned:
                        suffix_parts.append(f"[图片OCR] {cleaned}")
            if suffix_parts:
                suffix = "\n" + "\n".join(suffix_parts)
                last.chunk_text += suffix
                last.embedding_text = last.chunk_text
                last.token_count = _estimate_tokens(last.chunk_text)

        # ── Dedup: 去除重复的 table_chunk（DOCX 页眉表格重复问题）──
        chunks = self._dedup_table_chunks(chunks)

        if self.parent_child:
            all_chunks = []
            for parent in chunks:
                parent.extra["is_parent"] = True
                all_chunks.append(parent)
                all_chunks.extend(self._generate_child_chunks(parent))
            return all_chunks

        return chunks


    def chunk_document(
        self,
        text: str,
        doc_id: str,
        version_no: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """
        主入口：对文档文本做智能切分。

        策略：
        1. 先尝试按标题分节（## / 第X章 / 一、二、三）
        2. 节内按段落切
        3. 段落过长则按字符固定窗口切
        4. 检测表格块单独处理
        """
        meta = metadata or {}
        sections = self._split_by_sections(text)
        chunks: List[Chunk] = []
        chunk_index = 0

        for section_title, section_text in sections:
            # 检测表格
            table_blocks, text_blocks = self._separate_tables(section_text)

            # 切分文本块
            for block in text_blocks:
                paragraphs = self._split_by_paragraphs(block)
                merged = self._merge_short_paragraphs(paragraphs)
                merged = self._merge_adjacent_short_chunks(merged, min_chars=150)

                for para in merged:
                    para_stripped = para.strip()
                    if len(para_stripped) < self.min_chunk_chars:
                        continue

                    sub_chunks = self._split_long_text(para_stripped)
                    for sub in sub_chunks:
                        chunk = self._create_chunk(
                            doc_id=doc_id,
                            version_no=version_no,
                            chunk_index=chunk_index,
                            chunk_type="text_chunk",
                            chunk_text=sub.strip(),
                            page_num=None,
                            section_title=section_title,
                            metadata=meta,
                            source=meta.get("source", "native"),
                        )
                        chunks.append(chunk)
                        chunk_index += 1

            # 表格块
            for table_text in table_blocks:
                chunk = self._create_chunk(
                    doc_id=doc_id,
                    version_no=version_no,
                    chunk_index=chunk_index,
                    chunk_type="table_chunk",
                    chunk_text=table_text.strip(),
                    page_num=None,
                    section_title=section_title,
                    metadata=meta,
                    source=meta.get("source", "native"),
                )
                chunks.append(chunk)
                chunk_index += 1

        if self.parent_child:
            all_chunks = []
            for parent in chunks:
                parent.extra["is_parent"] = True
                all_chunks.append(parent)
                all_chunks.extend(self._generate_child_chunks(parent))
            return all_chunks

        return chunks

    def _split_by_sections(self, text: str) -> List[tuple]:
        """按标题层级切分：支持 Markdown 标题、中文序号标题。"""
        # 匹配模式：## 标题 / 第X章 / 一、二、三、/ （一）（二）
        section_pattern = re.compile(
            r"^(?:"
            r"#{1,4}\s+.+|"  # Markdown 标题
            r"第[一二三四五六七八九十\d]+[章节条款部分].+|"  # 第X章
            r"[一二三四五六七八九十]+[、\.].+|"  # 一、二、
            r"（[一二三四五六七八九十\d]+）.+|"  # （一）（二）
            r"\d+[\.\、]\s*.+"  # 1. 2. 3.
            r")$",
            re.MULTILINE,
        )

        matches = list(section_pattern.finditer(text))
        if not matches:
            return [("", text)]

        sections = []
        for i, match in enumerate(matches):
            title = match.group().strip().lstrip("#").strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            if content:
                sections.append((title, content))

        # 如果第一个标题前有内容
        if matches[0].start() > 0:
            preamble = text[: matches[0].start()].strip()
            if preamble:
                sections.insert(0, ("", preamble))

        return sections if sections else [("", text)]

    def _separate_tables(self, text: str) -> tuple:
        """分离表格块（以 | 分隔的行）和普通文本。"""
        lines = text.split("\n")
        table_blocks = []
        text_blocks = []
        current_table = []
        current_text = []

        for line in lines:
            # 判断是否是表格行：至少包含两个 |
            if line.count("|") >= 2:
                if current_text:
                    text_blocks.append("\n".join(current_text))
                    current_text = []
                current_table.append(line)
            else:
                if current_table:
                    table_blocks.append("\n".join(current_table))
                    current_table = []
                current_text.append(line)

        if current_table:
            table_blocks.append("\n".join(current_table))
        if current_text:
            text_blocks.append("\n".join(current_text))

        return table_blocks, text_blocks

    def _split_by_paragraphs(self, text: str) -> List[str]:
        """按段落分割（双换行 or 单换行后有缩进）。"""
        paragraphs = re.split(r"\n\s*\n", text)
        return [p.strip() for p in paragraphs if p.strip()]

    def _merge_short_paragraphs(self, paragraphs: List[str]) -> List[str]:
        """将连续段落合并并以滑动窗口提供重叠。"""
        merged = []
        i = 0
        while i < len(paragraphs):
            buffer_paras = []
            curr_len = 0
            j = i
            # Fill the buffer up to max_chunk_chars
            while j < len(paragraphs):
                p = paragraphs[j].strip()
                if not p:
                    j += 1
                    continue
                # Length after adding this paragraph
                added_len = len(p)
                if curr_len > 0:
                    added_len += 2 # count the \n\n separator
                if curr_len + added_len > self.max_chunk_chars:
                    if curr_len == 0:
                        # If a single paragraph exceeds max_chunk_chars, we must include it
                        buffer_paras.append(p)
                        curr_len += len(p)
                        j += 1
                    break
                buffer_paras.append(p)
                curr_len += added_len
                j += 1
            
            if buffer_paras:
                merged.append("\n\n".join(buffer_paras))
            
            if j >= len(paragraphs):
                break
                
            # Slide window: find how many trailing paragraphs fit within the overlap budget (overlap_chars)
            overlap_len = 0
            overlap_count = 0
            for k in range(j - 1, i - 1, -1):
                p = paragraphs[k].strip()
                if not p:
                    continue
                added_len = len(p)
                if overlap_len > 0:
                    added_len += 2
                if overlap_len + added_len <= self.overlap_chars:
                    overlap_len += added_len
                    overlap_count += 1
                else:
                    break
            
            # Advance start index: index should go to j - overlap_count
            advance = (j - i) - overlap_count
            # Ensure index always moves forward to avoid infinite loop
            i += max(1, advance)
            
        return merged


    def _split_long_text(self, text: str) -> List[str]:
        """将过长的文本按固定窗口切分（带重叠）。"""
        if len(text) <= self.max_chunk_chars:
            return [text]

        chunks = []
        start = 0
        while start < len(text):
            end = start + self.max_chunk_chars

            # 尝试在句号、换行处断开
            if end < len(text):
                for sep in ["。\n", "。", "；", "\n", "，"]:
                    last_sep = text.rfind(sep, start, end)
                    if last_sep > start + self.min_chunk_chars:
                        end = last_sep + len(sep)
                        break

            chunks.append(text[start:end])
            next_start = end - self.overlap_chars
            if next_start <= start:
                next_start = end
            start = next_start

        return chunks

    def _merge_adjacent_short_chunks(self, paragraphs: List[str], min_chars: int = 150) -> List[str]:
        """
        To avoid data duplication and vector redundancy, instead of padding short chunks via copy-paste
        (which generates duplicate overlapping chunks), we merge any chunk shorter than min_chars
        directly into its neighbor.
        """
        if len(paragraphs) <= 1:
            return paragraphs

        result = []
        i = 0
        while i < len(paragraphs):
            para = paragraphs[i]
            # If this paragraph is short, we try to merge it
            if len(para.strip()) < min_chars:
                # Decide whether to merge with the previous chunk (in result) or the next chunk (in paragraphs)
                if result:
                    # Merge with the last added chunk in result
                    prev = result.pop()
                    merged_text = prev.strip() + "\n\n" + para.strip()
                    result.append(merged_text)
                else:
                    # If there's no previous chunk, we must merge with the next chunk
                    if i + 1 < len(paragraphs):
                        next_p = paragraphs[i + 1]
                        merged_text = para.strip() + "\n\n" + next_p.strip()
                        paragraphs[i + 1] = merged_text
                    else:
                        # No previous and no next chunk (should not happen if len > 1, but be safe)
                        result.append(para)
            else:
                result.append(para)
            i += 1
        return result

    def _generate_child_chunks(self, parent: Chunk) -> List[Chunk]:
        """Slices a parent chunk's text into small child chunks."""
        text = parent.chunk_text.strip()
        if len(text) <= self.child_max_chars:
            child = Chunk(
                chunk_id=f"{parent.chunk_id}_child_0",
                doc_id=parent.doc_id,
                version_no=parent.version_no,
                chunk_index=parent.chunk_index,
                chunk_type="child_chunk",
                chunk_text=text,
                token_count=_estimate_tokens(text),
                raw_text=parent.raw_text,
                context_prefix=parent.context_prefix,
                embedding_text=text,
                page_num=parent.page_num,
                section_title=parent.section_title,
                source_oss_key=parent.source_oss_key,
                source=parent.source,
                title=parent.title,
                owner_dept=parent.owner_dept,
                category_l1=parent.category_l1,
                category_l2=parent.category_l2,
                permission_level=parent.permission_level,
                kb_type=parent.kb_type,
                risk_level=parent.risk_level,
                is_active=parent.is_active,
                sensitive_redacted=parent.sensitive_redacted,
                extra=parent.extra.copy()
            )
            child.extra["parent_id"] = parent.chunk_id
            return [child]

        child_texts = []
        start = 0
        while start < len(text):
            end = start + self.child_max_chars
            if end < len(text):
                for sep in ["。\n", "。", "；", "\n", "，"]:
                    last_sep = text.rfind(sep, start, end)
                    if last_sep > start + 20:
                        end = last_sep + len(sep)
                        break
            
            segment = text[start:end].strip()
            if segment:
                child_texts.append(segment)
            
            next_start = end - self.child_overlap_chars
            if next_start <= start:
                next_start = end
            start = next_start

        children = []
        for idx, child_text in enumerate(child_texts):
            child = Chunk(
                chunk_id=f"{parent.chunk_id}_child_{idx}",
                doc_id=parent.doc_id,
                version_no=parent.version_no,
                chunk_index=parent.chunk_index,
                chunk_type="child_chunk",
                chunk_text=child_text,
                token_count=_estimate_tokens(child_text),
                raw_text="",
                context_prefix=parent.context_prefix,
                embedding_text=child_text,
                page_num=parent.page_num,
                section_title=parent.section_title,
                source_oss_key=parent.source_oss_key,
                source=parent.source,
                title=parent.title,
                owner_dept=parent.owner_dept,
                category_l1=parent.category_l1,
                category_l2=parent.category_l2,
                permission_level=parent.permission_level,
                kb_type=parent.kb_type,
                risk_level=parent.risk_level,
                is_active=parent.is_active,
                sensitive_redacted=parent.sensitive_redacted,
                extra=parent.extra.copy()
            )
            child.extra["parent_id"] = parent.chunk_id
            children.append(child)

        return children

    def _chunk_procedure_steps(
        self,
        blocks: list,
        doc_id: str,
        version_no: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """按步骤切块（procedure_image_guide 模式）。

        - 每个 step_no 行独立成一个 step_card chunk
        - 非步骤行（标题、元数据、工具列表等）合并为 header chunk
        - 步骤文本中的图号引用（figure_refs）映射到对应 image asset
        """
        meta = metadata or {}
        chunks: List[Chunk] = []
        chunk_index = 0

        header_lines = []      # 非步骤行文本缓冲
        step_blocks = []       # 步骤行列表

        # 分离步骤行和非步骤行
        for blk in blocks:
            if blk.block_type == "heading":
                # sheet 标题不入库
                continue
            extra = blk.extra or {}
            if extra.get("step_no") is not None:
                step_blocks.append(blk)
            else:
                text = blk.text.strip()
                if text:
                    header_lines.append(text)

        # 1. Header chunk（目的范围 + 工具 + 表头等）
        if header_lines:
            header_text = "\n".join(header_lines)
            chunk = self._create_chunk(
                doc_id=doc_id,
                version_no=version_no,
                chunk_index=chunk_index,
                chunk_text=header_text,
                chunk_type="text_chunk",  # 通用类型；下游检索/服务不识别 procedure_header
                metadata=meta,
            )
            chunk.extra["is_procedure_header"] = True
            chunks.append(chunk)
            chunk_index += 1

        # 2. 每个步骤一个 step_card chunk
        for blk in step_blocks:
            extra = blk.extra or {}
            step_no = extra.get("step_no", 0)
            fig_refs = extra.get("figure_refs", [])

            # 构建步骤文本
            step_text = blk.text.strip()

            chunk = self._create_chunk(
                doc_id=doc_id,
                version_no=version_no,
                chunk_index=chunk_index,
                chunk_text=step_text,
                chunk_type="step_card",
                metadata=meta,
                page_num=blk.page_num,
            )
            chunk.extra["step_no"] = step_no
            if fig_refs:
                chunk.extra["figure_refs"] = fig_refs
            chunks.append(chunk)
            chunk_index += 1

        return chunks

    # ── Section 关键词 → section_type 映射（产品规格书）──
    _SPEC_SECTION_PATTERNS = [
        # (关键词列表, section_type, chunk_type)
        # 顺序重要：product_photo 必须在 appendix 之前检查
        (["物料基本信息", "物料名称", "品牌名称"], "product_info", "product_info_card"),
        (["原材料信息", "原辅材料"], "raw_material", "raw_material_card"),
        (["生产工艺流程", "工艺流程图", "关键工序", "关键控制点"], "process_ccp", "process_ccp_card"),
        (["技术标准要求", "技术标准"], "tech_standard_header", "spec_header_card"),
        (["包装规格", "外包装类型"], "packaging", "packaging_card"),
        (["物料图片", "产品正反面图片", "产品装箱", "单条产品图片", "单个实物图片",
          "标签信息照片", "外箱图片", "包装方式体现"], "product_photo", "product_photo_card"),
        (["附件信息", "文件修订", "会签确认"], "appendix", "appendix_card"),
    ]

    # 技术标准子分区（在 tech_standard 内部细分）
    _SPEC_SUB_SECTIONS = [
        (["感官要求"], "spec_sensory", "spec_sensory_card"),
        (["物理指标", "尺寸指标"], "spec_dimension", "spec_dimension_card"),
        (["微生物指标"], "spec_safety", "spec_micro_card"),
        (["理化指标"], "spec_safety", "spec_chem_card"),
        (["其他指标", "内控要求", "使用性能"], "spec_performance", "spec_performance_card"),
    ]

    def _detect_spec_section(self, text: str):
        """检测行文本属于哪个 section。返回 (section_type, chunk_type) 或 None。"""
        for keywords, sec_type, chunk_type in self._SPEC_SECTION_PATTERNS:
            for kw in keywords:
                if kw in text:
                    return sec_type, chunk_type
        # 技术标准子分区
        for keywords, sec_type, chunk_type in self._SPEC_SUB_SECTIONS:
            for kw in keywords:
                if kw in text:
                    return sec_type, chunk_type
        return None

    def _chunk_product_spec(
        self,
        blocks: list,
        doc_id: str,
        version_no: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Chunk]:
        """按 section 切块（product_spec_instruction 模式）。

        产品规格书结构固定：物料信息 → 原材料 → 工艺 → 技术标准(感官/尺寸/微生物/理化/性能) → 附件
        每个 section 合并为一个 typed card chunk。
        """
        import re
        meta = metadata or {}
        chunks: List[Chunk] = []
        chunk_index = 0

        # 收集所有 paragraph blocks（跳过 heading）
        para_blocks = [b for b in blocks if b.block_type == "paragraph"]

        # 按 section 分组，同时记录行号范围
        sections = []  # [(section_type, chunk_type, [block_texts], min_row, max_row)]
        current_sec = ("header", "spec_header_card")
        current_texts = []
        current_min_row = 9999
        current_max_row = 0

        for blk in para_blocks:
            text = blk.text.strip()
            if not text:
                continue
            row_num = blk.extra.get("row_num", 0) if blk.extra else 0

            detected = self._detect_spec_section(text)
            if detected:
                # 保存前一个 section
                if current_texts:
                    sections.append((current_sec[0], current_sec[1], current_texts, current_min_row, current_max_row))
                current_sec = detected
                current_texts = [text]
                current_min_row = row_num
                current_max_row = row_num
            else:
                current_texts.append(text)
                if row_num < current_min_row:
                    current_min_row = row_num
                if row_num > current_max_row:
                    current_max_row = row_num

        # 保存最后一个 section
        if current_texts:
            sections.append((current_sec[0], current_sec[1], current_texts, current_min_row, current_max_row))

        # 合并相同 section_type 的连续 sections
        merged = []
        for sec_type, chunk_type, texts, rmin, rmax in sections:
            if merged and merged[-1][0] == sec_type:
                prev = merged[-1]
                merged[-1] = (sec_type, chunk_type, prev[2] + texts, min(prev[3], rmin), max(prev[4], rmax))
            else:
                merged.append((sec_type, chunk_type, texts, rmin, rmax))

        # 专用 *_card 类型仅用于内部 section 语义，下游检索/服务只认通用类型
        # （image / table_chunk / text_chunk / step_card / procedure_parent）。
        # 因此对外发出通用 chunk_type，把 section 语义保留在 extra["spec_section"]。
        # 额外收益：product_photo 现在发出 "image"，服务端才会按图片渲染（修复历史遗漏）。
        _CARD_TO_GENERIC = {
            "spec_header_card": "text_chunk",
            "product_info_card": "text_chunk",
            "raw_material_card": "table_chunk",
            "process_ccp_card": "table_chunk",
            "packaging_card": "text_chunk",
            "spec_sensory_card": "table_chunk",
            "spec_dimension_card": "table_chunk",
            "spec_chem_card": "table_chunk",
            "spec_performance_card": "table_chunk",
            "appendix_card": "text_chunk",
            "product_photo_card": "image",
        }

        # 生成 chunks
        for sec_type, chunk_type, texts, rmin, rmax in merged:
            combined = "\n".join(texts)
            # 跳过太短的（纯标题行等）
            if len(combined.strip()) < 10:
                continue

            generic_type = _CARD_TO_GENERIC.get(chunk_type, chunk_type)
            chunk = self._create_chunk(
                doc_id=doc_id,
                version_no=version_no,
                chunk_index=chunk_index,
                chunk_text=combined,
                chunk_type=generic_type,
                metadata=meta,
            )
            chunk.extra["spec_section"] = sec_type
            chunk.extra["spec_card_type"] = chunk_type  # 保留原始细分类型
            chunk.extra["spec_row_start"] = rmin
            chunk.extra["spec_row_end"] = rmax
            chunks.append(chunk)
            chunk_index += 1

        return chunks

