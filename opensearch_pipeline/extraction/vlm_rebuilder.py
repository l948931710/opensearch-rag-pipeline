# -*- coding: utf-8 -*-
"""
extraction/vlm_rebuilder.py — Increment 1: VLM 版面重建 (per-page escalation).

把规则提取后"无法提取/扫描/坏字体"的 PDF 页面升级为 Qwen-VL 结构化重建：
  render page → Qwen-VL → 结构化 typed-block JSON → 拼回 result.blocks (原位)。

设计契约（务必遵守，否则破坏下游）：
  - VLM 只产出 **typed blocks (heading/paragraph/table)**，绝不产出 markdown 整页流，
    也绝不触碰 image_ref / assets（图片锚定始终走规则路径）。
  - 升级是 **逐页** 的：只替换不可提取页，其它页的确定性输出原样保留。
  - 重建块 source="multimodal"（不是 "ocr"，后者会被 step chunker 跳过）。
  - 一切受 **总开关 RAG_REBUILD_ENABLED + 成本熔断器** 控制；默认关闭即完全 no-op。
  - 数字保真：表格/数值由 VLM 重建时 prompt 明确"逐字转录、严禁臆造数字"；
    对仍有原生文本层的页面不升级（只升级 ~0 字符页），避免用生成模型覆盖可信文本。

复用：ocr_client 的 fitz 渲染思路；image_funnel_processor 的双端点 VLM 调用 + JSON 解析；
      cost_breaker.gate_vlm_rebuild 做成本闸。
"""
from __future__ import annotations

import base64
import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# 每页文本低于此字符数 → 视为"不可提取"，升级 VLM 重建
_REBUILD_PAGE_CHAR_THRESHOLD = 30


def _page_char_counts(pdf_path: str) -> List[int]:
    """逐页原生文本字符数（pdfplumber）。失败返回空表示无法判断。"""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pl:
            return [len((pl.pages[i].extract_text() or "").strip()) for i in range(len(pl.pages))]
    except Exception as e:
        logger.warning("[vlm_rebuilder] page char count failed: %s", e)
        return []


def _render_page_image(pdf_path: str, page_idx: int, zoom: float = 2.2):
    """渲染单页并压缩为可上传体积，返回 (bytes, mime)。

    密集中文页用 ~2.2x 保证清晰；超过 ~600KB 或边长 >2200px 时转 JPEG(q82) 并限边，
    避免大 payload 上传写超时（首版踩到的 write timeout）。失败返回 (None, None)。
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        try:
            pix = doc.load_page(page_idx).get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            png = pix.tobytes("png")
        finally:
            doc.close()
    except Exception as e:
        logger.warning("[vlm_rebuilder] render page %s failed: %s", page_idx, e)
        return None, None
    # 始终转 JPEG 并限边 ~1568px（与 image_funnel 一致的上传体积区间，文字仍清晰），
    # 避免大 base64 payload 上传写超时。
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(png)).convert("RGB")
        if max(im.size) > 1568:
            im.thumbnail((1568, 1568))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=78)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return png, "image/png"


_RECON_PROMPT = (
    "你是企业文档版面重建器。请把这张文档页面图片重建为**结构化 JSON**，"
    "严格逐字转录页面上的所有文字，**绝不臆造、绝不补全、绝不修改任何数字/编号/日期**。\n"
    "输出格式（只输出 JSON，不要解释）：\n"
    '{"blocks":[{"type":"heading|paragraph|table","text":"...","level":1}]}\n'
    "规则：\n"
    "1. 标题用 type=heading 并给 level(1-3)；正文段落用 paragraph；表格用 type=table，"
    "text 用 markdown 竖线表格，保留每一个单元格（含合并单元格的值），逐行逐列转录。\n"
    "2. 按阅读顺序排列 blocks（多栏时先左栏后右栏）。\n"
    "3. 只转录文字，不要描述图片/印章/签名的视觉外观。无文字的纯图案忽略。\n"
    "4. 若页面无任何可读文字，输出 {\"blocks\":[]}。"
)


def _vlm_reconstruct_page(img_bytes: bytes, cfg, doc_title: str = "",
                          mime: str = "image/png") -> List[dict]:
    """调用 Qwen-VL 重建单页 → [{type, text, level}]。失败/降级返回 []。"""
    import requests
    model = cfg.ocr.vlm_model or cfg.ocr.model
    api_key = cfg.ocr.api_key
    base_url = cfg.ocr.api_base_url
    if not api_key:
        logger.warning("[vlm_rebuilder] no VLM api_key; skip reconstruction")
        return []
    # 生产硬约束：禁止 Gemini（与 config 守卫一致）
    if "google" in (base_url or "").lower() or "gemini" in (model or "").lower():
        if cfg.environment in ("production", "staging"):
            raise ValueError("[vlm_rebuilder] refusing Gemini under production/staging")

    b64 = base64.b64encode(img_bytes).decode()
    prompt = _RECON_PROMPT + (f"\n文档标题：{doc_title[:80]}" if doc_title else "")
    use_compat = "qwen3" in model.lower() or "compatible" in (base_url or "").lower()
    try:
        if use_compat:
            import re as _re
            _dm = _re.search(r'https?://([^/]+)', base_url or "")
            domain = _dm.group(1) if _dm else "dashscope.aliyuncs.com"
            url = f"https://{domain}/compatible-mode/v1/chat/completions"
            payload = {"model": model, "temperature": 0,
                       "messages": [{"role": "user", "content": [
                           {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                           {"type": "text", "text": prompt}]}]}
            r = requests.post(url, json=payload,
                              headers={"Authorization": f"Bearer {api_key}",
                                       "Content-Type": "application/json"}, timeout=(15, 150))
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        else:
            url = base_url.rstrip("/") + "/services/aigc/multimodal-generation/generation"
            payload = {"model": model, "input": {"messages": [{"role": "user", "content": [
                {"text": prompt}, {"image": f"data:{mime};base64,{b64}"}]}]},
                "parameters": {"temperature": 0}}
            r = requests.post(url, json=payload,
                              headers={"Authorization": f"Bearer {api_key}"}, timeout=(10, 120))
            r.raise_for_status()
            content = r.json()["output"]["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        # strip markdown fences
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.rstrip().endswith("```"):
                content = content.rstrip()[:-3]
        s, e = content.find("{"), content.rfind("}")
        if s == -1 or e == -1:
            return []
        data = json.loads(content[s:e + 1])
        blocks = data.get("blocks", [])
        return [b for b in blocks if isinstance(b, dict) and (b.get("text") or "").strip()]
    except Exception as e:
        logger.warning("[vlm_rebuilder] VLM reconstruct failed: %s", e)
        return []


def maybe_rebuild_pdf(task: dict, result, cfg, breaker=None):
    """PDF 版面重建主入口。在规则 PDF 提取之后调用。

    - 总开关 cfg.rebuild.enabled 关闭 → 直接返回（no-op）。
    - 找出 ~0 字符的不可提取页 → 成本闸 → 逐页 VLM 重建 → 拼回 result.blocks。
    - 升级被拒（成本超限）→ gate 已封存文档；本函数仅返回原 result（规则回退）。

    返回（可能被修改的）result。
    """
    if not getattr(cfg, "rebuild", None) or not cfg.rebuild.enabled:
        return result
    if (result.file_ext or "").lower() != "pdf":
        return result
    local_path = task.get("local_path", "")
    if not local_path:
        return result

    char_counts = _page_char_counts(local_path)
    if not char_counts:
        return result
    escalate = [i for i, ch in enumerate(char_counts) if ch < _REBUILD_PAGE_CHAR_THRESHOLD]
    if not escalate:
        return result  # 全部可提取 → 不升级（保护数字保真）

    # ── 成本闸（强制：未传入 breaker 则按 cfg 现造一个，保证永远过闸）──
    from opensearch_pipeline.extraction.cost_breaker import CostBreaker, gate_vlm_rebuild
    if breaker is None:
        breaker = CostBreaker(cfg)
    simulate_db = bool(getattr(cfg, "simulate_db", True))
    gate_doc = {
        "doc_id": task.get("doc_id", "?"), "version_no": task.get("version_no", 1),
        "file_ext": "pdf", "owner_dept": task.get("owner_dept", "unknown"),
        "unit_count": 0, "cached_count": 0, "ocr_page_count": len(escalate),
    }
    allowed, est = gate_vlm_rebuild(breaker, gate_doc, simulate_db=simulate_db)
    if not allowed:
        print(f"    [vlm_rebuilder] cost gate DENIED rebuild of {len(escalate)} pages "
              f"(est {est.est_cost_rmb} RMB); falling back to rule output", flush=True)
        return result

    from opensearch_pipeline.extraction.schema import ExtractedBlock
    doc_title = task.get("doc_title", "") or task.get("filename", "")
    added = 0
    new_blocks_by_page = {}
    for pidx in escalate:
        img, mime = _render_page_image(local_path, pidx)
        if not img:
            continue
        recon = _vlm_reconstruct_page(img, cfg, doc_title, mime=mime)
        page_blocks = []
        for b in recon:
            bt = b.get("type", "paragraph")
            if bt not in ("heading", "paragraph", "table"):
                bt = "paragraph"
            blk = ExtractedBlock(
                block_type=bt, text=(b.get("text") or "").strip(),
                level=int(b.get("level", 0) or 0), page_num=pidx + 1,
                source="multimodal",
            )
            blk.extra = {"rebuilt_by": "vlm"}
            page_blocks.append(blk)
        if page_blocks:
            new_blocks_by_page[pidx + 1] = page_blocks
            added += len(page_blocks)

    if not added:
        return result

    # 先算 recovered 文本（在拼回消费 new_blocks_by_page 之前）
    recovered = "\n".join(b.text for pg in sorted(new_blocks_by_page)
                          for b in new_blocks_by_page[pg])
    n_pages_rebuilt = len(new_blocks_by_page)

    # ── 拼回：保留原 blocks，把每页重建块插到该页最后一个规则块之后（维持阅读顺序）；
    #    该页若无规则块则按 page 顺序追加。重建只针对不可提取页，故不会与规则块内容重叠。──
    last_idx_by_page = {}
    for i, blk in enumerate(result.blocks):
        pg = getattr(blk, "page_num", None)
        if pg is not None:
            last_idx_by_page[pg] = i
    appended_after = {i: pg for pg, i in last_idx_by_page.items() if pg in new_blocks_by_page}
    merged = []
    for i, blk in enumerate(result.blocks):
        merged.append(blk)
        if i in appended_after:
            merged.extend(new_blocks_by_page.pop(appended_after[i]))
    for pg in sorted(new_blocks_by_page):   # 原 blocks 中没有该 page 的，按页号追加
        merged.extend(new_blocks_by_page[pg])
    result.blocks = merged

    # 更新 text / extract_method
    if recovered:
        result.text = (result.text + "\n\n" + recovered) if result.text else recovered
        result.text_length = len(result.text)
    result.extract_method = (result.extract_method or "") + "+vlm_rebuild"
    print(f"    [vlm_rebuilder] rebuilt {n_pages_rebuilt} page(s), "
          f"+{added} blocks, +{len(recovered)} chars", flush=True)
    return result
