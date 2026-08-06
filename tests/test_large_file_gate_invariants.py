# -*- coding: utf-8 -*-
"""大文件摄取链的**跨边界不变量**（2026-08-05,单文件上限提到 300MB 时建立）。

这条链上有五道闸分散在四个文件、两种语言里,谁也不认识谁:

    上传闸(kb_upload)  <  摄取下载闸(pipeline_nodes)
    前端 XHR 超时(kb.ts) <  upload token TTL(kb_upload)
    前端兜底常量(kb.ts)  ==  上传闸(kb_upload)

**漂移不会报错**,只会让文件"传得上去、却静默不完整":
  · 上传闸 ≥ 下载闸  ⇒ 用户传完了,摄取端 HEAD 一看就拒下载,文档以 NEEDS_REVIEW 卡住,
    前台没有任何异常显示——最难查的一种;
  · XHR 超时 ≥ TTL   ⇒ 浏览器还在传、令牌已过期,用户白等满全程才拿到失败;
  · 前端常量 ≠ 后端  ⇒ config 未回的那一小段窗口里客户端预检用错数(放行了后端要 413
    的文件,或反过来白拦一个合法文件)。

所以这里钉的是**闸门之间的关系**,不是某个具体数值——把 300 改成 500 不该让本文件变红,
把 300 改成 500 却忘了动下载闸才该变红。
"""
import pathlib
import re

_KB_TS = pathlib.Path("console-app/src/lib/kb.ts")
_NODES = pathlib.Path("opensearch_pipeline/pipeline_nodes.py")


def _extract_gate_default() -> int:
    """pipeline_nodes 里 RAG_EXTRACT_MAX_BYTES 的**代码默认值**。

    刻意读源码而非 import：这个闸只在 node_extract_text 的局部作用域里算,没有模块级常量
    可读;而它恰恰是最该被钉住的一个(生产 DataWorks 侧从未显式设过该 env —— 2026-08-05
    全仓核查 dataworks_nodes/ 与 deploy/ 零注入 ⇒ **跑的就是这个默认值**)。
    """
    m = re.search(r'RAG_EXTRACT_MAX_BYTES",\s*\n?\s*str\((\d+)\s*\*\s*1024\s*\*\s*1024\)', _NODES.read_text(encoding="utf-8"))
    assert m, "抽取下载闸的默认值形态变了 —— 本守卫已失效,请同步正则"
    return int(m.group(1)) * 1024 * 1024


def _ts_const(name: str) -> int:
    m = re.search(rf"export const {name} = (\d+)", _KB_TS.read_text(encoding="utf-8"))
    assert m, f"kb.ts 里找不到 {name}"
    return int(m.group(1))


def _ts_xhr_timeout_ms() -> int:
    m = re.search(r"xhr\.timeout = (\d+) \* 60 \* 1000", _KB_TS.read_text(encoding="utf-8"))
    assert m, "kb.ts 的 xhr.timeout 形态变了 —— 本守卫已失效"
    return int(m.group(1)) * 60


def test_摄取下载闸必须严格大于上传闸():
    from opensearch_pipeline import kb_upload

    up, gate = kb_upload.MAX_UPLOAD_BYTES, _extract_gate_default()
    assert gate > up, (
        f"抽取下载闸 {gate/1048576:.0f}MB 未严格大于自助上传闸 {up/1048576:.0f}MB：\n"
        "用户能传上来、摄取端却拒绝下载 ⇒ 文档以 NEEDS_REVIEW 静默卡住,前台毫无异常。\n"
        "改 RAG_MAX_UPLOAD_MB 的默认值时必须同时抬高 pipeline_nodes 的 RAG_EXTRACT_MAX_BYTES 默认值。")


def test_前端XHR超时必须小于上传令牌TTL():
    from opensearch_pipeline import kb_upload

    xhr, ttl = _ts_xhr_timeout_ms(), kb_upload.UPLOAD_TOKEN_TTL
    assert xhr < ttl, (
        f"前端 XHR 超时 {xhr/60:.0f}min ≥ 令牌 TTL {ttl/60:.0f}min：\n"
        "浏览器还在传、令牌已过期 ⇒ 用户白等满全程才拿到失败。")


def test_前端兜底常量与后端上限一致():
    from opensearch_pipeline import kb_upload

    ts_mb, py_mb = _ts_const("MAX_UPLOAD_MB"), kb_upload.MAX_UPLOAD_BYTES // 1048576
    assert ts_mb == py_mb, (
        f"kb.ts MAX_UPLOAD_MB={ts_mb} ≠ 后端 {py_mb}MB。真值虽走 /api/kb/config,但 config\n"
        "未回的那一小段窗口里客户端预检用的是这个常量(放行后端要 413 的文件 / 白拦合法文件)。")


def test_上传闸能被环境变量覆盖且单位是MB():
    """配置化本身也要守：现网靠 SAE 注入 RAG_MAX_UPLOAD_MB 免重打镜像调参。"""
    import importlib
    import os

    old = os.environ.get("RAG_MAX_UPLOAD_MB")
    os.environ["RAG_MAX_UPLOAD_MB"] = "77"
    try:
        from opensearch_pipeline import kb_upload
        importlib.reload(kb_upload)
        assert kb_upload.MAX_UPLOAD_BYTES == 77 * 1024 * 1024
    finally:
        if old is None:
            os.environ.pop("RAG_MAX_UPLOAD_MB", None)
        else:
            os.environ["RAG_MAX_UPLOAD_MB"] = old
        from opensearch_pipeline import kb_upload as _k
        importlib.reload(_k)


def test_付费页上限不得高于免费页上限():
    """OCR / 图片挖掘都是**按页按图计费**,而原生抽取是本地 CPU。付费的那两条爬到免费
    那条之上,意味着为一份没被原生抽取覆盖的页去付 OCR 钱 —— 一定是配错了,不是策略。"""
    from opensearch_pipeline.config import get_config

    cfg = get_config()
    assert cfg.ocr.max_ocr_pages <= cfg.pdf_native_max_pages, (
        f"OCR 页上限 {cfg.ocr.max_ocr_pages} > 原生抽取页上限 {cfg.pdf_native_max_pages}")
    assert cfg.pdf_image_max_pages <= cfg.pdf_native_max_pages, (
        f"图片挖掘页上限 {cfg.pdf_image_max_pages} > 原生抽取页上限 {cfg.pdf_native_max_pages}")


def test_extractor实例值与config同源():
    """UnifiedExtractor 有类属性兜底 + __init__ getattr 兜底 + config 三处默认值。
    三处曾各写各的,改一处就静默分叉(gate-only 的 __new__ 用法读类属性)。"""
    from opensearch_pipeline.config import get_config
    from opensearch_pipeline.extraction.unified_extractor import UnifiedExtractor

    cfg = get_config()
    assert UnifiedExtractor.pdf_native_max_pages == cfg.pdf_native_max_pages
    assert UnifiedExtractor.pdf_image_max_pages == cfg.pdf_image_max_pages


def test_dataclass默认与工厂默认不得分叉(monkeypatch):
    """第三处默认值来源：dataclass 字段默认。

    ⚠️ **它对正常路径是死代码** —— `load_config()` 恒显式传 `_env_int("...", <字面量>)`，
    dataclass 上写的那个数根本不参与。于是两处能长期分叉而无人察觉：改了 dataclass 以为
    改了行为（实际没有），改了工厂而 dataclass 留旧值（读代码的人被误导）。
    2026-08-05 变异验证实测踩中：把 dataclass 的 max_ocr_pages 改成 2000，全部守卫照绿。

    这条断言把两者钉在一起：清掉相关 env 后重新 load_config()，结果必须等于裸 dataclass。
    """
    import dataclasses

    from opensearch_pipeline.config import OCRConfig, PipelineConfig, load_config

    for k in ("RAG_OCR_MAX_PAGES", "RAG_PDF_NATIVE_MAX_PAGES", "RAG_PDF_IMAGE_MAX_PAGES"):
        monkeypatch.delenv(k, raising=False)
    fresh = load_config()

    def _default(cls, field):
        return next(f.default for f in dataclasses.fields(cls) if f.name == field)

    assert fresh.pdf_native_max_pages == _default(PipelineConfig, "pdf_native_max_pages")
    assert fresh.pdf_image_max_pages == _default(PipelineConfig, "pdf_image_max_pages")
    assert fresh.ocr.max_ocr_pages == _default(OCRConfig, "max_ocr_pages")
