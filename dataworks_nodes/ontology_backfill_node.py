# -*- coding: utf-8 -*-
"""
DataWorks PyODPS 3 节点 — ontology_backfill：本体历史别名回填 worker（P0 PR9）

  · 输入   File 资源 CSV（历史 Excel/U盘导出的编号观测；列契约见 seeding.CsvSnapshotSource；
           U8 T-1 同步库待 go/no-go ① 签字后经 U8SnapshotSource 接入，届时替换资源下载段）
  · 动作   mention 观测语义：已激活跳过（幂等）· 高置信唯一候选 auto 别名（入抽检队列）·
           低置信/多候选/无候选入 resolution_case 聚合观测（steward 工作台裁决）·
           active 别名 × open case 尸案对账愈合 —— 全部写 fuling_operation（027/028 表族）
  · 播报   轮末 resolution_coverage（总 + product/sku/mold/material 分品类；M2 门指标）

策略/守卫详见 opensearch_pipeline/ontology/backfill.py 模块 docstring（dry-run 默认、
RAG_ONTOLOGY_BACKFILL_ENABLE 第二道闸、单条失败不拖垮批、断点续跑幂等）。

建议调度：每日一次，错开 stage 节点 / retention(03:30) / ops_health_monitor
（如 04:10 Asia/Shanghai），资源组 data_process，低优先级。
⚠️ 新建节点走 DataStudio 控制台（node id >2^53 MCP 改不动）。

上线节奏（与 retention 节点同哲学，双开关缺一不可）：
  阶段1（先跑数天）：DRY_RUN = True 观察每日 auto/case/愈合量（本文件默认形态，零写）。
  阶段2（**go/no-go ①③④ 签字 + 用户确认后**）：DRY_RUN 改 False——它会同时设
  RAG_ONTOLOGY_BACKFILL_ENABLE=true 过 backfill.py 的第二道闸；签字前禁真实回填
  （真实回填=真实播种禁令的延伸，见 docs/ontology_p0_plan_2026-07-10.md Phase 0）。
退出码：0=ok；2=第二道闸拒绝 / 批内有单条错误（DataWorks 标失败引人来看）；3=作业失败。

凭据（PR-D P0-09）：本文件【不含明文密钥，也**不得**粘贴明文密钥】——凭据一律经
DataWorks 平台注入（调度参数/工作空间环境变量/KMS），节点源码只读 os.environ。
仅需 RDS 三件套（RAG_NO_MODEL_RESOLUTION=ack 豁免模型 key；不碰 OSS/HA3——回填核心只走
rule 候选，不调 embedding）。缺失即 raise（下方 _required 守卫）。
"""
import os
import sys
import subprocess
import zipfile

# ═══════════════════════════════════════════════════════════════
# 0. 安装依赖（PyODPS 3.7 pod 无 pymysql/dbutils；纯 RDS 作业，不装 oss2/ha3）
# ═══════════════════════════════════════════════════════════════
# PR-D（P0-09）：依赖锁版本——公网未锁版本安装 = 供应链面（构建不可复现 +
# 上游投毒直进生产 pod）。版本对齐 pyproject 生产档；升级须改这里并重跑 staging。
# 批次8（ultra ontology_backfill_node:41）：serverless 执行器实为 py3.7（2026-07-17
# stage 节点实证），requests==2.32.3 要求 ≥3.8 → pip 第 0 步就死。对齐 stage 节点的
# 版本分支：py3.7 钉 2.31.0（cp37 实证集），镜像恢复 3.8+ 自动走现代钉版。
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
# 1. 环境（必须在 import pipeline 代码之前；config.py 首次 import 即读取）
# ═══════════════════════════════════════════════════════════════
os.environ["RAG_SIMULATE"] = "false"
os.environ["RAG_ENVIRONMENT"] = "production"
# 回填是纯 RDS 作业，不碰检索后端/OSS。显式声明这两路走 mock：
#   ① 短路 config 的 production 完整性守卫 R5（production 必须有检索后端）；
#   ② 免配 HA3/OSS 凭据（本节点不需要）。
# RDS 仍真实：simulate_db 不设 → 继承 RAG_SIMULATE=false → 真连生产 RDS。
os.environ["RAG_SIMULATE_OPENSEARCH"] = "true"
os.environ["RAG_SIMULATE_OSS"] = "true"

# 阶段开关：阶段1 = True（dry-run 只报数）；阶段2 = False（须 gate ①③④ 签字 + 用户确认）
DRY_RUN = True
if not DRY_RUN:
    os.environ["RAG_ONTOLOGY_BACKFILL_ENABLE"] = "true"   # backfill.py 的第二道闸

# 观测语义（默认）不铸新对象；确属主数据档案回灌才改 "master"
MODE = "mention"
# 单轮安全阀（断点续跑：幂等 skip-active，重跑自动续后面的）；None=不限
LIMIT = None

# 输入：DataWorks File 资源名（DataStudio 上传观测 CSV 后填这里）
CSV_RESOURCE = "ontology_backfill_snapshot.csv"

# ── 凭据（PR-D P0-09）：**禁止**在源码里赋值明文密钥——一律经 DataWorks 平台注入
# （节点「调度参数」/ 工作空间环境变量），本节点只读 os.environ 并守卫缺失。
# 需要：
# RAG_RDS_HOST / RAG_RDS_PORT / RAG_RDS_USER / RAG_RDS_PASSWORD / RAG_RDS_DATABASE /
# RAG_RDS_OPERATION_DATABASE / RAG_RDS_ONTOLOGY_DATABASE。
# P1-15：纯 RDS 作业不再要求 DashScope key——RAG_NO_MODEL_RESOLUTION=ack 令 config 把
# llm/ocr/vlm/embedding 全解析为惰性哨兵（无供应商端点，意外模型调用立刻失败），
# 生产供应商守卫据此豁免 key 要求（禁 Gemini 检查照跑）。本节点只碰 RDS。
os.environ.setdefault("RAG_NO_MODEL_RESOLUTION", "ack")
_required = ["RAG_RDS_HOST", "RAG_RDS_PASSWORD"]
_missing = [v for v in _required if not os.environ.get(v)]
if _missing:
    raise RuntimeError("缺少生产环境变量（经 DataWorks 平台注入，禁止粘源码）: %s" % _missing)

# ═══════════════════════════════════════════════════════════════
# 2. 下载资源：代码包 zip + 观测 CSV（odps 为 PyODPS 隐式入口对象）
# ═══════════════════════════════════════════════════════════════
print("=== 下载 Archive 资源 opensearch_pipeline_production.zip ===")
resource = odps.get_resource('opensearch_pipeline_production.zip')  # noqa: F821 (PyODPS 运行时注入)
with resource.open(mode='rb') as reader:
    with open('opensearch_pipeline_production.zip', 'wb') as writer:
        writer.write(reader.read())
def _safe_extractall(zf, dest):
    """PR-D（P0-09）Zip-Slip 防护：拒绝绝对路径/../ 越界成员——资源包被替换时
    不能借解压写任意路径。"""
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

print("=== 下载 File 资源 %s ===" % CSV_RESOURCE)
_csv_local = 'ontology_backfill_snapshot.csv'
csv_resource = odps.get_resource(CSV_RESOURCE)  # noqa: F821
with csv_resource.open(mode='rb') as reader:
    with open(_csv_local, 'wb') as writer:
        writer.write(reader.read())

# ═══════════════════════════════════════════════════════════════
# 3. 运行回填（DataWorks 以退出码判成败；2/3 都会标失败引人来看）
# ═══════════════════════════════════════════════════════════════
import opensearch_pipeline  # noqa: E402
print("opensearch_pipeline:", opensearch_pipeline.__file__)
from opensearch_pipeline.ontology.backfill import main  # noqa: E402

_argv = [_csv_local, "--mode", MODE]
if LIMIT is not None:
    _argv += ["--limit", str(LIMIT)]
if not DRY_RUN:
    _argv.append("--commit")
sys.exit(main(_argv))
