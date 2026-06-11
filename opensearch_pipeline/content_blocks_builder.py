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
from typing import Any, Dict, List, Optional

from opensearch_pipeline.oss_url import generate_signed_url

logger = logging.getLogger(__name__)

# LLM 在回答中标记图片插入位置的占位符格式
# 兼容双尖括号 <<IMG:3>> 和单尖括号 <IMG:3>（LLM 经常简化括号）
_IMG_PLACEHOLDER_PATTERN = re.compile(r'<{1,2}IMG:(\d+)>{1,2}')

# 句末收尾标点 + 闭合括号：按占位符切分后落在片段开头时，应回挂到上一文本块末尾
_LEADING_CLOSER_PATTERN = re.compile(r'^[。，、；：！？）】」』]+')

# ── 跨文档重复图片抑制 ──────────────────────────────────────────────
# 目标是【真重复】：同一文件重复注册（A1员工行为管理标准 4 次注册）、docx+pdf 双格式
# double-ingest、同一张截图被多份 SOP 原样内嵌（产成品入库单菜单截图 ×3 份 SOP）——
# 这些的 VLM caption 逐字相同或高度一致（VLM cache 按图片 MD5 命中同一段文本）。
# 阈值用真实答卷标定：真重复对 jac≥0.4/lcs≥35；最近的必留对（两份手册里不同路径的
# U8 菜单截图）jac=0.342/lcs=12。
# ⚠️ 三类绝不判重（2026-06-10 对抗评审用真实语料逐一证实的误杀面）：
#   1. 同一文档（doc_id 相同）的多图 —— SOP 各步骤截图天然相似但各属其步；
#   2. 值发散的同屏变体 —— 两份文档各自截的登录窗带不同部门的账套/工号标注
#      （抑制其一会让读者照着错误账套操作，比展示两张更糟）；
#   3. 引号目标发散的样板句 —— VLM caption 框架高度公式化（"左侧为业务导航…红色
#      箭头指向'X'"），框架重合 ≠ 内容重合，'采购发票审核' vs '人事管理' 必须都保留。
# 比较文本只用 VLM 出身的 visual_summary（near_dup_text）：ocr_text 由界面公共
# 文案主导（"业务导航 常用功能…"），两张不同截图会 OCR 出同一段字，绝不能参与判重。
_NEAR_DUP_MIN_CAPTION = 10     # 比较文本太短没有判别信号，一律不判重（宁可多展示）
_NEAR_DUP_JAC_HIGH = 0.6       # 字符 bigram Jaccard 整体重合度极高 → 判重（仍受否决项约束）
_NEAR_DUP_JAC = 0.35           # 中等重合度需叠加长公共子串（同屏截图的措辞锚点）
_NEAR_DUP_LCS = 16
_NEAR_DUP_MAX_CMP = 300        # 比较文本截断：LCS 是 O(n·m) 纯 Python DP，判别信号在头部
_NEAR_DUP_MIN_BIGRAMS = 12     # bigram 并集太小 = 低熵样板文本（"操作界面如下图所示"），不判重
_NEAR_DUP_MIN_SIDE_BIGRAMS = 8
_VLM_DEGRADED_PATTERN = re.compile(r'^\s*\[VLM')          # "[VLM Fallback] …" 降级 caption 全网相同
_QUOTED_TOKEN_PATTERN = re.compile(r'[‘“]([^‘’“”]{1,40})[’”]')   # VLM 引号里的菜单/按钮名
_VALUE_TOKEN_PATTERN = re.compile(r'[A-Za-z0-9.\-@]{2,}')        # 工号/账套/IP/日期等取值 token
# 箭头/高亮的目标项 = 这张截图的真正主语（"展开至'存货核算'→'记账'，红色箭头指向'X'"
# 的路径引号会大量重合，只有 X 区分两张不同菜单截图）
_ARROW_TARGET_PATTERN = re.compile(
    r'(?:箭头指向|高亮选中|高亮的|红框标注|红框高亮)[^‘“’”，。；]{0,6}[‘“]([^‘’“”]{1,40})[’”]'
)


def _near_dup_normalize(text: str) -> str:
    return re.sub(r'\s+', '', text or '')[:_NEAR_DUP_MAX_CMP]


def _caption_bigrams(text: str) -> set:
    s = _near_dup_normalize(text)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _longest_common_substring(a: str, b: str) -> int:
    """最长公共子串长度（去空白、截断到 _NEAR_DUP_MAX_CMP 后）。"""
    a = _near_dup_normalize(a)
    b = _near_dup_normalize(b)
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for ch_a in a:
        cur = [0] * (len(b) + 1)
        for j, ch_b in enumerate(b, 1):
            if ch_a == ch_b:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _is_near_dup_caption(a: str, b: str) -> bool:
    """两段 VLM caption 是否描述同一张图（真重复），而非仅仅同类界面。

    相似度门限（jac/lcs）先粗筛，再过三道否决项（任何一道命中即判不重复）：
      低熵/降级样板 caption、引号目标发散、取值 token 发散。标定依据见常量注释。
    """
    a = (a or '').strip()
    b = (b or '').strip()
    if len(a) < _NEAR_DUP_MIN_CAPTION or len(b) < _NEAR_DUP_MIN_CAPTION:
        return False
    if _VLM_DEGRADED_PATTERN.match(a) or _VLM_DEGRADED_PATTERN.match(b):
        return False   # 降级 caption 是常量文本，相同 ≠ 同图
    set_a, set_b = _caption_bigrams(a), _caption_bigrams(b)
    if (
        len(set_a | set_b) < _NEAR_DUP_MIN_BIGRAMS
        or min(len(set_a), len(set_b)) < _NEAR_DUP_MIN_SIDE_BIGRAMS
    ):
        return False   # 低熵文本（重复字符/超短样板句）没有判别力
    jac = len(set_a & set_b) / len(set_a | set_b)
    if jac < _NEAR_DUP_JAC:
        return False
    quoted_a = set(_QUOTED_TOKEN_PATTERN.findall(a))
    quoted_b = set(_QUOTED_TOKEN_PATTERN.findall(b))
    if jac < _NEAR_DUP_JAC_HIGH:
        # 中带相似度（公式化框架天然重合）必须有引号主语为证：双方都要引出界面/
        # 菜单名且有长公共子串，否则"职位信息维护 vs 部门信息维护"这类同框架
        # 不同界面会被误判（真重复的 caption 逐字相同，走 ≥HIGH 快路，不受此限）
        if not (quoted_a and quoted_b):
            return False
        if _longest_common_substring(a, b) < _NEAR_DUP_LCS:
            return False
    # 否决项 0：箭头/高亮目标发散 —— 菜单路径引号（'存货核算''记账'）会重合，
    # 但"红色箭头指向'X'"的 X 才是截图主语；X 不同 = 不同操作的截图
    target_a = set(_ARROW_TARGET_PATTERN.findall(a))
    target_b = set(_ARROW_TARGET_PATTERN.findall(b))
    if target_a and target_b and target_a.isdisjoint(target_b):
        return False
    # 否决项 1：引号目标发散 —— caption 框架公式化，引号里才是这张图的"主语"。
    # 要求引号集多数重合（不止共享一个泛词如'我的桌面'），否则视为不同内容
    if quoted_a and quoted_b and len(quoted_a & quoted_b) * 2 < min(len(quoted_a), len(quoted_b)):
        return False
    # 否决项 2：取值 token 发散 —— 真重复共享全部取值（同图同值）；
    # 同屏不同部门变体恰好差在这些 token（FL063 vs FL0062、(888)PLS vs 188@FLSJ）
    values_a = set(_VALUE_TOKEN_PATTERN.findall(a))
    values_b = set(_VALUE_TOKEN_PATTERN.findall(b))
    if values_a != values_b:
        return False
    return True


def _find_duplicate_image(
    accepted: List[Dict[str, str]],
    doc_id: str,
    oss_key: str,
    near_dup_text: str,
) -> Optional[Dict[str, str]]:
    """候选图片与已采纳（将渲染）图片重复 → 返回命中项，否则 None。

    两级判定：
      1. oss_key 完全相同 —— 同一文件在一条回答里渲染两次永远无益（不分文档）；
      2. VLM caption 真重复（_is_near_dup_caption）—— 仅适用于【不同 doc_id】之间，
         且双方都要有 VLM 出身的比较文本（near_dup_text 为空 = 无判别信号，保守保留）。
    """
    for item in accepted:
        if item["oss_key"] == oss_key:
            return item
        if (
            doc_id and item["doc_id"] and item["doc_id"] != doc_id
            and near_dup_text and item["near_dup_text"]
            and _is_near_dup_caption(item["near_dup_text"], near_dup_text)
        ):
            return item
    return None


def strip_image_markers(text: Optional[str]) -> str:
    """去除 <<IMG:N>> / <IMG:N> 图片占位符（客户端可见文本的最终清理）。

    占位符是服务端与渲染层之间的内部协议，绝不能泄漏给用户
    （blocks 为空时小程序端会把 answer 原文当纯文本渲染）。
    ⚠️ 调用顺序约束：必须先用【原始 answer】构建图文 blocks，再做本清理 ——
    blocks 的穿插位置依赖占位符。
    """
    if not text:
        return ""
    return _IMG_PLACEHOLDER_PATTERN.sub('', text).strip()


def _reattach_leading_punct(blocks: List[Dict[str, str]], fragment: str) -> str:
    """把片段开头的句末标点回挂到最近一个 markdown 块末尾，返回剩余片段。

    LLM 常写「…图标 <<IMG:3>>。」—— 切分后句号落到下一片段开头（或成孤立结尾块）。
    回挂保持句子完整；若前面没有文本块（图片开头），直接丢弃这些标点。
    """
    m = _LEADING_CLOSER_PATTERN.match(fragment)
    if not m:
        return fragment
    for blk in reversed(blocks):   # 跳过 image 块，找最近的文本块
        if blk.get("type") == "markdown":
            blk["content"] += m.group(0)
            break
    return fragment[m.end():].lstrip()


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
                    # near_dup_text = 跨文档判重的比较文本，只取 VLM 出身的 visual_summary；
                    # 展示用 visual_summary 可能混入 ocr_text 兜底（界面公共文案），绝不参与判重
                    "near_dup_text": chunk.get("visual_summary", ""),
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
                            # step_card 的 image_refs 把图注存在 caption（chunker），
                            # 经 retriever 重建后仍是 caption；ocr_text 在该路径恒为空。
                            # 优先 caption，回退 visual_summary / ocr_text。
                            "visual_summary": (
                                ref.get("caption")
                                or ref.get("visual_summary")
                                or ref.get("ocr_text", "")
                            ),
                            # caption 可能由 chunker 用 ocr_text 兜底拼成（出身不可考），
                            # 判重只信 ref 自带的 visual_summary；为空则该图不参与近重判定
                            "near_dup_text": ref.get("visual_summary") or "",
                            "title": chunk.get("title", ""),
                        })
                if refs_list:
                    image_map[i] = refs_list

        elif chunk_type in ("text_chunk", "clause_chunk", "ocr_chunk", "visual_knowledge"):
            # 路径 3: text/clause/ocr/visual_knowledge chunk 携带的 image_refs
            image_refs = chunk.get("image_refs") or []
            if image_refs:
                refs_list = []
                for ref in image_refs:
                    oss_key = ref.get("oss_key") or ref.get("source_image", "")
                    if oss_key:
                        refs_list.append({
                            "source_image": oss_key,
                            "visual_summary": ref.get("visual_summary", "") or ref.get("ocr_text", ""),
                            # 判重不收 ocr_text：两张不同截图会 OCR 出同一段界面公共文案
                            "near_dup_text": ref.get("visual_summary", ""),
                            "title": chunk.get("title", ""),
                        })
                if refs_list:
                    image_map[i] = refs_list
            elif chunk_type == "visual_knowledge":
                # chunker Phase-5.5 的 visual_knowledge 变体用 source_image/oss_key 携带单图（无 image_refs）
                source_image = chunk.get("source_image") or chunk.get("oss_key", "")
                if source_image:
                    image_map[i] = [{
                        "source_image": source_image,
                        "visual_summary": chunk.get("visual_summary", "") or chunk.get("caption", ""),
                        "near_dup_text": chunk.get("visual_summary", ""),
                        "title": chunk.get("title", ""),
                    }]

    return image_map


def build_content_blocks(
    answer: str,
    chunks: List[Dict[str, Any]],
    max_images: int = 3,
    url_expires: Optional[int] = None,
    max_caption_len: Optional[int] = 100,
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
        url_expires: OSS 签名 URL 有效期（秒）；None 取 config.oss.signed_url_expires
                     （RAG_OSS_URL_EXPIRES，默认 3600）
        max_caption_len: caption 截断长度；None 表示不截断（小程序屏幕空间更大，可传 None 取全文）

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
    #    url_expires=None 由 generate_signed_url 统一解析为 config.oss.signed_url_expires。
    signed_images: Dict[int, List[Dict[str, str]]] = {}
    generated_count = 0
    accepted: List[Dict[str, str]] = []   # 已采纳（将渲染）图片：近重抑制的比较基准
    for doc_idx in referenced_order:
        if generated_count >= max_images:
            break
        # doc_idx 由 enumerate(chunks, 1) 产生，必在界内；doc_id 用于「同文档绝不判近重」
        chunk_doc_id = chunks[doc_idx - 1].get("doc_id", "")
        signed_list = []
        for img_info in image_map[doc_idx]:
            if generated_count >= max_images:
                break
            oss_key = img_info["source_image"]
            summary = img_info.get('visual_summary', '')
            near_dup_text = img_info.get("near_dup_text", "")
            dup = _find_duplicate_image(accepted, chunk_doc_id, oss_key, near_dup_text)
            if dup is not None:
                # 被抑制的近重图不消耗 max_images 配额（continue 在计数之前）
                logger.info(
                    "近重图片抑制: 丢弃 '%s' (doc=%s)，与已采纳 '%s' (doc=%s) 描述同屏",
                    oss_key, chunk_doc_id, dup["oss_key"], dup["doc_id"],
                )
                continue
            url = generate_signed_url(oss_key, expires=url_expires)
            if url:
                signed_list.append({
                    "url": url,
                    # oss_key 随块落库：签名 URL 1h 过期，卡片回调重建时按它重签
                    # （refresh_image_block_urls）；卡片模板/小程序只读 url，多余键无害
                    "oss_key": oss_key,
                    "title": "",
                    "caption": (summary[:max_caption_len] if max_caption_len else summary) if summary else "",
                })
                # 判重基准存 VLM 出身的 near_dup_text（caption 可能被 max_caption_len 截短/混 OCR）
                accepted.append({
                    "doc_id": chunk_doc_id, "oss_key": oss_key, "near_dup_text": near_dup_text,
                })
                generated_count += 1
            else:
                logger.warning(
                    "Skipping image chunk %d: signed URL generation failed for '%s'",
                    doc_idx, oss_key,
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

        # 占位符前的文本块（清理签名失败的图片残留占位符；句首孤立标点回挂上一块）
        text_before = answer[last_end:match.start()].strip()
        text_before = _IMG_PLACEHOLDER_PATTERN.sub('', text_before).strip()
        text_before = _reattach_leading_punct(blocks, text_before)
        if text_before:
            blocks.append({"type": "markdown", "content": text_before})

        # 插入对应的图片块（可能有多张）
        if doc_idx in signed_images and doc_idx not in used_indices:
            for img in signed_images[doc_idx]:
                blocks.append({
                    "type": "image",
                    "title": img["title"],
                    "url": img["url"],
                    "oss_key": img.get("oss_key", ""),
                    "caption": img["caption"],
                })
            used_indices.add(doc_idx)

        last_end = match.end()

    # 占位符后剩余文本
    remaining = answer[last_end:].strip()
    if remaining:
        # 清理可能残留的未匹配占位符；句首孤立标点（如孤立的「。」尾块）回挂上一块
        remaining = _IMG_PLACEHOLDER_PATTERN.sub('', remaining).strip()
        remaining = _reattach_leading_punct(blocks, remaining)
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


def build_mini_program_blocks(
    answer: str,
    chunks: List[Dict[str, Any]],
    max_images: int = 3,
    url_expires: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """为钉钉小程序生成图文 blocks（供 <text>/<image> 原生渲染）。

    复用 build_content_blocks 的「<<IMG:N>> 标记解析 / 仅展示被引用图片 / 配额截断」核心逻辑
    （不复制不分叉），仅在最外层做字段重命名，使其更贴合小程序原生组件：
      {"type":"markdown","content":X}        -> {"type":"text","format":"markdown","text":X}
      {"type":"image","url":U,"caption":C}   -> {"type":"image","url":U,"caption":C,"alt":C}
    小程序屏幕空间更大，caption 取全文（max_caption_len=None），不再像卡片那样截断到 100 字。
    纯文字答案 / 未引用图片时返回 []（与 build_content_blocks 行为一致）。
    """
    raw = build_content_blocks(
        answer, chunks, max_images=max_images, url_expires=url_expires, max_caption_len=None
    )
    out: List[Dict[str, Any]] = []
    for b in raw:
        if b.get("type") == "image":
            cap = b.get("caption", "")
            out.append({
                "type": "image",
                "url": b.get("url", ""),
                "oss_key": b.get("oss_key", ""),
                "caption": cap,
                "alt": cap,
            })
        else:  # markdown
            text = b.get("content", "")
            if text:
                out.append({"type": "text", "format": "markdown", "text": text})
    return out


def _oss_key_from_url(url: str) -> str:
    """从存量签名 URL 解析 object key（兜底没有 oss_key 的旧落库行）。

    形如 https://{bucket}.{region}.aliyuncs.com/{quoted_key}?Expires=...&Signature=...
    非阿里云 OSS 域名一律不解析（防止把外部 URL 的 path 当成 key 去重签）。
    """
    if not url:
        return ""
    try:
        from urllib.parse import unquote, urlparse
        parsed = urlparse(url)
        if not parsed.hostname or not parsed.hostname.endswith(".aliyuncs.com"):
            return ""
        return unquote(parsed.path.lstrip("/"))
    except Exception:
        return ""


def refresh_image_block_urls(blocks_json: str, url_expires: Optional[int] = None) -> str:
    """重签 content_blocks JSON 里 image 块的 OSS URL（卡片回调重建用）。

    落库的签名 URL 默认 1h 过期：用户在旧卡片上点反馈触发重建时，原样回放会渲染
    一排 403 死图。新行直接用块里的 oss_key 重签；旧行回退从存量 URL 的 path 解析
    object key；解析/重签失败保留原 URL（优雅降级，绝不让回调白屏）。任何异常返回原串。
    """
    if not blocks_json:
        return blocks_json
    try:
        blocks = json.loads(blocks_json)
        if not isinstance(blocks, list):
            return blocks_json
        for b in blocks:
            if not isinstance(b, dict) or b.get("type") != "image":
                continue
            key = b.get("oss_key") or _oss_key_from_url(b.get("url", ""))
            if key:
                fresh = generate_signed_url(key, expires=url_expires)
                if fresh:
                    b["url"] = fresh
        return json.dumps(blocks, ensure_ascii=False)
    except Exception:
        logger.warning("content_blocks URL 重签失败（保留原 JSON）", exc_info=True)
        return blocks_json
