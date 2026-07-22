# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — Stage 2: Canonical → Safe Chunks
上传到 DataWorks 调度节点中直接使用。

⚠️ 默认生产模式（SIMULATE=False）。冒烟测试：在节点环境变量设 RAG_NODE_SIMULATE=true。
"""
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone

# ═══════════════════════════════════════════════════════════════
# 🔧 模式开关：默认生产（False）。冒烟测试设环境变量 RAG_NODE_SIMULATE=true。
#    旧写法硬编码 True，部署忘改 → 整阶段跑 mock 却 exit 0（DataWorks 绿），语料静默停更。
# ═══════════════════════════════════════════════════════════════
# ⚠️ 冒烟开关:临时取消下一行注释 = 干跑(全 mock,不碰真实 RDS/OSS/HA3/LLM)。
# ⚠️ 测完必须重新注释掉,否则每日调度永远跑 mock 且 DataWorks 全绿(语料静默停更事故)!
# os.environ["RAG_NODE_SIMULATE"] = "true"
SIMULATE = os.environ.get("RAG_NODE_SIMULATE", "false").strip().lower() in ("true", "1", "yes")

# ═══════════════════════════════════════════════════════════════
# 🔐 环境变量配置（必须在 import pipeline 代码之前设置）
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = str(SIMULATE).lower()
os.environ["RAG_ENVIRONMENT"] = "production"


# ── 生产安全姿态断言(批次5 P0-07d)——不设这两行,节点在 load_config() 就 ValueError 崩 ──
# DataWorks 代码包从 claude/ontology-p0 打(≠main),production 启动须显式表态。
# 这两个 flag 只被 api/retriever/readiness 读(服务侧),摄取与运维脚本零读取,设 true 无行为影响。
# 2026-07-21 stage3 实地踩过;另一条路 RAG_ALLOW_LEGACY_OPEN_PROD=ack:<当日> 午夜过期,不适合调度任务。
os.environ["RAG_REQUIRE_AUTH"] = "true"
os.environ["RAG_ACL_FAIL_CLOSED"] = "true"
# ── Robustness features (validated GO 2026-06-23; default-OFF in code, enabled here for prod) ──
# Stage-2. setdefault → overridable (set the env var to 'false' to disable).
# Chunk-explosion: gate on; MODE stays 'warn' (default) — telemetry only, does NOT quarantine
# (set RAG_CHUNK_EXPLOSION_MODE=quarantine to escalate). Image-OCR PII: scans asset['ocr_text'],
# medium→redact, high→whole-doc quarantine (shadow showed 0 high-sev currently).
os.environ.setdefault("RAG_CHUNK_EXPLOSION_GATE", "true")
os.environ.setdefault("RAG_IMAGE_OCR_PII", "true")

if not SIMULATE:
    # 生产凭证：由 DataWorks 调度参数注入
    required_vars = [
        "DASHSCOPE_API_KEY", "RAG_RDS_HOST", "RAG_RDS_PASSWORD",
        "RAG_OSS_ACCESS_KEY_ID", "RAG_OSS_ACCESS_KEY_SECRET",
        # ↓ stage2 本身不碰索引，但 config 守卫 R5/D7 要求 production 配置检索后端+显式表名
        #   （同 stage1，2026-06-11）。节点内联粘贴时从 清理stage3 顶部复制 RAG_HA3_* 赋值。
        "RAG_HA3_ENDPOINT", "RAG_HA3_TABLE_NAME",
    ]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing required env vars for production: {missing}")

# ═══════════════════════════════════════════════════════════════
# 1. 下载并解压代码包
# ═══════════════════════════════════════════════════════════════
print("=== 1. 下载 Archive 资源 ===")
resource = odps.get_resource('opensearch_pipeline_production.zip')
with resource.open(mode='rb') as reader:
    with open('opensearch_pipeline_production.zip', 'wb') as writer:
        writer.write(reader.read())

print("=== 2. 解压代码包 ===")
if not os.path.exists('opensearch_pipeline_production.zip'):
    raise RuntimeError("❌ 未能下载 opensearch_pipeline_production.zip")

def _safe_extractall(zf, dest):
    """B4（生产级外审 2026-07-17 P1-02/P1-03）安全解压：Zip-Slip 越界 + 加密成员 +
    成员数/单成员/总量/压缩比预算——资源包被替换成恶意 archive 时，不能借解压写任意
    路径或耗尽磁盘/内存。预算≈生产包实测 10×：成员≤4000、单成员≤200MB、总量≤500MB、
    压缩比≤200:1。"""
    dest_root = os.path.abspath(dest)
    infos = zf.infolist()
    if len(infos) > 4000:
        raise RuntimeError("zip 成员数超预算: %d > 4000" % len(infos))
    total = 0
    for info in infos:
        name = info.filename
        target = os.path.abspath(os.path.join(dest_root, name))
        if not (target == dest_root or target.startswith(dest_root + os.sep)):
            raise RuntimeError("zip 成员越界（Zip-Slip）: %r" % name)
        if info.flag_bits & 0x1:
            raise RuntimeError("zip 加密成员（拒绝）: %r" % name)
        if info.file_size > 200 * 1024 * 1024:
            raise RuntimeError("zip 单成员超预算: %r（%d bytes）" % (name, info.file_size))
        if info.compress_size and info.file_size / float(info.compress_size) > 200:
            raise RuntimeError("zip 压缩比异常（疑似 zip-bomb）: %r" % name)
        total += info.file_size
    if total > 500 * 1024 * 1024:
        raise RuntimeError("zip 总解压量超预算: %d bytes" % total)
    zf.extractall(dest_root)


def _verify_zip_integrity(zip_name):
    """B4（P1-02）+δ3（M13，Majors 批次 δ，codex 共识 2026-07-21）制品完整性：算 zip
    sha256 并留痕；有 sidecar 资源 <zip_name>.sha256 时硬比对（不匹配=资源被替换，拒绝
    执行）；暂缺=默认过渡期放行（打包侧 deploy/build_dataworks_zip.sh 随包生成）。
    δ3 增量：①RAG_DW_SIDECAR_STRICT=on（调度 env）→ 缺 sidecar/不可读/无 HMAC key 均
    raise（默认 off=现状放行+显著警告）；②调度 env 配 RAG_DW_ZIP_HMAC_KEY → 对
    <zip_name>.sha256.hmac 硬验（信任边界分离：能替换资源者可连 sidecar 一起换，但读
    不到调度 env 就伪造不了签名；打包侧 --hmac 产第三份资源）。威胁模型边界（如实）：
    能改节点代码本身或能读调度 env 的攻击者不在本防护面。"""
    import hashlib
    import hmac as _hmac
    _strict = (os.environ.get('RAG_DW_SIDECAR_STRICT', '') or '').strip().lower() \
        in ('1', 'true', 'yes', 'on')
    _key = (os.environ.get('RAG_DW_ZIP_HMAC_KEY', '') or '').strip()
    h = hashlib.sha256()
    with open(zip_name, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    digest = h.hexdigest()
    print("[B4] %s sha256=%s" % (zip_name, digest))
    if _strict and not _key:
        raise RuntimeError("[δ3 M13] RAG_DW_SIDECAR_STRICT=on 但调度 env 缺 "
                           "RAG_DW_ZIP_HMAC_KEY——无 key 无从硬验，拒绝执行")
    try:
        _sc = odps.get_resource(zip_name + '.sha256')  # noqa: F821 — DataWorks 运行时注入
        with _sc.open(mode='r') as r:
            expected = (r.read() or '').strip().split()[0].lower()
    except Exception as e:  # noqa: BLE001 — 默认过渡期放行；STRICT 硬拒
        if _strict:
            raise RuntimeError("[δ3 M13] STRICT：sidecar %s.sha256 不存在/不可读"
                               "（重新上传 zip+sidecar 三份资源）: %s" % (zip_name, e))
        print("[B4] ⚠️ sidecar %s.sha256 不存在/不可读（过渡期放行；"
              "RAG_DW_SIDECAR_STRICT=on 将硬拒）: %s" % (zip_name, e))
        return digest
    if expected != digest:
        raise RuntimeError("[B4] 制品完整性校验失败: sha256=%s != sidecar=%s"
                           % (digest, expected))
    if _key:
        try:
            _hc = odps.get_resource(zip_name + '.sha256.hmac')  # noqa: F821 — 同上注入
            with _hc.open(mode='r') as r:
                _sig = (r.read() or '').strip().split()[0].lower()
        except Exception as e:  # noqa: BLE001 — key 已配则 hmac sidecar 必须在场
            raise RuntimeError("[δ3 M13] 调度 env 已配 HMAC key 但 %s.sha256.hmac 缺失/"
                               "不可读（打包侧用 --hmac 重产并三份同批上传）: %s"
                               % (zip_name, e))
        _want = _hmac.new(_key.encode('utf-8'), digest.encode('ascii'),
                          hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(_sig, _want):
            raise RuntimeError("[δ3 M13] HMAC 校验失败：sha256 sidecar 可被连带伪造，"
                               "签名对不上调度 env key，拒绝执行")
        print("[δ3] ✅ HMAC 校验通过（信任边界=调度 env key）")
    print("[B4] ✅ 制品完整性校验通过（sidecar 匹配）")
    return digest


_verify_zip_integrity('opensearch_pipeline_production.zip')
with zipfile.ZipFile('opensearch_pipeline_production.zip', 'r') as zip_ref:
    _safe_extractall(zip_ref, '.')
print("✅ 解压成功")

current_dir = os.path.abspath(".")
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

# 安装依赖（按运行时 Python 版本选择；2026-07-17 发现 serverless 资源组执行器 = py3.7）
# py3.7 陷阱: pypdf 5.0.0 / python-docx 1.1.2 元数据谎报兼容实则要求 3.8+ → 必须钉真兼容版
# （钉版集经 linux/cp37 全依赖解析 + 节点冒烟实证）。镜像恢复 3.8+ 后自动走现代分支。
print("=== 2.5 安装依赖（Python %d.%d 运行时）===" % sys.version_info[:2])
import subprocess
if sys.version_info >= (3, 8):
    _DEPS = [
        "PyMySQL", "DBUtils", "oss2", "requests",
        "pdfplumber", "pypdf>=4.0", "PyMuPDF",
        "python-docx", "openpyxl", "python-pptx>=0.6",
        "jieba", "Pillow",
    ]
else:
    _DEPS = [
        "PyMySQL==1.1.1", "DBUtils==3.1.2", "oss2==2.19.1", "requests==2.31.0",
        "typing_extensions==4.7.1",
        "pypdf==3.17.4", "pdfplumber==0.9.0", "PyMuPDF==1.22.5",
        "python-docx==1.1.0", "openpyxl==3.1.3", "python-pptx==0.6.23",
        "jieba==0.42.1", "Pillow==9.5.0",
    ]
subprocess.check_call([
    sys.executable, "-m", "pip", "install",
    "--force-reinstall", "--no-cache-dir", "-q", *_DEPS,
])
subprocess.call([sys.executable, "-m", "pip", "freeze"])   # δ3（M13）：传递闭包可见化（冻结回填 user-gated）

# 强校验：装完必须能 import, 否则 fail-fast (避免再次重蹈 RD 61D861 / pptx 静默坑)
try:
    import pypdf
    import pdfplumber
    import pptx as _pptx_check  # noqa: F401  (python-pptx)
    import docx as _docx_check  # noqa: F401  (python-docx)
    import openpyxl as _xlsx_check  # noqa: F401
    import pymysql as _db_check  # noqa: F401
    import oss2 as _oss_check  # noqa: F401
    print(f"✅ pypdf={pypdf.__version__}  pdfplumber={pdfplumber.__version__}  pptx/docx/openpyxl/pymysql/oss2 OK")
except ImportError as e:
    raise RuntimeError(
        f"❌ 依赖安装后仍无法 import: {e}. "
        f"sys.path={sys.path}. 检查 DataWorks 资源组 Python env."
    ) from e

# ═══════════════════════════════════════════════════════════════
# 2. 解析调度参数
# ═══════════════════════════════════════════════════════════════
print("=== 3. 解析调度参数 ===")
# 兜底 bizdate = 北京 T-1（与 DataWorks ${bizdate} 语义一致）。原先硬编码 '20260521'：
# 节点漏配调度参数时会永远跑在那个过期日期上且毫无报错。
# P3-10：曾用容器本地 datetime.now()——DataWorks pod 是 UTC，其翻日时刻比北京晚 8h，
# 北京 00:00-08:00 间兜底会算错业务日 → 改按固定 UTC+8 求北京 T-1（bizdate 仅作
# 溯源标注，各阶段选行是纯状态 drain，不按日期过滤——见 dataworks_orchestrator docstring）。
bizdate = (datetime.now(timezone.utc) + timedelta(hours=8) - timedelta(days=1)).strftime("%Y%m%d")
if len(sys.argv) > 1:
    arg_val = sys.argv[1]
    bizdate = arg_val.split("=")[-1].strip() if "=" in arg_val else arg_val.strip()
    print(f"💡 bizdate: {bizdate}")
else:
    print(f"⚠️ 未获取到调度参数，按 T-1 兜底: {bizdate}")

# ═══════════════════════════════════════════════════════════════
# 3. 执行 Stage 2（排空式：drain 整批）
# ═══════════════════════════════════════════════════════════════
# 用 run_stage_drained（与 stage-1/3 一致、与已部署节点对齐）：生产排空整个 stage-2 待处理集。
# pre-drain 对账只在 stage-3（stage-1/2 无）。SIMULATE=True 时 drained 退化为单次 run_stage（不变）。
print(f"=== 4. 启动 Stage 2 排空（{'模拟' if SIMULATE else '生产'}）===")
from opensearch_pipeline.dataworks_orchestrator import run_stage_drained
run_stage_drained(stage=2, bizdate=bizdate, simulate=SIMULATE)
print("✅ Stage 2 完成！")
