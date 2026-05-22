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
    chunk_type: str  # text_chunk / table_chunk / faq_chunk / section_chunk
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
            if "source_image_vector" in self.extra and self.extra["source_image_vector"] is not None:
                doc["source_image_vector"] = self.extra["source_image_vector"]
                
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
            "chunk_text_store": self.chunk_text,
            "source_url": self.source_oss_key or "",
            "is_active": 1 if self.is_active else 0,
        }
        if self.embedding_vector:
            doc["dense_vector"] = list(self.embedding_vector)
        if self.sparse_vector_indices:
            doc["sparse_vector_indices"] = list(self.sparse_vector_indices)
            doc["sparse_vector_values"] = list(self.sparse_vector_values or [])

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

