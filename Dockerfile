# ═══════════════════════════════════════════════════════════════
# OpenSearch RAG Pipeline — SAE Production Image
# ═══════════════════════════════════════════════════════════════

FROM python:3.11-slim AS base

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
#   --workers 2  足够处理并发（I/O 密集型，非 CPU 密集型）
#   --timeout-keep-alive 65  SAE SLB 默认 keep-alive 60s，服务端需略大于此值
#   --log-level info
CMD ["python", "-m", "uvicorn", \
     "opensearch_pipeline.api:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--timeout-keep-alive", "65", \
     "--log-level", "info"]
