# Coverage-gap investigation — registered docs vs the rebuilt HA3 index

**Date:** 2026-06-07 · **Index:** `fuling_kb_chunks` · **Method:** read-only RDS (`document_meta` ⋈ `chunk_meta`) · **Manifest:** `coverage_gap_manifest.json`

> **Correction note:** an earlier draft of this report concluded "258 docs (39%) registered but unsearchable." That was an artifact of **exact-title matching**. After normalized-title matching (stripping extensions, `00N`/`A##` prefixes, punctuation) the real picture is very different — see below. Per the doc owner: the old cohort is **legacy-format data already converted**, and the remaining items **await the permission/ACL system before import.**

## Corrected headline

`document_meta` has 654 rows; 396 have active chunks. Of the 258 "registered-but-no-active-chunks" rows (256 excluding 2 `~$` Office temp files):

| Bucket | Count | Meaning |
|---|---|---|
| **Superseded legacy format** | **249** | old `.doc`/un-normalized registrations whose **converted twin IS indexed** (normalized-title sim ≥ 0.6, e.g. `车辆进出管理规定.doc` → indexed `A41车辆进出管理规定`). **Content is searchable.** |
| Weak match (0.4–0.6) | 4 | likely converted twins with title drift; spot-check |
| **Genuinely absent** | **~7** | no indexed presence in any form (confirmed by title probes) |
| `~$` temp/lock files | 2 | garbage, should be deleted |

So there is **no 39% content gap.** The 2026-05-12 cohort is the **pre-conversion legacy load**; the 2026-05-13 cohort is the **converted, indexed version**. The stale 05-12 rows are RDS hygiene (they inflate `document_meta` counts), not missing knowledge.

## The ~7 genuinely-absent docs (confirmed: zero indexed presence)

| Doc | Dept | Note |
|---|---|---|
| `A53动火安全管理制度` | hr | 0 indexed titles contain "动火" — matches eval miss RAG-18 |
| `A37-7油库泄、着火` | hr | 0 indexed "油库" |
| `《员工路费补贴》作业指导书` | hr | 0 indexed "路费" |
| `注塑修模具流程.pptx` | production | 0 indexed "修模" |
| `FL-QC-015-035 淋膜产品检验作业指导书` | production | indexed has FL-QC-015-**033/034** (纸杯) but not 035 (淋膜) |
| `FL-QC-015-036 印刷产品检验作业指导书` | production | not indexed (035/036/037 absent) |
| `FL-QC-015-037 模切产品检验作业指导书` | production | not indexed |

Small, specific production-inspection SOPs + a few HR safety/welfare docs. The eval already surfaced the relevant ones as honest retrieval misses (e.g. RAG-18 动火).

## Finance `CWD-*` 制度 docs — awaiting the permission system (intentional)

Not a pipeline failure: the finance **policy/control 制度** docs (`CWD-003…019`, 印花税) are **not in `document_meta` at all**, and there is no `财务`/`finance` `owner_dept` (only hr/production/admin/it). Per the doc owner, these (and other restricted docs) **need the permission/ACL system completed before import** — consistent with the fact that **all 396 indexed docs are currently `permission_level=public`** (dept-restricted access isn't wired yet, so L5 permission was N/A in the eval). The finance **operations** content (U8财务手册 211 chunks, 发票 SOPs) is already indexed (tagged `it`).

## Revised remediation (much smaller than first thought)

1. **RDS hygiene (low priority):** mark/clear the ~249 superseded legacy rows + 2 `~$` temp rows so `document_meta` reflects reality (654 → ~403). Avoids future "missing doc" false alarms. **Do NOT reprocess them** — that would create duplicates of already-indexed converted content.
2. **Re-ingest the ~7 genuinely-absent docs** (if in scope): add/convert their sources → DAG1→3. These are the only real content additions.
3. **Finance / restricted 制度 docs:** gated on the **permission/ACL system** — import after dept-permission is wired (set `owner_dept=finance`, appropriate `permission_level`). Planned, not a defect.

**Net:** the rebuilt index's coverage is essentially complete for what's currently in-scope (public, converted corpus). The eval's `live_scorable` exclusion correctly handled the not-yet-imported docs, so the 0.93 recall@5 stands.
