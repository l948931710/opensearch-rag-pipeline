-- ════════════════════════════════════════════════════════════════════════════
-- 062_acl_projection_epoch.sql — C3/C3′ ACL 投影收敛：投影失效代次 + 投影水位
--
-- @DB fuling_knowledge（staging 为 fuling_knowledge_stg）
--
-- 拍板单=docs/ops/c3prime_acl_projection_convergence_signoff_2026-08-03.md
-- 设计来源=docs/acl_retention_design_patterns_2026-08-03.md §1.2 P1
--   （在 remote 分支 claude/code-review-ultra-console-74ccdf@c92fed6，未合 main）
-- codex 评审 2026-08-03（第二批第 1 轮 REVISE → 本文件为修订版 v2）
--
-- ✅ **2026-08-03 已 apply staging + 生产**（Sam 当日授权 PROD-RW:2026-08-03）。
-- ✅ **第 1 步写方已落码**（commit 72c9e22）：bump 单一入口（不受 flag 门控）+ 7 个权威写点
--    + 三处 stamp（含 stage-2 chunk INSERT，capability 探测降级）+ projection_complete + 姿态 A。
--    ⇒ **epoch 已在正常积累**。
-- ⚠️ **但仍无消费方**：全版本 sweep 与 C3′ 多版本 materializer **未实施**（三条 blocker 已立项，
--    见拍板单 §5.5）。对 serving/HA3 行为逐字节不变。
-- 🔴 **已知潜伏缺口**（拍板单 §5.5-B2）：materialize 在投影值相等时于 stamp **之前**就
--    `return unchanged` ⇒「值正确但 acl_epoch IS NULL」的文档永远盖不上章。今天不咬人
--    （生产零 active chunk），**开 sweep 前必须先修**。
--
-- ── 为何需要（C3/C3′ 的机制根）─────────────────────────────────────────────
-- allowed_depts_reconcile._prescreen_unchanged 用 **diff** 判"无漂移"：want(权威) vs
--   have(投影)。而 diff 在"从未投影过"时【恒为空】——want=[] 、have=[] ⇒ 判 unchanged ⇒
--   materialize 永不执行 ⇒ 越权窗口从过渡态变【永久态】。这不是实现 bug，是**用 diff 表达
--   "已处理"这件事本身的结构性缺陷**：Kendra(OrderingId + ListGroupsOlderThanOrderingId)、
--   Coveo(NOT_UPDATED 独立态)、Azure(既有 indexer 启用 ACL ⇒ 自动检测=否，必须 /resync
--   permissions 做 backfill)、Elastic(缺时间戳即重跑)、OpenText(读写共用同一 token 函数)
--   —— 五个互相独立的系统，**没有一个靠 diff 推断"从未处理过"**。
--
-- 泄露方向（2026-08-03 核验，纠正研究文档 §1.5 的判断）：
--   `chunk_meta.allowed_depts IS NULL` 在检索侧【确实 fail-closed】(retriever
--   _build_permission_filter 是正向匹配，无值即不召回，非 Elastic/Kendra 的"无 ACL 即公开")。
--   **但泄露不来自 NULL，来自没翻的 owner 轴**：未投影的 node 文档 chunk_meta.owner_dept
--   仍是旧真实 owner ⇒ 过滤器的 (permission_level='dept_internal' AND owner_dept=旧owner)
--   分支照常命中；正确投影后那里本该是 acl_policy.NODE_OWNER_SENTINEL。
--   ⇒ 可用性(新授权部门搜不到) 与 机密性(旧 owner 组仍可读) **两个后果同时成立**。
--
-- ── 设计说明：epoch 放 document_meta（**与研究文档原文的刻意偏离**）───────────
-- 研究文档 P1 写的是"kb_doc_node_grant / kb_access_request 加 acl_epoch"，此处改为
--   **document_meta 单一投影失效代次**：①单一文档水位，避免谓词按 acl_mode 分叉——那正是 C3
--   「两份判定逻辑漂移」的根因，不该在修它的迁移里重造一遍；②chunk 侧水位逐版本，为 C3′
--   的多版本 writer **提供表达能力**（见下方 ⚠️：只是表达能力，不等于解决）。
--   ⚠️ codex 校准：原稿理由"两表 MAX 聚合必然 N+1"**不严格成立**（可用两条批量 GROUP BY），
--     真实理由只有 ①②；成本/复杂度更高是次要论据，不是硬约束。
--
-- ⚠️ **不复用 document_meta.acl_revision**（060 建、061 扩注释）：那是**管理面编辑 CAS**，
--   doc-meta 改 title/category 也 +1（061 明确要求）⇒ 与 ACL 无关的编辑会触发全量重投影；
--   反之 access_request 批准不走管理面、不会 +1 ⇒ 该漏的照漏。两者语义正交，必须分列。
--
-- ⚠️ **不用 updated_at 派生水位**（codex 2026-08-03 否决，已核验）：三张表时间戳均秒级
--   （001:86 / 008:31 / 060:81），同秒内"投影→再改权威"产生相同 token；删最新行还会让
--   MAX 倒退；且 document_meta.updated_at 被 category/summary/title 等非 ACL 写刷新
--   （pipeline_nodes.py:2620）⇒ 持续假 dirty。派生只适合低频全扫 backstop，且应派生
--   **规范化 ACL fingerprint** 而非墙钟，不适合热路径候选筛选。
--
-- ── 两列语义 ────────────────────────────────────────────────────────────────
--   document_meta.acl_epoch  【投影失效代次】（单调只增）—— 不是完整 ACL 权威版本
--   chunk_meta.acl_epoch     投影水位（NULL = 从未投影过）
--
--   dirty 谓词：  cm.acl_epoch IS NULL  OR  cm.acl_epoch < dm.acl_epoch
--
-- ⚠️ **epoch 只证明"RDS 投影已按当时权威算过"，不证明 HA3 已发布**——后者由
--    chunk_meta.index_status 承载，两个阶段不可混淆（codex MISSING EVIDENCE 要求明确）。
--
-- ⚠️ **范围严格 = project_doc_acl 的产出二元组 (owner_dept, allowed_depts)**（codex 2026-08-03
--    BLOCKER）：`permission_level` **不在 epoch 的【认证输出】范围内**。理由——投影器只产出该二元组
--    （acl_policy.py:201）；materializer 的 legacy 分支把 **每版本 chunk_meta.permission_level**
--    当 gate 输入用（access_grants.py:424），从不比较也不修复 document_meta.permission_level；
--    而控制台 set-visibility 只改当前版本的 chunk 权限（kb_console.py:3122）。
--    ⇒ 若把 permission 纳入 epoch：certify 可能在 permission 仍分叉时盖同一 epoch；反之若让
--    certify 按 dm.permission 修 chunk，就等于**抢先替 C9 做出"RDS 赢"的产品裁决**。
--    permission 的同步归 C9 决策单，本迁移不碰。
--    ⚠️⚠️ **但"不认证"≠"不 bump"**（codex 第 3 轮 BLOCKER）：materializer 先按**每版本**
--    permission 做 gate 再产出 allowed_depts（access_grants.py:424）⇒ 有效 permission 由
--    public 变 dept_internal 时，期望的 allowed_depts 会从空变成 approved groups。而
--    set-visibility 的 ACL 重投影**受 flag 门控**（kb_console.py:3160）⇒ flag 关时改级别
--    只动 chunk permission、不投影；此时若不 bump，开 flag 后 epoch 相等、sweep 跳过，
--    **已批准的跨部门授权永不投影**。⇒ **有效每版本 permission 变化必须 bump**（这不是让
--    RDS permission 赢，只是让二元投影重新计算）。
--
-- ⚠️ **epoch 覆盖不了"投影语义本身变了"**（codex BLOCKER）：`resolve_allowed_depts` 读运行时
--    组码白名单（access_grants.py:75）、`project_doc_acl` 的哨兵/规范化/编码规则也可能改——
--    这类变化**不触发任何 DB writer**，epoch 不动而 dirty 谓词照判 clean（trigger 同样抓不到）。
--    ⇒ 必须配一条**完全绕过 epoch 候选集**的低频全量【语义 fingerprint 审计】：发现
--    "epoch 相等但 fingerprint 不等"时至少 bump+enqueue 或产生阻断告警，**不得只记日志**；
--    投影规则变更须配套全局 invalidate（或另引入 policy version）。
--
-- ── v2 相对 v1 的三处删改（codex 第 1 轮 REVISE 后）────────────────────────
-- ❌ **删除 chunk_meta.acl_state**：三态全部可由 epoch 推导（NULL=未投影 / < =落后 / = =最新），
--    且它**不是独立防线**——若将来新增投影轴而忘记 bump，state 同样不会自行改变。处理生命周期
--    已由 outbox(attempts/last_error/generation) 与 chunk index_status 承载。若将来要加回，
--    必须先定义：UP_TO_DATE 指 RDS 投影完成还是 HA3 发布完成、谁在何时写各态、PROCESSING/失败
--    如何表达、以及合法组合的 CHECK 约束——否则 epoch 与 state 会互相矛盾并造成永久 dirty。
-- ❌ **删除 idx_acl_projection_state**：原索引主依赖低选择性的 is_active 与 `<> 'UP_TO_DATE'`，
--    5 行 scratch 验证不了收益。**待生产规模 EXPLAIN 后单独取号补建**（不与本迁移绑定）。
-- ✏️ **删除 v1 的"顺带覆盖 C3′ 版本轴"表述**：schema 只提供逐版本**表达能力**，不改变
--    current-only 的 materializer（access_grants.py:382 只解析 current、416 只更新该版本）。
--
-- ── 上线次序（硬约束，codex BLOCKER 3）────────────────────────────────────
-- 1) 本 DDL 可提前 apply（纯 additive，旧代码不读不写，行为逐字节不变）。
-- 2) 再部署**写方**：权威变更 bump + **所有投影创建者 stamp**（含 stage-2 的
--    node_write_chunk_meta INSERT —— 否则每篇新摄取文档的 chunk 天生 acl_epoch=NULL、
--    永久 dirty，sweep 对新文档永不收敛）。
-- 3) **跨所有 active 版本的 dirty sweep 开关，必须与 C3′ 多版本 materializer 同批启用。**
--    若必须先观测，只能把 sweep 明确限定 current version，且**不得宣称已覆盖 C3′**。
--    否则旧 active v1 会每轮进 targets、每轮写不到、每轮占预算（且 outbox 对非
--    skipped_locked 仍标 done，access_grants.py:556）。
-- ⚠️ bump **不得受 RAG_ALLOWED_DEPTS_ACL 门控**：flag 关闭期间发生的权威变化若不 bump，
--    水位永久丢失，开 flag 后这批文档永远判 unchanged。
--
-- ADDITIVE：两处加列，information_schema 守卫幂等。
-- ⚠️ 幂等加列用【内联 SET @+PREPARE】(同 020/060)，**不用** CALL _add_column_if_not_exists
--   —— 那两个 helper 建在 fuling_operation，knowledge 库(含 _stg)里不存在，CALL 直接 1305。
-- ⚠️ **必须 apply-before-enable**：消费侧谓词 JOIN 这两列，缺列 1054 直接报错而非降级
--    （与 048 flag 默认关⇒可先部署 不同）。消费侧应挂 flag（建议 RAG_ACL_EPOCH_SWEEP，默认关）。
-- ════════════════════════════════════════════════════════════════════════════

-- ── 1. 投影失效代次：document_meta.acl_epoch ────────────────────────────────────
SET @c = (SELECT COUNT(*) FROM information_schema.COLUMNS
          WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='document_meta' AND COLUMN_NAME='acl_epoch');
SET @ddl = IF(@c = 0,
    "ALTER TABLE document_meta ADD COLUMN acl_epoch BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '【投影失效代次】：任何改变本文档【有效投影输入】的写须同事务 +1——acl_mode、legacy owner_dept、node grant 生效集(授/撤/scope/dept)、access_request 的 approved 态与 requester_depts。⚠️ 【认证输出】严格限定为 project_doc_acl 的产出 (owner_dept, allowed_depts);permission_level 不在认证范围内(其同步归 C9),epoch 相等【不】证明 permission 已同步。**但有效的每版本 permission 是 allowed_depts 的 gate 输入,其变化必须 bump**——否则 flag 关时 public→dept_internal 不 bump,开 flag 后 epoch 相等、approved 授权永不投影。⚠️ pending 态 INSERT 不改变有效授权,不 bump。与 acl_revision 正交(后者是管理面编辑 CAS,含 title/category),绝不可互相复用'",
    'SELECT 1');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ── 2. 投影水位：chunk_meta.acl_epoch（NULL = 从未投影过）──────────────────
SET @c = (SELECT COUNT(*) FROM information_schema.COLUMNS
          WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='chunk_meta' AND COLUMN_NAME='acl_epoch');
SET @ddl = IF(@c = 0,
    "ALTER TABLE chunk_meta ADD COLUMN acl_epoch BIGINT UNSIGNED DEFAULT NULL COMMENT '投影水位：投影创建者(materialize / stage-2 node_write_chunk_meta)落投影时 stamp 其所依据的 document_meta.acl_epoch。NULL=从未投影过(存量回填即此值,刻意 fail-closed)——这是 diff 口径永远看不见、C3 越权永久态的那一类。⚠️ 只证明 RDS 投影已按当时权威算过,【不】证明 HA3 已发布(那是 index_status)'",
    'SELECT 1');
PREPARE s FROM @ddl; EXECUTE s; DEALLOCATE PREPARE s;

-- ── 3. 台账（F-35 纪律：schema 文件 + schema_migrations 一行，缺一即事故预备役）──
-- ⚠️ checksum 由 apply_migration.py 计算写入（032 起的同名异 checksum 漂移检测依赖它）；
--    **不要手写 INSERT**，一律走 `python scripts/apply_migration.py schema/062_acl_projection_epoch.sql`。
