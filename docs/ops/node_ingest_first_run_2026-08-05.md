# node 摄取链路首次生产运行验证 · 2026-08-05

> 对象:`DOC_01KZ89ZJCPNHFS9Q2F2ETB790T`(D_人力_制度_…杭州分公司考勤制度及规则,7 页 PDF)
> —— 全库**唯一**活文档,也是 node-ACL 上线以来第一篇走完全链路的 node 文档
> (此前 acl_mode 分布:legacy 1938 / node 0)。
>
> 背景:Sam 2026-08-05 定方向「所有文档重灌、路径按组织树、跨部门可见由部门管理员自选」。
> 退役的 1562 篇不迁移,新语料从零开始全 node ⇒ 本次首跑是该方向的**可行性验证**。
> 全程 prod 只读核查(`prod_access` / `RAG_ENV=prod_ro`),零生产写。

## 1. 结论

**通过。** 上传登记 → stage1 抽取 → stage2 分块 → stage3 索引 → HA3 投影 → ACL 检索,
六段全绿。schema/060 的两个核心不变量**在生产上第一次被真实执行并成立**:
① 归属轴不被 stage-1 重登记覆写;② 检索投影用哨兵而非组码。

## 2. 七环判据与实测

| # | 环节 | 判据 | 实测 |
|---|---|---|---|
| 1 | stage-1 抽取 | canonical 产出 | ✅ 7 页 / 4855 字 / `pdfplumber_layout` / OCR `NOT_REQUIRED`;json+md 双产出 |
| 2 | stage-2 分块 | `chunk_meta` 出行 | ✅ **6 chunk**(clause 4 + table 2);`DONE` / `PUBLISHED` |
| 3 | **归属轴不被覆写** | `owner_dept` 恒 NULL | ✅ `acl_mode=node`、`owner_dept=NULL`、`owner_dept_id=34265162` 三次核查稳定 |
| 4 | 摄取默认授权 no-op | 授权行不被改写 | ⚠️ **不算已验证**,见 §4 |
| 5 | outbox drain | `done_at` 落值 | ✅ `2026-08-05 16:18:36`,`attempts=0`,`last_error=NULL` |
| 6 | **HA3 投影** | 哨兵 + `d:<id>` | ✅ 6/6 present(`missing=[]` `unknown=[]`):`owner_dept=__acl_node_mode_v1__`、`allowed_depts=['d:34265162']`、`permission_level=dept_internal`、`is_active=1` |
| 7 | **ACL 检索隔离** | 本部门可见/他部门不可见 | ✅ 人力资源部(祖先链含 34265162)命中 5 条、目标文档可见(score 0.486/0.424);生产中心(不含)**0 条** |

`gate_status` 全程停在 `pending_clean` —— **正常,非漏跑**。全仓仅三个写方
(`pipeline_nodes`/`register_new_files` 写初值、`cost_breaker` 与 `spot_checker` 写
`'quarantined'`),没有任何一方会把它翻成"通过"。它是**只在出事时才置位的哨兵**,
`pending_clean` 即"未被隔离"的常态。`schema/001:149` 的 DEFAULT 与代码初值一致,无漂移。

## 3. 三个把人骗到的坑(本次实测踩中,全部值得记)

**① HA3 的 PK 是整数 `chunk_meta.id`,不是 `chunk_id` 字符串。**
`clients.ha3_fetch_by_pks` 对非整数 PK 是**静默 `continue` 跳过**的 ⇒ 传 chunk_id 会得到
`{"totalCount":0,"result":[]}`。**那不是"没有",是"根本没查"**,而它长得和数据丢失一模一样
(与 memory `ha3-row-evaporation-2026-07-17` 的六次伪影同族)。生产 PK 来自 RDS 自增 id;
`chunker._stable_pk_from_chunk_id` 的 md5 兜底**只服务模拟/进程内推送**。

**② `content_process_status` 是 stage-2 的认领位,不是 stage-1 的完成位。**
stage-1 跑完它仍是 `NOT_STARTED` 属**正常**;stage-1 的成功信号是 `canonical_json_key` 落值。
本次首判"stage-1 没认领"即由此误读而来。

**③ 本地探针必须显式对齐现网 flag,否则 fail-closed 会伪装成功能故障。**
`.env.prod_ro` 不含 `RAG_NODE_ACL_GRANT`,探针进程里它是 `False` ⇒ 节点读通道整体关闭 ⇒
**所有** node 文档对**所有**身份不可见,三个用例齐刷刷 0 命中,极易被读成"node 检索不通"。
用已实证的现网值(`/api/kb/config` 回 `node_acl_grant=true`)重跑才得到真结果。
⚠️ 只对齐**已实证**的 flag:`allowed_depts_acl` 现网取值未知,本次保持 `False` 且检索照常
成立 ⇒ 顺带证明 node 读通道不依赖该 flag。

## 4. 唯一未验证项:摄取侧默认授权(`2dda4f1`)

本篇的 `kb_doc_node_grant` 是 **console 上传时写的**(`granted_by` 为真实 staffId、
`note='register'`)。摄取侧新代码走 `ON DUPLICATE KEY UPDATE id=id` 的 no-op ⇒
**新包(no-op)与旧包(压根不写)在本次运行中观察结果完全一致**,这一跑**区分不了**二者。

该修复的判别性场景是「**不经 console、直接落 OSS `raw/node-<id>/` 的文档**」——那时授权行
不存在,新包会补出 subtree 授权、旧包则产出 allowed_depts=[] 的隐身文档。在真正走那条路
之前,`2dda4f1` 属于**已上线但未被现网实证**。

## 5. 代表性说明(如实)

第 7 环用的是本机 `retriever` 库对**生产 HA3** 实跑,不是 SAE 服务进程内。
已核 `retriever.py` / `acl_policy.py` / `access_grants.py` 三文件在 HEAD 与**已部署的
`f918e37` 逐字节一致**,故 ACL 判定逻辑具代表性;差异只在进程 env,而关键 flag
(`node_acl_grant`)已按现网实证值对齐。

**仍建议补一次真人链路**:让人力资源部同事在钉钉问一句考勤问题(应答得出并引用本篇),
非人力同事问同一句(应答不出)。那一步覆盖的是 SAE 进程 + 钉钉身份解析,本文档不覆盖。
