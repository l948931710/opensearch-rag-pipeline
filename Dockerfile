# ═══════════════════════════════════════════════════════════════
# OpenSearch RAG Pipeline — SAE Production Image
# P0-6（重评审计 2026-07-11）：多阶段构建——node 阶段产 console 前端 dist，
# clean 镜像 /console 不再回「尚未构建」404（console-app/dist 与
# webconsole/next-dist 均 gitignored，此前镜像里根本没有前端产物）。
# ═══════════════════════════════════════════════════════════════

# ── Stage 1: console 前端构建（Vite/Vue3 → opensearch_pipeline/webconsole/next-dist）──
# 基础镜像按 digest 固定（2026-07-11 重审计 §6 供应链：tag 可被上游改写，digest 不可）。
# 升级基础镜像：docker buildx imagetools inspect node:20-slim 取新 digest，连同下方
# python digest 一起换、一起过 make test + 镜像冒烟。
FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS console-build

WORKDIR /build/console-app
# 依赖清单先拷（层缓存：lock 不变不重装）
COPY console-app/package.json console-app/package-lock.json ./
RUN npm ci --no-audit --no-fund
# vite.config outDir = ../opensearch_pipeline/webconsole/next-dist（相对 console-app）
COPY console-app/ ./
RUN mkdir -p /build/opensearch_pipeline/webconsole \
    && npm run build

# ── Stage 2: python 运行时 ────────────────────────────────────────
FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3 AS base

# 部署版本指纹（canary 校验 / 回滚确认）：构建期烤入 git 短 SHA，运行期经 RAG_GIT_SHA 暴露给
# versions.git_commit() → /api/version。打包步骤传 --build-arg GIT_SHA=$(git rev-parse --short HEAD)；
# 不传则为 'unknown'（不影响功能，仅版本端点显示 unknown）。
ARG GIT_SHA=unknown
ENV RAG_GIT_SHA=$GIT_SHA

# 阿里云 VPC 内网不需要代理，保持 pip 默认源即可
# 如果构建环境在国内公网，可取消注释下行加速
# RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/

WORKDIR /app

# 先拷贝依赖清单，利用 Docker 层缓存
COPY requirements-prod.lock ./

# 依赖装自带 hash 的 lock（重审计 §6：pyproject 全 >= 无上界，直接 resolve = 每次构建
# 吃最新上游，供应链投毒/破坏性升级零防线）。lock 由
#   uv pip compile pyproject.toml --extra api --extra production --generate-hashes \
#     --python-version 3.11 --python-platform x86_64-unknown-linux-gnu -o requirements-prod.lock
# 生成；--require-hashes 逐包验 sha256，--no-deps 禁止 pip 自行拉未锁传递依赖。
# 包代码不装进 site-packages：uvicorn 以 WORKDIR /app 起，opensearch_pipeline 从 cwd 导入
#（与旧「空包 + extras」形态运行语义一致）。
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements-prod.lock

# 拷贝应用代码 + 前端产物（来自 node 构建阶段）
COPY opensearch_pipeline/ ./opensearch_pipeline/
COPY --from=console-build /build/opensearch_pipeline/webconsole/next-dist \
     ./opensearch_pipeline/webconsole/next-dist
# schema/ 随镜像走：/api/ready 的 schema_migrations checksum 漂移探针要用本地权威
# DDL 与台账比对（readiness.py；不带则报 no_local_files）
COPY schema/ ./schema/

# 非 root 用户运行
RUN useradd -m appuser
USER appuser

# SAE 健康检查端口
EXPOSE 8000

# uvicorn 启动：
#   --workers 1  必须单 worker：会话存储（session_store）与「补充原因」AWAITING_COMMENT 状态
#                都是进程内内存，多 worker 会各持一份、互不可见，导致会话/反馈错乱。并发由
#                FastAPI 线程池承载（处理器声明为 def，阻塞 I/O 不占事件循环）。要横向扩容请
#                先把这些状态迁到 Redis，再上调 worker 数。
#   --timeout-keep-alive 65  SAE SLB 默认 keep-alive 60s，服务端需略大于此值
#   --log-level info
CMD ["python", "-m", "uvicorn", \
     "opensearch_pipeline.api:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--timeout-keep-alive", "65", \
     "--log-level", "info"]
