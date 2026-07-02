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
# 兼容双尖括号 <<IMG:3>> 和单尖括号 <IMG:3>（LLM 经常简化括号）。
# #F-mm6：group(2) = 可选子下标 M（<<IMG:3.2>> = 第 3 文档第 2 张图，图级寻址）。
# 无条件扩宽（模块级常量，不门控）：对标准 <<IMG:N>> 完全兼容（group(2)=None），
# 仅额外识别 .M 后缀——OFF 时用于把畸形 .M 残片也 strip 干净（无害增强）。
_IMG_PLACEHOLDER_PATTERN = re.compile(r'<{1,2}IMG:(\d+)(?:\.(\d+))?>{1,2}')

# LLM 偶发的畸形标记变体：全角冒号（<<IMG：3>>）、中文括号（【IMG:3】/《IMG:3》）、
# 括号内多余空白。标准正则不认识这些变体 → 既不出图也不被 strip 清除，字面残片
# 直接渲染给用户。宽容归一化（RAG_IMG_MARKER_LENIENT，默认 OFF）在解析/清洗前把
# 它们重写为标准 <<IMG:N>>。pattern 收紧到 IMG 关键词 + 数字，避免误伤正文。
_IMG_MARKER_VARIANT_PATTERN = re.compile(r'(?:<{1,2}|【|《)\s*IMG\s*[:：]\s*(\d+)\s*(?:>{1,2}|】|》)')


def _marker_lenient_enabled() -> bool:
    """RAG_IMG_MARKER_LENIENT 开关（fail-open：配置不可用时视为关闭）。"""
    try:
        from opensearch_pipeline.config import get_config
        return bool(get_config().rag.img_marker_lenient)
    except Exception:
        return False


def normalize_img_markers(text: str) -> str:
    """把畸形图片标记变体归一化为标准 <<IMG:N>>（对标准标记幂等）。"""
    return _IMG_MARKER_VARIANT_PATTERN.sub(lambda m: f'<<IMG:{m.group(1)}>>', text)


def _log_render_stats(stats: Dict[str, Any]) -> None:
    """出图漏斗遥测：单条结构化日志，可按 outcome/计数分桶排查「为什么这题没图」。

    纯旁路观测（fail-open）：遥测自身的任何异常绝不影响答案构建。
    第一期 log-only，不进 qa_session_log（那是 schema 迁移，F-35 另行走）。
    """
    try:
        logger.info("image_render_stats %s", json.dumps(stats, ensure_ascii=False, sort_keys=True))
    except Exception:
        pass

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
    if _marker_lenient_enabled():
        text = normalize_img_markers(text)
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


def renderable_image_refs(chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单个 chunk 的【可渲染图列表】—— serving 出图的单一权威（#F-mm6）。

    只收带 oss_key 的图、按 chunk 内顺序返回。这是 <<IMG:N.M>> 的 M 与最终渲染
    第 M 张图【同源】的关键：llm_generator 编 m 与本函数、_extract_image_chunks
    都基于这同一份过滤后列表，任一侧另起过滤都会让 LLM 看到的 m 与渲染错位、
    图-步错绑（验证阶段点名的最大坑）。

    Returns: [{"source_image", "visual_summary", "near_dup_text", "title"}, ...]（可空）
    """
    chunk_type = chunk.get("chunk_type", "")
    title = chunk.get("title", "")

    if chunk_type == "image":
        source_image = chunk.get("source_image", "")
        if source_image:
            return [{
                "source_image": source_image,
                "visual_summary": chunk.get("visual_summary", ""),
                # near_dup_text = 跨文档判重的比较文本，只取 VLM 出身的 visual_summary；
                # 展示用 visual_summary 可能混入 ocr_text 兜底（界面公共文案），绝不参与判重
                "near_dup_text": chunk.get("visual_summary", ""),
                "title": title,
            }]
        return []

    if chunk_type == "step_card":
        refs_list = []
        for ref in (chunk.get("image_refs") or []):
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
                    "title": title,
                })
        return refs_list

    if chunk_type in ("text_chunk", "clause_chunk", "ocr_chunk", "visual_knowledge"):
        refs_list = []
        for ref in (chunk.get("image_refs") or []):
            oss_key = ref.get("oss_key") or ref.get("source_image", "")
            if oss_key:
                refs_list.append({
                    "source_image": oss_key,
                    "visual_summary": ref.get("visual_summary", "") or ref.get("ocr_text", ""),
                    # 判重不收 ocr_text：两张不同截图会 OCR 出同一段界面公共文案
                    "near_dup_text": ref.get("visual_summary", ""),
                    "title": title,
                })
        if refs_list:
            return refs_list
        if chunk_type == "visual_knowledge":
            # chunker Phase-5.5 的 visual_knowledge 变体用 source_image/oss_key 携带单图（无 image_refs）
            source_image = chunk.get("source_image") or chunk.get("oss_key", "")
            if source_image:
                return [{
                    "source_image": source_image,
                    "visual_summary": chunk.get("visual_summary", "") or chunk.get("caption", ""),
                    "near_dup_text": chunk.get("visual_summary", ""),
                    "title": title,
                }]
        return []

    return []


def _extract_image_chunks(chunks: List[Dict[str, Any]]) -> Dict[int, List[Dict[str, Any]]]:
    """
    从检索 chunks 中提取图片信息，按文档编号索引（逐 chunk 委托 renderable_image_refs）。

    _format_context() 中文档编号从 1 开始（[文档1], [文档2], ...），
    所以这里用 enumerate(chunks, 1) 对齐。

    Returns:
        {doc_index: [{"source_image": ..., "visual_summary": ..., "title": ...}, ...]}
        注意：值为 list 以支持多图 chunk
    """
    image_map: Dict[int, List[Dict[str, Any]]] = {}
    for i, chunk in enumerate(chunks, 1):
        refs = renderable_image_refs(chunk)
        if refs:
            image_map[i] = refs
    return image_map


def _img_subindex_enabled() -> bool:
    """RAG_IMG_SUBINDEX 开关（fail-open：配置不可用时视为关闭）。"""
    try:
        from opensearch_pipeline.config import get_config
        return bool(get_config().rag.img_subindex)
    except Exception:
        return False


def _parse_marker(match) -> "tuple":
    """把一个 <<IMG:N>> / <<IMG:N.M>> 匹配解析为 (N, M)，M 缺省为 None。"""
    n = int(match.group(1))
    m = match.group(2)
    return n, (int(m) if m else None)


def plan_image_rotation(counts: List[int], max_images: int) -> List[int]:
    """轮转配额的纯规划函数——build_content_blocks 步骤 3 配额语义的单点文档。

    counts[i] = 第 i 个被引用文档/步骤携带的图片数（按首次引用顺序）；
    返回每个文档计划渲染的张数：每轮按引用顺序各取 1 张，直到配额（max_images）
    用尽或全部取完。评测侧（eval_harness/mm_answer_metrics）用它模拟
    referenced-only 实渲数量，与生产共用同一份语义，防口径漂移；
    不含签名失败/近重抑制（离线不可知，生产循环里两者不消耗配额）。
    ⚠️ 改动生产配额循环时必须同步本函数并跑 conformance 测试
    （tests/test_mm_answer_metrics_referenced_only.py）。
    """
    plan = [0] * len(counts)
    remaining = max_images
    while remaining > 0:
        progressed = False
        for i, c in enumerate(counts):
            if remaining <= 0:
                break
            if plan[i] < c:
                plan[i] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break
    return plan


def build_content_blocks(
    answer: str,
    chunks: List[Dict[str, Any]],
    max_images: Optional[int] = None,
    url_expires: Optional[int] = None,
    max_caption_len: Optional[int] = 100,
) -> List[Dict[str, str]]:
    """
    将 LLM 回答拆分为 content_blocks（图文穿插格式）。

    策略（只展示 LLM 主动用 <<IMG:N>> 引用的图片）:
    1. 扫描 answer 中的 <<IMG:N>> 占位符，得到 LLM 引用的文档序号
       （去重、保持首次引用顺序）
    2. 只对“被引用”的图片签名，配额按**轮转**分配：第一轮每个被引用文档/步骤
       各取 1 张，仍有余额再按引用顺序轮转补各自剩余图 —— 多图步骤不再整段
       独占名额、把后位步骤（如扫码枪实拍）彻底挤出（2026-06-11，吸塑扫码报检
       step2×2+step3×3 在旧顺序消耗下使 step4 的图排第 6 位永远出不来）
    3. 若 LLM 没有引用任何图片（纯文字答案 / 负样本）→ 返回空 list
       —— 不再把检索到的图片无差别追加到末尾（修复 over-attachment）
    4. 按占位符位置把被引用的图片穿插进文本；同一文档内的图保持原始顺序

    历史行为对比：旧实现会把所有携带图片的 chunk 一律签名并追加到末尾
    （无论 LLM 是否引用），导致负样本被塞图、跨文档图片污染，以及被引用
    图片被 max_images 截断挤掉。现在一律以 LLM 的 <<IMG:N>> 标记为准。

    Args:
        answer: LLM 生成的回答文本
        chunks: 检索返回的 chunks 列表
        max_images: 最多展示的图片数量；None 取 config.rag.max_answer_images
                    （RAG_MAX_ANSWER_IMAGES，默认 6 ≈ 每步一张+少量补充）
        url_expires: OSS 签名 URL 有效期（秒）；None 取 config.oss.signed_url_expires
                     （RAG_OSS_URL_EXPIRES，默认 3600）
        max_caption_len: caption 截断长度；None 表示不截断（小程序屏幕空间更大，可传 None 取全文）

    Returns:
        [] → 无（被引用的）图片，卡片走 answer 降级显示
        [{type, content/url/title/caption}, ...] → 图文穿插
    """
    if not answer:
        return []

    if _marker_lenient_enabled():
        # 畸形变体（全角冒号/中文括号）归一化为标准 <<IMG:N>>，本函数内的解析、
        # 穿插与 _sanitize_blocks 都基于归一化后的文本，保证位置一致。
        answer = normalize_img_markers(answer)

    if max_images is None:
        from opensearch_pipeline.config import get_config
        max_images = get_config().rag.max_answer_images

    # 出图漏斗遥测（#F-mm2）：纯旁路计数，任何分支 return 前打一条结构化日志
    stats: Dict[str, Any] = {
        "outcome": "", "n_chunks": len(chunks), "n_chunks_with_images": 0,
        "n_images_available": 0, "n_placeholders": 0, "n_invalid_refs": 0,
        "invalid_ns": [], "n_referenced": 0, "n_neardup_suppressed": 0,
        "n_sign_failed": 0, "n_quota_dropped": 0, "n_images_out": 0,
        "max_images": max_images,
    }

    # 1. 提取所有携带图片的 chunk（doc_index → [img dicts]）
    image_map = _extract_image_chunks(chunks)
    stats["n_chunks_with_images"] = len(image_map)
    stats["n_images_available"] = sum(len(v) for v in image_map.values())
    if not image_map:
        # 没有任何图片 chunk → 返回空，走 answer 降级
        stats["outcome"] = "no_image_chunks"
        _log_render_stats(stats)
        return []

    # 2. 扫描占位符；解析为 (N, M)（#F-mm6：M=图级子下标，仅 RAG_IMG_SUBINDEX 尊重）。
    #    单元语义：某 N 若有任一有效 .M 引用 → 分图模式（每个 distinct (N,m) 独立渲染，
    #    该 N 的裸标记被忽略）；否则纯 N 整包（向后兼容）。OFF 时 M 恒 None → 全部纯 N，
    #    与历史逐字节一致。
    subindex = _img_subindex_enabled()
    placeholders = list(_IMG_PLACEHOLDER_PATTERN.finditer(answer))
    parsed = []  # 对齐 placeholders 的 (N, M)；OFF 时 M 强制 None
    for match in placeholders:
        n, m = _parse_marker(match)
        parsed.append((n, m if subindex else None))

    # 哪些 N 处于分图模式（有 ≥1 个有效 .M 引用）
    subindexed_ns = set()
    if subindex:
        for n, m in parsed:
            if m is not None and n in image_map and 1 <= m <= len(image_map[n]):
                subindexed_ns.add(n)

    # 单元化 + 保序去重；placeholder_units 对齐 placeholders 供穿插期复用（不重复判定）
    referenced_units: List[tuple] = []
    seen_units = set()
    placeholder_units: List[Optional[tuple]] = []
    invalid_ns = set()
    for n, m in parsed:
        if n not in image_map:
            placeholder_units.append(None)
            stats["n_invalid_refs"] += 1
            invalid_ns.add(n)
            continue
        if n in subindexed_ns:
            if m is None:
                placeholder_units.append(None)   # 分图模式下的裸标记：歧义，忽略（不计幻觉）
                continue
            if not (1 <= m <= len(image_map[n])):
                placeholder_units.append(None)   # M 越界：幻觉
                stats["n_invalid_refs"] += 1
                invalid_ns.add(n)
                continue
            unit = (n, m)
        else:
            # 纯 N 整包（M 给定但该 N 无任何有效 .M → 该 .M 越界，计幻觉）
            if m is not None:
                placeholder_units.append(None)
                stats["n_invalid_refs"] += 1
                invalid_ns.add(n)
                continue
            unit = (n, None)
        placeholder_units.append(unit)
        if unit not in seen_units:
            seen_units.add(unit)
            referenced_units.append(unit)
    stats["invalid_ns"] = sorted(invalid_ns)[:10]
    stats["n_placeholders"] = len(placeholders)
    stats["n_referenced"] = len(referenced_units)

    if not referenced_units:
        # LLM 没有引用任何图片 → 不展示图片（走 answer 降级）
        # 有图可用却零引用（referenced_none 且 n_images_available>0）= referenced-only
        # 策略下最常见的丢图形态，此前不可观测
        stats["outcome"] = "referenced_none"
        _log_render_stats(stats)
        return []

    # 3. 只为“被引用”的单元签名，配额**轮转**分配后再受 max_images 截断：
    #    每轮按引用顺序给每个单元取 1 张（跳过近重与签名失败、不消耗配额），
    #    直到配额用尽或全部取完。整包单元（N,None）逐轮弹队首保原始顺序；
    #    分图单元（N,m）只含单张。url_expires=None 由 generate_signed_url 统一解析。
    signed_images: Dict[tuple, List[Dict[str, str]]] = {}
    generated_count = 0
    accepted: List[Dict[str, str]] = []   # 已采纳（将渲染）图片：近重抑制的比较基准
    # 单元 queue：(N,None)=该 chunk 全部可渲染图；(N,m)=第 m 张单图
    def _unit_queue(unit):
        n, m = unit
        refs = image_map[n]
        return list(refs) if m is None else [refs[m - 1]]
    queues = [(unit, _unit_queue(unit)) for unit in referenced_units]
    while generated_count < max_images and any(q for _, q in queues):
        progressed = False
        for unit, q in queues:
            if generated_count >= max_images:
                break
            chunk_doc_id = chunks[unit[0] - 1].get("doc_id", "")
            # 本轮为该单元取 1 张：弹出队首直到拿到一张可用图（近重/签名失败不耗配额）
            while q:
                img_info = q.pop(0)
                oss_key = img_info["source_image"]
                summary = img_info.get('visual_summary', '')
                near_dup_text = img_info.get("near_dup_text", "")
                dup = _find_duplicate_image(accepted, chunk_doc_id, oss_key, near_dup_text)
                if dup is not None:
                    # 被抑制的近重图不消耗 max_images 配额
                    stats["n_neardup_suppressed"] += 1
                    logger.info(
                        "近重图片抑制: 丢弃 '%s' (doc=%s)，与已采纳 '%s' (doc=%s) 描述同屏",
                        oss_key, chunk_doc_id, dup["oss_key"], dup["doc_id"],
                    )
                    continue
                url = generate_signed_url(oss_key, expires=url_expires)
                if not url:
                    stats["n_sign_failed"] += 1
                    logger.warning(
                        "Skipping image unit %s: signed URL generation failed for '%s'",
                        unit, oss_key,
                    )
                    continue
                signed_images.setdefault(unit, []).append({
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
                progressed = True
                break
        if not progressed:
            break

    # 循环退出时仍留在队列里的图 = 因配额（或整轮无进展）未被尝试的图
    stats["n_quota_dropped"] = sum(len(q) for _, q in queues)
    stats["n_images_out"] = generated_count

    if not signed_images:
        # 被引用图片的签名全部失败 → 返回空，走 answer 降级
        stats["outcome"] = "no_signed_images"
        _log_render_stats(stats)
        return []

    stats["outcome"] = "ok"
    _log_render_stats(stats)

    # 4. 按占位符位置把被引用的图片穿插进文本（placeholder_units 对齐 placeholders）
    blocks = _build_interleaved(answer, placeholders, placeholder_units, signed_images)

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
    placeholder_units: List[Optional[tuple]],
    signed_images: Dict[tuple, List[Dict[str, str]]],
) -> List[Dict[str, str]]:
    """按占位符位置穿插图片（#F-mm6：按渲染单元 (N,M) 而非 chunk 号）。

    signed_images 只含被引用单元；每个单元的图在其【首个】占位符处插入，同单元
    二次引用被 used 跳过。分图模式下 (N,1)/(N,2) 是不同单元 → 可在不同步骤各出各图。
    placeholder_units 对齐 placeholders（build 期已算好，此处不重复判定）。
    """
    blocks = []
    last_end = 0
    used_units = set()

    for match, unit in zip(placeholders, placeholder_units):
        # 占位符前的文本块（清理签名失败的图片残留占位符；句首孤立标点回挂上一块）
        text_before = answer[last_end:match.start()].strip()
        text_before = _IMG_PLACEHOLDER_PATTERN.sub('', text_before).strip()
        text_before = _reattach_leading_punct(blocks, text_before)
        if text_before:
            blocks.append({"type": "markdown", "content": text_before})

        # 插入对应单元的图片块（整包单元可能多张；分图单元单张）
        if unit is not None and unit in signed_images and unit not in used_units:
            for img in signed_images[unit]:
                blocks.append({
                    "type": "image",
                    "title": img["title"],
                    "url": img["url"],
                    "oss_key": img.get("oss_key", ""),
                    "caption": img["caption"],
                })
            used_units.add(unit)

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
