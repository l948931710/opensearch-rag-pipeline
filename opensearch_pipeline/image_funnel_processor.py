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
                return "CLEAN", "[VLM Fallback Summary] 图片或示意图内容。"
            else:
                return "SENSITIVE", ""

        import base64
        import requests
        import json
        import io

        try:
            # ── 大图片压缩：避免 base64 过大导致上传超时 ──
            file_size = os.path.getsize(local_path)
            MAX_RAW_BYTES = 500 * 1024  # 500KB 阈值

            if file_size > MAX_RAW_BYTES:
                # 压缩图片到 JPEG quality=60，限制最大边 1280px
                try:
                    with Image.open(local_path) as img:
                        # 转换 RGBA/P 为 RGB（JPEG 不支持 alpha）
                        if img.mode in ("RGBA", "P"):
                            img = img.convert("RGB")
                        # 限制最大边
                        max_side = 1280
                        if max(img.size) > max_side:
                            img.thumbnail((max_side, max_side), Image.LANCZOS)
                        buf = io.BytesIO()
                        img.save(buf, format="JPEG", quality=60)
                        b64_data = base64.b64encode(buf.getvalue()).decode("utf-8")
                        mime_type = "image/jpeg"
                        compressed_kb = len(buf.getvalue()) / 1024
                        print(f"    [VLM] Compressed {filename}: {file_size/1024:.0f}KB → {compressed_kb:.0f}KB")
                except Exception as comp_err:
                    print(f"    ⚠️ Image compression failed: {comp_err}, using raw file")
                    with open(local_path, "rb") as f:
                        b64_data = base64.b64encode(f.read()).decode("utf-8")
                    ext = os.path.splitext(local_path)[1].lower()
                    mime_type = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
            else:
                with open(local_path, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("utf-8")
                ext = os.path.splitext(local_path)[1].lower()
                mime_type = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"

            # ── 组装中文多模态 prompt ──
            # 要求返回中文 summary，以便 BM25 和 Dense embedding 与中文 query 对齐
            safety_instruction = ""
            if not bypass_safety:
                safety_instruction = (
                    "- 'SENSITIVE': 如果图片包含敏感的公司红色印章、机密签名、身份证件、"
                    "护照封面、银行账号或安全警告印章。\n"
                )

            prompt = (
                "你是一名企业文档图片审核专家。分析这张图片的内容和用途，"
                "将其归入以下类别之一：\n"
                f"{safety_instruction}"
                "- 'LOW_RELEVANCE': 如果图片是装饰性占位图、页边留白图、通用素材图或空白排版线条。\n"
                "- 'CLEAN': 如果图片包含有价值的企业操作流程图、技术示意图、设备安装步骤、"
                "数据表格、系统界面截图或工作流程图。\n\n"
                "请用严格的 JSON 格式返回以下两个字段：\n"
                "{\n"
                '  "status": "SENSITIVE" | "LOW_RELEVANCE" | "CLEAN",\n'
                '  "summary": "用中文精确描述此图片展示的技术或业务信息内容（约100字）"\n'
                "}\n"
                "不要包含任何代码块标记或多余解释。"
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

            resp = requests.post(url, json=payload, headers=headers, timeout=60)
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
                summary = result_json.get("summary", "[VLM 内容描述]")

                # 纠合逻辑：如果 bypass_safety 为真，即便是 SENSITIVE 也强制转为 CLEAN
                if bypass_safety and status == "SENSITIVE":
                    status = "CLEAN"

                return status, summary
            except Exception as e:
                print(f"    ⚠️ Warning failed to parse Qwen-VL response JSON: {e}. Output content: {content[:300]}")
                # 容灾降级
                return "CLEAN", "[VLM 解析异常] 企业文档内图片资产。"

        except Exception as e:
            print(f"    ⚠️ Warning VLM API execution failed: {e}")
            if bypass_safety:
                return "CLEAN", f"[VLM 降级] 图片资产 {filename} 在降级模式下解析。"
            else:
                return "SENSITIVE", ""

