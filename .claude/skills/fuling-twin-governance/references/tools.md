# 工具契约与用法 — retire_twins_batch / coverage / image_audit / seed

工具都在 `scratch/`（gitignore，本地资产）。生产访问一律经 `opensearch_pipeline/
prod_access.py`（只读会话强制 READ ONLY；RW 需当日 `PROD_RW_ACK=PROD-RW:<date>`），
**严禁手拼 .env.production**。

## retire_twins_batch.py

```bash
python scratch/retire_twins_batch.py scan                      # 只读盘点 → manifest+审核表+金集标记
python scratch/retire_twins_batch.py execute --manifest M      # 预览（零写入）
PROD_RW_ACK=PROD-RW:$(date +%F) python ... execute --manifest M --commit
```

- **scan**：active 文档按 raw_key basename stem 分组（≥2 doc_id 成组；title 有
  `.pd/.do` 截断脏数据不可用）；聚合指标（chunks/带图数/CHAR_LENGTH 文本量/
  extract 信息）；金集 expected_doc_ids 交叉标 gold_hit。⚠️ scan 会**重写 manifest**
  ——已有人工裁决/coverage 字段的 manifest 不要重跑 scan 覆盖。
- **execute 预检**（全局）：keep∩retire=∅、keep∈members、retire⊆members、
  成员 stem 从 RDS 现查重断言、review 组无 `--force-reviewed` 拒绝。
  （每组）：keep 侧 RDS `is_active=1 AND index_status='INDEXED'` 计数 >0 **且**
  HA3 按 doc_id 过滤实查命中 ≥1（防 G20 裂脑）。
- **执行**：每 retire 成员单事务（chunk_meta `is_active=0,index_status='DELETED'`
  + version/meta `inactive`，影响行数≠预取 id 数即回滚）→ HA3 `cmd=delete` 按
  chunk id 批 100（幂等容忍 not_found）。
- **journal**（`twin_retire_journal_<manifest名>.json`）：逐组逐成员落盘
  pending/done/skipped_already_retired/failed + chunk_ids；重跑同命令自动跳过
  done；"RDS 已提交但 HA3 失败"的成员重跑只补 HA3 删（already_retired 分支会回查
  `is_active=0 AND index_status='DELETED'` 的 id 幂等重推删除）。
- **manifest 字段**：组 = `{stem, members[{doc_id,dept,ext,chunks,img_chunks,
  text_len}], keep, retire[], needs_human_review, review_reason, gold_hit,
  cross_dept, coverage{<retire_id>: {cov/fz 四向, superset_holds, ...}}}`。
  翻转 = 交换 keep↔retire（members 不动），必须同时写 review_reason 留痕。

## twin_content_coverage.py

读 manifest 全组 → 双向 cov/fz → 回写 manifest coverage 字段 + 输出 MD 报告
（含逐组"退役将丢"明细）。幂等可重跑（翻转后复验用）。度量陷阱见
`coverage-metrics.md`。

## twin_image_audit.py

`--download` 时把"keep 侧无对应"的 retire 图下载到 `/tmp/twin_img_audit/<stem>/`
（R_ 前缀）+ keep 全量图（K_ 前缀）做对照。匹配 = OCR 8-gram 包含为主、
visual_summary 为辅，阈值 0.55。**score 低 ≠ 真独有**：照片/裁切差导致大量假阴，
用 PIL 拼 contact-sheet 后逐组目检才是终审。目标过滤基于 manifest 的 coverage
字段（superset_holds ∧ retire_imgs>keep_imgs）。

## 单文档/小批回灌 seed（仿 seed_round_20260612.py）

铁律：**先部署含修复的 zip 再 seed**（旧代码回灌只是白耗版本号）。
- 候选 = latest active 版本 ∧ `content_process_status='DONE'` ∧ 非隔离路径；
  候选数 ≠ 期望数即中止。
- seed 前断言 stage1 队列（NOT_STARTED ∧ canonical 为空）= 0，防混批。
- INSERT 新版本行：`raw_key_hash = SHA2(CONCAT(raw_key,'#v',n+1),256)` 满足
  uk_raw_key_hash；状态 NOT_STARTED；零停机（旧版本 chunk 保持 active，DAG3
  索引新版本后才停用）。
- 之后用户在 DataStudio 按序跑 `opensearch_stage1_canonicalize1` →
  `opensearch_stage2_safe_chunk` → **`清理stage3`**（勿用坏的
  opensearch_stage3_push_index）。stage1 完成形态 = canonical 落位 + 状态回
  NOT_STARTED（DONE 是 stage2 设的），别误判失败。

## 退役后双侧验证（每批必做）

对 journal 中本批 done 的每个 retire 成员断言三条：
`RDS is_active=1 计数 = 0` ∧ `HA3 doc 命中 = 0` ∧ `keep 侧 HA3 命中 > 0`。
任何一条不满足按 journal 续跑路径处理，不得手工补 SQL。
