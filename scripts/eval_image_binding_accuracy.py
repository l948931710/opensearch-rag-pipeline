#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图文绑定精度验证 V2 — 用文本内容匹配而非 step_no

问题：V1 用全局 step_no 对比，但 chunker 按 section 重新编号，导致数字不匹配。
V2 策略：直接比较图片前面的步骤文本与 step_card 的文本是否一致。

Ground truth:
  DOCX blocks 中，image_ref 前面最近的步骤文本段落 = 该图片应该归属的步骤。
  Chunker 产出的 step_card.chunk_text 如果包含该步骤文本的核心内容 → 绑定正确。
"""

import json
import os
import sys
import glob
import re

os.environ["RAG_ENV"] = "local"

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

from opensearch_pipeline.extraction.docx_extractor import extract_docx_with_images
from opensearch_pipeline.chunker import DocumentChunker

STEP_RE = re.compile(
    r"(?:"
    r"^\s*(?:步骤\s*[一二三四五六七八九十\d]+|"
    r"第\s*[一二三四五六七八九十\d]+\s*步|"
    r"\d+\s*[、.．]\s*|"
    r"\d+\s*[)）]\s*|"
    r"[（(]\s*\d+\s*[)）]\s*)"
    r")",
    re.MULTILINE,
)

docx_dir = "/Users/laijunchen/Downloads/opensearch-rag-pipeline/fuling_chunk_exp"
docx_files = sorted(glob.glob(os.path.join(docx_dir, "*.docx")))

chunker_step = DocumentChunker(
    max_chunk_chars=600, min_chunk_chars=20, overlap_chars=0,
    split_mode="step", prepend_title=True, prepend_section=True,
)

print(f"\n{BOLD}{CYAN}{'━' * 70}{RESET}")
print(f"{BOLD}{CYAN}  图文绑定精度验证 V2 — 文本匹配法{RESET}")
print(f"{BOLD}{CYAN}{'━' * 70}{RESET}")

total_docs = 0
docs_with_bindings = 0
total_images_checked = 0
total_images_correct = 0
doc_results = []

for docx_path in docx_files:
    basename = os.path.basename(docx_path)
    total_docs += 1

    try:
        blocks, img_assets = extract_docx_with_images(docx_path)
        if not blocks:
            continue

        # ── Ground Truth: blocks 顺序 ──
        # 记录每张图前面最近的非空文本段落（不限于步骤）
        gt_bindings = []  # [(image_index, preceding_text_50chars, target_ref)]
        last_text = ""
        last_step_text = ""

        for b in blocks:
            if b.block_type in ("paragraph", "heading"):
                text = (b.text or "").strip()
                if text:
                    last_text = text
                    if STEP_RE.search(text):
                        last_step_text = text
            elif b.block_type == "image_ref":
                img_idx = (b.extra or {}).get("image_index")
                target_ref = (b.extra or {}).get("target_ref", "")
                if img_idx is not None and last_text:
                    gt_bindings.append({
                        "image_index": img_idx,
                        "preceding_text": last_text,
                        "preceding_step": last_step_text,
                        "target_ref": target_ref,
                    })

        if not gt_bindings:
            continue

        # ── Pipeline: step chunker ──
        blocks_dict = [{
            "block_type": b.block_type,
            "text": b.text or "",
            "page_num": b.page_num,
            "section_path": b.section_path,
            "source": b.source or "native",
            "extra": b.extra or {},
        } for b in blocks]

        meta = {
            "title": basename.replace(".docx", ""),
            "owner_dept": "it", "category_l1": "sop", "category_l2": "",
            "permission_level": "public", "kb_type": "public",
            "risk_level": "low", "source_oss_key": f"raw/{basename}",
        }

        doc_id = f"VERIFY_{basename[:20]}"
        chunks = chunker_step.chunk_from_blocks(blocks_dict, doc_id, 1, meta)

        # 建立 image_index → step_card chunk_text 映射
        pipeline_bindings = {}  # image_index → chunk_text
        step_cards = [c for c in chunks if c.chunk_type == "step_card"]

        for sc in step_cards:
            img_refs = (sc.extra or {}).get("image_refs", [])
            for ref in img_refs:
                img_idx = ref.get("image_index")
                if img_idx is not None:
                    pipeline_bindings[img_idx] = sc.chunk_text

        if not pipeline_bindings:
            continue

        docs_with_bindings += 1

        # ── 对比: GT preceding_text 是否出现在 pipeline chunk_text 中 ──
        doc_correct = 0
        doc_checked = 0
        mismatches = []
        correct_examples = []

        for gt in gt_bindings:
            img_idx = gt["image_index"]
            if img_idx not in pipeline_bindings:
                continue

            doc_checked += 1
            total_images_checked += 1

            gt_text = gt["preceding_text"]
            pl_text = pipeline_bindings[img_idx]

            # 匹配策略: GT 步骤文本的核心内容（去掉步骤编号前缀后取前 30 字）
            # 是否出现在 pipeline chunk_text 中
            gt_core = re.sub(r"^[\s\d\.、）)（(]+", "", gt_text)[:40]

            if gt_core and gt_core in pl_text:
                doc_correct += 1
                total_images_correct += 1
                correct_examples.append({
                    "img_idx": img_idx,
                    "gt_core": gt_core[:35],
                    "target_ref": os.path.basename(gt["target_ref"]) if gt["target_ref"] else f"img_{img_idx}",
                })
            else:
                # 二次匹配: 取 GT 文本前 20 字检查
                gt_core2 = gt_text[:25]
                if gt_core2 in pl_text:
                    doc_correct += 1
                    total_images_correct += 1
                    correct_examples.append({
                        "img_idx": img_idx,
                        "gt_core": gt_core2[:35],
                        "target_ref": os.path.basename(gt["target_ref"]) if gt["target_ref"] else f"img_{img_idx}",
                    })
                else:
                    mismatches.append({
                        "img_idx": img_idx,
                        "gt_text": gt_text[:50],
                        "pl_text": pl_text[:50],
                        "target_ref": os.path.basename(gt["target_ref"]) if gt["target_ref"] else f"img_{img_idx}",
                    })

        accuracy = doc_correct / doc_checked * 100 if doc_checked > 0 else 0
        status = f"{GREEN}✅{RESET}" if accuracy >= 95 else (f"{YELLOW}⚠️{RESET}" if accuracy >= 70 else f"{RED}❌{RESET}")

        doc_results.append({
            "name": basename,
            "step_cards": len(step_cards),
            "checked": doc_checked,
            "correct": doc_correct,
            "accuracy": accuracy,
            "mismatches": mismatches,
        })

        acc_str = f"{GREEN}{accuracy:.0f}%{RESET}" if accuracy >= 95 else (f"{YELLOW}{accuracy:.0f}%{RESET}" if accuracy >= 70 else f"{RED}{accuracy:.0f}%{RESET}")
        print(f"\n  {status} {basename[:55]}")
        print(f"      step_card: {len(step_cards)}  |  图片已检: {doc_checked}  |  正确: {doc_correct}  |  准确率: {acc_str}")

        if mismatches:
            for m in mismatches[:3]:
                print(f"      {RED}✗{RESET} [{m['target_ref']}]")
                print(f"         GT文本: 「{m['gt_text']}」")
                print(f"         绑定到: 「{m['pl_text']}」")
        if correct_examples:
            for ex in correct_examples[:2]:
                print(f"      {GREEN}✓{RESET} [{ex['target_ref']}] → 「{ex['gt_core']}…」")

    except Exception as e:
        print(f"\n  {RED}✗ {basename}: {e}{RESET}")
        import traceback; traceback.print_exc()

# ══════════════════════════════════════════════════════════════
print(f"\n{BOLD}{CYAN}{'━' * 70}{RESET}")
print(f"{BOLD}  汇总{RESET}")
print(f"{'━' * 70}")
print(f"  扫描文档: {total_docs}")
print(f"  含图文绑定: {docs_with_bindings}")
print(f"  总检查图片: {total_images_checked}")
print(f"  正确绑定: {total_images_correct}")
overall_acc = total_images_correct / total_images_checked * 100 if total_images_checked > 0 else 0
acc_color = GREEN if overall_acc >= 95 else (YELLOW if overall_acc >= 70 else RED)
print(f"  {BOLD}总准确率: {acc_color}{overall_acc:.1f}%{RESET}")

wrong_docs = [d for d in doc_results if d["accuracy"] < 95]
if wrong_docs:
    print(f"\n  {YELLOW}需关注的文档:{RESET}")
    for d in wrong_docs:
        print(f"    • {d['name'][:45]}: {d['accuracy']:.0f}% ({len(d['mismatches'])} 错)")
else:
    print(f"\n  {GREEN}所有图文绑定均正确 ✓{RESET}")

print(f"{'━' * 70}\n")
