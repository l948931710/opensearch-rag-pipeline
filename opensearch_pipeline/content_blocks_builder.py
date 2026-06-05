# -*- coding: utf-8 -*-
"""
content_blocks_builder.py — 图文穿插内容块构建器

将 LLM 回答 + 图片 chunks 转为钉钉互动卡片 content_blocks 数据结构。

content_blocks 是一个 JSON Array，每个元素为：
  - {"type": "markdown", "content": "文本内容"}
  - {"type": "image", "title": "图片标题", "url": "签名URL", "caption": "说明"}

当模板收到非空 content_blocks 时，用 Loop 组件渲染图文穿插；
当 content_blocks 为空时，降级显示纯 answer 文本。
"""

import json
import logging
import re
from typing import Any, Dict, List

from opensearch_pipeline.oss_url import generate_signed_url

logger = logging.getLogger(__name__)

# LLM 在回答中标记图片插入位置的占位符格式
# 兼容双尖括号 <<IMG:3>> 和单尖括号 <IMG:3>（LLM 经常简化括号）
_IMG_PLACEHOLDER_PATTERN = re.compile(r'<{1,2}IMG:(\d+)>{1,2}')


def _extract_image_chunks(chunks: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    """
    从检索 chunks 中提取图片信息，按文档编号索引。

    支持两种图片来源：
      1. 独立 image chunk（chunk_type="image"）— 现有路径
      2. step_card 绑定的 image_refs — Phase 2 新增

    _format_context() 中文档编号从 1 开始（[文档1], [文档2], ...），
    所以这里用 enumerate(chunks, 1) 对齐。

    Returns:
        {doc_index: [{"source_image": ..., "visual_summary": ..., "title": ...}, ...]}
        注意：值改为 list 以支持 step_card 多图
    """
    image_map: Dict[int, List[Dict[str, Any]]] = {}
    for i, chunk in enumerate(chunks, 1):
        chunk_type = chunk.get("chunk_type", "")

        if chunk_type == "image":
            # 路径 1: 独立 image chunk
            source_image = chunk.get("source_image", "")
            if source_image:
                image_map[i] = [{
                    "source_image": source_image,
                    "visual_summary": chunk.get("visual_summary", ""),
                    "title": chunk.get("title", ""),
                }]

        elif chunk_type == "step_card":
            # 路径 2: step_card 绑定的 image_refs
            image_refs = chunk.get("image_refs") or []
            if image_refs:
                refs_list = []
                for ref in image_refs:
                    oss_key = ref.get("oss_key") or ref.get("source_image", "")
                    if oss_key:
                        refs_list.append({
                            "source_image": oss_key,
                            "visual_summary": ref.get("ocr_text", ""),
                            "title": chunk.get("title", ""),
                        })
                if refs_list:
                    image_map[i] = refs_list

        elif chunk_type in ("text_chunk", "clause_chunk", "ocr_chunk"):
            # 路径 3: text/clause/ocr chunk 携带的 image_refs（text/clause 模式修复后）
            image_refs = chunk.get("image_refs") or []
            if image_refs:
                refs_list = []
                for ref in image_refs:
                    oss_key = ref.get("oss_key") or ref.get("source_image", "")
                    if oss_key:
                        refs_list.append({
                            "source_image": oss_key,
                            "visual_summary": ref.get("visual_summary", "") or ref.get("ocr_text", ""),
                            "title": chunk.get("title", ""),
                        })
                if refs_list:
                    image_map[i] = refs_list

    return image_map


def build_content_blocks(
    answer: str,
    chunks: List[Dict[str, Any]],
    max_images: int = 3,
    url_expires: int = 3600,
) -> List[Dict[str, str]]:
    """
    将 LLM 回答拆分为 content_blocks（图文穿插格式）。

    策略（只展示 LLM 主动用 <<IMG:N>> 引用的图片）:
    1. 扫描 answer 中的 <<IMG:N>> 占位符，得到 LLM 引用的文档序号
       （去重、保持首次引用顺序）
    2. 只对“被引用”的图片签名，且按引用顺序签名后再受 max_images 截断
       —— 被引用的图片永远不会被靠前 chunk 的图片挤掉（修复 cap-eviction）
    3. 若 LLM 没有引用任何图片（纯文字答案 / 负样本）→ 返回空 list
       —— 不再把检索到的图片无差别追加到末尾（修复 over-attachment）
    4. 按占位符位置把被引用的图片穿插进文本

    历史行为对比：旧实现会把所有携带图片的 chunk 一律签名并追加到末尾
    （无论 LLM 是否引用），导致负样本被塞图、跨文档图片污染，以及被引用
    图片被 max_images 截断挤掉。现在一律以 LLM 的 <<IMG:N>> 标记为准。

    Args:
        answer: LLM 生成的回答文本
        chunks: 检索返回的 chunks 列表
        max_images: 最多展示的图片数量
        url_expires: OSS 签名 URL 有效期（秒）

    Returns:
        [] → 无（被引用的）图片，卡片走 answer 降级显示
        [{type, content/url/title/caption}, ...] → 图文穿插
    """
    if not answer:
        return []

    # 1. 提取所有携带图片的 chunk（doc_index → [img dicts]）
    image_map = _extract_image_chunks(chunks)
    if not image_map:
        # 没有任何图片 chunk → 返回空，走 answer 降级
        return []

    # 2. 扫描 <<IMG:N>> 占位符；只保留指向真实图片的有效引用，
    #    去重并保持首次引用顺序（截断时按此顺序定优先级）
    placeholders = list(_IMG_PLACEHOLDER_PATTERN.finditer(answer))
    referenced_order: List[int] = []
    seen_refs = set()
    for match in placeholders:
        doc_idx = int(match.group(1))
        if doc_idx in image_map and doc_idx not in seen_refs:
            referenced_order.append(doc_idx)
            seen_refs.add(doc_idx)

    if not referenced_order:
        # LLM 没有引用任何图片 → 不展示图片（走 answer 降级）
        return []

    # 3. 只为“被引用”的图片签名，按引用顺序处理后再受 max_images 截断。
    #    因为只签名被引用的图片且按引用顺序消耗配额，被引用的图片不会被挤掉。
    signed_images: Dict[int, List[Dict[str, str]]] = {}
    generated_count = 0
    for doc_idx in referenced_order:
        if generated_count >= max_images:
            break
        signed_list = []
        for img_info in image_map[doc_idx]:
            if generated_count >= max_images:
                break
            url = generate_signed_url(img_info["source_image"], expires=url_expires)
            if url:
                summary = img_info.get('visual_summary', '')
                signed_list.append({
                    "url": url,
                    "title": "",
                    "caption": summary[:100] if summary else "",
                })
                generated_count += 1
            else:
                logger.warning(
                    "Skipping image chunk %d: signed URL generation failed for '%s'",
                    doc_idx, img_info["source_image"],
                )
        if signed_list:
            signed_images[doc_idx] = signed_list

    if not signed_images:
        # 被引用图片的签名全部失败 → 返回空，走 answer 降级
        return []

    # 4. 按占位符位置把被引用的图片穿插进文本
    blocks = _build_interleaved(answer, placeholders, signed_images)

    # 最终清理：确保所有 markdown 块不残留 <IMG:N> 占位符
    return _sanitize_blocks(blocks)


def _sanitize_blocks(blocks: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """最终清理：去除所有 markdown 块中残留的 <IMG:N> 占位符。"""
    cleaned = []
    for block in blocks:
        if block.get("type") == "markdown":
            content = _IMG_PLACEHOLDER_PATTERN.sub('', block.get("content", "")).strip()
            if content:
                cleaned.append({"type": "markdown", "content": content})
        else:
            cleaned.append(block)
    return cleaned


def _build_interleaved(
    answer: str,
    placeholders: list,
    signed_images: Dict[int, List[Dict[str, str]]],
) -> List[Dict[str, str]]:
    """按 <<IMG:N>> 占位符位置穿插图片。

    signed_images 只包含被 LLM 引用的 chunk，因此每个图片都会在其占位符处插入；
    不再把未被引用的图片追加到末尾（over-attachment 修复）。
    """
    blocks = []
    last_end = 0
    used_indices = set()

    for match in placeholders:
        doc_idx = int(match.group(1))

        # 占位符前的文本块（清理签名失败的图片残留占位符）
        text_before = answer[last_end:match.start()].strip()
        text_before = _IMG_PLACEHOLDER_PATTERN.sub('', text_before).strip()
        if text_before:
            blocks.append({"type": "markdown", "content": text_before})

        # 插入对应的图片块（可能有多张）
        if doc_idx in signed_images and doc_idx not in used_indices:
            for img in signed_images[doc_idx]:
                blocks.append({
                    "type": "image",
                    "title": img["title"],
                    "url": img["url"],
                    "caption": img["caption"],
                })
            used_indices.add(doc_idx)

        last_end = match.end()

    # 占位符后剩余文本
    remaining = answer[last_end:].strip()
    if remaining:
        # 清理可能残留的未匹配占位符
        remaining = _IMG_PLACEHOLDER_PATTERN.sub('', remaining).strip()
        if remaining:
            blocks.append({"type": "markdown", "content": remaining})

    return blocks


def content_blocks_to_json(blocks: List[Dict[str, str]]) -> str:
    """
    将 content_blocks 序列化为 JSON 字符串。

    钉钉 cardParamMap 的值必须是字符串类型，
    所以 content_blocks 需要 json.dumps() 序列化后传入。
    """
    if not blocks:
        return ""
    return json.dumps(blocks, ensure_ascii=False)
