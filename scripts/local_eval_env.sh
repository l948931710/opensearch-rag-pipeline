#!/usr/bin/env bash
# local_eval_env.sh — 本地受控 A/B 评测环境一键管理
#
# 双实例约定（详见 docs/local_eval_env.md）：
#   :8001 = 新管线   RAG_ENV=local_ab_new  → 索引 locale2e_v1
#   :8002 = 旧对照   RAG_ENV=local_ab_old  → 索引 locale2e_old_v1
#   :8000 = 连生产 HA3 的 test 服务（本脚本不启动，down --all 时顺带清理）
#
# 用法：
#   scripts/local_eval_env.sh up        # 起双实例（pid/日志在 logs/local_eval/）
#   scripts/local_eval_env.sh down      # 停双实例；down --all 额外清 :8000
#   scripts/local_eval_env.sh status    # 端口/索引/DB/rerank 配置总览
#   scripts/local_eval_env.sh smoke     # 双端各问 1 题（BIND-01）验证可用
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$REPO/logs/local_eval"
PY="${PYTHON:-python3}"

ARMS=("new:8001:local_ab_new:locale2e_v1" "old:8002:local_ab_old:locale2e_old_v1")

_die() { echo "❌ $*" >&2; exit 1; }

_preflight() {
    docker ps --format '{{.Names}}' | grep -q '^rag-mysql-local$' \
        || _die "rag-mysql-local 容器未运行（docker start rag-mysql-local）"
    docker ps --format '{{.Names}}' | grep -q '^rag-opensearch-local$' \
        || _die "rag-opensearch-local 容器未运行（docker start rag-opensearch-local）"
    for arm in "${ARMS[@]}"; do
        IFS=: read -r _ _ env _ <<< "$arm"
        [ -f "$REPO/.env.$env" ] || _die ".env.$env 缺失。从 .env.local 派生：
  sed 's/^RAG_OPENSEARCH_INDEX=.*/RAG_OPENSEARCH_INDEX=<索引名>/' .env.local > .env.$env
  echo 'RAG_RERANK_ENABLE=true' >> .env.$env"
    done
}

up() {
    _preflight
    mkdir -p "$RUN_DIR"
    for arm in "${ARMS[@]}"; do
        IFS=: read -r name port env index <<< "$arm"
        if curl -sf -o /dev/null "localhost:$port/api/health" 2>/dev/null; then
            echo "⏭  :$port ($name) 已在运行，跳过"
            continue
        fi
        ( cd "$REPO" && RAG_ENV="$env" nohup "$PY" -m uvicorn opensearch_pipeline.api:app \
            --host 127.0.0.1 --port "$port" > "$RUN_DIR/serve_$port.log" 2>&1 & \
          echo $! > "$RUN_DIR/serve_$port.pid" )
        echo "▶  :$port ($name, $env → $index) pid=$(cat "$RUN_DIR/serve_$port.pid")"
    done
    for arm in "${ARMS[@]}"; do
        IFS=: read -r name port _ _ <<< "$arm"
        for _ in $(seq 1 60); do
            curl -sf -o /dev/null "localhost:$port/api/health" 2>/dev/null && break
            sleep 1
        done
        curl -sf -o /dev/null "localhost:$port/api/health" \
            || _die ":$port 健康检查超时，看日志 $RUN_DIR/serve_$port.log"
        echo "✅ :$port ($name) healthy"
    done
}

down() {
    local ports=(8001 8002)
    [ "${1:-}" = "--all" ] && ports+=(8000)
    for port in "${ports[@]}"; do
        local pidfile="$RUN_DIR/serve_$port.pid" killed=""
        if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
            kill "$(cat "$pidfile")" 2>/dev/null && killed="pid $(cat "$pidfile")"
            rm -f "$pidfile"
        fi
        # uvicorn 可能有子进程仍占端口：总是按端口兜底
        local pids
        pids=$(lsof -ti:"$port" 2>/dev/null || true)
        if [ -n "$pids" ]; then
            echo "$pids" | xargs kill 2>/dev/null || true
            killed="${killed:+$killed + }port listeners"
        fi
        if [ -n "$killed" ]; then
            echo "■  :$port stopped ($killed)"
        else
            echo "⏭  :$port 未在运行"
        fi
    done
}

status() {
    echo "== 实例 =="
    for port in 8000 8001 8002; do
        if curl -sf -o /dev/null "localhost:$port/api/health" 2>/dev/null; then
            echo "  :$port UP"
        else
            echo "  :$port down"
        fi
    done
    echo "== 索引 doc 数 =="
    for idx in locale2e_v1 locale2e_old_v1 fuling_knowledge_v1; do
        c=$(curl -s "localhost:9200/$idx/_count" 2>/dev/null | "$PY" -c \
            "import json,sys;print(json.load(sys.stdin).get('count','missing'))" 2>/dev/null || echo "?")
        echo "  $idx: $c"
    done
    echo "== MySQL chunk 计数（is_active=1）=="
    PW=$(docker exec rag-mysql-local printenv MYSQL_ROOT_PASSWORD)
    docker exec rag-mysql-local mysql -uroot -p"$PW" fuling_knowledge -N -e "
        SELECT CONCAT('  ', grp, ': ', c) FROM (
            SELECT CASE WHEN doc_id LIKE 'LOCALE2EOLD_%' THEN 'LOCALE2EOLD(旧对照)'
                        WHEN doc_id LIKE 'LOCALE2E_%' THEN 'LOCALE2E(新管线)'
                        ELSE 'other' END AS grp, COUNT(*) AS c
            FROM chunk_meta WHERE is_active=1 GROUP BY grp) t;" 2>/dev/null
    echo "== 各臂解析配置 =="
    for arm in "${ARMS[@]}"; do
        IFS=: read -r name _ env _ <<< "$arm"
        (cd "$REPO" && RAG_ENV="$env" "$PY" -c "
from opensearch_pipeline.config import load_config
c = load_config()
print(f'  $name: index={c.opensearch.index_name} rerank={c.alibaba_vector.rerank_enable} top_k={c.rag.default_top_k}')" 2>/dev/null | tail -1)
    done
}

smoke() {
    local q='吸塑车间扫码报检怎么操作？'
    for arm in "${ARMS[@]}"; do
        IFS=: read -r name port _ _ <<< "$arm"
        echo "── :$port ($name) ──"
        curl -sf -XPOST "localhost:$port/api/ask" -H 'Content-Type: application/json' \
             --max-time 120 -d "{\"question\":\"$q\",\"user_id\":\"smoke-$name\"}" | "$PY" -c "
import json, sys
d = json.load(sys.stdin)
ans = d.get('answer') or ''
assert ans.strip(), 'empty answer'
srcs = [s.get('title') or '' for s in d.get('sources', [])]
hit = any('吸塑扫码报检' in t for t in srcs)
imgs = sum(1 for b in d.get('blocks', []) if b.get('type') == 'image')
print(f'  answer: {ans[:60]}...')
print(f'  expected-doc hit: {hit} | images: {imgs} | sources: {srcs[:2]}')
assert hit, 'expected doc not in sources'
print('  ✅ smoke pass')"
    done
}

case "${1:-}" in
    up) up ;;
    down) down "${2:-}" ;;
    status) status ;;
    smoke) smoke ;;
    *) grep '^#' "$0" | head -16 | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
