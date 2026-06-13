# -*- coding: utf-8 -*-
"""ref_keys.py — union ImageRef 坐标系 + per-fmt 匹配 + 集合 Jaccard

四格式坐标系一表(GT 与 chunk.image_refs 共用):

  DOCX  : image_index            (UnifiedExtractor 全文 1-based 序号)
  PDF   : page + in_page_idx     (in_page_idx 可缺省 → "该页任一张都算对"弱身份)
  XLSX  : block_index            (复用 UnifiedExtractor block 序号,image 也是 block)
  PPTX  : slide_no + shape_idx   (shape_idx 可缺省 → slide 级 presence,非精度)

设计要点(SplitL4 抢救):
- ImageRef 是 dataclass 而不是 dict 字符串拼接 — 类型可控,扩展 PPTX shape_idx
  时改动收敛在 match_strict() 这一个函数。
- jaccard(empty, empty) = 1.0 — xlsx_spec 这种"该 step 不该有图"的负例 GT
  必须显式入正确分子,否则 binding_jaccard 会被分母不均衡拖低。
- 同一 ImageRef 在不同格式下"等价"判定独立(format-aware),不允许跨格式比较。
- xlsx 同 anchor_row 多图消歧用 filename 次级身份(向后兼容:GT 不标 filename 时退回旧 block_index-only 语义)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional


@dataclass(frozen=True)
class ImageRef:
    """图编号坐标 — 四格式 union 表达。

    fmt 决定哪些字段是 load-bearing:
      docx:  image_index 必填(UnifiedExtractor 全文 1-based 序号)
      pdf:   image_index 必填(同 docx,extractor 全文 1-based;Funnel-1 弃的
             装饰图会留序号空位)。page 字段保留作辅助/可读性,不参与判等
      xlsx:  block_index 必填(UnifiedExtractor block 流序号,image 也作 block 计);
             filename 为次级身份,GT 显式标注后用于同 anchor_row 多图消歧
             (向后兼容:GT 不标 filename 时退回旧 block_index-only 语义)
      pptx:  slide_no 必填, shape_idx 可选(缺省=slide 级 presence,弱身份)

    2026-06-12 修正:PDF 由 (page, in_page_idx) 改 image_index 主键 —
    原设计臆造"页内重启 in_page_idx",与生产 chunker 的全文 image_index 脱节,
    实测 PDF SOP 首轮 Jaccard=0.0(坐标系错位全 miss)。
    """
    fmt: str                                # 'docx' | 'pdf' | 'xlsx' | 'pptx'
    image_index: Optional[int] = None       # docx + pdf 主键
    page: Optional[int] = None              # pdf 辅助(可读性,不参与 strict 判等)
    in_page_idx: Optional[int] = None       # [deprecated] PDF 旧坐标系遗留,新 GT 不要用
    block_index: Optional[int] = None       # xlsx
    slide_no: Optional[int] = None          # pptx
    shape_idx: Optional[int] = None         # pptx (可选)
    filename: Optional[str] = None       # xlsx 次级身份（同 block_index 多图消歧；GT 必须显式标，pred 总有）

    def is_weak(self) -> bool:
        """弱身份 = 缺主键的 ref,仅做 presence 比对,不做精度判定。

        PDF 标了 page 但没标 image_index = weak(GT 标注未细化时的中间态)。
        """
        if self.fmt == "pdf" and self.image_index is None:
            return True
        if self.fmt == "pptx" and self.shape_idx is None:
            return True
        return False

    def primary_key(self) -> tuple:
        """主键:用于"出现/不出现"层面的相等判定。

        weak PDF GT(只标 page)用 page 级 presence 命名空间;strong 走 image_index。
        命名空间不同(`pdf` vs `pdf:page`)避免 weak 与 strong 误等。
        """
        if self.fmt == "docx":
            return ("docx", self.image_index)
        if self.fmt == "pdf":
            if self.image_index is not None:
                return ("pdf", self.image_index)
            return ("pdf:page", self.page)
        if self.fmt == "xlsx":
            return ("xlsx", self.block_index)
        if self.fmt == "pptx":
            return ("pptx", self.slide_no)
        raise ValueError(f"unknown fmt: {self.fmt}")

    def strict_key(self) -> tuple:
        """精度键:用于"哪张图"层面的相等判定(完整次级标识符)。"""
        if self.fmt == "docx":
            return ("docx", self.image_index)
        if self.fmt == "pdf":
            return ("pdf", self.image_index)
        if self.fmt == "xlsx":
            # filename 次级身份：当 GT 显式标了哪张图时，strict 比对走 (block, filename)；
            # 否则退回 block_index-only 兼容旧 GT（每个 anchor 任一图都算对）。
            if self.filename:
                return ("xlsx", self.block_index, self.filename)
            return ("xlsx", self.block_index)
        if self.fmt == "pptx":
            return ("pptx", self.slide_no, self.shape_idx)
        raise ValueError(f"unknown fmt: {self.fmt}")


def parse_ref_dict(d: Dict[str, Any], fmt: str) -> ImageRef:
    """从 GT JSON 或 chunk.image_refs_json 的一个 dict 解出 ImageRef。

    容错:未知字段忽略,缺失字段保留 None — 下游 strict/primary 判定时再消化。
    """
    return ImageRef(
        fmt=fmt,
        image_index=d.get("image_index"),
        page=d.get("page") or d.get("page_num"),
        in_page_idx=d.get("in_page_idx") or d.get("image_index_in_page"),
        block_index=d.get("block_index") or d.get("anchor_row"),  # xlsx 兼容
        slide_no=d.get("slide_no") or d.get("slide"),
        shape_idx=d.get("shape_idx") or d.get("shape_index"),
        filename=d.get("filename"),
    )


def jaccard(gt_refs: Iterable[ImageRef], pred_refs: Iterable[ImageRef],
            strict: bool = True) -> float:
    """两组 ImageRef 的 Jaccard 相似度。

    Args:
        gt_refs: GT 期望该 chunk 绑定的图集
        pred_refs: chunker 实际给该 chunk 绑的图集
        strict: True=用 strict_key(精度模式), False=primary_key(presence 模式)

    返回:
        |intersect| / |union| ∈ [0, 1]
        约定:**empty-vs-empty = 1.0**(显式负例正确,xlsx_spec 多数 chunk 无图场景)
    """
    gt_list = list(gt_refs)
    pred_list = list(pred_refs)
    # 仅在 xlsx GT 显式启用 filename 次级身份时让 pred 参与 filename 严格比对；
    # 否则把 pred 的 filename 抹掉，回退到旧 (fmt, block_index) presence 语义
    # —— 让旧 GT 文件无须改动也照常通过（incremental upgrade）。fmt-gated 避免
    # 非 xlsx GT 偶然带 filename 字段时把 xlsx pred 错误升级到严格模式（cross-fmt 漏）。
    gt_uses_filename = strict and any(
        getattr(r, "filename", None) and r.fmt == "xlsx" for r in gt_list
    )
    def _key(r):
        if strict:
            k = r.strict_key()
            if not gt_uses_filename and r.fmt == "xlsx":
                # canonicalize pred strict_key 到不含 filename 的形态
                return ("xlsx", r.block_index)
            return k
        return r.primary_key()
    g = {_key(r) for r in gt_list}
    p = {_key(r) for r in pred_list}
    if not g and not p:
        return 1.0  # 显式负例正确(both sides agree on "no image")
    if not g or not p:
        return 0.0  # 一边有一边没 = 完全不匹配
    return len(g & p) / len(g | p)


def img_dup_factor(all_refs: Iterable[ImageRef]) -> float:
    """全文 image_refs 数 / 唯一图身份数。

    1.0 = 完美(每张图只被一个 step 绑定一次)
    > 1.5 = 已知 over-attach bug(每子步骤被塞所有图)
    本方案 hard 闸 p95 <= 1.20(容忍多步骤合理共享总览图/封面图)

    NB. 这里的"唯一身份"故意忽略 xlsx 的 filename 次级身份——dup_factor 度量的是
    "同一锚点位重复出现"，引入 filename 会让"同 anchor 多张不同文件"被算作不同
    身份而失去 over-attach 信号（同 anchor=12 两张图原本就该 dup=2 触警，加 filename
    后会变 dup=1 漏报）。
    """
    refs = list(all_refs)
    if not refs:
        return 1.0
    def _dup_key(r):
        # 对 xlsx，强制退回 (fmt, block_index) 计身份；其他格式沿用 strict_key
        if r.fmt == "xlsx":
            return ("xlsx", r.block_index)
        return r.strict_key()
    unique = {_dup_key(r) for r in refs}
    return len(refs) / len(unique) if unique else 1.0
