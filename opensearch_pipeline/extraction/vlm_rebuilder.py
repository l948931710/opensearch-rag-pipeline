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
from typing import List

logger = logging.getLogger(__name__)

# 每页文本低于此字符数 → 视为"不可提取"，升级 VLM 重建
_REBUILD_PAGE_CHAR_THRESHOLD = 30


def _safe_int_level(v, default: int = 0) -> int:
    """把 VLM 返回的 level 字段安全转成 int。

    模型可能回 "一" / "1." / "H1" / None 等非纯整数；直接 int() 会抛 ValueError，
    若未捕获会丢掉整页重建。容错解析失败时回落 default。
    """
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        pass
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return default


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
        # 成本封存：标记结果，让下游 (node_redact_or_quarantine → chunk/publish) 跳过本文档，
        # 避免"RDS 已封存 / 索引里仍有 chunk"的裂脑状态。
        result.cost_quarantined = True
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
            # 单块容错：一个畸形块 (如 level="一") 不应丢掉整页重建
            try:
                bt = b.get("type", "paragraph")
                if bt not in ("heading", "paragraph", "table"):
                    bt = "paragraph"
                blk = ExtractedBlock(
                    block_type=bt, text=(b.get("text") or "").strip(),
                    level=_safe_int_level(b.get("level")), page_num=pidx + 1,
                    source="multimodal",
                )
                blk.extra = {"rebuilt_by": "vlm"}
                page_blocks.append(blk)
            except Exception as _be:
                logger.warning("[vlm_rebuilder] skip malformed VLM block on page %s: %s", pidx + 1, _be)
                continue
        if page_blocks:
            new_blocks_by_page[pidx + 1] = page_blocks
            added += len(page_blocks)

    if not added:
        # 放行但实际未产生任何重建块 (渲染/VLM 全失败) → 退还预留，勿空耗运行预算
        breaker.refund(gate_doc["doc_id"], est)
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


# ════════════════════════════════════════════════════════════════════════════
# Increment 2 — PDF 表格精修（结构错乱表格 → VLM 重建，数字保真闸把关）
# ════════════════════════════════════════════════════════════════════════════

def _number_multiset(text: str):
    """文本中的数字 token → Counter（多重集；表格会重复数字，不能用 set）。

    token = 连续数字串（含小数点/千分位逗号），归一化去掉千分位逗号后计数。
    用于数字保真比对：原生表格的每个数字都必须在 VLM 表格里出现（计数 >=）。
    """
    import re
    from collections import Counter
    toks = re.findall(r"\d+(?:[.,]\d+)*", text or "")
    return Counter(t.replace(",", "") for t in toks)


def _table_tokens(text: str) -> set:
    """表格的"内容指纹"：CJK 二元组 + 字母数字串 + 数字。用于判断两张表是否是同一张。"""
    import re
    s = text or ""
    cjk = re.findall(r"[一-鿿]", s)
    bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    alnum = set(re.findall(r"[A-Za-z0-9]{2,}", s))
    return bigrams | alnum


def _content_corresponds(nat_tokens: set, vlm_tokens: set, min_overlap: float = 0.5) -> bool:
    """内容对应闸：VLM 表必须覆盖原生表至少 min_overlap 的内容 token，
    确保精修的是"同一张表"，而不是同页里的另一张表（如把数据表错换成文档抬头表）。
    原生表无可判定 token → 返回 False（不精修，安全）。"""
    if not nat_tokens:
        return False
    return len(nat_tokens & vlm_tokens) / len(nat_tokens) >= min_overlap


def _native_numbers_preserved(native_text: str, vlm_text: str) -> bool:
    """数字保真闸：VLM 表格必须包含原生表格的每一个数字（多重集 ⊆）。

    返回 False（VLM 漏了/改了某个原生数字）→ 调用方应拒绝精修、保留原生表格。
    注意：VLM 多出的数字（从借线误判/合并单元格里救回的）是允许的（正是收益）；
    但 prompt 已强约束"严禁臆造数字"、temperature=0，且保留 fallback_text 供抽查。
    """
    nat = _number_multiset(native_text)
    vlm = _number_multiset(vlm_text)
    return all(vlm.get(tok, 0) >= cnt for tok, cnt in nat.items())


def _table_is_mangled(text: str) -> bool:
    """从渲染后的 pipe-markdown 判断一个 table 块是否结构错乱（值得送 VLM 精修）。

    信号：退化单列（列被 lines 策略合并）、列数参差（ragged）、空单元格占比高（合并表头/借线误判）。
    结构良好的表格返回 False → 不精修（省成本、避免动好表）。
    """
    rows = [r.strip() for r in (text or "").splitlines() if r.strip()]
    if len(rows) < 2:
        return True
    cell_counts, empties, total = [], 0, 0
    for r in rows:
        cells = [c.strip() for c in r.strip("|").split("|")]
        cell_counts.append(len(cells))
        total += len(cells)
        empties += sum(1 for c in cells if not c)
    if max(cell_counts) <= 1:           # 退化单列
        return True
    if (max(cell_counts) - min(cell_counts)) >= 2:   # 列数参差
        return True
    if total and empties / total >= 0.4:             # 空单元格过多
        return True
    return False


def maybe_refine_tables(task: dict, result, cfg, breaker=None):
    """Increment 2: 对 PDF 中结构错乱(ragged/merged)的 table 块做 VLM 精修（原位覆盖 .text）。

    安全契约（强制）：
      - 受 cfg.rebuild.enabled + cfg.rebuild.refine_tables 双开关控制；任一关闭 → 完全 no-op（零回归）。
        （要求 enabled=True：成本熔断器以 enabled 为总闸，关闭时熔断器放行一切，故必须 enabled 才上线。）
      - **数字保真闸**：VLM 表格必须含原生表格的每个数字（多重集 ⊆），否则拒绝、保留原生表格。
      - 只重写 table 块 .text；保留 source='native'，merge extra（fallback_text/refined_by），
        不新增/移动 image_ref、不触碰其它块。失败 fail-open（上层 try/except）。
    """
    rc = getattr(cfg, "rebuild", None)
    if not rc or not getattr(rc, "enabled", False) or not getattr(rc, "refine_tables", False):
        return result
    if (result.file_ext or "").lower() != "pdf":
        return result
    # 该文档已被 rebuild 阶段成本封存 → 不再花钱精修一个将被跳过的文档
    if getattr(result, "cost_quarantined", False):
        return result
    local_path = task.get("local_path", "")
    if not local_path:
        return result

    targets = [b for b in result.blocks
               if getattr(b, "block_type", "") == "table" and _table_is_mangled(getattr(b, "text", ""))]
    if not targets:
        return result
    pages = sorted({getattr(b, "page_num", None) for b in targets if getattr(b, "page_num", None)})
    if not pages:
        return result

    # ── 成本闸（按需精修的页数计费；breaker 缺省现造，enabled=True 时才真正限额）──
    from opensearch_pipeline.extraction.cost_breaker import CostBreaker, gate_vlm_rebuild
    if breaker is None:
        breaker = CostBreaker(cfg)
    gate_doc = {
        "doc_id": task.get("doc_id", "?"), "version_no": task.get("version_no", 1),
        "file_ext": "pdf", "owner_dept": task.get("owner_dept", "unknown"),
        "unit_count": 0, "cached_count": 0, "ocr_page_count": len(pages),
    }
    # 表格精修是可选"锦上添花"：成本拒绝时只跳过精修、保留原生表格，绝不封存文档
    # (quarantine_on_deny=False)。文档本身可用，不能因可选精修被否决而被丢出索引。
    allowed, est = gate_vlm_rebuild(breaker, gate_doc,
                                    simulate_db=bool(getattr(cfg, "simulate_db", True)),
                                    quarantine_on_deny=False)
    if not allowed:
        print(f"    [table_refine] cost gate DENIED ({len(pages)} page(s), "
              f"est {est.est_cost_rmb} RMB); keeping native tables", flush=True)
        return result

    doc_title = task.get("doc_title", "") or task.get("filename", "")
    page_tables = {}  # page_num → [vlm table markdown, ...]（每页渲染+重建一次）
    refined = rejected = 0
    for b in targets:
        pg = getattr(b, "page_num", None)
        if pg is None:
            continue
        if pg not in page_tables:
            img, mime = _render_page_image(local_path, pg - 1)
            recon = _vlm_reconstruct_page(img, cfg, doc_title, mime=mime) if img else []
            page_tables[pg] = [(rb.get("text") or "").strip() for rb in recon
                               if rb.get("type") == "table" and (rb.get("text") or "").strip()]
        cands = page_tables[pg]
        if not cands:
            continue
        native = getattr(b, "text", "")
        nat_tokens = _table_tokens(native)
        # 同页多表时，选与原生表"内容"(文本 token+数字)重叠最多的 VLM 表，避免错配到同页别的表
        best = max(cands, key=lambda t: len(nat_tokens & _table_tokens(t)))
        # 双闸：① 数字保真（VLM 不漏原生数字）② 内容对应（确是同一张表，非同页别的表）
        if not _native_numbers_preserved(native, best) \
                or not _content_corresponds(nat_tokens, _table_tokens(best)):
            rejected += 1
            continue
        merged_extra = dict(getattr(b, "extra", {}) or {})
        merged_extra["fallback_text"] = native
        merged_extra["refined_by"] = "vlm"
        b.extra = merged_extra
        b.text = best
        refined += 1

    if refined:
        from opensearch_pipeline.extraction.text_extractor import blocks_to_text
        result.text = blocks_to_text(result.blocks)
        result.text_length = len(result.text)
        result.extract_method = (result.extract_method or "") + "+vlm_table_refine"
        print(f"    [table_refine] refined {refined} table(s), rejected {rejected} "
              f"(number-fidelity), across {len(pages)} page(s)", flush=True)
    else:
        # 放行但无一张表通过数字保真闸 → 退还预留，勿空耗运行预算
        breaker.refund(gate_doc["doc_id"], est)
    return result
