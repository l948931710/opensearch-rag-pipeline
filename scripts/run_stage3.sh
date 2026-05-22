#!/bin/bash
# ==============================================================================
# run_stage3.sh — 手动触发 Stage 3 (Chunks -> OpenSearch Index)
# ==============================================================================
# 用法：
#   ./scripts/run_stage3.sh             # 默认使用当天日期 (YYYYMMDD)
#   ./scripts/run_stage3.sh 20260520    # 处理指定业务日期的文档
# ==============================================================================

set -euo pipefail

# 确保脚本在根目录执行
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# 1. 自动计算或提取业务日期
BIZDATE=${1:-}
if [ -z "$BIZDATE" ]; then
    BIZDATE=$(date +%Y%m%d)
    echo "💡 未提供日期，自动使用今天作为业务日期: $BIZDATE"
else
    echo "💡 使用指定的业务日期: $BIZDATE"
fi

# 2. 从 .env 安全加载环境变量（使用 source 替代 export $(xargs)，避免特殊字符问题）
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    echo "🌐 环境配置已从 .env 自动加载 (RAG_ENVIRONMENT=${RAG_ENVIRONMENT:-未设置})"
else
    echo "⚠️ 警告: 未找到 .env 配置文件，将使用系统默认环境变量"
fi

# 3. 打印提示信息
echo "🚀 开始手动执行 Stage 3: Chunks -> OpenSearch Index (向量生成与推送)..."
echo "📂 业务日期: $BIZDATE"
echo "--------------------------------------------------"

# 4. 执行调度命令
python3 opensearch_pipeline/dataworks_orchestrator.py \
  --stage 3 \
  --bizdate "$BIZDATE" \
  --environment "${RAG_ENVIRONMENT:-production}" \
  --simulate "${RAG_SIMULATE:-false}"

EXIT_CODE=$?
echo "--------------------------------------------------"
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ Stage 3 执行成功！数据已被推送至 OpenSearch。"
else
    echo "❌ Stage 3 执行失败！请检查上方输出的错误日志。"
    exit $EXIT_CODE
fi
