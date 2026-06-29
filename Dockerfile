# ═══════════════════════════════════════════════════════════════
# OpenSearch RAG Pipeline — SAE Production Image
# ═══════════════════════════════════════════════════════════════

FROM python:3.11-slim AS base

# 部署版本指纹（canary 校验 / 回滚确认）：构建期烤入 git 短 SHA，运行期经 RAG_GIT_SHA 暴露给
# versions.git_commit() → /api/version。打包步骤传 --build-arg GIT_SHA=$(git rev-parse --short HEAD)；
# 不传则为 'unknown'（不影响功能，仅版本端点显示 unknown）。
ARG GIT_SHA=unknown
ENV RAG_GIT_SHA=$GIT_SHA

# 阿里云 VPC 内网不需要代理，保持 pip 默认源即可
# 如果构建环境在国内公网，可取消注释下行加速
# RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/

WORKDIR /app

# 先拷贝依赖描述文件，利用 Docker 层缓存
COPY pyproject.toml ./

# 安装 api + production 依赖（不装 dev/test/ocr）
RUN pip install --no-cache-dir ".[api,production]"

# 拷贝应用代码
COPY opensearch_pipeline/ ./opensearch_pipeline/

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
