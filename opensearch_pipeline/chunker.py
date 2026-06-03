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
        r'(\d+)\s*[)）]\s*'                           # 1) / 2）
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
                # 如果 heading 文本匹配编号格式且文档已检测到步骤，
                # 将 heading 当做新步骤的开始，避免图片丢失。
                heading_step_match = re.match(
                    r'^(\d+(?:\.\d+)*)\s*[\.．、]?\s*\S', text
                )
                if heading_step_match and found_any_step:
                    if current_step is not None:
                        step_groups.append(current_step)
                    # heading 虚拟步骤 step_no=0 表示 section 级标题
                    # section_no 保留原始编号（如 "3.2.4"）
                    current_step = {
                        "step_no": 0,
                        "section_no": heading_step_match.group(1),
                        "title": text[:80],
                        "text_parts": [text],
                        "image_refs": [],
                        "page_num": page_num,
                        "section": current_section,
                        "source": source,
                    }
                continue

            # image_ref 块 → 归入当前步骤
            if block_type == "image_ref":
                if current_step is not None:
                    current_step["image_refs"].append(extra)
                elif current_section and found_any_step:
                    # 兜底：没有步骤但有 section 上下文，创建虚拟步骤
                    current_step = {
                        "step_no": 0,
                        "title": current_section[:80],
                        "text_parts": [current_section],
                        "image_refs": [extra],
                        "page_num": page_num,
                        "section": current_section,
                        "source": source,
                    }
                # 如果还没有步骤，image_ref 会被忽略（前导图片很少见）
                continue

            if not text:
                continue

            # 表格 block → 独立 table_chunk
            if block_type == "table":
                if current_step is not None:
                    current_step["text_parts"].append(text)
                else:
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

            # ── 检测步骤边界 ──
            match = self._STEP_BOUNDARY_RE.match(text)
            if match:
                # 提取步骤编号（5 个捕获组）
                step_no_str = (match.group(1) or match.group(2) or
                               match.group(3) or match.group(4) or
                               match.group(5))

                # ── 过滤误判：通用编号格式（N. / N、/ N)）需要足够长的文本 ──
                # 避免将材料清单 "4. 胶带" 误判为步骤。
                # 明确的步骤标记（步骤N / Step N / 第N步）不受此限制。
                is_generic_numbering = match.group(4) or match.group(5)
                if is_generic_numbering and len(text) < 15:
                    # 太短，当普通文本处理
                    if current_step is not None:
                        current_step["text_parts"].append(text)
                    elif found_any_step:
                        postamble_texts.append((text, page_num, current_section, source))
                    else:
                        preamble_texts.append((text, page_num, current_section, source))
                    continue

                found_any_step = True
                try:
                    # 中文数字转换（注意：不能用 dict.get(k, int(k))，
                    # 因为 Python 会先求值 int("三") 导致 ValueError）
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

                # 开始新步骤组
                current_step = {
                    "step_no": step_no,
                    "title": text[:80],
                    "text_parts": [text],
                    "image_refs": [],
                    "page_num": page_num,
                    "section": current_section,
                    "source": source,
                }
            else:
                # 非步骤边界文本
                if current_step is not None:
                    current_step["text_parts"].append(text)
                elif found_any_step:
                    postamble_texts.append((text, page_num, current_section, source))
                else:
                    preamble_texts.append((text, page_num, current_section, source))

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
        for section_key, sec_steps in section_groups.items():
            step_card_chunks_sec = []
            step_card_ids_sec = []
            vk_images_sec = []   # visual_knowledge 图片（保留引用 + 复制生成独立 chunk）

            for sg in sec_steps:
                step_text = "\n".join(sg["text_parts"])

                # 收集图片的 OCR 文本和资产信息
                image_refs_list = []
                all_ocr_raw = []

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
                    if visual_summary:
                        all_ocr_raw.append(visual_summary)

                # 解析 annotation_map
                combined_ocr = " ".join(all_ocr_raw)
                annotation_map = parse_annotation_map(step_text, combined_ocr)
                annotation_text = expand_annotation_map(annotation_map)
                ocr_keywords = clean_ocr_keywords(combined_ocr)

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
                    parts.append(f"[图片关键词] {ocr_keywords}")

                final_chunk_text = "\n".join(parts)

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
                    step_chunk.extra["image_ocr_raw"] = combined_ocr[:2000]
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

            # Phase 5: 生成 procedure_parent（每个 section 一个）
            doc_title = meta.get("title", "")
            section_label = section_key if section_key != "__default__" else ""
            step_titles = [f"步骤{sg['step_no']}：{sg['title'][:40]}" for sg in sec_steps]
            parent_text = f"{doc_title}"
            if section_label:
                parent_text += f"\n{section_label}"
            parent_text += "\n" + "\n".join(step_titles)

            parent_chunk = self._create_chunk(
                doc_id=doc_id, version_no=version_no,
                chunk_index=chunk_index, chunk_type="procedure_parent",
                chunk_text=parent_text,
                page_num=sec_steps[0]["page_num"] if sec_steps else None,
                section_title=section_label or (sec_steps[0]["section"] if sec_steps else current_section),
                metadata=meta,
            )
            parent_chunk.extra["child_chunk_ids"] = step_card_ids_sec
            parent_chunk.extra["step_count"] = len(step_card_ids_sec)
            parent_chunk_id = parent_chunk.chunk_id
            chunk_index += 1

            # 回填 parent_chunk_id
            for sc in step_card_chunks_sec:
                sc.extra["parent_chunk_id"] = parent_chunk_id

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

            # Phase 6: 追加到结果
            chunks.append(parent_chunk)
            chunks.extend(step_card_chunks_sec)

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
            r'（[一二三四五六七八九十\d]+）|'  # （一）（二）
            r'\d+\.\d+(?:\.\d+)?\s'  # 9.1 9.2 or 2.2.1
            r')',
            re.MULTILINE,
        )

        # 1. Collect all paragraph text and handle tables/headings
        all_para_texts: List[str] = []
        table_chunks: List[Chunk] = []

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
            return table_chunks

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
            return table_chunks + chunks

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

        # 5. Create chunks from segments
        for seg in merged_segments:
            if len(seg) > self.max_chunk_chars:
                sub_texts = self._split_long_text(seg)
                for sub in sub_texts:
                    if len(sub.strip()) < self.min_chunk_chars:
                        continue
                    chunks.append(self._create_chunk(
                        doc_id=doc_id,
                        version_no=version_no,
                        chunk_index=chunk_index,
                        chunk_type="clause_chunk",
                        chunk_text=sub.strip(),
                        section_title=current_section,
                        metadata=meta,
                    ))
                    chunk_index += 1
            else:
                if len(seg.strip()) < self.min_chunk_chars:
                    continue
                chunks.append(self._create_chunk(
                    doc_id=doc_id,
                    version_no=version_no,
                    chunk_index=chunk_index,
                    chunk_type="clause_chunk",
                    chunk_text=seg.strip(),
                    section_title=current_section,
                    metadata=meta,
                ))
                chunk_index += 1

        return table_chunks + chunks

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

        meta = metadata or {}
        chunks: List[Chunk] = []
        chunk_index = 0
        current_section: Optional[str] = None
        
        # We need to buffer consecutive text blocks in the same section to merge them
        buffered_texts = []
        buffered_page_num = None
        buffered_source = "native"

        def commit_buffer():
            nonlocal chunk_index, buffered_texts
            if not buffered_texts:
                return
            merged_paras = self._merge_short_paragraphs(buffered_texts)
            merged_paras = self._merge_adjacent_short_chunks(merged_paras, min_chars=150)
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
                    chunk_index += 1
            buffered_texts.clear()

        for block in blocks:
            # 兼容 dict 和 dataclass
            if isinstance(block, dict):
                block_type = block.get("block_type", "paragraph")
                text = block.get("text", "")
                page_num = block.get("page_num")
                section_path = block.get("section_path")
                source = block.get("source", "native")
            else:
                block_type = block.block_type
                text = block.text
                page_num = block.page_num
                section_path = block.section_path
                source = block.source

            # 更新 section 跟踪
            if block_type == "heading":
                commit_buffer()
                current_section = section_path or text
                continue  # heading 自身不生成 chunk，作为后续 chunk 的 section_title

            if not text:
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

