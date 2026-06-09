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

    def process_image(self, local_path: str, doc_id: str, is_public: bool = True,
                       doc_title: str = "") -> Dict[str, Any]:
        """
        三阶段级联过滤单张图像。
        
        参数:
            local_path: 图片本地文件路径。
            doc_id: 关联的文档ID。
            is_public: 该图片对应的原始文档是否在普通 raw 目录下（无 _quarantine/ 路径标记）。
                       若为 True，则表示内部公开文档，完全绕过 VLM 敏感内容审计 (Funnel 3)。
            doc_title: 文档标题，为 VLM 提供业务上下文。
                       
        返回:
            Dict[str, Any]: 包含路由决策结果的元数据字典。
                - "status": "DISCARD_DECORATIVE" | "ROUTE_TO_TEXT" | "ROUTE_TO_VECTOR" | "QUARANTINE_SENSITIVE"
                - "ocr_text": OCR 提取的文本（仅在 ROUTE_TO_TEXT 或有效时存在）
                - "visual_summary": VLM caption（仅在 ROUTE_TO_VECTOR 时存在）
                - "image_category": VLM 判断的图片类别（仅在 ROUTE_TO_VECTOR 时存在）

                - "vlm_annotation_map": VLM 识别的标注映射（仅在 ROUTE_TO_VECTOR 时存在）
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
        
        # 文本密度标记：高文字量的图片不再直接 ROUTE_TO_TEXT，
        # 而是交给 VLM 判断是否为实物照片（包装箱/合格证等虽然文字多但本质是产品照片）。
        # 仅当 VLM 确认为纯文字内容（非实物照片）时才走文本路由。
        is_text_heavy = len(ocr_text.strip()) > 120

        # ─── Funnel 3: Semantic Value & Safety Filter (VLM Verification) ───
        # 如果是普通 raw 目录下的内部公开文档，强制 bypass 安全性及敏感印章审计，不向 VLM 传递敏感内容检验需求
        bypass_safety = is_public
        
        vlm_result = self._vlm_audit_and_summary(
            local_path=local_path,
            doc_id=doc_id,
            bypass_safety=bypass_safety,
            doc_title=doc_title,
            ocr_text=ocr_text.strip(),
        )
        vlm_status = vlm_result["status"]
        # 若 VLM 调用是降级兜底（超时/解析失败），结论不可信，向下游传递 degraded 标记，
        # 使其不被写入跨文档持久缓存。
        vlm_degraded = bool(vlm_result.get("degraded", False))

        if vlm_status == "SENSITIVE":
            print(f"    [Funnel 3] 🚨 Sensitive content detected in non-public asset: {filename}")
            return {
                "status": "QUARANTINE_SENSITIVE",
                "width": width,
                "height": height,
                "file_size_kb": file_size_kb,
                "degraded": vlm_degraded,
                "reason": "Funnel 3: VLM Audit Detected Sensitive Entities (Seals, Stamps, ID Card, Signatures)"
            }
        elif vlm_status == "LOW_RELEVANCE":
            # 低语义价值：如果文字多，仍然可以做文本路由（不浪费 OCR 结果）
            if is_text_heavy:
                print(f"    [Funnel 2→3] Routed text-heavy low-relevance image to text: {filename} (text_len={len(ocr_text)})")
                return {
                    "status": "ROUTE_TO_TEXT",
                    "ocr_text": ocr_text.strip(),
                    "width": width,
                    "height": height,
                    "file_size_kb": file_size_kb,
                    "reason": "Funnel 2+3: High OCR Text + VLM Low Relevance → Text Paragraph"
                }
            print(f"    [Funnel 3] Discarded low-relevance graphic: {filename}")
            return {
                "status": "DISCARD_DECORATIVE",
                "width": width,
                "height": height,
                "file_size_kb": file_size_kb,
                "reason": "Funnel 3: Low Semantic Value or Generic Stock Photo"
            }
        else:
            # ROUTE_TO_VECTOR — VLM 认为有语义价值
            caption = vlm_result.get("caption", "")
            img_cat = vlm_result.get("image_category", "unknown")
            anno_map = vlm_result.get("annotation_map", {})

            # 实物照片类别：即使文字多也保留为图片（包装箱/合格证/标签等）
            _PHOTO_CATS = {"product_photo", "inspection_photo", "test_photo",
                           "packaging_photo", "process_flow"}
            
            if is_text_heavy and img_cat not in _PHOTO_CATS:
                # 文字密集 + VLM 未识别为实物照片 → 降级为文本段落
                # 仍保留 VLM 元数据（annotation_map / visual_summary），供下游步骤绑定使用
                print(f"    [Funnel 2→3] Routed text-heavy image to text: {filename} (cat={img_cat}, text_len={len(ocr_text)})")
                return {
                    "status": "ROUTE_TO_TEXT",
                    "ocr_text": ocr_text.strip(),
                    "image_category": img_cat,
                    "visual_summary": caption,
                    "vlm_annotation_map": anno_map,
                    "width": width,
                    "height": height,
                    "file_size_kb": file_size_kb,
                    "degraded": vlm_degraded,
                    "reason": f"Funnel 2+3: High OCR Text + Non-photo category '{img_cat}' → Text Paragraph"
                }

            print(f"    [Funnel 3] Routed to vector: {filename} -> [{img_cat}] {caption[:80]}")
            return {
                "status": "ROUTE_TO_VECTOR",
                "visual_summary": caption,
                "image_category": img_cat,
                "vlm_annotation_map": anno_map,
                "ocr_text": ocr_text.strip(),
                "width": width,
                "height": height,
                "file_size_kb": file_size_kb,
                "degraded": vlm_degraded,
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

    def _vlm_audit_and_summary(self, local_path: str, doc_id: str, bypass_safety: bool,
                                doc_title: str = "", ocr_text: str = "") -> Dict[str, Any]:
        """
        调用通义千问多模态大模型进行安全与语义审计 + 结构化语义提取。
        
        返回:
            Dict[str, Any]: {
                "status": "CLEAN" | "SENSITIVE" | "LOW_RELEVANCE",
                "caption": str,
                "image_category": str,
                "annotation_map": Dict[str, str],
            }
        """
        filename = os.path.basename(local_path).lower()

        # 模拟模式或 API 模拟模式下的规则逻辑（支持 deterministic 的单元测试）
        if self.simulate or self.simulate_api:
            # 模拟审计机制：通过文件名或特定的模拟前缀测试
            if not bypass_safety and any(k in filename for k in ["seal", "stamp", "id_card", "signature", "confidential"]):
                return {"status": "SENSITIVE", "caption": "", "image_category": "decorative",
                        "annotation_map": {}}
            
            if any(k in filename for k in ["logo", "banner", "decoration", "spacer", "background"]):
                return {"status": "LOW_RELEVANCE", "caption": "", "image_category": "decorative",
                        "annotation_map": {}}

            # 正常高价值商业图表
            return {
                "status": "CLEAN",
                "caption": f"[Simulated] Informative diagram in doc {doc_id} describing system workflows.",
                "image_category": "step_screenshot",
                "annotation_map": {},
            }

        # ── 生产模式：调用阿里云通义千问 Qwen-VL 视觉大模型 ──
        config = get_config()
        api_key = config.ocr.api_key
        api_base_url = config.ocr.api_base_url
        # VLM 使用独立模型配置，fallback 到 OCR 模型
        model_name = config.ocr.vlm_model or config.ocr.model

        if not api_key:
            # 容灾兜底：如果没有 API KEY，在公开文档下通过，在隔离文档下高风险隔离。
            # degraded=True → 不缓存（没有真正审计过，下次有 key 时应重跑）。
            print("    ⚠️ VLM API Key is missing. Falling back to safe defaults.")
            if bypass_safety:
                return {"status": "CLEAN", "caption": "[VLM Fallback] 图片或示意图内容。",
                        "image_category": "unknown", "annotation_map": {}, "degraded": True}
            else:
                return {"status": "SENSITIVE", "caption": "", "image_category": "unknown",
                        "annotation_map": {}, "degraded": True}

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
                        if img.mode in ("RGBA", "P"):
                            img = img.convert("RGB")
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

            # ── 组装结构化 prompt ──
            safety_instruction = ""
            if not bypass_safety:
                safety_instruction = (
                    "- 'SENSITIVE': 图片包含公司红色印章、机密签名、身份证件、"
                    "护照封面、银行账号或安全警告印章。\n"
                )

            # 上下文信息（可选）
            context_block = ""
            if doc_title or ocr_text:
                context_parts = []
                if doc_title:
                    context_parts.append(f"文档标题：{doc_title[:100]}")
                if ocr_text:
                    context_parts.append(f"图片OCR文本：{ocr_text[:300]}")
                context_block = "\n【参考信息】\n" + "\n".join(context_parts) + "\n"

            prompt = (
                "你是企业工业SOP图文知识库的图片语义解析器。"
                "分析这张图片并输出严格JSON，不要输出Markdown或多余解释。\n\n"
                "【业务背景】\n"
                "文档来自塑料制品企业的SOP、U8/ERP/MES操作手册、生产检验流程、"
                "验货流程、设备点检、包装规范、质量测试说明等。\n"
                f"{context_block}\n"
                "【任务】\n"
                "1. 判断 status（安全与价值过滤）：\n"
                f"{safety_instruction}"
                "   - 'LOW_RELEVANCE': 装饰图、封面Logo、页边留白、空白排版线条。\n"
                "   - 'CLEAN': 有业务价值的图片。\n\n"
                "2. 判断 image_category（图片类别）：\n"
                "   - step_screenshot: 系统操作截图（菜单、按钮、表单、U8/ERP界面）\n"
                "   - test_photo: 测试/实验照片（耐热测试、称重、温度计、样品测试）\n"
                "   - inspection_photo: 验货/检查现场照片（外方验货、包装检查）\n"
                "   - form_image: 表单/记录单截图（交货单、检验单、入库单）\n"
                "   - visual_knowledge: 独立知识图（字段说明、缺陷对照、规格对照表）\n"
                "   - process_flow: 工艺流程图、生产流程图、CCP流程图\n"
                "   - product_photo: 产品实物照片、包装成品照片、外箱照片、标签照片、合格证照片\n"
                "   - logo_header: 封面Logo、公司标志、页眉装饰\n"
                "   - decorative: 其他纯装饰图/留白/线条\n"
                "   - unknown: 无法判断\n\n"
                "3. 生成 caption：用一句简洁中文描述图片可见内容（50-100字）。\n"
                "   只描述看得见的对象/动作/界面/字段，不要编造看不清的读数或结论。\n\n"
                "4. 生成 annotation_map：如果图片有①②③标注、红框、箭头等标记，\n"
                "   提取标注对应关系（如{\"①\":\"业务导航\"}）。没有则返回空对象{}。\n\n"
                "【输出JSON格式】\n"
                "{\n"
                '  "status": "CLEAN",\n'
                '  "image_category": "step_screenshot",\n'
                '  "caption": "描述文本",\n'
                '  "annotation_map": {}\n'
                "}"
            )

            # ── 判断端点模式 ──
            use_compat = "qwen3" in model_name.lower() or "compatible" in api_base_url.lower()

            if use_compat:
                import re as _re
                domain_match = _re.search(r'https?://([^/]+)', api_base_url)
                domain = domain_match.group(1) if domain_match else "dashscope.aliyuncs.com"
                url = f"https://{domain}/compatible-mode/v1/chat/completions"

                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": model_name,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}},
                                {"type": "text", "text": prompt}
                            ]
                        }
                    ]
                }

                resp = requests.post(url, json=payload, headers=headers, timeout=(10, 90))
                if resp.status_code != 200:
                    raise RuntimeError(f"VLM (compat) HTTP {resp.status_code}: {resp.text[:400]}")

                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            else:
                # DashScope 原生多模态端点（兼容旧版 qwen-vl-ocr-latest 等）
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

                resp = requests.post(url, json=payload, headers=headers, timeout=(10, 90))
                if resp.status_code != 200:
                    raise RuntimeError(f"Qwen-VL VLM HTTP {resp.status_code}: {resp.text[:400]}")

                data = resp.json()
                choices = data["output"]["choices"]
                content = choices[0]["message"]["content"]

                # 兼容旧版返回的 list 格式
                if isinstance(content, list):
                    content = "".join(item.get("text", "") for item in content if isinstance(item, dict))

            # ── 解析 JSON 结果 ──
            try:
                result_str = content if isinstance(content, str) else str(content)
                result_str = result_str.replace("```json", "").replace("```", "").strip()
                result_json = json.loads(result_str)

                status = result_json.get("status", "CLEAN")
                caption = result_json.get("caption", "[VLM 内容描述]")
                image_category = result_json.get("image_category", "unknown")
                annotation_map = result_json.get("annotation_map", {})

                # 纠合逻辑：bypass_safety 时 SENSITIVE 强制转 CLEAN
                if bypass_safety and status == "SENSITIVE":
                    status = "CLEAN"

                # 确保 annotation_map 是 dict
                if not isinstance(annotation_map, dict):
                    annotation_map = {}

                return {
                    "status": status,
                    "caption": caption,
                    "image_category": image_category,
                    "annotation_map": annotation_map,
                }
            except Exception as e:
                print(f"    ⚠️ Failed to parse VLM JSON: {e}. Raw: {str(content)[:300]}")
                # 容灾降级：把原始文本当 caption 用。degraded=True → 调用方不得缓存此结论，
                # 否则一次解析失败会被持久化、跨文档/跨运行永久复用。
                fallback_caption = str(content)[:200] if content else "[VLM 解析异常]"
                return {
                    "status": "CLEAN",
                    "caption": fallback_caption,
                    "image_category": "unknown",
                    "annotation_map": {},
                    "degraded": True,
                }

        except Exception as e:
            print(f"    ⚠️ VLM API execution failed: {e}")
            # 兜底结论（超时/网络/HTTP 错误）必须标记 degraded：这是一次性故障，不能被缓存成
            # 永久标签（否则一次超时会让某张图在所有文档里永远被误判为 CLEAN 或 SENSITIVE）。
            if bypass_safety:
                return {"status": "CLEAN", "caption": f"[VLM 降级] 图片资产 {filename}。",
                        "image_category": "unknown", "annotation_map": {}, "degraded": True}
            else:
                return {"status": "SENSITIVE", "caption": "", "image_category": "unknown",
                        "annotation_map": {}, "degraded": True}


