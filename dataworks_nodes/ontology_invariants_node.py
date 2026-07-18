# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — ontology_invariants：本体不变量对账 reaper（PR-H，P1「可观测闭环」）

  · 动作   只读扫描八类不变量（孤儿对象 / active 别名×open case 并存 / resolved case
           断链 / active 别名指非 active 对象 / superseded_by 悬空·环 / active link 端点
           非 active / normalized_title 回填缺口 / population 快照陈旧——后四类=
           unknown-unknowns 批次3c 扩面）——原子化（PR-C）之后这些半状态只能来自
           历史脏数据或未知 bug，出现即应有人来看。
  · 播报   JSON 报告进节点日志；违例 → exit 1（DataWorks 标失败=告警面），零违例 exit 0。
  · 纪律   **绝不修数**（处置权在工作台/人工——reaper 自动改数会把 bug 掩埋成"自愈"）。

建议调度：每日一次，错开 retention(03:30) / ontology_backfill(04:10)（如 04:40
Asia/Shanghai），资源组 data_process，低优先级。
⚠️ 新建节点走 DataStudio 控制台（node id >2^53 MCP 改不动）；**部署 user-gated**。

凭据（同 ontology_backfill_node 纪律，PR-D P0-09）：一律经 DataWorks 平台注入
（调度参数/工作空间环境变量），源码禁明文密钥。只读作业，只需 RDS 连接件 +
无需 DASHSCOPE key（RAG_NO_MODEL_RESOLUTION=ack 惰性哨兵豁免，纯 RDS 作业）。
"""
import os
import subprocess
import sys
import zipfile

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖（锁版本——PR-D 供应链纪律）
# ═══════════════════════════════════════════════════════════════
# 批次8（ultra ontology_backfill_node:41 同族）：serverless 执行器实为 py3.7——
# requests==2.32.3 要求 ≥3.8，pip 第 0 步就死。对齐 stage 节点版本分支。
if sys.version_info >= (3, 8):
    DEPS = ["PyMySQL==1.1.1", "DBUtils==3.1.0", "requests==2.32.3"]
else:
    DEPS = ["PyMySQL==1.1.1", "DBUtils==3.1.0", "requests==2.31.0"]
subprocess.check_call([
    sys.executable, "-m", "pip", "install", *DEPS, "-t", "/tmp/pydeps", "-q"
])
if "/tmp/pydeps" not in sys.path:
    sys.path.insert(0, "/tmp/pydeps")

# ═══════════════════════════════════════════════════════════════
# 1. 环境（必须在 import pipeline 代码之前）
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = "false"
os.environ["RAG_ENVIRONMENT"] = "production"
os.environ["RAG_SIMULATE_OPENSEARCH"] = "true"   # 纯 RDS 只读作业
os.environ["RAG_SIMULATE_OSS"] = "true"

# P1-15：纯 RDS 作业不再要求 DashScope key——RAG_NO_MODEL_RESOLUTION=ack 令 config 把
# llm/ocr/vlm/embedding 全解析为惰性哨兵（无供应商端点，意外模型调用立刻失败），
# 生产供应商守卫据此豁免 key 要求（禁 Gemini 检查照跑）。本节点只碰 RDS。
os.environ.setdefault("RAG_NO_MODEL_RESOLUTION", "ack")
_required = ["RAG_RDS_HOST", "RAG_RDS_PASSWORD"]
_missing = [v for v in _required if not os.environ.get(v)]
if _missing:
    raise RuntimeError("缺少生产环境变量（经 DataWorks 平台注入，禁止粘源码）: %s" % _missing)

# ═══════════════════════════════════════════════════════════════
# 2. 下载代码包（Zip-Slip 防护同 backfill 节点）
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
# 3. 只读扫描（违例 exit 1 → DataWorks 标失败引人来看）
# ═══════════════════════════════════════════════════════════════
from opensearch_pipeline.ontology.invariants import main  # noqa: E402

sys.exit(main())
