#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# day7_chunker_postfix_verify.sh — Day 7 chunker 修复后的复测 runbook
# ─────────────────────────────────────────────────────────────────────────────
# 目的:
#   D6 锁档报告点名了 chunker 端 ThreadPoolExecutor 完成顺序导致的 ingestion
#   非确定性(xlsx_sop step5/6 jaccard D5=0 / D6=1.0 摇摆)。Day 7 已对 assets
#   列表做稳定 key 排序(file_index/sheet_index/anchor_row)。本脚本以 5 次连跑
#   ingestion-only 的 deterministic 字段 byte-equal 作为判定:
#
#     ALL_EQUAL ✅  — 5 轮 per_chunk 完全 byte-equal、per_fmt mean std=0 → 升 hard
#     STD_OK ⚠️    — per_chunk 有飘但 per_fmt mean std ≤ 0.02 → micro-noise,可接受
#     DRIFT ❌      — per_fmt mean std > 0.02 → 回炉
#
# Panel 决策(默认 NOT NOW):
#   默认只在最后一轮 outdir 收 deterministic,不跑 panel。
#   加 --full-panel 才在每轮 outdir 都收 panel(贵,~5-7 min/轮)。
#
# 用法:
#   bash scripts/day7_chunker_postfix_verify.sh                # 5 连跑 + 报告
#   bash scripts/day7_chunker_postfix_verify.sh --n-runs 3     # 冒烟
#   bash scripts/day7_chunker_postfix_verify.sh --vlm-serial   # RAG_VLM_CONCURRENCY=1
#   bash scripts/day7_chunker_postfix_verify.sh --diagnose     # 失败继续出 partial
#   bash scripts/day7_chunker_postfix_verify.sh --full-panel   # 每轮收 panel(贵)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── 0. 仓库根 + bash 守卫(macOS 自带 3.2 — 关键负索引语法已规避,
#         但仍打印一行版本提示)──────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

echo "[+] bash version: ${BASH_VERSION:-unknown}"
# 不强制 bash >= 4(脚本已用 bash 3.2 兼容语法),仅记录

# ── 1. 默认参数 ──────────────────────────────────────────────
N_RUNS=5
VLM_CONCURRENCY="${RAG_VLM_CONCURRENCY:-8}"
DIAGNOSE=0
FULL_PANEL=0
GOLDSET="${GOLDSET:-eval_harness/goldset/golden_50.json}"
LAYERS="${LAYERS:-l4}"
GT_DIR="${GT_DIR:-$HOME/Downloads/opensearch-rag-data/eval_samples/ground_truth}"
DOCS_DIR="${DOCS_DIR:-$HOME/Downloads/opensearch-rag-data/eval_samples/documents}"
PYTHON_BIN="${PYTHON_BIN:-/Users/laijunchen/opt/anaconda3/envs/stack-test/bin/python}"

# ── 2. 解析 flags ────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --n-runs)       N_RUNS="$2"; shift 2 ;;
    --vlm-serial)   VLM_CONCURRENCY=1; shift ;;
    --diagnose)     DIAGNOSE=1; shift ;;
    --full-panel)   FULL_PANEL=1; shift ;;
    --goldset)      GOLDSET="$2"; shift 2 ;;
    --gt-dir)       GT_DIR="$2"; shift 2 ;;
    --docs-dir)     DOCS_DIR="$2"; shift 2 ;;
    -h|--help)      sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "[!] 未知参数: $1"; exit 2 ;;
  esac
done

# ── 3. 环境守卫 ──────────────────────────────────────────────
export RAG_ENV="${RAG_ENV:-prod_ro}"
export RAG_READONLY="${RAG_READONLY:-true}"
export RAG_VLM_CONCURRENCY="${VLM_CONCURRENCY}"
# DOCX 独立 strict 路径(env 命名归到 EVAL_L4_* namespace);ingestion_binding patch
# 若未合入,该 env 无效,docx 字段 N/A 属预期 — 不影响 verdict(verdict 不读 docx)
export EVAL_L4_DOCX_STRICT_ENABLE="${EVAL_L4_DOCX_STRICT_ENABLE:-true}"

# ── L4-ingestion 触发的 env(run_eval.py L91-103 读取)──────
# 不传 CLI flag — run_eval.py argparse 不识别 --gt-dir / --docs-dir
PDF_GT="${GT_DIR}/gt_pdf_analysis.json"
XLSX_GT="${GT_DIR}/gt_xlsx_pptx_analysis.json"
DOCX_GT="${GT_DIR}/gt_docx_analysis.json"
GT_LIST=""
for gt in "${PDF_GT}" "${XLSX_GT}" "${DOCX_GT}"; do
  if [[ -f "${gt}" ]]; then
    if [[ -z "${GT_LIST}" ]]; then GT_LIST="${gt}"; else GT_LIST="${GT_LIST},${gt}"; fi
  fi
done
if [[ -z "${GT_LIST}" ]]; then
  echo "[!] GT_DIR=${GT_DIR} 下找不到 gt_{pdf,xlsx_pptx,docx}_analysis.json 任一,abort"
  exit 4
fi
if [[ ! -d "${DOCS_DIR}" ]]; then
  echo "[!] DOCS_DIR=${DOCS_DIR} 不是目录,abort"
  exit 4
fi
export EVAL_L4_GT_FILES="${GT_LIST}"
export EVAL_L4_DOCS_DIR="${DOCS_DIR}"

if [[ "${RAG_ENV}" != "prod_ro" ]]; then
  echo "[!] RAG_ENV=${RAG_ENV},非 prod_ro。继续? (Ctrl-C abort)"; sleep 3
fi

# clean tree 检查(只看 tracked, 排除 reports/scratch 白名单)
DIRTY="$(git status --porcelain --untracked-files=no 2>/dev/null | grep -v -e '^.. eval_harness/reports/' -e '^.. scratch/' || true)"
if [[ -n "${DIRTY}" ]]; then
  echo "[!] working tree 有未提交改动(代码层,已排除 reports/scratch):"
  echo "${DIRTY}" | sed 's/^/    /'
  echo "[!] 复测建议在 clean tree 上跑;Ctrl-C abort,或回车继续 (5s)"
  sleep 5
fi

# ── 4. 时间戳目录 ────────────────────────────────────────────
STAMP="$(date +%Y%m%d_%H%M%S)"
STAMP_DIR="${REPO_ROOT}/scratch/day7_chunker_verify_${STAMP}"
mkdir -p "${STAMP_DIR}"

echo "──────────────────────────────────────────────────────────────"
echo " Day 7 chunker post-fix verify"
echo "──────────────────────────────────────────────────────────────"
echo "  stamp:                  ${STAMP}"
echo "  out:                    ${STAMP_DIR}"
echo "  N_RUNS:                 ${N_RUNS}"
echo "  FULL_PANEL:             ${FULL_PANEL}  (0=只 deterministic, 1=每轮收 panel bundle)"
echo "  RAG_ENV:                ${RAG_ENV}"
echo "  RAG_READONLY:           ${RAG_READONLY}"
echo "  VLM_CONCURRENCY:        ${RAG_VLM_CONCURRENCY}"
echo "  PYTHON_BIN:             ${PYTHON_BIN}"
echo "  EVAL_L4_GT_FILES:       ${EVAL_L4_GT_FILES}"
echo "  EVAL_L4_DOCS_DIR:       ${EVAL_L4_DOCS_DIR}"
echo "  EVAL_L4_DOCX_STRICT:    ${EVAL_L4_DOCX_STRICT_ENABLE}  (若 ingestion_binding patch 未合,docx 字段 N/A 属预期)"
echo "  goldset:                ${GOLDSET}"
echo "  layers:                 ${LAYERS}  (只跑 L4 ingestion 支柱,L3 跳过)"
echo "  HEAD:                   $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "──────────────────────────────────────────────────────────────"
echo "  ⓘ 首轮若 scratch/vlm_cache.json 不存在,Qwen-VL 冷启动 +3-5 min"
echo "    热 cache:每轮 ~6-8 min;冷启动总耗时可达 60+ min"
echo "──────────────────────────────────────────────────────────────"

# ── 5. 输入锁档(goldset + GT md5,真守卫:每轮重 hash 比对)──
INPUTS_LOCK="${STAMP_DIR}/inputs.lock"
hash_inputs() {
  # 输出固定顺序的 md5 行,供 diff 比对
  for f in "${GOLDSET}" "${PDF_GT}" "${XLSX_GT}" "${DOCX_GT}"; do
    if [[ -f "${f}" ]]; then
      if command -v md5sum >/dev/null 2>&1; then
        md5sum "${f}"
      else
        md5 -r "${f}"
      fi
    fi
  done
}

{
  echo "# Day 7 chunker post-fix verify — inputs lock"
  echo "# generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# 5 次连跑必须复用相同输入;任何一轮 md5 mismatch 即 abort"
  echo ""
  hash_inputs
} > "${INPUTS_LOCK}"
echo "[+] inputs locked → ${INPUTS_LOCK}"
sed 's/^/    /' "${INPUTS_LOCK}"
echo ""

# 取出锁档里仅 md5 行(忽略 # 注释 + 空行)
LOCK_HASH_ONLY="${STAMP_DIR}/inputs.lock.hashes"
grep -v -e '^#' -e '^[[:space:]]*$' "${INPUTS_LOCK}" > "${LOCK_HASH_ONLY}"

verify_inputs() {
  local cur="${STAMP_DIR}/inputs.cur.hashes"
  hash_inputs > "${cur}"
  if ! diff -q "${LOCK_HASH_ONLY}" "${cur}" >/dev/null 2>&1; then
    echo "[!] 输入 md5 与 inputs.lock 不一致,abort:"
    diff "${LOCK_HASH_ONLY}" "${cur}" | sed 's/^/    /'
    return 1
  fi
  return 0
}

# ── 6. 主循环:N 次 ingestion-only ───────────────────────────
RUN_DIRS=()
for i in $(seq 1 "${N_RUNS}"); do
  RUN_OUTDIR="${STAMP_DIR}/run_${i}"
  mkdir -p "${RUN_OUTDIR}"
  RUN_DIRS+=("${RUN_OUTDIR}")
  echo "──────────────────────────────────────────────────────────────"
  echo " [run ${i}/${N_RUNS}] → ${RUN_OUTDIR}"
  echo "──────────────────────────────────────────────────────────────"

  # 每轮重 hash 比对锁档 — 真守卫(不是 just-record)
  if ! verify_inputs; then
    echo "[!] run ${i} 输入校验失败,abort"
    exit 5
  fi

  # ingestion-only:layers=l4 → l4_multimodal._run_ingestion(env 触发)
  # 不传不存在的 CLI flag;run_eval.py 只认 phase/--goldset/--layers/--limit/--outdir
  set +e
  "${PYTHON_BIN}" -m eval_harness.run_eval run \
    --goldset "${GOLDSET}" \
    --layers "${LAYERS}" \
    --outdir "${RUN_OUTDIR}" \
    2>&1 | tee "${RUN_OUTDIR}/run.log"
  RC=${PIPESTATUS[0]}
  set -e

  if [[ "${RC}" -ne 0 ]]; then
    echo "[!] run ${i} 失败(rc=${RC});日志 → ${RUN_OUTDIR}/run.log"
    if [[ "${DIAGNOSE}" -eq 0 ]]; then
      echo "[!] 非 --diagnose 模式,abort。要继续看 partial,加 --diagnose"
      exit "${RC}"
    fi
  fi
  if [[ ! -f "${RUN_OUTDIR}/report.json" ]]; then
    echo "[!] run ${i} 未产 report.json"
    if [[ "${DIAGNOSE}" -eq 0 ]]; then exit 3; fi
  fi
done

# ── 7. helper 对比:N 个 outdir 算 std + per_chunk byte-diff ─
echo ""
echo "──────────────────────────────────────────────────────────────"
echo " 对比 ${N_RUNS} 轮 deterministic 字段"
echo "──────────────────────────────────────────────────────────────"
COMPARE_JSON="${STAMP_DIR}/compare.json"
COMPARE_MD="${STAMP_DIR}/compare.md"
FINAL_REPORT="${REPO_ROOT}/eval_harness/reports/D7_chunker_postfix_report.md"

# bash 3.2 安全的逗号拼接(不用 IFS=,;array[*])
RUNS_CSV=""
for d in "${RUN_DIRS[@]}"; do
  if [[ -z "${RUNS_CSV}" ]]; then RUNS_CSV="${d}"; else RUNS_CSV="${RUNS_CSV},${d}"; fi
done

# baseline 路径(若 D6 baseline 在,helper 自动 diff;不在则跳)
BASELINE_D6="${REPO_ROOT}/eval_harness/reports/run_l4_baseline_d6/report.json"
BASELINE_ARG=()
if [[ -f "${BASELINE_D6}" ]]; then
  BASELINE_ARG=(--baseline-d6 "${BASELINE_D6}")
fi

set +e
"${PYTHON_BIN}" "${SCRIPT_DIR}/day7_chunker_postfix_compare.py" \
  --runs "${RUNS_CSV}" \
  --out "${COMPARE_JSON}" \
  --report "${COMPARE_MD}" \
  --final-report "${FINAL_REPORT}" \
  "${BASELINE_ARG[@]:-}"
CMP_RC=$?
set -e

# ── 8. 打印 deterministic verdict ───────────────────────────
echo ""
echo "══════════════════════════════════════════════════════════════"
echo " Day 7 deterministic verdict"
echo "══════════════════════════════════════════════════════════════"
# bash 3.2 安全的末元素索引
LAST_IDX=$(( ${#RUN_DIRS[@]} - 1 ))
LAST_OUTDIR="${RUN_DIRS[${LAST_IDX}]}"

case "${CMP_RC}" in
  0)
    echo "  ALL_EQUAL ✅ — ${N_RUNS} 轮 per_chunk byte-equal,chunker 已确定"
    echo ""
    if [[ "${FULL_PANEL}" -eq 1 ]]; then
      echo "  --full-panel 模式:每轮 outdir 都收 judge_bundle_binding.json(贵,5×panel)"
    else
      echo "  默认 deterministic-only 模式:不自动跑 panel"
      echo "  若要在最后一轮(${LAST_OUTDIR})跑 image_binding panel(看 mean ≥ 4.0?):"
      echo ""
      echo "    # 1) inline 切 shard(run_eval 无 shard 子命令,用 python -c)"
      echo "    SHARD_DIR=\"${LAST_OUTDIR}/shards_binding\""
      echo "    mkdir -p \"\${SHARD_DIR}\""
      echo "    \"${PYTHON_BIN}\" - <<'PY'"
      echo "    import json, os, math"
      echo "    bundle = json.load(open('${LAST_OUTDIR}/judge_bundle_binding.json'))"
      echo "    shard_dir = '${LAST_OUTDIR}/shards_binding'"
      echo "    n_shards = 2"
      echo "    size = math.ceil(len(bundle) / n_shards) if bundle else 0"
      echo "    for i in range(n_shards):"
      echo "        chunk = bundle[i*size:(i+1)*size]"
      echo "        with open(os.path.join(shard_dir, f'shard_{i:03d}.json'), 'w') as fh:"
      echo "            json.dump(chunk, fh, ensure_ascii=False, indent=1)"
      echo "    print(f'wrote {n_shards} shards, total {len(bundle)} items')"
      echo "PY"
      echo ""
      echo "    # 2) 跑 panel workflow(3 评委)"
      echo "    claude workflow run eval_harness/judge_panel_workflow.js \\"
      echo "      --args '{\"shard_dir\":\"'\"\${SHARD_DIR}\"'\",\"n_shards\":2,\"n_judges\":3}' \\"
      echo "      > ${LAST_OUTDIR}/judge_verdicts.json"
      echo ""
      echo "    # 3) merge — run_eval merge 子命令真实存在(L175 choices=['run','merge'])"
      echo "    \"${PYTHON_BIN}\" -m eval_harness.run_eval merge \\"
      echo "      --results ${LAST_OUTDIR}/report.json \\"
      echo "      --verdicts ${LAST_OUTDIR}/judge_verdicts.json"
      echo ""
      echo "    # 4) 若 image_binding mean ≥ 4.0(D6=3.314),手写 ${FINAL_REPORT} 锁档"
    fi
    exit 0
    ;;
  2)
    echo "  STD_OK ⚠️ — per_fmt mean std ≤ 0.02 可接受,但 per_chunk 有飘"
    echo "      看 ${COMPARE_MD} 中 top-N 飘动 chunk;不强制回炉,但建议局部排查后重跑"
    exit 2
    ;;
  *)
    echo "  DRIFT ❌ — chunker 仍非确定;比对报告 → ${COMPARE_MD}"
    echo "      panel 不跑(噪声叠噪声),先回炉看 ThreadPool 顺序 / asset 排序"
    exit 1
    ;;
esac
