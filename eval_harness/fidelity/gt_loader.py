# -*- coding: utf-8 -*-
"""fidelity/gt_loader.py — 保真 GT 加载 + 完整性校验（fidelity_v1 schema）。

GT JSON（默认 eval_samples/fidelity_gt/fidelity_gt.json，含全文转写 → 不进仓库，
与 binding GT 同规矩）：

  {
    "_meta": {"schema": "fidelity_v1", "notes": "..."},
    "<doc_label>": {
      "_doc_meta": {
        "local_path": "eval_samples/docs/xxx.pdf",   // 笔记本本地路径
        "doc_sha256": "<源文件字节 sha256>",          // 防替换锚定（load-bearing）
        "file_ext": "pdf",
        "requires_real_ocr": false,   // true = 扫描件，只在 --real 下计分
        "degraded": false             // 转写半完工 → 不进 hard 口径，只算趋势
      },
      "pages": [
        {"page": 1, "ref_text": "……逐字参考转写……",
         "scope": "body"}             // body=不含页眉页脚（默认）| full
      ],
      "tables": [
        {"page": 2, "ref_grid": [["名称","规格","数量"], ["刀叉","A-100","200"]]}
      ]
    }
  }

校验姿态与 binding gt_loader 一致：结构错误 fail-fast（宁可评测红也不静默漏测）；
文件缺失/哈希不符是【环境】问题 → 该 doc 记 SKIP 状态，不阻断其他 doc。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_GT_PATH = os.path.join("eval_samples", "fidelity_gt", "fidelity_gt.json")
SCHEMA = "fidelity_v1"


@dataclass
class FidelityDoc:
    label: str
    local_path: str
    doc_sha256: str
    file_ext: str
    requires_real_ocr: bool = False
    degraded: bool = False
    pages: List[dict] = field(default_factory=list)    # {page, ref_text, scope}
    tables: List[dict] = field(default_factory=list)   # {page, ref_grid}

    def verify_bytes(self) -> Optional[str]:
        """返回 None=通过；否则返回 SKIP 原因（文件缺失/哈希不符）。"""
        if not os.path.exists(self.local_path):
            return f"missing local file: {self.local_path}"
        actual = hashlib.sha256(open(self.local_path, "rb").read()).hexdigest()
        if actual != self.doc_sha256:
            return (f"doc_sha256 mismatch (GT 针对的不是这份文件): "
                    f"expected {self.doc_sha256[:12]}…, got {actual[:12]}…")
        return None


def load_gt(path: str = DEFAULT_GT_PATH) -> List[FidelityDoc]:
    """加载并结构校验。结构问题直接 raise（fail-fast）。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    meta = raw.get("_meta") or {}
    if meta.get("schema") != SCHEMA:
        raise ValueError(f"GT schema 必须是 {SCHEMA!r}，得到 {meta.get('schema')!r}（{path}）")

    docs: List[FidelityDoc] = []
    for label, entry in raw.items():
        if label.startswith("_"):
            continue
        dm = entry.get("_doc_meta") or {}
        for key in ("local_path", "doc_sha256", "file_ext"):
            if not dm.get(key):
                raise ValueError(f"{label}: _doc_meta.{key} 缺失（fail-fast，不静默漏测）")
        pages = entry.get("pages") or []
        tables = entry.get("tables") or []
        if not pages and not tables:
            raise ValueError(f"{label}: pages 与 tables 都为空——没有任何参考产物可比")
        for p in pages:
            if not isinstance(p.get("page"), int) or "ref_text" not in p:
                raise ValueError(f"{label}: pages 条目需要 int page + ref_text: {p}")
            p.setdefault("scope", "body")
        for t in tables:
            grid = t.get("ref_grid")
            if (not isinstance(t.get("page"), int) or not isinstance(grid, list)
                    or not grid or not all(isinstance(r, list) for r in grid)):
                raise ValueError(f"{label}: tables 条目需要 int page + 非空二维 ref_grid")
        docs.append(FidelityDoc(
            label=label,
            local_path=dm["local_path"],
            doc_sha256=dm["doc_sha256"],
            file_ext=str(dm["file_ext"]).lower().lstrip("."),
            requires_real_ocr=bool(dm.get("requires_real_ocr", False)),
            degraded=bool(dm.get("degraded", False)),
            pages=pages,
            tables=tables,
        ))
    if not docs:
        raise ValueError(f"GT 为空：{path} 只有 _meta，没有任何文档条目")
    return docs
