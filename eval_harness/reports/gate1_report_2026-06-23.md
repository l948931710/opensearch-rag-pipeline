# GATE 1 — Re-resolution, negative re-validation, dept normalization (read-only)

Branch `eval/goldset-rebuild` · 2026-06-23 · all read-only (fuling_ro) + local writes only.

## A. Survivors (positives)
- **225 positive cases / 120 distinct expected docs.** Re-resolved directly against the live inventory (build_goldset is **not re-runnable** — 2 of 3 raw sources missing: `text_eval_queries.json` + the multimodal/registry JSONs; only `scratch/xlsx_goldset_raw.json` survives).
- **120/120 pinned expected docs still active + served.** **0 missing, 0 permission-drift.** No positive lost its target → all 225 positives carry forward (subject to dept normalization below).

## B. Changed resolutions / twin hazard
- **0 positives re-resolved to a different doc** (IDs are pinned; the fuzzy resolver is not being re-run).
- **R2 twin-hazard scan (fuzzy title ≥0.6):** 58 expected docs have a similar-title active sibling (mostly benign old SOP pairs). **Only 2 have a *campaign* twin** — notably `《员工离职手续》作业指导书` (DOC_HR_…E3CD28) ↔ campaign `《人员离职退保手续》作业指导书` (0.857, dept_internal). **Latent risk only** — would matter only if the fuzzy builder were ever re-run (it can't be now). Flagged for the authoring guide.

## C. Negatives — re-validated (adversarial, evidence-cited)
**8 RETIRED** (now answerable — corpus growth added the assumed-absent fact; each CONFIRMED by an independent skeptic with verbatim chunk evidence):

| qid | now-answerable evidence (live active chunk) |
|---|---|
| J-spec_rim_od | `*杯口外径 89.3±0.2` — DOC_PRODUCTION_…58669A (知兰规格书, campaign 06-22) |
| J-spec_migration | `总迁移量(4%乙酸,70℃,2h)≤10` — DOC_PRODUCTION_…2E81D6 (8OZ包材规格书) |
| J-sop_reject | `不合格品...按《不合格品处理流程》执行...停机隔离` — FL-QC-015-036 / QU0P001-01 |
| J-qc_sticker_pwd | `要求打印电脑设置密码，每三个月更改一次密码` — FL-QC-015-016 |
| J-qc_change_depts | `通知生产、采购、PMC、品质部、仓库等相关部门` — FL-QC-015-016 |
| J-manual_docno | `文控编号: UF_U8130_FLCW_18_04` — DOC_IT_…C6FD16 (now in body → metadata) |
| J-xsop_corrosive | `有腐蚀性的样品不得直接接触天平托盘...` — FL-QC-005-001-3 电子天平操作规程 |
| J-xsop_clean | `清洁 干抹布...每天日班工作前 当班巡检` — FL-QC-005-001-3 |

**18 KEPT:**
- **15 boundary-refusal (RAG-46..60)** — test refusal-on-policy, still valid. Several now have matching content (RAG-49 绩效, RAG-58 药品清单, RAG-59 固定资产) → the refusal path is now exercised against **real** content (a stronger test). `still_fires_refusal=true` for all.
- **3 J-* genuinely still absent:** J-psop_neg_gloves (防护手套**型号** absent), J-itin_neg_antistatic (防静电手环**品牌型号** absent), J-visit_neg_fee (留宿**固定**费用 absent — only "按市场价").
- **0 rescoped.**

## D. neg_type for all 26 (assigned)
- **near_miss_answer_absent ×18**, **live_data ×4** (RAG-51/52/53/60), **modality_gap ×3** (RAG-54/55/56), **metadata ×1** (J-manual_docno).
- **refusal_class (boundary axis):** sensitive_pii ×6 (RAG-46/47/48/49/50/57), realtime ×4, image_request ×3, table_dump ×2 (RAG-58/59).
- **⚠️ Taxonomy gap:** the 5-type neg_type taxonomy has **no `sensitive_pii` type** — 6 PII-refusals + 2 table-dumps are shoehorned into `near_miss_answer_absent` + a `refusal_class` tag. **Recommendation:** add a `refusal_class` field (or a `sensitive_pii`/`boundary_refusal` neg_type) so PII-refusal scoring is explicit. (Surfaced for Gate-2 decision.)

## E. Normalized dept distribution (251 cases)
- **Raw** `{人力资源部 86, None 51, 行政部 45, 财务部 43, 边界控制 15, 跨部门 11}`
- **Normalized** `{hr 101, admin 50, finance 43, None 23, boundary 15, it 12, production 7}` (228 cases re-tagged to English `DEPTS` codes from inventory `owner_dept`; the 23 `None` are negatives without an expected doc + cross-dept).
- Confirms the gold is **heavily skewed to the old public HR/admin/finance corpus** — the entire dept_internal majority + production/marketing/rd are unrepresented (→ Phase-2 authoring targets).

## F. Net effect on the negative set (drives Phase 2)
After retiring 8: **golden_full negatives = 18** (15 boundary + 3 J-*). **0 `off_topic` in golden_full** (the S5-off-* off_topic cases live only in the gated golden_50). → Phase 2 **must author ≥8 off_topic** (L2 AUC gate needs ≥5; headroom target 8) + new near_miss/metadata to replace retired traps.
