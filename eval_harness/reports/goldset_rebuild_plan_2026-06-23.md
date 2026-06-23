# Plan — Rebuild the eval gold set & re-run L0–L6 against the current ~27.7k-chunk corpus

**System:** Alibaba HA3 enterprise RAG for Fuling Plastics. **Mode:** READ-ONLY / CONTROLLED. **Branch:** `eval/goldset-rebuild`.
**Date:** 2026-06-23.

> **Evidence tiers.** `[CODE]` = verified by direct repo read (file:line). `[SQL]` = confirmed by **two independent prod read-only SQL runs that agree** (interactive forensics 2026-06-22/23 + the goldset-rebuild workflow coverage agent), persisted in **`eval_harness/reports/coverage_gap_2026-06-23.json`** (supersedes the stale 2026-06-07 `coverage_gap_findings.md` that predates the dept_internal campaign). All corpus numbers below are `[SQL]` and reproducible from that artifact — they are **not** memory-asserted.

---

## 0. Why now — the gold set is blind to 92.6% of the corpus

`[SQL]` The gold set (251 cases / 120 distinct expected docs) was authored against the **old all-public snapshot** (gold docs created 2026-05-13/14). The corpus has since grown to **1,621 active docs / 27,729 active chunks**, of which:

- **1,501 active docs (92.6%) have ZERO gold coverage** — the entire **1,056-doc dept_internal campaign** (2026-06-20..22) + a 445-doc pre-campaign tail.
- **dept_internal is now 65% of docs / 76% of chunks (21,008)** and has **0 gold cases** → **L5 ACL eval (previously N/A) is now both exercisable and completely untested.**
- **≥1 answer-absent negative is already broken** by campaign growth (`J-spec_rim_od` — a spec doc now states `杯口外径 89.3±0.2`), with 2 more at risk and ~7 needing spot-checks.

---

## 1. Scope & what "read-only/controlled" actually enforces

**In scope:** fresh dated recon artifact; re-resolve the 251 against live RDS; author new cases (incl. the dept_internal authorized-path family); run L0–L6; rerank OFF-vs-ON A/B; `rerank_ab.py` pool sweep; Claude judge panel; L2 score capture for threshold recalibration; new frozen baseline. **All writes are LOCAL** (gold JSONs, `reports/`, `recalibrate` JSON, `baseline.json`) on the branch, shown as `git diff` before commit.

**Out of scope (HARD):** any prod-KB change — no DDL, no re-chunk/re-index/deactivate, no deploy, **no flag flips** (esp. not prod `RAG_RERANK_ENABLE`). Threshold recalibration here yields a **recommendation only**. Any harness *code change* this plan flags (recall@20, L5 stratification, dim-3/4/5/6 probes, cohort stratum) is itself out of the read-only scope and is called out where it arises.

**What enforces read-only (corrected — the obvious assumption is wrong):**
- `[CODE]` The eval harness opens RDS via `eval_harness/ha3live.py:125` `rds_conn()` = a **raw `pymysql.connect()`**. It does **NOT** use `prod_access.get_prod_readonly_conn()`, so the `SET SESSION TRANSACTION READ ONLY` guard is **never armed**. Real safety = **(a)** the read-only-*granted* account `RAG_RDS_USER=fuling_ro` (`.env.prod_ro`; prod RW is `fuling_admin` in `.env.production`) + **(b)** all harness SQL is SELECT-only by construction. → **Phase-0 must `SHOW GRANTS FOR CURRENT_USER()` to confirm `fuling_ro` has no write grant.**
- `[CODE]` `envboot.boot()` runs on import (`envboot.py:106`) and **rewrites your env**: blanks `RAG_ENV` (`:55`), forces `RAG_ENVIRONMENT=test`→prod_ro alias (`:54`), forces `RAG_SIMULATE*=false` (`:56-58`), only `setdefault`s the remote-acks (`:60-61`); it **never sets `RAG_READONLY`** (that comes solely from `.env.prod_ro`, and only if not already shell-exported). → Passing `RAG_ENV=prod_ro` on the CLI is a **no-op**. **Run from a clean shell** with `RAG_READONLY`/`RAG_RDS_USER`/`RAG_ALLOW_REMOTE_*`/`RAG_HA3_*` unset, then **assert resolved `RAG_RDS_USER==fuling_ro`, `RAG_READONLY==true`, `simulate==false`** (log `envboot.facts()`) before any layer runs.
- `[CODE]` HA3 has no read-only session mode; protection is that the harness only calls `query_vector`/`get_table`/`stats`/`search_chunks`/`retrieve_and_enrich`/zero-vector-enum. → **Phase-0 grep gate:** no eval module calls `push`/`deactivate`/`update_index_status`.
- `[CODE]` The Claude judge is an **unsandboxed local subprocess** (`run_judge.py:52`, `subprocess.run([CLAUDE,"-p",…], cwd="/tmp", timeout=900)`); confirm `RAG_CLAUDE_BIN` is unset/`claude` and not a write-capable profile.

---

## 2. Gold-set rebuild

### 2.1 RECON-0 (done) + re-resolve the 251
- **RECON-0 — DONE:** `eval_harness/reports/coverage_gap_2026-06-23.json` holds all `[SQL]` counts. Future runs cite this file, not session SQL.
- `[CODE]` **Snapshot before rebuild (R0):** `build_goldset.py` overwrites `golden_full.json` **and** `golden_50.json` unconditionally **and ignores `additions.*.json`** — a bare rebuild destroys the 76-case run-set + all hand-authored S5 cases. → commit + `.bak_pre_rebuild` first; rebuild outputs to `golden_full+.json` etc.
- **R1** re-run `build_goldset` (clean shell) → fresh resolution vs live inventory. **R2** diff old-vs-new resolution per positive (flag every changed resolution — fuzzy `thr=0.6` first-wins onto a near-dup title is the documented twin hazard, `build_goldset.py:64-84`). **R3** re-validate ALL negatives (§2.4).

### 2.2 Coverage gaps → stratified authoring targets `[SQL]`
Zero-coverage docs: **by dept** production 799 / marketing 180 / rd 175 / hr 102 / quality 55 / finance 50 / pmc 49 / admin 35 / it 33 / supply 23; **by filetype** pdf 643 / docx 510 / xlsx 337; **by category** standard 492 / sop 425 / reference 203 …; **by capability** 279 SOP (step_card) / 623 multimodal / 247 image-bound / 339 xlsx / 511 table / 566 public.
- **dept normalization (GATE-1 blocker):** `[CODE]` L1 `by_dept` keys on the raw case `dept` string; live `golden_50.json` carries Chinese names (人力资源部/行政部) + `None` (33/76) while S5 cases use English codes → the stratum fragments. `validate_gold_additions.py` only lints the additions file, not the merged set. → re-tag every surviving case's `dept` to the English `DEPTS` code (from inventory `owner_dept` where None) before any run.

### 2.3 Authoring method (raw-first)
`[CODE: AUTHORING_GUIDE.md]` (1) **verify before authoring** — a positive must be answerable via `retrieve_and_enrich` (rerank ON); (2) **`keyword_gt` must be VERBATIM** substrings of an answer-bearing chunk (`matching.py:82-109` does literal `kw in text`); (3) fix the pipeline, never loosen matching; (4) `neg_type` decides expected behaviour; (5) tag `expected_permission` from live inventory, not the stale all-public rule. **Per-field schema (gen-2, `neg_type` not `subtype`):** `qid`/`source`/`module`/`dept`(English)/`query`/`kind`/`neg_type`(off_topic|near_miss_answer_absent|metadata|modality_gap|live_data)/`expected_docs`/`expected_doc_ids`/`resolution`/`answer_points`/`pass_criteria`/`keyword_gt`/`difficulty`/`expect_images`/`expected_images`/`expected_permission`/`live_scorable`. **Loop:** LLM drafts (query, answer_points, candidate keyword_gt) from sampled chunk text → `validate_gold_additions.py --verify-live` grounds each → human-calibrate the judge on a sample (`human_calibration_template.json`, separate artifact) → **merge by hand** into `golden_full+.json` (re-merge after any rebuild).

### 2.4 dept_internal / ACL family — CONFIRMED needed (not contingent)
`[SQL]` dept_internal is live (1,055 docs / 21,008 chunks; `L5_now_exercisable: true`). **Two distinct work items — state honestly:**
1. **dim-2 authorized-path positives (gold authoring).** Author dept_internal positives (production/marketing/rd-led) with correct `owner_dept` + `expected_permission=["dept_internal"]`. `[CODE]` These feed **L1/L2/L3** (the authorized path; L1 *excludes* non-public positives from public recall via `_publicly_retrievable`, `l1_retrieval.py:27-29`).
2. **dim-8 per-dept ACL evidence — NOT produced by L5 as built.** `[CODE]` `l5_permission.py:30` `run(max_docs=5)` **auto-discovers** gated docs and tests only the **first 5** + `gated[0]` for the 4 injection payloads; **it never reads gold cases.** So authored cases do **not** drive L5. To answer "is ACL correct per dept," either **(a)** raise `max_docs` + add per-`owner_dept`/umbrella/marketing-shared stratification to `l5_permission.py` (**code change — out of scope, needs approval**), or **(b)** build a standalone read-only ACL probe on the `search_chunks(user_dept=…)` pattern iterating every gated dept (public excludes / authorized includes / umbrella sees subtree / non-owner excluded / injection-safe). **Stop claiming authored gold cases ARE the L5 test.**

### 2.5 Negatives refresh — `[SQL]` 1 confirmed broken + 2 at risk
- **RETIRE/re-scope `J-spec_rim_od`** (now answerable — `DOC_PRODUCTION_…_58669A` has `杯口外径 89.3±0.2`). **Author-review `J-spec_migration`, `J-manual_docno`.** **Spot-check** ~7 J-* (gloves/antistatic/sticker_pwd/…). Procedure per negative: `retrieve_and_enrich` (rerank ON) + targeted `chunk_text LIKE` probe; if the asked fact is now present → retire/re-scope; else keep + **re-tag `neg_type`** (the 11 J-* + 15 RAG negatives in the builder output carry no `neg_type`; only the gated `golden_50.json` is fully tagged).
- **Author ≥8 off_topic negatives** (was 5) — `[CODE]` L2 AUC gate enforced only at ≥5 (`RAG_EVAL_L2_MIN_OFFTOPIC=5`, `l2_calibration.py:20`); a denser dept_internal corpus raises near-topic leakage.
- **Boundary-refusal RAG-49/58/59** now have matching dept_internal docs — they don't break (refusal-on-policy), but **verify the refusal still fires** against real content.

### 2.6 Target composition (PROPOSED, not derived)
Tier A `golden_full+.json` ≈ 360 (survivors + new). Tier B gated run-set ≈ 110: ~40 carry-forward survivors · ~18 dept_internal authorized positives · ~8 cross-dept denial (for the §2.4 ACL probe) · ~12 public positives by dept · ~8 SOP/step-card · ≥6 image (`[CODE]` <5 image answers → L4-serving `not_executed` strict FAIL) · ~6 xlsx/table · ≥8 off_topic · ~6 other negatives. **If the gated set >130, re-estimate judge cost before Phase 3.** Maintain `golden_l4_serving+.json` (currently 25 image cases).

---

## 3. Re-run design

### 3.1 Layers `[CODE]` (`--layers l0,l1,l2,l3,l4,l5,l6`)
- **L0** (embeds only): G0 docCount vs RDS `total_active_chunks` (delta<0 = DATA LOSS fail; surplus within `max(5, 0.5%)` passes — coarse gate, **not** exact parity), G2 dense self-query ≥98% of 60, G3 sparse ≥90% of 40, G4 cos≥0.99.
- **L1** (DashScope embed + HA3, `top_k=10`): recall@1/3/5/10, MRR, nDCG@10 (+CIs), `found_rate`, strata `by_module/by_source/by_dept/by_difficulty`, `pos/neg_top1_score_dist`, **serial** latency p50/p90/p95/p99. **No recall@20** — `metrics.py:93` `ks=(1,3,5,10)`; the user's **R@20 needs a 1-line `ks` change (code, flag it).**
- **L2** (no live calls): label bands 高/中/低, `frac_高`, `separation_auc_offtopic`, `neg_high_by_type`. Switches to rerank bands 0.9/0.8 when rerank ON, weighted 7.7/5.8 when OFF (`l2_calibration.py:54-59`). **Threshold-recalibration input.**
- **L3** (Qwen `gen_nothink`, `top_k=7`): `reasoning_leak`, `over_refusal_rate`, `coverage_gap_refusal_rate`, `source_leak_rate`, `mean_keyword_coverage`, neg `interception_rate`; emits `judge_bundle.json`. **Global + pos/neg only — NOT by_dept/by_capability.**
- **L4** A ingestion (`binding_jaccard_{pdf,xlsx,docx,pptx}`) + B serving (`marker_validity`/`orphan_rate`/…). **<5 image answers → strict FAIL.**
- **L5** (no LLM): see §2.4 — default 5-doc probe only.
- **L6** chunk quality (families B/C/D/E/F/H; 3-state GO/NO_GO_DEFECT/NO_GO_INCOMPLETE_EVIDENCE). **TRAP:** `[CODE]` Family-H zero-vector enum is `top_k=12000` (`l6_chunk_quality.py:622`) < 27,729 active → enum truncates → **L6 can never reach GO**. → **raise to `>= total_active_chunks()*1.2` at runtime, or page if the endpoint caps.** This is a ~28k-row HA3 read — re-budget.

### 3.2 rerank OFF-vs-ON A/B (eval-harness-local, NOT a prod flip)
`[CODE]` Prod serves rerank OFF; the frozen baseline regime is **rerank ON** (`baseline.json regime.rerank_enable=true`). The harness does not force `RAG_RERANK_ENABLE`; `_regime_guard` (`run_eval.py:94-106`) fails the calibration gate unless active rerank == `RAG_EVAL_CALIBRATION_RERANK` (default true), and L2 switches bands by rerank state — **a rerank-OFF run scored vs weighted thresholds is invalid.** Plan: two **aligned** arms — **(ON)** full L0–L6 + judge (freeze baseline here); **(OFF)** `RAG_EVAL_CALIBRATION_RERANK=false`, **L0/L1/L2 only** (no L3 gen, no judge). The +10.5pp R@1 from the old A/B (`rerank_findings.md:19`) is directional only — re-confirm on the new corpus.

### 3.3 Threshold recalibration (dim 7) — tool reality
`[CODE]` `recalibrate.py` services the **WEIGHTED arm only** (single high/medium pair, env_lines `RAG_SCORE_THRESHOLD_HIGH/MEDIUM`, fallbacks 8.0/5.0, no rerank branch). → run it against the **rerank-OFF (weighted) arm report only**; derive the **rerank-arm** recommendation **by hand** from the rerank-ON L1 0–1 score dists. **Recommendation only** — applying to prod is out of scope.

### 3.4 Dims 3/4/5/6 — what the run adds vs what needs a probe (honest)
The default L0–L6 run does **not** answer four originally-asked dimensions:
- **Dim 3 (top-k & rerank tuning):** `[CODE]` `top_k` is hardcoded (10/7). → run **`rerank_ab.py --pool {10,20,30} --models qwen3-rerank,qwen3-vl-rerank`** + repeat L1 at a few top_k values (recall-vs-k curve). The binary ON/OFF A/B alone is NOT tuning.
- **Dim 4 (same-source saturation):** no harness metric. → add a read-only probe over L1 per-query retrieved sets (distinct-doc count in top-k, max single-doc slot share); author multi-relevant cases (`ranking_multidoc` exists in L1). *(small code add — flag)*
- **Dim 5 (routing & filter selectivity):** only ACL *safety* (L5) exists. → add a per-dept authorized-vs-public recall-delta + candidate-pool-size probe via `search_chunks` with/without `user_dept`; use `rerank_ab.py` text-vs-VL routing as multimodal-routing evidence. *(code add — flag)*
- **Dim 6 (latency vs throughput/capacity):** L1 gives **serial single-query latency only**. No throughput/QPS probe anywhere. → either add a read-only concurrent-load probe (ThreadPool over `retrieve_and_enrich`) **or** scope throughput OUT (SAE pins `--workers 1` + in-mem sessions → capacity is a deployment-topology question). State which.
- **Old-vs-new cohort & per-dept/per-capability ANSWER quality:** no cohort stratum; L3/judge emit global+pos/neg only; `eval_set_sha` changes so one combined run can't emit a clean delta. → run surviving-251-subset and new-authored-subset as **two separate gold files** and diff (no code change), and scope per-dept/per-capability to **L1 retrieval** unless L3 aggregation is approved.

### 3.5 Controlled-cost budget (judge is the dominant, sequential surface)
| Class | Layers | Driver |
|---|---|---|
| Cheap/no-LLM | L0, L2, L5, L6-deterministic | embeddings (L0 self-query n=60) + HA3 reads + 1 RDS pull + **1 zero-vector enum at ~28k rows** (raised from 12000 — modest read, not negligible) |
| DashScope | L1 embeds, L3 gen (ON arm: ~N gens), reranker (~N×20 scored) | OFF arm = L0/L1/L2 only (no gen/judge) |
| **Claude judge (ON arm only)** | L3 + L4(×2) + L6-chunk | `[CODE]` `claude -p` per **20-item batch**, `timeout=900`, panels=3, **strictly sequential** (`run_judge.py:63-66`) |

`[CODE]` **Worked judge estimate, N=110:** calls = panels(3) × ⌈N/20⌉ × #bundles → L3 ≈ 18, L4 ≈ 6, L6-chunk ≈ ~6–27 → **~30–50 `claude -p` calls per ON-arm run, each ≤900s, sequential → multi-hour worst case.** $ figures not in code → `TBD (measure on smoke run)`. **Smoke first:** `run --limit 5 --layers l0,l1,l2`, extrapolate before any judge spend.

### 3.6 Judge panel + gate `[CODE]`
`run_judge --bundle … --panels 3 --rubric answer` (+ `--rubric chunk`) → `run_eval merge --strict` (the real CI gate; `run --strict` fails pre-judge by design). Hard gates: faithfulness/correctness/completeness ≥4.0, **source-leak ≤0.05** (`report.py:86`), inter-judge stdev ≤1.2 (`report.py:13`). Judge = unsandboxed subprocess (§1).

---

## 4. The user's 8 dimensions → what the run actually produces

| # | Dimension | Produced by | Verdict |
|---|---|---|---|
| 1 | Index health / parity | L0 (docCount/self-query/fidelity) + **L6 Family-H idset reconciliation** (`missing_in_ha3=0`) | **Full** (lead with L6 exact idset; L0 surplus ≠ exact parity) |
| 2 | Retrieval quality | L1 recall@1/3/5/10 + MRR + nDCG@10 + CIs; `by_module/source/dept/difficulty` | **Full for retrieval**; **R@20 needs `ks` code change**; old-vs-new via two-file split |
| 3 | Top-k & rerank tuning | **`rerank_ab.py` sweep + repeated-top_k L1** (§3.4) | **Added step** (default run doesn't tune) |
| 4 | Same-source saturation | **New per-query diversity probe** (§3.4) | **Not in harness — added probe or defer** |
| 5 | Routing & filter selectivity | **New per-dept recall-delta probe** + `rerank_ab` routing (§3.4) | **Not in harness — added probe or defer** |
| 6 | Latency / throughput / capacity | L1 serial latency only; **load probe or scope-out** (§3.4) | **Latency only** by default |
| 7 | Threshold / score calibration | L2 bands + L1 dists; `recalibrate.py` (weighted) + hand-derived rerank | **Full**, with the tool caveat |
| 8 | Permission / ACL | L5 (5-doc + injection); **per-dept = standalone probe or L5 code change** (§2.4) | **Partial by default**; per-dept needs §2.4 probe |

Net: the gold rebuild + L0–L6 fully re-answers ~4.5/8 (1, 2-retrieval, 7, 6-latency, multimodal); **3/4/5/6-throughput/8-per-dept require the explicit added read-only probes above.**

---

## 5. Sequencing + gates

- **Phase 0 — safety preflight + smoke (no spend).** Branch; clean shell; **assert** `RAG_RDS_USER==fuling_ro` + `RAG_READONLY==true` + `simulate==false` (log `envboot.facts()`); `SHOW GRANTS` shows no write; grep gate (no push/deactivate/update_index_status). Smoke `run --limit 5 --layers l0,l1,l2`. **GATE 0:** asserts pass + smoke green + cost extrapolates OK.
- **Phase 1 — recon + snapshot + re-resolve + negatives.** (RECON-0 done.) R0 snapshot; R1 rebuild→`+` files; R2 resolution diff; R3 negative re-validation (incl. `J-spec_rim_od` retire); dept normalization. **GATE 1 (show+approve):** survivors/retirements/re-tags + **neg_type re-tag of ALL surviving negatives is a blocker.**
- **Phase 2 — author (verify-live, local writes).** Stratified targets + dim-2 dept_internal positives + §2.4 ACL-probe cases. `validate_gold_additions --verify-live`; calibrate judge. Merge by hand; show `git diff`. **GATE 2:** validator exit 0 + every positive's keyword_gt verbatim-confirmed + composition approved; **re-estimate judge cost if gated >130.**
- **Phase 3 — full eval + A/B + sweeps (controlled spend).** Raise L6 H `top_k`. ON arm full L0–L6; OFF arm L0/L1/L2. `rerank_ab.py` sweep + repeated-top_k L1 (dim 3); dim-4/5 probes; dim-6 load probe or out-of-scope note; standalone ACL probe (dim 8). Judge ON arm → `merge --strict`. **GATE 3:** `merge --strict` exit 0; L0 delta≥0 AND L6 Family-H `missing_in_ha3=0`; L6=GO; L2 AUC≥0.85 (≥5 off_topic); regime guard passes.
- **Phase 4 — recalibration recommendation + freeze.** Weighted via `recalibrate.py` (OFF arm); rerank hand-derived — **recommendation only.** `baseline-freeze` → new `baseline.json` (new `eval_set_sha`); **records the current prod threshold label, does NOT change any threshold.** **GATE 4 (show+approve).**

**Guardrails throughout:** no `--commit` to prod, no DDL, no push/delete/update_index_status, no DataWorks runs, no prod flag flips. Only local writes, all on-branch, all `git diff`'d.

---

## 6. Deliverables + GO/NO-GO

**Deliverables:** ✅ `coverage_gap_2026-06-23.json` (done) · rebuilt `golden_full+.json` (~360) + gated run-set (~110) + `golden_l4_serving+.json` + `.bak_pre_rebuild` · fresh `resolution_report.json` + old-vs-new diff · per-negative verdicts · two runs (ON full / OFF L0-L2) + `rerank_ab` sweep + dim-4/5/6 probes + ACL probe + reports + judge verdicts · recommended thresholds per arm (recommendation only) · new `baseline.json` (on approval).

**GO to trust the new baseline:** L6 Family-H `idset_jaccard≈1.0`/`missing_in_ha3=0`; L0 G0 delta≥0, G2≥98%/60, G3≥90%, G4 cos≥0.99; **L6 verdict=GO** (H-enum not truncated; D1-D7 JSON current); `merge --strict` exit 0 with judge ≥4.0 / source-leak≤0.05 / stdev≤1.2; regime guard passes; L2 AUC≥0.85 (≥5 off_topic); ACL: L5 PASS **and** the standalone per-dept probe passes; coverage: every major dept ≥1 scorable positive, dept_internal ≥10 authorized positives + ≥5 cross-dept denials, ≥5 live image cases.

**NO-GO:** any L0 data-loss or L6 `missing_in_ha3>0`; L6 NO_GO (incl. truncated enum); ACL public/injection leak; AUC unenforceable; judge stdev>1.2 or source-leak>0.05; any authored positive whose keyword_gt isn't verbatim-confirmed.

---

## 7. Risks & traps
1. **Safety mechanism** is the `fuling_ro` grant + SELECT-only code, **NOT** the (unarmed) `prod_access` session guard; `RAG_READONLY` holds only if no shell var shadows it → GATE-0 asserts.
2. **`build_goldset` overwrites the live gold files + ignores additions** → R0 snapshot + `+` files.
3. **L6 Family-H enum truncates at 12000 on a 27.7k corpus → L6 never GO** → raise top_k / page.
4. **Threshold↔fusion↔rerank coupling** → two aligned arms; `recalibrate.py` on OFF arm only; freeze in ON arm.
5. **`eval_set_sha` changes** → fresh baseline, not a gated delta; old-vs-new only via two-file split.
6. **Judge cost** ~30–50 sequential `claude -p` (≤900s each) → ON arm only, keep Tier B small, smoke-measure.
7. **L4-serving/L6 fail-closed** below evidence floors → ≥6 image cases + current D1-D7 JSON + raised H top_k.
8. **R@20, dims 3/4/5/6-throughput, per-dept ACL, per-dept/capability answer-quality, cohort stratum** are NOT produced by the default run → explicit added read-only probes (some need approved code changes) or explicit out-of-scope decisions — never imply coverage the run doesn't produce.
9. **dept labels** Chinese/None pollute `by_dept` → normalize before run (GATE-1 blocker).
