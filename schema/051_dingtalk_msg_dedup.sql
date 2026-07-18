-- 051_dingtalk_msg_dedup.sql — B7-P2-04（Sam 拍板 2026-07-18）：钉钉 msgId 幂等 RDS 兜底
--
-- 为什么：消息去重主层（memory/redis）均 fail-open——Redis 故障窗口内重投会被放行
--   成重复回答（已知残余风险）。本表以 msg_id 唯一键做第二层：两层独立故障域，
--   仅同时故障才可能漏重。同时充当四态机（processing/sent/retryable_failed/
--   final_failed/delivery_unknown）的持久面，DELIVERY_UNKNOWN 对账靠它。
-- 消费方：dingtalk_bot._rds_dedup_claim/_rds_dedup_mark（kill switch
--   RAG_MSG_DEDUP_RDS_FALLBACK 默认 on；表缺失 1146 → 负缓存 1h 优雅降级，
--   先部署后 apply 安全）。行按 updated_at 判新鲜（去重窗+60s），陈旧行复用；
--   历史行清理归 retention 后续作业（P3，量级=消息量级、行极窄）。
-- 目标库：fuling_operation（运营事件面，与 qa_session_log 同库）。

CREATE TABLE IF NOT EXISTS dingtalk_msg_dedup (
  msg_id      VARCHAR(128) NOT NULL PRIMARY KEY COMMENT '钉钉 msgId（回调幂等键）',
  state       VARCHAR(24)  NOT NULL DEFAULT 'processing'
              COMMENT 'processing|sent|retryable_failed|final_failed|delivery_unknown',
  attempts    INT NOT NULL DEFAULT 1 COMMENT '处理尝试数（RAG_MSG_MAX_ATTEMPTS 封顶）',
  message_id  VARCHAR(64) DEFAULT NULL COMMENT '内部 message_id（重投复用答案的读回锚点）',
  created_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  updated_at  DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
              ON UPDATE CURRENT_TIMESTAMP(3),
  KEY idx_msg_dedup_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='B7-P2-04 钉钉消息幂等兜底（主层 memory/redis fail-open 的第二层）';
