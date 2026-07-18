#!/usr/bin/env bash
# B4（生产级外审 2026-07-17 P1-02）：DataWorks 代码包构建——制品 + sha256 sidecar。
#
# 产物（默认 ~/Downloads/dw_upload_<date>/）：
#   opensearch_pipeline_production.zip         —— git archive HEAD（含 build_info.json：
#                                                 git SHA/构建时间，运行日志可溯源到提交）
#   opensearch_pipeline_production.zip.sha256  —— sidecar；上传为 DataWorks **File 资源**
#                                                 （与 zip 同名 + .sha256 后缀），节点
#                                                 _verify_zip_integrity 下载后硬比对，
#                                                 资源被替换即拒绝执行。
# ⚠️ 每次重新上传 zip 必须同步重新上传 sidecar——只换 zip 不换 sidecar 会被节点拒掉。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

OUT_DIR="${1:-$HOME/Downloads/dw_upload_$(date +%Y%m%d)}"
mkdir -p "$OUT_DIR"
ZIP="$OUT_DIR/opensearch_pipeline_production.zip"

GIT_SHA=$(git rev-parse HEAD)
DIRTY=$(git status --porcelain | grep -cv '^??' || true)
if [ "$DIRTY" -gt 0 ]; then
    echo "⚠️ 工作区有未提交改动（$DIRTY 处）——archive 打的是 HEAD，不含这些改动！" >&2
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
printf '{"git_sha": "%s", "built_at": "%s", "dirty_worktree": %s}\n' \
    "$GIT_SHA" "$(date -u +%FT%TZ)" "$([ "$DIRTY" -gt 0 ] && echo true || echo false)" \
    > "$TMP/build_info.json"

git archive --format=zip -o "$ZIP" --add-file="$TMP/build_info.json" HEAD \
    opensearch_pipeline schema requirements.txt

shasum -a 256 "$ZIP" | awk '{print $1}' > "$ZIP.sha256"

echo "✅ $ZIP"
echo "   sha256 = $(cat "$ZIP.sha256")"
echo "   git    = $GIT_SHA"
echo "→ DataWorks 上传两份资源：zip（Archive 资源）+ zip.sha256（File 资源，同名加后缀）"
