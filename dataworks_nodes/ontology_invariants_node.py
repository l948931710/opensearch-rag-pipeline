# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — ontology_invariants：本体不变量对账 reaper（PR-H，P1「可观测闭环」）

  · 动作   只读扫描四类不变量（孤儿对象 / active 别名×open case 并存 / resolved case
           断链 / active 别名指非 active 对象）——原子化（PR-C）之后这些半状态只能来自
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
DEPS = ["PyMySQL==1.1.1", "DBUtils==3.1.0", "requests==2.32.3"]
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
    dest_root = os.path.abspath(dest)
    for name in zf.namelist():
        target = os.path.abspath(os.path.join(dest_root, name))
        if not (target == dest_root or target.startswith(dest_root + os.sep)):
            raise RuntimeError("zip 成员越界（Zip-Slip）: %r" % name)
    zf.extractall(dest_root)


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
