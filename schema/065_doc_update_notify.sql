-- 065_doc_update_notify.sql — 文档升版可见范围提醒（doc-update-notify）
-- 库：fuling_operation（交互/运营域，与 user_feedback(002)/qa_retrieved_doc(013)/
--     dingtalk_msg_dedup(051) 同域；解析在 Python 侧完成，**不写跨库 SQL JOIN**）
-- 设计稿：docs/doc_update_notify_design_2026-08-04_DRAFT.md（rev3 + §11 Sam 拍板）
--
-- ── 这两张表解决什么 ─────────────────────────────────────────────────────────
-- 文档升到新 version 并在检索侧真正生效后，要提醒「对该文档有可见权限」的人。
-- 触发是**日调状态扫描**（摄取管线零改动），受众用 acl_policy.can_read_doc 离线枚举
-- （通知面 ⊆ 检索面）。这里的两张表是该流程的 **outbox 台账**：
--   · doc_update_event  —— 每 (doc, version) 一行 = 事件身份 + 防重放水位
--   · doc_update_notice —— 每 (event, user, channel) 一行 = 投递状态机
--
-- ⚠️ **doc_update_event 是防重放台账，永不进 retention 删除、永不进归档**
--    （opensearch_pipeline/retention.py 显式豁免）：discover 的「无事件行」反连接
--    依赖行存在，清掉行 = 远古升版被重新发现并群发。年增量 ≤ 事件上限×365 ≈ 1.8 万行。
--    doc_update_notice 反之：user_id 键控个人数据 ⇒ 有留存窗 + 进 _purge_jobs 主体擦除。
--
-- ⚠️ **状态语义二分**（HOLD 与 SUPPRESSED 不可混用，评审 R2-A5）：
--    · HOLD       = 运维性拦截（权威不可达/组织快照过期/姿态未知/超上限）⇒ **可回放**
--                   （--requeue），绝不是「这篇没人可读」。
--    · SUPPRESSED = 语义性终态（内容未变/public 策略/受众为零/切换过旧/已隔离退役）。
--    把 audience_cap、bulk_guard 这类节流条件落成 SUPPRESSED 会让事件永久静音 —— 那是
--    R1 稿的缺陷，本 DDL 用 status 值域 + reason 分词把两类严格分开。
--
-- ⚠️ **notice.state 有 SENDING 认领态**（评审 R2-B B-1）：DataWorks 手动重跑与日调实例
--    重叠时，两个进程会同扫 PENDING 各自发送 —— UNIQUE 键只防插入重复、不防双发。
--    出队必须是原子认领 UPDATE（PENDING/FAILED → SENDING），SENDING 超时可回收。
--
-- 先部署后 apply 安全：读写两侧均带 1146 探测降级（表不存在 ⇒ serving 返回空列表、
-- 节点 exit 2），apply 前后行为逐字节一致（与 048/049/050/059 同款纪律）。

-- @DB fuling_operation

-- ── 1. 事件台账（每 doc×version 一行）───────────────────────────────────────
CREATE TABLE IF NOT EXISTS doc_update_event (
    id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    doc_id           VARCHAR(100) NOT NULL COMMENT '↔ fuling_knowledge.document_meta.doc_id（同口径；跨库只在 Python 侧比对）',
    version_no       INT          NOT NULL COMMENT '本次生效的新版本号（= document_meta.current_version_no 或二次谓词命中的最高已生效版本）',
    prev_version_no  INT          DEFAULT NULL COMMENT '被 supersede 的最近前版（取 canonical_sha256 非 NULL 的最新行，与 stage-1 skip-gate 同款选取）',
    content_changed  VARCHAR(16)  DEFAULT NULL COMMENT '双指纹判定：text=正文变 | assets=正文同而原始字节变（图集/格式更新，对齐 skip-gate 资产撤销语义 P1-5） | none=两者皆同 | NULL=未判定',
    status           VARCHAR(16)  NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING=待解析 | HOLD=运维性拦截(可 --requeue 回放) | RESOLVED=已解析出受众 | SUPPRESSED=语义性终态',
    reason           VARCHAR(32)  DEFAULT NULL COMMENT 'HOLD: stale_snapshot|authority_unavailable|posture_unknown|posture_mismatch|audience_cap|bulk_guard；SUPPRESSED: unchanged|missing_sha|public_policy|audience_zero|stale_switch|quarantined_or_retired|meta_missing|baseline|manual',
    audience_count   INT          DEFAULT NULL COMMENT '解析出的受众人数（观测用；上限见 RAG_DOC_NOTIFY_MAX_AUDIENCE，public 事件不受该上限约束）',
    resolved_at      DATETIME     DEFAULT NULL COMMENT '受众解析完成时刻 —— **同时是钉钉新近度锚**：回放会刷新它，故 HOLD 回放不会被 stale 规则误杀',
    created_at       DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_doc_version (doc_id, version_no),
    KEY idx_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='文档升版提醒事件台账（每 doc×version 一行；UNIQUE 即防重放水位，永不留存删除）';

-- ── 2. 投递状态机（每 事件×用户×通道 一行）─────────────────────────────────
CREATE TABLE IF NOT EXISTS doc_update_notice (
    id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    event_id    BIGINT UNSIGNED NOT NULL COMMENT '→ doc_update_event.id（同库；刻意不加 FK，与本仓其余表一致）',
    doc_id      VARCHAR(100) NOT NULL COMMENT '冗余自 event，console 读侧免 JOIN',
    user_id     VARCHAR(128) NOT NULL COMMENT '钉钉 staffId（与 qa_session_log.user_id 同口径）；⚠️ 只存 id，不存姓名（仓库为 public，与 staff_dim 同纪律）',
    channel     VARCHAR(16)  NOT NULL COMMENT 'console=站内信(行本身即通知) | dingtalk=工作通知(需投递)；**只为解析时已开启的通道建行**，子闸后开不补历史行',
    state       VARCHAR(16)  NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING=待投递(console 即未读) | SENDING=已原子认领(防双实例双发) | SENT=已投递 | READ=已读 | FAILED=投递失败待重试 | SKIPPED=放弃投递(见 reason)',
    reason      VARCHAR(32)  DEFAULT NULL COMMENT 'SKIPPED: acl_revoked(发送前复核不通过)|quarantined_or_retired|stale(落行 >3 天未发出)|attempts_exhausted',
    attempts    INT          NOT NULL DEFAULT 0 COMMENT '投递尝试次数（随原子认领一同 +1）；≥3 ⇒ SKIPPED(attempts_exhausted)+告警',
    last_error  VARCHAR(200) DEFAULT NULL COMMENT '最近一次失败的 errcode/errmsg 截断（⚠️ 不得写入文档标题）',
    claim_ts    DATETIME     DEFAULT NULL COMMENT '认领时刻；SENDING 超 30 分钟视为死认领，可被下轮回收',
    sent_at     DATETIME     DEFAULT NULL,
    read_at     DATETIME     DEFAULT NULL,
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_event_user_channel (event_id, user_id, channel),
    KEY idx_user_state (user_id, channel, state, created_at),
    KEY idx_channel_state (channel, state, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='文档升版提醒投递状态机（console 站内 / 钉钉工作通知；user_id 键控个人数据 ⇒ 有留存窗+主体擦除）';

-- ── 台账（F-35 纪律）─────────────────────────────────────────────────────────
-- ⚠️ 不要手写 INSERT INTO schema_migrations —— 一律走：
--     python3 scripts/apply_migration.py --file schema/065_doc_update_notify.sql \
--         --target {staging|prod} --ack <当日 PROD-RW 令牌>
-- （checksum 由脚本计算写入，032 起的「同名异 checksum」漂移检测依赖它）。
