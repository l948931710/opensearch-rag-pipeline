# -*- coding: utf-8 -*-
"""
image_funnel_processor.py — 图像 Embedding 三大过滤漏斗处理器

实现对文档提取的图片资产进行三阶段过滤：
1. 静态 Heuristics 过滤：去除低分辨率、小尺寸、极端高宽比的装饰性线段或图标。
2. 文本密度过滤：评估 OCR 提取文本的长度，若文本密度极高则直接路由为纯文本块。
3. 语义与安全多模态审计：评估业务相关性并对隔离文档进行合规性（印章、签名、身份证等）审计。
支持公共 Raw 路径免 LLM/VLM 敏感审计的成本控制设计。
"""

import os
from typing import Dict, Any, Tuple
from PIL import Image

from opensearch_pipeline.config import get_config
from opensearch_pipeline.extraction.ocr_client import OCRClient


class ImageFunnelProcessor:
    """图像过滤漏斗核心处理器。"""

    def __init__(self, simulate: bool = None):
        config = get_config()
        self.simulate = simulate if simulate is not None else config.simulate
        self.simulate_api = config.simulate_api
        
        # 实例化 OCR 客户端
        self.ocr_client = OCRClient(
            api_key=config.ocr.api_key,
            api_base_url=config.ocr.api_base_url,
            ocr_model=config.ocr.model,
            simulate=self.simulate or self.simulate_api
        )

    def process_image(self, local_path: str, doc_id: str, is_public: bool = True) -> Dict[str, Any]:
        """
        三阶段级联过滤单张图像。
        
        参数:
            local_path: 图片本地文件路径。
            doc_id: 关联的文档ID。
            is_public: 该图片对应的原始文档是否在普通 raw 目录下（无 _quarantine/ 路径标记）。
                       若为 True，则表示内部公开文档，完全绕过 VLM 敏感内容审计 (Funnel 3)。
                       
        返回:
            Dict[str, Any]: 包含路由决策结果的元数据字典。
                - "status": "DISCARD_DECORATIVE" | "ROUTE_TO_TEXT" | "ROUTE_TO_VECTOR" | "QUARANTINE_SENSITIVE"
                - "ocr_text": OCR 提取的文本（仅在 ROUTE_TO_TEXT 或有效时存在）
                - "visual_summary": VLM 语义摘要（仅在 ROUTE_TO_VECTOR 时存在）
                - "width": 像素宽
                - "height": 像素高
                - "file_size_kb": 文件大小 (KB)
        """
        filename = os.path.basename(local_path)

        # ─── Funnel 1: Heuristic Structural & Layout Filter ───
        width, height, file_size_kb = self._static_heuristics(local_path)
        aspect_ratio = max(width / max(height, 1), height / max(width, 1))

        # 过滤规则：分辨率小于50px，文件小于3KB，或者长宽比大于8.0的装饰线条
        if width < 50 or height < 50 or file_size_kb < 3.0 or aspect_ratio > 8.0:
            print(f"    [Funnel 1] Discarded decorative image: {filename} ({width}x{height}, {file_size_kb:.1f}KB, ratio={aspect_ratio:.1f})")
            return {
                "status": "DISCARD_DECORATIVE",
                "width": width,
                "height": height,
                "file_size_kb": file_size_kb,
                "reason": "Funnel 1: Structural Heuristics Low Resolution/Size/Aspect Ratio"
            }

        # ─── Funnel 2: Text Density Filter (OCR Coverage) ───
        ocr_result = self.ocr_client.ocr_image(local_path, doc_id)
        ocr_text = ocr_result.combined_text or ""
        
        # 文本密度规则：若提取文本大于120个字符，或者高度密集的表格文字，归入正文段落
        if len(ocr_text.strip()) > 120:
            print(f"    [Funnel 2] Routed image to text paragraph: {filename} (text_len={len(ocr_text)})")
            return {
                "status": "ROUTE_TO_TEXT",
                "ocr_text": ocr_text.strip(),
                "width": width,
                "height": height,
                "file_size_kb": file_size_kb,
                "reason": "Funnel 2: High OCR Text Density (>120 chars)"
            }

        # ─── Funnel 3: Semantic Value & Safety Filter (VLM Verification) ───
        # 如果是普通 raw 目录下的内部公开文档，强制 bypass 安全性及敏感印章审计，不向 VLM 传递敏感内容检验需求
        bypass_safety = is_public
        
        vlm_status, visual_summary = self._vlm_audit_and_summary(
            local_path=local_path,
            doc_id=doc_id,
            bypass_safety=bypass_safety
        )

        if vlm_status == "SENSITIVE":
            print(f"    [Funnel 3] 🚨 Sensitive content detected in non-public asset: {filename}")
            return {
                "status": "QUARANTINE_SENSITIVE",
                "width": width,
                "height": height,
                "file_size_kb": file_size_kb,
                "reason": "Funnel 3: VLM Audit Detected Sensitive Entities (Seals, Stamps, ID Card, Signatures)"
            }
        elif vlm_status == "LOW_RELEVANCE":
            print(f"    [Funnel 3] Discarded low-relevance graphic: {filename}")
            return {
                "status": "DISCARD_DECORATIVE",
                "width": width,
                "height": height,
                "file_size_kb": file_size_kb,
                "reason": "Funnel 3: Low Semantic Value or Generic Stock Photo"
            }
        else:
            # ROUTE_TO_VECTOR
            print(f"    [Funnel 3] Routed image to vector queue: {filename} -> Summary: {visual_summary}")
            return {
                "status": "ROUTE_TO_VECTOR",
                "visual_summary": visual_summary,
                "ocr_text": ocr_text.strip(),
                "width": width,
                "height": height,
                "file_size_kb": file_size_kb,
            }

    def _static_heuristics(self, local_path: str) -> Tuple[int, int, float]:
        """获取图片的基础物理属性。"""
        try:
            with Image.open(local_path) as img:
                width, height = img.size
            file_size_bytes = os.path.getsize(local_path)
            file_size_kb = file_size_bytes / 1024.0
            return width, height, file_size_kb
        except Exception as e:
            # 兜底返回，如果解析异常则置零，在 Funnel 1 直接丢弃
            print(f"    ⚠️ Warning failed to read image heuristics: {e}")
            return 0, 0, 0.0

    def _vlm_audit_and_summary(self, local_path: str, doc_id: str, bypass_safety: bool) -> Tuple[str, str]:
        """
        调用通义千问多模态大模型进行安全与语义审计。
        
        返回:
            Tuple[str, str]: (safety_and_relevance_status, summary_text)
                status 可以为: "CLEAN", "SENSITIVE", "LOW_RELEVANCE"
        """
        filename = os.path.basename(local_path).lower()

        # 模拟模式或 API 模拟模式下的规则逻辑（支持 deterministic 的单元测试）
        if self.simulate or self.simulate_api:
            # 模拟审计机制：通过文件名或特定的模拟前缀测试
            if not bypass_safety and any(k in filename for k in ["seal", "stamp", "id_card", "signature", "confidential"]):
                return "SENSITIVE", ""
            
            if any(k in filename for k in ["logo", "banner", "decoration", "spacer", "background"]):
                return "LOW_RELEVANCE", ""

            # 正常高价值商业图表
            summary = f"[Simulated Multimodal Caption] An informative diagram or chart found in doc {doc_id} describing system workflows."
            return "CLEAN", summary

        # ── 生产模式：调用阿里云通义千问 Qwen-VL 视觉大模型 ──
        config = get_config()
        api_key = config.ocr.api_key
        api_base_url = config.ocr.api_base_url
        model_name = config.ocr.model

        if not api_key:
            # 容灾兜底：如果没有 API KEY，在公开文档下通过，在隔离文档下高风险隔离
            print("    ⚠️ VLM API Key is missing. Falling back to safe defaults.")
            if bypass_safety:
                return "CLEAN", "[VLM Fallback Summary] Diagram or image illustration content."
            else:
                return "SENSITIVE", ""

        import base64
        import requests
        import json

        try:
            with open(local_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")
            
            ext = os.path.splitext(local_path)[1].lower()
            mime_type = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"

            # 组装安全与语义判定的多模态 prompt
            # 如果 bypass_safety 为 True，则不对个人指纹印章进行高敏感检验，从而专注于相关性
            safety_instruction = ""
            if not bypass_safety:
                safety_instruction = (
                    "- 'SENSITIVE': If the image contains sensitive corporate red seal stamps, "
                    "confidential signatures, personal ID cards, passport covers, bank accounts, or safety warning seals."
                )

            prompt = (
                "You are an advanced document image layout auditor. Analyze the visual schema of this image page. "
                "Classify this asset status into one of these categories:\n"
                f"{safety_instruction}\n"
                "- 'LOW_RELEVANCE': If the image is a decorative placeholder, margin graphic, stock photograph, or raw layout blank line.\n"
                "- 'CLEAN': If the image contains rich corporate or business informative schematics, architecture charts, technical tables, data plots, or visual workflows.\n\n"
                "Return a strict JSON format containing these two keys:\n"
                "{\n"
                '  "status": "SENSITIVE" | "LOW_RELEVANCE" | "CLEAN",\n'
                '  "summary": "A highly precise 100-character description of the technical or informational semantic meaning of the image"\n'
                "}\n"
                "Do not include any extra code block wrappers or explanations."
            )

            # 请求通义千问多模态端点
            url = f"{api_base_url}/services/aigc/multimodal-generation/generation"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model_name,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"image": f"data:{mime_type};base64,{b64_data}"},
                                {"text": prompt}
                            ]
                        }
                    ]
                }
            }

            resp = requests.post(url, json=payload, headers=headers, timeout=40)
            if resp.status_code != 200:
                raise RuntimeError(f"Qwen-VL VLM HTTP {resp.status_code}: {resp.text[:400]}")

            data = resp.json()
            try:
                choices = data["output"]["choices"]
                content = choices[0]["message"]["content"]
                
                # 兼容可能返回的字符串或包含 text 的 list
                result_str = ""
                if isinstance(content, list):
                    result_str = "".join(item.get("text", "") for item in content if isinstance(item, dict))
                elif isinstance(content, str):
                    result_str = content
                
                # 清洗 JSON 标记
                result_str = result_str.replace("```json", "").replace("```", "").strip()
                result_json = json.loads(result_str)
                
                status = result_json.get("status", "CLEAN")
                summary = result_json.get("summary", "[VLM Captured Schema Summary]")

                # 纠合逻辑：如果 bypass_safety 为真，即便是 SENSITIVE 也强制转为 CLEAN
                if bypass_safety and status == "SENSITIVE":
                    status = "CLEAN"

                return status, summary
            except Exception as e:
                print(f"    ⚠️ Warning failed to parse Qwen-VL response JSON: {e}. Output content: {content[:300]}")
                # 容灾降级
                return "CLEAN", "[VLM Analysis Timeout] Informative business asset details."

        except Exception as e:
            print(f"    ⚠️ Warning VLM API execution failed: {e}")
            if bypass_safety:
                return "CLEAN", f"[VLM Fallback Captioned] Graphic asset {filename} parsed under degradation mode."
            else:
                return "SENSITIVE", ""
