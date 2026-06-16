#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_mermaid.py — 把仓库 Markdown 里的 ```mermaid 代码块离线渲染成 SVG。

为什么有这个脚本：架构图以 mermaid 写在 .md 里，GitHub / 部分编辑器能原生渲染，
但本地终端、纯文本预览、以及对话内联渲染（CSP 拦 CDN）都看不到。本脚本用
mermaid-cli (mmdc) 把每个 mermaid 块导出成 docs/diagrams/<名字>.svg —— 不依赖任何
编辑器或在线服务，生成的 SVG 在哪都能看，也能直接嵌进文档 / 周报。

用法：
    python scripts/render_mermaid.py            # 渲染全仓库（默认）
    python scripts/render_mermaid.py FILE...    # 只渲染指定 .md
    python scripts/render_mermaid.py --list     # 只列出 mermaid 块，不渲染（零依赖）

依赖：node + npx。优先用全局 mmdc；否则 `npx -y @mermaid-js/mermaid-cli` 按需拉取
（首次会下载 mermaid-cli + Chromium，较慢；之后走缓存）。无网络时本脚本会优雅报错，
不会破坏任何东西。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "diagrams"
EXCLUDE_DIRS = {".git", "node_modules", "archive", ".pytest_cache", "build", "dist", ".venv"}

_FENCE_OPEN = re.compile(r"^```+\s*mermaid\s*$")
_FENCE_CLOSE = re.compile(r"^```+\s*$")


def find_md_files(roots: list[str]) -> list[Path]:
    """收集待扫描的 .md 文件（去重、排序、跳过噪声目录）。"""
    found: list[Path] = []
    for root in roots:
        p = Path(root)
        if not p.is_absolute():
            p = (REPO / p).resolve()
        if p.is_file() and p.suffix == ".md":
            found.append(p)
        elif p.is_dir():
            for f in sorted(p.rglob("*.md")):
                if any(part in EXCLUDE_DIRS for part in f.relative_to(REPO).parts):
                    continue
                found.append(f)
    # 去重保序
    seen: set[Path] = set()
    uniq: list[Path] = []
    for f in found:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def extract_blocks(md_path: Path) -> list[str]:
    """提取一个 .md 中所有 ```mermaid 块的源码（按出现顺序）。"""
    blocks: list[str] = []
    lines = md_path.read_text(encoding="utf-8").splitlines()
    i, n = 0, len(lines)
    while i < n:
        if _FENCE_OPEN.match(lines[i].strip()):
            buf: list[str] = []
            i += 1
            while i < n and not _FENCE_CLOSE.match(lines[i].strip()):
                buf.append(lines[i])
                i += 1
        else:
            i += 1
            continue
        blocks.append("\n".join(buf).strip("\n"))
        i += 1  # 跳过收尾 fence
    return blocks


def out_name(stem: str, idx: int, total: int) -> str:
    """单块用 <stem>.svg；多块用 <stem>-N.svg。"""
    return f"{stem}.svg" if total == 1 else f"{stem}-{idx}.svg"


def resolve_mmdc() -> list[str]:
    """优先全局 mmdc，否则回退 npx 按需拉取。"""
    if shutil.which("mmdc"):
        return ["mmdc"]
    if shutil.which("npx"):
        return ["npx", "-y", "@mermaid-js/mermaid-cli"]
    return []


# mermaid 渲染配置（含 CJK 字体）—— 主流程与 view_doc 拆图共用
_MMDC_CONFIG = {
    "securityLevel": "loose",
    "fontFamily": "PingFang SC, Microsoft YaHei, Helvetica Neue, Arial, sans-serif",
    "flowchart": {"htmlLabels": True, "useMaxWidth": True},
    "themeVariables": {"fontSize": "15px"},
}


def render_source(src: str, out_svg: Path, theme: str = "neutral") -> bool:
    """把单段 mermaid 源码渲染成 SVG（view_doc 拆图复用）。"""
    mmdc = resolve_mmdc()
    if not mmdc:
        return False
    out_svg.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "mmdc.json"
        cfg.write_text(json.dumps(_MMDC_CONFIG), encoding="utf-8")
        pup = Path(td) / "puppeteer.json"
        pup.write_text(json.dumps({"args": ["--no-sandbox"]}), encoding="utf-8")
        mmd = Path(td) / "in.mmd"
        mmd.write_text(src, encoding="utf-8")
        cmd = [*mmdc, "-i", str(mmd), "-o", str(out_svg),
               "-b", "transparent", "-t", theme, "-c", str(cfg), "-p", str(pup)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.returncode == 0 and out_svg.exists()


def main() -> int:
    ap = argparse.ArgumentParser(description="把仓库 Markdown 的 mermaid 块渲染成 SVG")
    ap.add_argument("paths", nargs="*", default=["."], help="要扫描的 .md 或目录（默认全仓库）")
    ap.add_argument("--list", action="store_true", help="只列出 mermaid 块，不渲染")
    ap.add_argument("--theme", default="neutral", help="mermaid 主题（default/neutral/forest/dark）")
    args = ap.parse_args()

    md_files = find_md_files(args.paths)
    inventory: list[tuple[Path, list[str]]] = []
    for md in md_files:
        blocks = extract_blocks(md)
        if blocks:
            inventory.append((md, blocks))

    total_blocks = sum(len(b) for _, b in inventory)
    if not inventory:
        print("未发现任何 ```mermaid 块。")
        return 0

    print(f"发现 {total_blocks} 个 mermaid 块，跨 {len(inventory)} 个文件：")
    for md, blocks in inventory:
        print(f"  {len(blocks):>2}  {md.relative_to(REPO)}")

    if args.list:
        return 0

    mmdc = resolve_mmdc()
    if not mmdc:
        print("\n✗ 找不到 mmdc，也没有 npx。请先装 node，或全局 "
              "`npm i -g @mermaid-js/mermaid-cli`。", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 清旧（只清本脚本会生成的 svg + 索引，不动其它）
    for old in OUT_DIR.glob("*.svg"):
        old.unlink()

    if mmdc[0] == "npx":
        print("\n首次运行会用 npx 下载 mermaid-cli + Chromium，可能要几分钟，请稍候…")

    # mermaid / puppeteer 配置写到临时文件（保持仓库干净）
    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "mmdc.json"
        cfg.write_text(json.dumps(_MMDC_CONFIG), encoding="utf-8")
        pup = Path(td) / "puppeteer.json"
        pup.write_text(json.dumps({"args": ["--no-sandbox"]}), encoding="utf-8")

        index_rows: list[str] = []
        ok = fail = 0
        for md, blocks in inventory:
            stem = md.stem
            for idx, src in enumerate(blocks, 1):
                svg = OUT_DIR / out_name(stem, idx, len(blocks))
                src_file = Path(td) / "in.mmd"
                src_file.write_text(src, encoding="utf-8")
                cmd = [*mmdc, "-i", str(src_file), "-o", str(svg),
                       "-b", "transparent", "-t", args.theme,
                       "-c", str(cfg), "-p", str(pup)]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode == 0 and svg.exists():
                    ok += 1
                    rel = svg.relative_to(REPO)
                    print(f"  ✓ {md.relative_to(REPO)} #{idx} → {rel}")
                    index_rows.append(
                        f"| [{svg.name}]({svg.name}) | `{md.relative_to(REPO)}` | 第 {idx} 块 |")
                else:
                    fail += 1
                    err = (res.stderr or res.stdout or "").strip().splitlines()
                    tail = err[-1] if err else "未知错误"
                    print(f"  ✗ {md.relative_to(REPO)} #{idx} 渲染失败：{tail}", file=sys.stderr)

        if index_rows:
            readme = OUT_DIR / "README.md"
            readme.write_text(
                "# 渲染产物（自动生成，勿手改）\n\n"
                "由 `make diagrams`（`scripts/render_mermaid.py`）从各 .md 的 ```mermaid 块导出。\n"
                "源图改了就重跑 `make diagrams`。\n\n"
                "| SVG | 来源文件 | 位置 |\n|---|---|---|\n"
                + "\n".join(index_rows) + "\n",
                encoding="utf-8")

    print(f"\n完成：{ok} 成功" + (f"，{fail} 失败" if fail else "") + f" → {OUT_DIR.relative_to(REPO)}/")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
