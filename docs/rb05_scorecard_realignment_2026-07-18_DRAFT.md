# RB-05 成绩单重对齐:决策文档 + refreeze runbook(2026-07-18)

> 问题:CI 全绿,但模型效果/容量证据全部过期——RAG 基线绑旧金集 SHA + 已退役的
> `qwen3.6-plus`;Agent L7 基线冻结后 runtime/dispatch/Ontology 大改且基线**无指纹**,
> 无人能发现它过期;压测成绩全部出自旧提交线且 report 不记 commit。
> 类比:学生换了教材、换了考题、换了答题方法,却拿上个月的成绩单申请毕业。
>
> 根因是共性的:**成绩只记数字不记 regime 指纹,过期后无任何机制报警。**
> 本轮除了准备重跑,核心交付是把「成绩单过期」变成 CI 红灯(`baseline-freshness` job)。

## 0. 拍板(Sam,2026-07-18)

- **双锚**:RAG 基线对齐 **main**(部署真身);Agent 基线 + 压测对齐 **claude/ontology-p0**
  (代码只在该分支)。R0-01 大合并落地后三份基线会再次过期——**这是接受的代价**,届时
  CI 新鲜度红灯会亮,按红灯提示统一 refreeze 一次即可(不在合并前后各烧两轮 live)。
- **压测 smoke 进 CI 硬门**(tier=local 零 API 费用,PR paths 触发)+ 每周日 schedule
  full 档非阻断。

## 1. 本轮已落地(代码侧,全部 sim/离线/mock 验证)

| 层 | 内容 | 文件 |
|---|---|---|
| D | 陈旧模型引用 `qwen3.6-plus`→`qwen3.7-plus`(两分支) | `.env.example` / `eval_harness/README.md` / `docs/architecture.md` / `docs/dingtalk-miniapp-design.md` |
| B1 | **Agent 基线指纹化**(仅 ontology-p0):`_regime()`(cases_sha16/model/tier/ontology_flag_on/prompt_sha16/provider + 信息性 code_commit/git_dirty)入 report;freeze 写入 regime、旧格式 report 拒冻;gate 前强制 `_regime_matches`,mismatch → exit 2 fail-closed | `eval_harness/agent/runner.py` + `tests/test_agent_eval_harness.py`(+3 例) |
| C1 | 压测报告↔提交绑定:`report.py::_git_fingerprint()`(git_sha/branch/dirty 入 payload+markdown 头);`stress.yml` 三通道(PR paths 硬门 smoke / 周日 schedule full / dispatch 任意档) | `stress_harness/report.py` / `.github/workflows/stress.yml`(仅 ontology-p0) |
| E1 | **基线新鲜度门**:金集 SHA、llm/embedding/reranker 模型(从 `load_config()` 工厂现算,不硬编码)、`.env.example` 防复发、agent 三指纹复算;任一 FAIL exit 1。CI 新 job `baseline-freshness`(**暂 continue-on-error**,refreeze 后翻硬门) | `scripts/check_baseline_freshness.py` / `.github/workflows/ci.yml`(两分支) |
| E1 | sim 效果门入 CI 阻塞步:`general_ability_eval`(离线零网络,本地两分支实跑 PASS) | `.github/workflows/ci.yml` test job |

**当前新鲜度检查实测(如实,这正是 RB-05 的可见化)**:
- 两分支 `rag.eval_set_sha` FAIL(现 `b3403db07db7b71a` vs 冻结 `3bed9881eefaf0ee`)、
  `rag.llm_model` FAIL(`qwen3.6-plus` vs `qwen3.7-plus`);ontology-p0 另有
  `agent.regime_present` FAIL(旧格式基线)。其余 PASS。
- **sim 过渡证据**(不是 release-gate 替代,只证明离线可测面在当前代码上健康):
  - `general_ability_eval`:PASS(金集零劫持,企业误放行 0);
  - G6 fidelity sim 档:PASS(char_f1 0.9615 / cer 0.0925 / table_cell_f1 0.9928,
    对冻结 sim 基线无回归;GT 在 data repo,CI 不可跑,故未入 CI)。

## 2. user-gated runbook(等 Sam 执行/授权;live 一律不由 Claude 自行发起)

### A2 — RAG 基线 refreeze(Sam 本机,main HEAD;live prod_ro + DashScope + claude CLI)

```bash
# 前置:claude CLI 已登录(judge=claude-opus-4-8): claude --version;prod_ro 凭证可用
# 不设 RAG_LLM_MODEL——让 config 默认解析 qwen3.7-plus(基线锚定「当前默认」)
# 0) 小样本冒烟(烧全量前验证连通性;485 盲文档大概率已随金集重生成治愈,先验)
RAG_EVAL_LIMIT=5 make release-gate     # 或直接 run_eval run --limit 5
# 1) 全量 live(envboot 强制 prod_ro + RAG_SIMULATE=false;rerank 必开,否则 L1/L2 不代表生产)
RAG_RERANK_ENABLE=true python -m eval_harness.run_eval run \
  --goldset eval_harness/goldset/golden_full.json --layers l0,l1,l2,l3,l4,l5,l6 \
  --outdir eval_harness/reports/refreeze_20260718
# 2) judge 面板(3 panels): python -m eval_harness.run_judge ...(参数照 docs/audits/l7_refreeze_release_gate_2026-07-12.md)
# 3) 人工裁决 L1 对旧基线的显著 diff(逐条看,不盲冻)
make eval-baseline-freeze RESULTS=eval_harness/reports/refreeze_20260718/report.json
# 4) A3 复跑出绿:
make release-gate                      # 预期 sha 预检过(≠exit 3)、strict merge exit 0
python scripts/check_baseline_freshness.py   # 预期 rag.* 全 PASS
```

### B2 — Agent 基线 refreeze(ontology-p0;36 例 live DashScope,费用极小但属 live)

```bash
make agent-eval          # 产出带 regime 的 report(B1 后新格式)
# 人工裁决:write_propose_rate 等软指标相对 0.5 的漂移——prompt/registry 冻结后大改,
# 数字预期会动;这是「记录新事实」不是回归,但必须人裁,不可盲冻
python -m eval_harness.agent.runner --freeze-baseline eval_harness/reports/agent_eval_<ts>.json
make agent-eval-gate     # 预期 exit 0(regime 匹配 + 指标过闸)
```

**B3(B2 后一并做)**:`runner.py` `GATED_METRICS` 追加 `ontology_tool_trigger_rate` /
`ontology_args_relevance`(现仅显示不进闸)并删对应「重冻前不进闸」注释。

### C3 — staging 压测(真实计费,可选)

```bash
make stress-staging STRESS_STAGING_ACK=I_UNDERSTAND_COSTS \
  STRESS_BUDGET_MODEL_CALLS=<N>   # + STRESS_TARGET_URL/STRESS_TARGET_TOKEN
```

### E2 — 新鲜度门翻硬(A2+B2 落地后)

两分支 `.github/workflows/ci.yml` 的 `baseline-freshness` job 删掉
`continue-on-error: true` 一行(有行内注释标记)。

## 3. 预期中间态(勿误判为故障)

- **A2 前**:CI `baseline-freshness` 红叉(continue-on-error 不阻塞)——预期,即 RB-05
  的可见化;文档已写 3.7 而冻结基线仍记 3.6 的窗口由该红灯看护。
- **R0-01 大合并后**:三份基线再次红——预期;合并 PR checklist 应写明「合并后按红灯
  refreeze × 3(RAG/Agent/压测)」。
- Agent 旧 `baseline.json`(2026-07-12,无 regime)在 B2 前被 gate 拒比对(exit 2)——
  这是 fail-closed 的正确行为,不是 harness 坏了。

## 4. 压测成绩单(C2,本地零成本档)

见 `stress_harness/reports/`(ontology-p0)最新 run:C1 后 report 带
`git.{git_sha,git_branch,git_dirty}`,「当前 HEAD 有效完整压测结果」的判据 =
`git_sha == 被验收 commit && git_dirty == false && hard_fail == false`。
