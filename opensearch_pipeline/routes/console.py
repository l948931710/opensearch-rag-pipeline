# -*- coding: utf-8 -*-
"""
routes/console.py — 网页控制台静态托管（Vite SPA / legacy H5 / console-next 重定向）。

F-A2 结构债拆分（2026-07-01）：从 api.py 机械搬移，行为不变；路径穿越守卫、
SPA 回退与缓存策略逐字保留。api.py re-export `_serve_console_spa`/`_NEXT_DIST`
（tests 直接引用）。规则见 routes/__init__.py。
"""

import mimetypes
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from opensearch_pipeline.api import logger  # noqa: F401  # 保持既有日志器名（SAE 日志按名过滤）

router = APIRouter()


# ═══════════════════════════════════════════════════════════════
# 控制台托管（P7 切换）：
#   · /console            = 新 Vite SPA（默认入口）；无尾斜杠 → 307 到 /console/（保留 query）
#   · /console/{path}     = SPA 静态 + 作用域 SPA 回退（构建 base 须为 /console/）
#   · /console-legacy     = 旧·自包含 H5 控制台（退居此处，保留 ≥1 发布周期，P8 退役）
#   · /console-next[/...] = 并行阶段路径 → 307 重定向到 /console/...（向后兼容，保留 query）
#   缓存（修正#9）：hash 资源 immutable，index.html / SPA 回退 no-cache。
#   作用域（修正#3）：回退仅作用于 /console 与 /console/*，不匹配 /api/* → 未知 API 仍 JSON 404。
#   小程序兼容：既有 web-view 链接 /console?token=&doc_id=... 零改动命中新 SPA（query 原样保留）。
# ═══════════════════════════════════════════════════════════════

# ⚠️ webconsole/ 静态资源在包根 opensearch_pipeline/ 下；本模块搬入 routes/ 子包
# 后 __file__ 深了一层，必须多取一次 parent，否则 next-dist 解析到 routes/webconsole → 全线 404。
_PKG_ROOT = Path(__file__).resolve().parent.parent
_NEXT_DIST = _PKG_ROOT / "webconsole" / "next-dist"
_KB_CONSOLE_HTML_CACHE: Dict[str, Any] = {"html": None}


def _serve_console_spa(rel: str, accept_encoding: str = "") -> Response:
    """从 next-dist 安全返回文件；越界/不存在 → index.html（SPA 回退，no-cache）。构建 base 须为 /console/。

    assets/ 走预压缩内容协商（Perf-3）：构建期 scripts/compress-dist.mjs 已产 .br/.gz
    旁路文件，按 Accept-Encoding 直发压缩字节（uvicorn 直出公网无反代，否则 ~350KB 原样
    下发）。**绝不用全局 GZipMiddleware**——它会缓冲 StreamingResponse，/api/ask/stream
    的 SSE 逐帧刷出会被攒成坨。旁路文件缺席自动回退原文件，零部署顺序风险。
    """
    base = _NEXT_DIST
    index = base / "index.html"
    if rel:
        target = (base / rel).resolve()
        # 路径穿越守卫：解析后必须仍落在 next-dist 之内
        if (target == base or base in target.parents) and target.is_file():
            if rel.startswith("assets/"):
                headers = {"Cache-Control": "public, max-age=31536000, immutable",
                           "Vary": "Accept-Encoding"}
                for enc, suffix in (("br", ".br"), ("gzip", ".gz")):
                    if enc in (accept_encoding or "").lower():
                        cand = target.with_name(target.name + suffix)
                        if cand.is_file():
                            mt = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                            return FileResponse(cand, media_type=mt,
                                                headers={**headers, "Content-Encoding": enc})
                return FileResponse(target, headers=headers)
            return FileResponse(target, headers={"Cache-Control": "no-cache"})
    if index.is_file():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<h1>/console 尚未构建（在 console-app 下 CONSOLE_BASE=/console/ npm run build）</h1>", status_code=404)


def _redirect_to_console(path: str, request: Request) -> RedirectResponse:
    """重定向到 /console/<path>，原样保留 query（小程序 ?token=&doc_id= 深链不可丢）。"""
    target = f"/console/{path}" if path else "/console/"
    q = request.url.query
    if q:
        target += f"?{q}"
    return RedirectResponse(url=target, status_code=307)


@router.get("/console", include_in_schema=False)
def kb_console_root(request: Request):
    """无尾斜杠 → 307 到 /console/（与构建 base 对齐；保留 query，避免 vue-router base 归一化歧义）。"""
    return _redirect_to_console("", request)


@router.get("/console/{path:path}", include_in_schema=False)
def kb_console_spa(path: str, request: Request):
    return _serve_console_spa(path, request.headers.get("accept-encoding", ""))


@router.get("/console-legacy", response_class=HTMLResponse, include_in_schema=False)
def kb_console_legacy():
    """旧·自包含 H5 控制台单页：jsapi 免登 → /api/auth/dingtalk → /api/kb/*（同源调用）。P8 退役。"""
    if _KB_CONSOLE_HTML_CACHE["html"] is None:
        p = _PKG_ROOT / "webconsole" / "console.html"
        try:
            _KB_CONSOLE_HTML_CACHE["html"] = p.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("console.html 读取失败: %s", e)
            _KB_CONSOLE_HTML_CACHE["html"] = "<h1>知识库控制台页面缺失</h1>"
    return HTMLResponse(_KB_CONSOLE_HTML_CACHE["html"])


@router.get("/console-next", include_in_schema=False)
def kb_console_next_root(request: Request):
    return _redirect_to_console("", request)


@router.get("/console-next/{path:path}", include_in_schema=False)
def kb_console_next_redirect(path: str, request: Request):
    """并行阶段 /console-next/* → 统一 307 到 /console/*（保留子路径 + query）。"""
    return _redirect_to_console(path, request)
