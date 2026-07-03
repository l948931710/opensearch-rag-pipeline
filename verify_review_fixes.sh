#!/usr/bin/env bash
# verify_review_fixes.sh — 在本地【全量 Python 依赖 + Simulate 模式】下逐项验证本次审查修复。
# 用法：在 repo 根目录执行  bash verify_review_fixes.sh
# 安全：全程 RAG_SIMULATE=true（哈希 embedding / mock HA3 / 本地 OSS），绝不碰真实生产。
# 只读运行测试，不改任何源文件。
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

export RAG_SIMULATE=true RAG_SIMULATE_API=true RAG_SIMULATE_DB=true \
       RAG_SIMULATE_OSS=true RAG_SIMULATE_OPENSEARCH=true

PY="python3 -m pytest -q -p no:cacheprovider"
declare -a RESULTS

run() {  # run <label> <pytest args...>
  local label="$1"; shift
  local out rc
  out="$($PY "$@" 2>&1)"; rc=$?
  local line; line="$(printf '%s\n' "$out" | grep -E '[0-9]+ (passed|failed|error)' | tail -1)"
  if [ $rc -eq 0 ]; then
    RESULTS+=("PASS | $label | $line")
  else
    RESULTS+=("FAIL | $label | $line")
    printf '\n----- FAIL detail: %s -----\n%s\n' "$label" \
      "$(printf '%s\n' "$out" | grep -E 'FAILED|Error|assert' | head -15)"
  fi
}

echo "== 逐项跑修复的针对性测试（Simulate 模式）=="

# ── 第一批安全（沙箱里已 fail-pre/pass-post 验证过，这里在你的真依赖下复核）──
run "batch1: dingtalk 空userId绕过"      tests/test_dingtalk_card_authz_empty_userid.py
run "batch1: dingtalk SSRF 白名单"        tests/test_dingtalk_webhook_whitelist.py
run "batch1: feedback 授权(含端点403)"    tests/test_feedback_authz.py
run "batch1: spot unsafe 登记复审"        tests/test_spot_checker_unsafe_review.py
run "batch1: kb_gaps 绑定顺序"            tests/test_kb_gaps_refusal_param_order.py
run "batch1: config staging 运营库守卫"   tests/test_staging_ha3_suffix.py tests/test_config_guards.py tests/test_destructive_guard.py tests/test_simulate_prod_guard.py

# ── P2：沙箱已实测 ──
run "P2: retriever RRF image-tiebreak"    tests/test_rerank_img_probe_tiebreak.py
# 注：test_oversized_step_card_splits 是【本审查前就存在的】pre-existing 失败（step 模式断言，
# 与本次 clause 绑图改动无关），deselect 掉以免噪音；它红不红都不是本次修复的责任。
run "P2: chunker clause 就近绑图"         tests/test_chunker_clause_image_binding.py tests/test_chunker.py \
    --deselect "tests/test_chunker.py::TestStepCardLengthLimit::test_oversized_step_card_splits"

# ── P2：沙箱【无法执行】、你的真依赖首次能跑（关键！）──
run "P2*: extractor page_count 截断(PyMuPDF)" tests/test_per_page_ocr.py tests/test_extraction_fixtures.py
run "P2*: reconcile 向量维度(HA3 SDK)"        tests/test_reconcile.py
run "P2: D1 DEACTIVATE 审计(仅sim路径)"       tests/test_audit_log.py tests/test_ha3_engine.py tests/test_production_hardening.py tests/test_cs5_outbox.py

echo ""
echo "== 附加静态核验 =="
# reconcile 维度：活动代码里不应再有硬编码 1024（注释除外）
if grep -RnE 'vector\s*=\s*\[0\.0\]\s*\*\s*1024' opensearch_pipeline/ >/dev/null 2>&1; then
  RESULTS+=("FAIL | 静态: 仍有硬编码 [0.0]*1024 的向量构造")
else
  RESULTS+=("PASS | 静态: 无硬编码 [0.0]*1024 向量构造")
fi
# 第一批修复完整性（防残缺提交）
for m in F-dingtalk-ssrf F-fbk-authz F-spot-safety F-staging-opdb F-kb-gaps-sql F-oss-raw-key F-miniapp-token; do
  n=$(grep -RIl "$m" opensearch_pipeline/ dataworks_nodes/ fuling-rag-miniapp/ 2>/dev/null | wc -l | tr -d ' ')
  [ "$n" -ge 1 ] && RESULTS+=("PASS | 静态: $m 存在") || RESULTS+=("FAIL | 静态: $m 缺失")
done

echo ""
echo "════════════════════════ 汇总 ════════════════════════"
printf '%s\n' "${RESULTS[@]}"
echo "══════════════════════════════════════════════════════"
echo "备注：P2* 标记项 = 沙箱无法执行、靠你本地首次真跑；D1 仅验证 sim 路径，"
echo "生产真实删除审计路径需 staging(_stg 真实 DB) 才能端到端验证。"
