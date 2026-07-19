-- 052_agent_run_dispatch_bind.sql — command→run 反向锚（R1 RB-RT-01，2026-07-18）
-- 库：fuling_operation
--
-- 背景（Agent Runtime 外审 ARR-20260718 RB-RT-01）：durable dispatch 的交接是
-- claim → executor.submit（create_run+起线程）→ bind_and_done 三步——bind 写库失败
-- 时原 run 已在跑、命令仍「claimed 未绑」，租约到期被恢复扫描重驱成**第二个 run**
-- （重复回答/重复计费/重复副作用；探针实证 original run 存活 + redriven=1）。
--
-- 修复：把绑定从「命令行的正向指针」升级为「run 行的反向锚」——create_run 的 INSERT
-- 同事务落 dispatch_command_id，UNIQUE 约束=DB 裁决的每命令至多一个 run：
--   · 恢复扫描先按锚查既有 run（find_run_by_dispatch_command）→ 有则 rebind 收口，
--     绝不新建第二个 run；
--   · 并发竞态兜底：两方同时未查到、双双 INSERT → 第二个撞 uk 1062 →
--     DispatchCommandBound → 取既有 run 返回（单赢家协议，同 P2-04b rowcount 纪律）。
-- 非 durable-dispatch 的 run 落 NULL（MySQL UNIQUE 允许多 NULL，零影响）。
--
-- 部署顺序：**代码可先行**（create_run 对 1054 未知列容错回退旧 INSERT + 一次性
-- warning；恢复查询 1054 → 回退旧重驱语义）。RAG_AGENT_DURABLE_DISPATCH 开启前
-- 必须 apply（readiness durable-dispatch 契约探针含本列，未 apply 即 critical）。

ALTER TABLE agent_run
  ADD COLUMN dispatch_command_id CHAR(32) DEFAULT NULL
    COMMENT 'durable dispatch 命令锚（052）：建 run 事务内原子落，UNIQUE=每命令至多一 run',
  ADD UNIQUE KEY uk_run_dispatch_cmd (dispatch_command_id);
