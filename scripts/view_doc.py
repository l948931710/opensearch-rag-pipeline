#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
view_doc.py — 把带 mermaid 的 Markdown 渲染成「图文一体」的精排版文档，可打开 / 导出 PDF。

流程：复用 docs/diagrams/ 下已渲染的 SVG（缺了就调 render_mermaid 补）→ 预处理（首个
H1 提成封面标题、剥掉手写「目录」改用自动 TOC、mermaid 块换成内联 SVG）→ pandoc 打成
自包含 HTML（精排版 CSS）→ 可 open 预览，或用 headless Chrome 打印成 PDF。

用法：
    python scripts/view_doc.py                       # 生成 HTML 并打开（默认架构文档）
    python scripts/view_doc.py --pdf                 # 额外导出 PDF 到 ~/Downloads
    python scripts/view_doc.py x.md --pdf --out a.pdf
    python scripts/view_doc.py --pdf --no-open       # 只出 PDF，不开 HTML
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "diagrams"          # 整个目录已被 .gitignore 忽略
BUILD = OUT_DIR / "_html"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_mermaid import render_source  # noqa: E402
from diagram_splits import SPLITS  # noqa: E402

_FENCE_OPEN = re.compile(r"^```+\s*mermaid\s*$")
_FENCE_CLOSE = re.compile(r"^```+\s*$")
_H1 = re.compile(r"^#\s+(.*\S)\s*$")
_TOC_HEAD = re.compile(r"^##\s*(目录|Contents|Table of Contents)\s*$", re.I)
_HR = re.compile(r"^---+\s*$")

_STYLE = """<style>
  :root{
    --accent:#0e7c66; --accent-ink:#0c5345; --accent-soft:#e7f3ef;
    --ink:#1f2328; --muted:#5b6470; --line:#e6e8eb; --code-bg:#f5f7f9;
  }
  *{box-sizing:border-box}
  body{max-width:880px;margin:0 auto;padding:0 32px 64px;color:var(--ink);
       font-family:"PingFang SC","Microsoft YaHei","Helvetica Neue",-apple-system,
                   BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       font-size:15.5px;line-height:1.78;
       -webkit-print-color-adjust:exact;print-color-adjust:exact}
  header#title-block-header{padding:34px 0 18px;margin:0 0 1.6em;
       border-bottom:3px solid var(--accent)}
  header#title-block-header h1.title{font-size:30px;font-weight:700;line-height:1.28;
       margin:0;border:none;letter-spacing:.2px}
  header#title-block-header .subtitle{color:var(--muted);font-size:15px;margin:.5em 0 0;font-weight:400}
  header#title-block-header .date{color:var(--muted);font-size:13px;margin-top:.9em}
  h1{font-size:24px;font-weight:700;margin:1.9em 0 .5em}
  h2{font-size:20px;font-weight:700;color:#14324a;margin:2.1em 0 .7em;
     padding-bottom:.28em;border-bottom:2px solid var(--line)}
  h2::before{content:"";display:inline-block;width:.42em;height:.92em;background:var(--accent);
     margin-right:.5em;vertical-align:-1px;border-radius:2px}
  h3{font-size:16.5px;font-weight:600;color:#1f3a52;margin:1.5em 0 .4em}
  p{margin:.7em 0}
  a{color:var(--accent);text-decoration:none;border-bottom:1px solid rgba(14,124,102,.28)}
  strong{font-weight:700;color:#10212e}
  hr{border:none;border-top:1px solid var(--line);margin:2.2em 0}
  ul,ol{padding-left:1.5em}
  li{margin:.25em 0}
  code{background:var(--code-bg);padding:.13em .42em;border-radius:4px;font-size:.86em;
       font-family:"SF Mono",Menlo,Consolas,monospace;color:#b03a64}
  pre{background:var(--code-bg);border:1px solid var(--line);border-radius:8px;
      padding:14px 16px;overflow-x:auto;line-height:1.55}
  pre code{background:none;padding:0;color:#222;font-size:12.5px}
  table{border-collapse:collapse;width:100%;margin:1.15em 0;font-size:13.5px;
        border:1px solid var(--line);border-radius:8px;overflow:hidden}
  th,td{border:1px solid var(--line);padding:7px 11px;text-align:left;vertical-align:top}
  th{background:var(--accent-soft);color:var(--accent-ink);font-weight:600}
  tr:nth-child(even) td{background:#fafbfc}
  blockquote{margin:1.2em 0;padding:.75em 1.15em;background:#fbfaf4;
             border-left:4px solid #d8b441;border-radius:0 7px 7px 0;color:#5b5236}
  blockquote p{margin:.3em 0}
  svg{max-width:100%;height:auto;display:block;margin:1.6em auto;
      border:1px solid var(--line);border-radius:10px;background:#fff;padding:14px;
      break-inside:avoid;page-break-inside:avoid}
  nav#TOC{background:#f6f9f9;border:1px solid var(--line);border-radius:10px;
          padding:14px 8px 14px 30px;margin:0 0 2em;font-size:13.6px}
  nav#TOC::before{content:"目录";display:block;margin-left:-18px;margin-bottom:8px;
          font-weight:700;color:var(--accent-ink);font-size:14.5px}
  nav#TOC a{border:none;color:var(--ink)}
  nav#TOC ul{padding-left:1.2em;margin:.2em 0}
  nav#TOC li{margin:.12em 0}
  @page{size:A4;margin:16mm 15mm}
  @media print{
    body{max-width:none;padding:0;font-size:10.6pt}
    header#title-block-header{padding-top:6px}
    h1,h2,h3{break-after:avoid}
    tr,pre,blockquote,svg{break-inside:avoid}
    a{border:none;color:var(--ink)}
    nav#TOC{break-after:page}
  }
</style>
"""


def block_count(lines: list[str]) -> int:
    return sum(1 for ln in lines if _FENCE_OPEN.match(ln.strip()))


def svg_name(stem: str, idx: int, total: int) -> str:
    return f"{stem}.svg" if total == 1 else f"{stem}-{idx}.svg"


def ensure_svgs(md: Path, total: int) -> bool:
    expected = [OUT_DIR / svg_name(md.stem, i, total) for i in range(1, total + 1)]
    if all(p.exists() for p in expected):
        return True
    print("缺少部分 SVG，先调 render_mermaid 渲染…")
    res = subprocess.run([sys.executable, str(REPO / "scripts" / "render_mermaid.py"), str(md)])
    return res.returncode == 0 and all(p.exists() for p in expected)


def preprocess(md: Path, total: int) -> tuple[str, str]:
    """返回 (预处理后的 markdown, 封面标题)。剥首个 H1 与手写「目录」段，mermaid→内联图。"""
    lines = md.read_text(encoding="utf-8").splitlines()
    title = md.stem
    out: list[str] = []
    i, n, idx = 0, len(lines), 0
    h1_taken = False
    while i < n:
        s = lines[i].strip()
        # 首个 H1 → 封面标题，从正文移除
        m = _H1.match(lines[i])
        if m and not h1_taken:
            title = m.group(1)
            h1_taken = True
            i += 1
            continue
        # 手写「目录」段（标题 → 下一条 --- 之间）整段剥掉，改用 pandoc 自动 TOC
        if _TOC_HEAD.match(s):
            i += 1
            while i < n and not _HR.match(lines[i].strip()):
                i += 1
            i += 1  # 连同收尾的 --- 一起跳过
            continue
        # mermaid 块 → 图片（命中拆解覆盖则换成多张页宽可读的子图）
        if _FENCE_OPEN.match(s):
            idx += 1
            i += 1
            while i < n and not _FENCE_CLOSE.match(lines[i].strip()):
                i += 1
            i += 1
            override = SPLITS.get(md.stem, {}).get(idx)
            if override:
                out.append("")
                for j, (cap, src) in enumerate(override, 1):
                    sub = OUT_DIR / "_split" / f"{md.stem}-{idx}{chr(96 + j)}.svg"
                    if render_source(src, sub):
                        out += [f"**{cap}**", "", f"![{cap}]({sub.resolve().as_posix()})", ""]
                    else:  # 渲染失败则回退整张宽图，不丢内容
                        whole = (OUT_DIR / svg_name(md.stem, idx, total)).resolve()
                        out += [f"![{md.stem} · 图 {idx}]({whole.as_posix()})", ""]
                        break
            else:
                svg = (OUT_DIR / svg_name(md.stem, idx, total)).resolve()
                out += ["", f"![{md.stem} · 图 {idx}]({svg.as_posix()})", ""]
            continue
        out.append(lines[i])
        i += 1
    # 折叠连续空行
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    return text, title


def find_chrome() -> str | None:
    for c in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if Path(c).exists():
            return c
    cache = Path.home() / ".cache" / "puppeteer" / "chrome"
    if cache.is_dir():
        bins = sorted(cache.glob("*/chrome-mac-*/*.app/Contents/MacOS/*"))
        if bins:
            return str(bins[-1])
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="把带 mermaid 的 Markdown 渲染成精排版文档 / PDF")
    ap.add_argument("md", nargs="?", default="docs/architecture.md", help="目标 .md（默认架构文档）")
    ap.add_argument("--pdf", action="store_true", help="导出 PDF（headless Chrome）")
    ap.add_argument("--out", help="PDF 输出路径（默认 ~/Downloads/<名字>.pdf）")
    ap.add_argument("--no-open", action="store_true", help="不自动打开产物")
    args = ap.parse_args()

    md = Path(args.md)
    if not md.is_absolute():
        md = (REPO / md).resolve()
    if not md.exists():
        print(f"✗ 找不到文件：{md}", file=sys.stderr)
        return 2
    if not shutil.which("pandoc"):
        print("✗ 未装 pandoc。`brew install pandoc` 后重试。", file=sys.stderr)
        return 2

    total = block_count(md.read_text(encoding="utf-8").splitlines())
    if total and not ensure_svgs(md, total):
        print("✗ SVG 渲染失败，无法成图。", file=sys.stderr)
        return 1

    BUILD.mkdir(parents=True, exist_ok=True)
    body, title = preprocess(md, total)
    pre_md = BUILD / f"{md.stem}.src.md"
    pre_md.write_text(body, encoding="utf-8")
    style = BUILD / "_style.html"
    style.write_text(_STYLE, encoding="utf-8")
    html = BUILD / f"{md.stem}.html"

    cmd = [
        "pandoc", str(pre_md), "-f", "gfm", "-t", "html", "--standalone",
        "--embed-resources", "--toc", "--toc-depth=2", "-H", str(style),
        "--metadata", f"title={title}",
        "--metadata", "subtitle=综合架构文档 · 摄取 / 索引 / 检索 / 服务全景",
        "--metadata", f"date=生成于 {date.today().isoformat()}",
        "-o", str(html),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("✗ pandoc 失败：\n" + (res.stderr or res.stdout), file=sys.stderr)
        return 1
    n_imgs = html.read_text(encoding="utf-8").count("<svg ")
    print(f"✓ HTML（{n_imgs} 张图内联）：{html.relative_to(REPO)}")

    pdf_path = None
    if args.pdf:
        chrome = find_chrome()
        if not chrome:
            print("✗ 找不到 Chrome/Chromium，无法导出 PDF。", file=sys.stderr)
            return 1
        pdf_path = Path(args.out).expanduser().resolve() if args.out \
            else (Path.home() / "Downloads" / f"{md.stem}.pdf")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pcmd = [
            chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
            f"--print-to-pdf={pdf_path}", html.resolve().as_uri(),
        ]
        pres = subprocess.run(pcmd, capture_output=True, text=True)
        if pres.returncode != 0 or not pdf_path.exists():
            print("✗ Chrome 导出 PDF 失败：\n" + (pres.stderr or pres.stdout), file=sys.stderr)
            return 1
        size_kb = pdf_path.stat().st_size // 1024
        print(f"✓ PDF（{size_kb} KB）：{pdf_path}")

    if not args.no_open and shutil.which("open"):
        subprocess.run(["open", str(pdf_path if pdf_path else html)])
        print("✓ 已打开")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
