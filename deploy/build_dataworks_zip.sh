#!/usr/bin/env bash
# B4（生产级外审 2026-07-17 P1-02）：DataWorks 代码包构建——制品 + sha256 sidecar。
# δ3（M13，Majors 批次 δ，codex 共识 2026-07-21）：可选 --hmac —— 用本地 env
# RAG_DW_ZIP_HMAC_KEY 对 zip 摘要签名产 .sha256.hmac 第三份资源。信任边界分离：
# 校验 key 配在 DataWorks **调度 env**（env 面 ≠ 资源面）——能替换资源的攻击者可以
# 连 sidecar 一起换，但没有调度 env 读权就伪造不了签名。威胁模型边界（如实）：能改
# 节点代码本身或能读调度 env 者不在防护面（那是控制台权限治理的事）。
#
# 产物（默认 ~/Downloads/dw_upload_<date>/）：
#   opensearch_pipeline_production.zip              —— git archive HEAD（含 build_info.json：
#                                                      git SHA/构建时间，运行日志可溯源到提交）
#   opensearch_pipeline_production.zip.sha256       —— sidecar；上传为 DataWorks **File 资源**
#                                                      （与 zip 同名 + .sha256 后缀），节点
#                                                      _verify_zip_integrity 下载后硬比对，
#                                                      资源被替换即拒绝执行。
#   opensearch_pipeline_production.zip.sha256.hmac  —— （--hmac 时）HMAC-SHA256(key, sha256hex)；
#                                                      调度 env 配 RAG_DW_ZIP_HMAC_KEY 的节点硬验。
# ⚠️ 每次重新上传 zip 必须同步重新上传全部 sidecar——只换 zip 不换 sidecar 会被节点拒掉；
#    调度 env 配了 key 后，缺 .hmac 同样被拒（三份资源同批上传）。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

WANT_HMAC=0
ARGS=()
for a in "$@"; do
    if [ "$a" = "--hmac" ]; then WANT_HMAC=1; else ARGS+=("$a"); fi
done
OUT_DIR="${ARGS[0]:-$HOME/Downloads/dw_upload_$(date +%Y%m%d)}"
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

# 布局对齐既定铁律（README「打包与部署」）：zip 根 = 仅 opensearch_pipeline/
#（节点内联 pip，无 requirements.txt）+ build_info.json 溯源戳
git archive --format=zip -o "$ZIP" --add-file="$TMP/build_info.json" HEAD \
    opensearch_pipeline

shasum -a 256 "$ZIP" | awk '{print $1}' > "$ZIP.sha256"

if [ "$WANT_HMAC" = "1" ]; then
    # δ3（M13）：--hmac 而 key 缺失=硬错——绝不产「看似签了实则空签」的半吊子产物
    if [ -z "${RAG_DW_ZIP_HMAC_KEY:-}" ]; then
        echo "❌ --hmac 需要本地 env RAG_DW_ZIP_HMAC_KEY（与 DataWorks 调度 env 同 key）" >&2
        exit 1
    fi
    printf '%s' "$(cat "$ZIP.sha256")" \
        | openssl dgst -sha256 -hmac "$RAG_DW_ZIP_HMAC_KEY" -r | awk '{print $1}' \
        > "$ZIP.sha256.hmac"
    echo "   hmac   = $(cat "$ZIP.sha256.hmac")（key 指纹 $(printf '%s' "$RAG_DW_ZIP_HMAC_KEY" | shasum -a 256 | cut -c1-8)…）"
fi

echo "✅ $ZIP"
echo "   sha256 = $(cat "$ZIP.sha256")"
echo "   git    = $GIT_SHA"
if [ "$WANT_HMAC" = "1" ]; then
    echo "→ DataWorks 上传三份资源：zip（Archive）+ zip.sha256 + zip.sha256.hmac（File，同名加后缀）"
else
    echo "→ DataWorks 上传两份资源：zip（Archive 资源）+ zip.sha256（File 资源，同名加后缀）"
    echo "  （调度 env 配了 RAG_DW_ZIP_HMAC_KEY 的节点须用 --hmac 重产三份，否则节点硬拒）"
fi
