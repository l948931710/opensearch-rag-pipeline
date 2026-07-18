# deploy/dataworks_monitor.Dockerfile — custom image for the DataWorks ops_health_monitor node.
#
# Purpose: run `python -m opensearch_pipeline.ops_monitor` (CS3/CS4 reconcilers + OBS-5 QA rollup) on
# a DataWorks Serverless resource group, on Python >=3.9 with all third-party deps baked in at the
# correct ABI. The default PyODPS pod is Python 3.7 and lacks our deps — hence this image.
#
# Build context = the REPO ROOT (so `COPY opensearch_pipeline` works):
#   docker build -f deploy/dataworks_monitor.Dockerfile -t <registry>/fuling-rag-monitor:<ver> .
# then push to a registry DataWorks can pull (Aliyun ACR), and register it under
# DataWorks console → 镜像管理 → 自定义镜像. See docs/ops_monitoring_schedule.md (Path C).
#
# ── BASE IMAGE ────────────────────────────────────────────────────────────────────────────────
# Do NOT trust this tag blindly — copy the EXACT current tag from the DataWorks 官方镜像列表 for your
# region (the Serverless custom-image flow lists them; tags are region/version-pinned). The official
# py311 pod image satisfies our `requires-python >=3.9` AND ships the DataWorks task-pod runtime
# components a node needs (a bare python:3.11-slim would NOT — it lacks those components).
ARG BASE=dataworks_pyodps_py311_task_pod:<FILL_EXACT_TAG_FROM_CONSOLE>
FROM ${BASE}

# ── DEPENDENCIES (version-FLOORED `>=`, NOT pinned — P3-12 更正：此前注释谎称 pinned) ────────────
# 每次 docker build 都会解析到当日最新兼容版：CI pip-audit 审的是它自己的解析结果，不能代表
# 本镜像里实际装到的版本。要可复现构建需引入 lockfile（pip-tools --generate-hashes / uv.lock）
# 并改用 --require-hashes 安装——在有 lockfile 之前，重建镜像后请核对关键 SDK 版本变化。
# The Serverless RG build network may not reach public PyPI — use the Aliyun mirror (or your NAT).
ARG PIP_INDEX=https://mirrors.cloud.aliyuncs.com/pypi/simple/
# Superset of the monitor's transitive needs (pyproject core + production + ocr). Installing the full
# set avoids a surprise transitive ImportError (the monitor imports pipeline_nodes/retriever which
# pull in the storage/SDK clients lazily). PyMuPDF + alibabacloud-ha3engine-vector ship binary/native
# wheels — confirm a manylinux wheel exists for the base image's Python/arch during the build.
RUN pip install --no-cache-dir -i ${PIP_INDEX} \
    "pymysql>=1.1" "DBUtils>=3.0" "opensearch-py>=2.4" "oss2>=2.18" "dashscope>=1.14" \
    "alibabacloud-ha3engine-vector>=1.1.18" "dingtalk-stream>=0.24" \
    "pypdf>=4.0" "pdfplumber>=0.11" "python-docx>=1.0" "python-pptx>=0.6" "openpyxl>=3.1" \
    "requests>=2.31" "PyMuPDF>=1.24"

# ── PACKAGE CODE (code-in-image → trivial node script, no ##@resource_reference needed) ──────────
# Trade-off: a code change requires rebuilding this image. Acceptable for a monitor (stable, rare
# rebuilds). If you'd rather update code without rebuilds, drop this COPY and instead reference
# opensearch_pipeline_production.zip in the node like the stage nodes do (needs that loader boilerplate).
COPY opensearch_pipeline /opt/rag/opensearch_pipeline
ENV PYTHONPATH=/opt/rag

# ── NON-ROOT USER（批次8 CI 首扫 DS-0002 HIGH；对齐主 Dockerfile 的 appuser 纪律）────────────
# 监控作业是纯出站读写（RDS/HA3/OSS/钉钉 API），无任何需要 root 的本地副作用。
# ⚠️ 若 DataWorks 任务 pod 运行时组件要求以 root 启动（构建/注册后首次调度即可验证），
# 删除下面两行并在此记录豁免理由——届时给 CI trivy 加 AVD 白名单而不是静默忍受红灯。
RUN useradd -m raguser
USER raguser

# NOTE: do NOT bake prod creds into the image. RAG_RDS_* / RAG_OSS_* / HA3 / RAG_OPS_ALERT_* are set
# as node/workspace env vars (or a secret) in the DataWorks scheduling config — see the runbook.
