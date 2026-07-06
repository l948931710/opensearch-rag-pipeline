# HA3 已索引文档"蒸发"事件取证（2026-07-06）——工单素材

> 结论先行：**已两次验证入索引的文档在无任何删除操作的情况下从可检索索引中消失，
> 而引擎 docCount 仍计数它们**。这是引擎侧（疑似段合并/实时段持久化）问题的签名，
> 非客户端推送失败。建议提交阿里工单（先例：`ha3-dense-fix-support-ticket.md`）。

## 实例信息

- 实例：`ha-cn-kgl4slr1n01`（公网 endpoint `.public.ha.aliyuncs.com`，HTTP/80）
- 表：`fuling_kb_chunks`（Swift 实时推送；`autoBuildIndex:false`，无离线全量源）
- 对账基准：RDS `fuling_knowledge.chunk_meta`（is_active=1 = 应在索引集）

## 时间线（2026-07-06，北京时间）

| 时刻 | 事件 | 证据 |
|---|---|---|
| ~03:40 | release-gate L6 全表对账：**missing=64**（6-21/22 批文档，RDS 标 INDEXED） | gate run1 report |
| 09:0x | 复位 64 → NOT_INDEXED（`rebuild_from_rds --commit`） | scratch parity.json |
| ~09:15 | 本地 stage-3 重推 61+3，**[04b] parity verify 通过**（推后逐一校验在库） | s3_run3.log |
| 09:32 | 全表对账：**RDS 27659 == HA3 27659，missing=0，complete=true** | reconcile 输出 |
| 09:32–11:30 | **无任何写/删操作**（无 purge、无 ha3_reconcile --commit、deactivate 全 no-op、RDS 零新建行） | RDS created_at 审计 |
| ~11:30 | gate run2 L6 对账：**missing=69**（又全是 6-21/22 批；与早上 64 仅 1 doc 交叠、6 chunk 交叠） | gate run2 report |
| ~12:10 | **点查（filter `id=<pk>`，唯一可靠单读）确认 69 个样本 5/5 不在** | 本文附录 A |
| ~12:15 | 复测对账：missing 稳定=69（不增长，一次性丢失非持续蒸发）；早上治愈的 64 抽样 5/5 仍在 | parity_now.json |
| ~12:15 | **引擎 stats：docCount=27659（与 RDS 一致！）**，partitions 13840+13819，segmentCount 3+3 | status_and_stats |

## 关键矛盾（工单核心）

1. `GetTable/stats` docCount = **27659**，但按主键枚举/点查只能找到 **27590**；
   差额 69 个文档 doc store 计数存在、可检索索引（含 id 属性过滤）查不到。
2. 其中 **6 个文档是当天 09:15 推送、[04b] 推后校验 + 09:32 全表对账两次确认在库**的
   —— 之后在无删除操作窗口内消失。
3. 消失文档**全部来自 2026-06-21/22 的同一大批灌入**（~18k chunks 批）；
   更早（5 月）与更晚的文档零丢失。该批在 6-22 也发生过 96 个同签名丢失（当时修复）。
   合理怀疑：该批所在实时段在段合并中被部分丢弃。

## 已排除

- 客户端删除：窗口内零删除调用（代码审计 + 操作日志）
- 推送失败：6 个受害文档有推后 parity verify 通过记录
- RDS 侧漂移：行数/状态零变化
- 枚举欠采（G30）：点查（`id=<pk>` 过滤）逐个确认缺失，非扫描噪声

## 我方缓解（已做/在做）

- `rebuild_from_rds --commit` + 本地 stage-3 重推（当天两轮：64 治愈存活、69 在治）
- stage-3 [04b] 推后 parity-verify + G9 重试预算/死信（已合 main，待 DataWorks 重打包）
- 待办：周期性 reconcile→重推的自动闭环（复用 PAUSED 的 `ops_health_monitor` 节点）

## 给阿里的问题

1. 该实例 07-06 09:30–11:30 之间是否有段合并/build/回收动作？其日志能否确认丢弃了实时段文档？
2. docCount 与可检索文档数不一致的机理？如何在客户端检测（不靠全表枚举）？
3. Swift 实时表在无离线全量源配置下，段合并对"仅存在于实时段"的文档的持久性保证是什么？
4. 2026-06-21/22 批（约 18k 文档）是否落在某个有问题的 generation？能否引擎侧修复而非客户端反复重推？

## 附录 A：点查样本（12:10）

```
DOC_MARKETING_20260622132045_675207_v1_c0074_05388503  pk=69211  absent
DOC_MARKETING_20260622132045_675207_v1_c0124_1BEE1BA8  pk=69261  absent
DOC_PRODUCTION_20260621121629_E673F4_v1_c0008_7DBC63B7 pk=30570  absent
DOC_PRODUCTION_20260621124624_D0CD68_v1_c0006_28D724A0 pk=30892  absent
DOC_PRODUCTION_20260621133134_A23BB9_v1_c0010_BDAB98F9 pk=31633  absent
```

完整 69 清单：会话 scratchpad `missing_now.json`（69 chunk / 55+ docs，均 6-21/22 批）。
