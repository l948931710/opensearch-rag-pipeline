# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — gap_semantic_groups：缺口相似问法语义组日批刷新（schema/040）

  · 拉近 N 天 NO_RESULT/REFUSAL 提问 → embedding（text-embedding-v4）→ 贪心归组
    → REPLACE 全量重写 qa_gap_semantic_group（fuling_operation）。幂等，重跑安全。
  · 产物仅供 kb_gaps 展示层归并（serving 侧 RAG_QA_GAP_SEMANTIC 门控），绝不驱动
    缺口自动关闭（schema/040 预注册边界）。

建议调度：每日一次，错开 stage 节点（如 04:00 Asia/Shanghai），资源组 data_process。
⚠️ 新建节点走 DataStudio 控制台（node id >2^53 MCP 改不动）。

上线节奏（与 retention 节点同哲学）：
  阶段1（先跑数天）：DRY_RUN=True 只打印归组预览（零写）。
  阶段2（用户确认后）：DRY_RUN 改 False —— REPLACE 真写映射表。
ENABLED 开关：serving 侧未开 RAG_QA_GAP_SEMANTIC 前无消费方，保持 False = no-op 退出 0
（不浪费 embedding 费用）；serving 开 flag 时一并改 True。
退出码：0=ok/未启用；非 0=作业失败（DataWorks 按退出码标失败）。

凭据：本文件【不含明文密钥】。控制台粘贴时从【清理stage3】节点顶部原样复制 RAG_* 赋值
贴到「凭据」标记处（RDS 三件套 + DashScope key——本节点要真 embedding，不豁免模型 key）。
"""
import os
import sys
import subprocess
import zipfile

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖（PyODPS 3.7 pod 无 pymysql/dbutils；纯 RDS+DashScope 作业）
# ═══════════════════════════════════════════════════════════════
# py3.7 serverless 执行器钉真兼容版（与 retention_node 同分支法）
if sys.version_info >= (3, 8):
    DEPS = ["PyMySQL", "DBUtils", "requests"]
else:
    DEPS = ["PyMySQL==1.1.1", "DBUtils==3.1.2", "requests==2.31.0"]
subprocess.check_call([
    sys.executable, "-m", "pip", "install", *DEPS, "-t", "/tmp/pydeps", "-q"
])
if "/tmp/pydeps" not in sys.path:
    sys.path.insert(0, "/tmp/pydeps")

# ═══════════════════════════════════════════════════════════════
# 1. 环境（必须在 import pipeline 代码之前；config.py 首次 import 即读取）
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = "false"
os.environ["RAG_ENVIRONMENT"] = "production"
# 纯 RDS+embedding 作业：检索后端/OSS 走 mock（短路 production 完整性守卫 R5，
# 免配 HA3/OSS 凭据）；RDS 与 DashScope 真实。
os.environ["RAG_SIMULATE_OPENSEARCH"] = "true"
os.environ["RAG_SIMULATE_OSS"] = "true"

# 开关（见文件头「上线节奏」）：
ENABLED = False          # serving 开 RAG_QA_GAP_SEMANTIC 时一并改 True
DRY_RUN = True           # 阶段2 改 False —— REPLACE 真写映射表
DAYS = 90                # 归组窗口（天）

# ── 凭据：粘贴【清理stage3】顶部的 RAG_* 赋值（取消注释并填真值）────────────────
# os.environ["RAG_RDS_HOST"]           = "..."
# os.environ["RAG_RDS_PORT"]           = "..."
# os.environ["RAG_RDS_USER"]           = "..."
# os.environ["RAG_RDS_PASSWORD"]       = "..."
# os.environ["RAG_RDS_DATABASE"]       = "..."
# os.environ["RAG_DASHSCOPE_API_KEY"]  = "..."   # 本节点要真 embedding
# ─────────────────────────────────────────────────────────────────────────────

if not ENABLED:
    print("gap_semantic_groups：ENABLED=False（serving 未开 RAG_QA_GAP_SEMANTIC），no-op 退出")
    sys.exit(0)

_required = ["RAG_RDS_HOST", "RAG_RDS_PASSWORD", "RAG_DASHSCOPE_API_KEY"]
_missing = [v for v in _required if not os.environ.get(v)]
if _missing:
    raise RuntimeError("缺少生产环境变量（从【清理stage3】顶部复制 RAG_* 赋值）: %s" % _missing)

# 生产写表走 env_guard 同日 ack（映射表属运营元数据；节点每日跑，按当日日期自续）
import datetime  # noqa: E402
os.environ.setdefault(
    "RAG_METADATA_PROD_ACK",
    "gap_semantic_groups:%s" % datetime.date.today().isoformat())

# ═══════════════════════════════════════════════════════════════
# 2. 下载并解压代码包（与 stage 节点同款；odps 为 PyODPS 隐式入口对象）
# ═══════════════════════════════════════════════════════════════
print("=== 下载 Archive 资源 opensearch_pipeline_production.zip ===")
resource = odps.get_resource('opensearch_pipeline_production.zip')  # noqa: F821 (PyODPS 运行时注入)
with resource.open(mode='rb') as reader:
    with open('opensearch_pipeline_production.zip', 'wb') as writer:
        writer.write(reader.read())
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
    """B4（P1-02）制品完整性：算 zip sha256 并留痕（运行结果可溯源到构建）；有
    sidecar 资源 <zip_name>.sha256 时硬比对（不匹配=资源被替换，拒绝执行）；暂缺=
    过渡期放行（旧包无 sidecar 不误伤；打包侧 deploy/build_dataworks_zip.sh 随包生成）。"""
    import hashlib
    h = hashlib.sha256()
    with open(zip_name, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    digest = h.hexdigest()
    print("[B4] %s sha256=%s" % (zip_name, digest))
    try:
        _sc = odps.get_resource(zip_name + '.sha256')  # noqa: F821 — DataWorks 运行时注入
        with _sc.open(mode='r') as r:
            expected = (r.read() or '').strip().split()[0].lower()
    except Exception as e:  # noqa: BLE001 — 过渡期：sidecar 未上传时放行
        print("[B4] ⚠️ sidecar %s.sha256 不存在/不可读（过渡期放行）: %s" % (zip_name, e))
        return digest
    if expected != digest:
        raise RuntimeError("[B4] 制品完整性校验失败: sha256=%s != sidecar=%s"
                           % (digest, expected))
    print("[B4] ✅ 制品完整性校验通过（sidecar 匹配）")
    return digest


_verify_zip_integrity('opensearch_pipeline_production.zip')
with zipfile.ZipFile('opensearch_pipeline_production.zip', 'r') as zf:
    _safe_extractall(zf, '.')
_cur = os.path.abspath('.')
if _cur not in sys.path:
    sys.path.insert(0, _cur)

# ═══════════════════════════════════════════════════════════════
# 3. 运行归组作业（脚本 argparse 读 sys.argv；DataWorks 以退出码判成败）
# ═══════════════════════════════════════════════════════════════
import opensearch_pipeline  # noqa: E402
print("opensearch_pipeline:", opensearch_pipeline.__file__)
sys.path.insert(0, os.path.join(_cur, "scripts"))
from build_qa_gap_semantic_groups import main  # noqa: E402

sys.argv = ["build_qa_gap_semantic_groups", "--days", str(DAYS)] \
    + ([] if DRY_RUN else ["--commit"])
sys.exit(main())
