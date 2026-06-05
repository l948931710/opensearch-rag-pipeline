# -*- coding: utf-8 -*-
"""
ocr_client.py — Qwen-VL OCR 统一客户端

单一 OCR 来源，消除 scan_pending_clean.py 和 faq_extract.py 的分叉。
支持 page-level 粒度：每页独立返回 OCR 文本。

生产依赖：dashscope API, fitz (PyMuPDF), oss2
模拟模式：返回模拟 OCR 结果
"""

import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from opensearch_pipeline.extraction.schema import ExtractedBlock


@dataclass
class OCRPageResult:
    """单页 OCR 结果。"""
    page_num: int
    text: str
    status: str = "DONE"     # DONE / FAILED / SIMULATED
    error: Optional[str] = None


@dataclass
class OCRResult:
    """完整 OCR 结果（按页保留粒度）。"""
    pages: List[OCRPageResult] = field(default_factory=list)
    combined_text: str = ""
    status: str = "DONE"     # DONE / FAILED / SIMULATED / SKIPPED
    error: Optional[str] = None

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def to_blocks(self) -> List[ExtractedBlock]:
        """将 OCR 结果转为 ExtractedBlock 列表（带 page_num）。"""
        blocks = []
        for page in self.pages:
            if page.text.strip():
                blocks.append(ExtractedBlock(
                    block_type="ocr_text",
                    text=page.text.strip(),
                    page_num=page.page_num,
                    source="ocr",
                ))
        return blocks


class OCRClient:
    """
    OCR 客户端。

    生产模式：调用 Qwen-VL 做图片 OCR。
    模拟模式：返回模拟结果。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base_url: str = "https://dashscope.aliyuncs.com/api/v1",
        ocr_model: str = "qwen-vl-max",
        max_ocr_pages: int = 20,
        simulate: bool = True,
    ):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self.api_base_url = api_base_url
        self.ocr_model = ocr_model
        self.max_ocr_pages = max_ocr_pages
        self.simulate = simulate

    def ocr_pdf(
        self,
        local_path: str,
        doc_id: str,
        oss_bucket=None,
        page_nums: Optional[List[int]] = None,
    ) -> OCRResult:
        """
        对 PDF 做 OCR（按页）。

        page_nums: 仅 OCR 这些页（1-based）；None = 前 max_ocr_pages 页（旧行为）。
                   用于 per-page OCR gate——只 OCR 扫描页/坏字体页，不浪费在有文本的页上。

        生产模式：PDF → 图片 → 上传 OSS → Qwen-VL OCR
        模拟模式：返回按页模拟文本
        """
        if self.simulate:
            return self._simulate_pdf_ocr(doc_id, page_nums)

        return self._real_pdf_ocr(local_path, doc_id, oss_bucket, page_nums)

    def ocr_image(
        self,
        local_path: str,
        doc_id: str,
        oss_bucket=None,
    ) -> OCRResult:
        """
        对单张图片做 OCR。

        生产模式：上传 → Qwen-VL OCR
        模拟模式：返回模拟文本
        """
        if self.simulate:
            return OCRResult(
                pages=[OCRPageResult(page_num=1, text="[OCR: image content recognized]", status="SIMULATED")],
                combined_text="[OCR: image content recognized]",
                status="SIMULATED",
            )

        return self._real_image_ocr(local_path, doc_id, oss_bucket)

    # ── 模拟实现 ──

    def _simulate_pdf_ocr(self, doc_id: str, page_nums: Optional[List[int]] = None) -> OCRResult:
        """模拟 PDF OCR。page_nums 指定时按这些页模拟（反映 per-page 选择），否则默认 2 页。"""
        nums = list(page_nums) if page_nums else [1, 2]
        nums = nums[:self.max_ocr_pages]
        pages = [
            OCRPageResult(
                page_num=p,
                text=f"[OCR page {p}: scanned content for {doc_id}]",
                status="SIMULATED",
            )
            for p in nums
        ]
        combined = "\n\n".join(p.text for p in pages)
        return OCRResult(pages=pages, combined_text=combined, status="SIMULATED")

    # ── 生产实现（基于 Gemini Vision） ──

    def _real_pdf_ocr(self, local_path: str, doc_id: str, oss_bucket=None,
                      page_nums: Optional[List[int]] = None) -> OCRResult:
        """
        真实 PDF OCR (Qwen-VL Vision)。
        流程：
        1. PDF → 图片（fitz）
        2. Base64 编码
        3. Qwen-VL API 提取文本

        page_nums: 仅 OCR 这些页（1-based）；None = 前 max_ocr_pages 页。
        """
        if not self.api_key:
            return OCRResult(status="FAILED", error="API KEY not configured")

        try:
            import fitz
        except ImportError:
            return OCRResult(status="FAILED", error="PyMuPDF (fitz) not installed")

        import base64
        pages = []

        try:
            doc = fitz.open(local_path)
            n = len(doc)
            # 选页：指定则只 OCR 这些页（去重、有序、限页内），否则前 N 页（旧行为）
            if page_nums:
                page_idxs = sorted({p - 1 for p in page_nums if 1 <= p <= n})
            else:
                page_idxs = list(range(n))
            if len(page_idxs) > self.max_ocr_pages:
                dropped = len(page_idxs) - self.max_ocr_pages
                page_idxs = page_idxs[:self.max_ocr_pages]
                print(f"    ⚠️ [ocr] capped at max_ocr_pages={self.max_ocr_pages}; "
                      f"{dropped} low-text page(s) left un-OCR'd", flush=True)

            for page_idx in page_idxs:
                page = doc[page_idx]
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                
                # 直接获取图像二进制数据
                img_data = pix.tobytes("png")
                b64_data = base64.b64encode(img_data).decode('utf-8')

                try:
                    page_text = self._call_ocr_api(b64_data, "image/png")
                    pages.append(OCRPageResult(
                        page_num=page_idx + 1,
                        text=page_text,
                        status="DONE",
                    ))
                except Exception as e:
                    pages.append(OCRPageResult(
                        page_num=page_idx + 1,
                        text="",
                        status="FAILED",
                        error=repr(e),
                    ))

            doc.close()
            combined = "\n\n".join(p.text for p in pages if p.text)
            return OCRResult(pages=pages, combined_text=combined, status="DONE")

        except Exception as e:
            return OCRResult(status="FAILED", error=repr(e))


    def _real_image_ocr(self, local_path: str, doc_id: str, oss_bucket=None) -> OCRResult:
        """真实图片 OCR (Gemini Vision)。"""
        if not self.api_key:
            return OCRResult(status="FAILED", error="API KEY not configured")

        import base64
        try:
            with open(local_path, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode('utf-8')
            
            ext = os.path.splitext(local_path)[1].lower()
            mime_type = "image/jpeg" if ext in [".jpg", ".jpeg"] else "image/png"
            
            text = self._call_ocr_api(b64_data, mime_type)

            return OCRResult(
                pages=[OCRPageResult(page_num=1, text=text, status="DONE")],
                combined_text=text,
                status="DONE",
            )
        except Exception as e:
            return OCRResult(status="FAILED", error=repr(e))

    def _call_ocr_api(self, base64_image: str, mime_type: str) -> str:
        """根据配置调用相应的 OCR API (Gemini or DashScope)。"""
        import requests

        is_dashscope = "dashscope.aliyuncs.com" in self.api_base_url

        if is_dashscope:
            url = f"{self.api_base_url}/services/aigc/multimodal-generation/generation"
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.ocr_model,
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"image": f"data:{mime_type};base64,{base64_image}"},
                                {"text": "请识别图片中的所有文字，保持原文顺序输出。只输出识别文本，不要解释。"}
                            ]
                        }
                    ]
                }
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            if resp.status_code != 200:
                raise RuntimeError(f"DashScope OCR HTTP {resp.status_code}: {resp.text[:500]}")
            
            data = resp.json()
            try:
                choices = data["output"]["choices"]
                content = choices[0]["message"]["content"]
                if isinstance(content, list):
                    return "".join(item.get("text", "") for item in content if isinstance(item, dict)).strip()
                elif isinstance(content, str):
                    return content.strip()
                return ""
            except (KeyError, IndexError):
                return ""
        else:
            # Default: Gemini API format
            # P0-2 Fix: API key 通过 header 传递，避免暴露在 URL 中被代理/日志记录
            if "/models/" in self.api_base_url:
                url = f"{self.api_base_url}:generateContent"
            else:
                url = f"{self.api_base_url}/models/{self.ocr_model}:generateContent"
            
            payload = {
                "contents": [{
                    "parts": [
                        {"text": "请识别图片中的所有文字，保持原文顺序输出。只输出识别文本，不要解释。"},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64_image
                            }
                        }
                    ]
                }],
                "generationConfig": {
                    "temperature": 0.0
                }
            }
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            }
            resp = requests.post(url, json=payload, headers=headers, timeout=60)
            if resp.status_code != 200:
                raise RuntimeError(f"Gemini OCR HTTP {resp.status_code}: {resp.text[:500]}")

            data = resp.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except (KeyError, IndexError):
                return ""
