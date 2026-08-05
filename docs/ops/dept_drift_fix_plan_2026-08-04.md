# 部门名漂移修复方案 rev3——ancestry 线(2026-08-04)

> 根因与证据 = `docs/ops/dept_name_drift_supply_zero_diag_2026-08-04.md`(prod-RO 三查)。
> 拍板:修复走「最近祖先制」(dept_id 锚)路线(用户 2026-08-04)。
> 评审:codex 限额(8/7 恢复)→ 本轮以三名独立子代理对抗核验替代(语义/接线、测试/运维、安全)。
> **三份全 REVISE,共 10 blocker,逐条核验后全部采纳**(核验记录见 §9)。方案+diff 记入 8/7 codex 补审批次。
>
> **rev2→rev3 关键变更(安全维度 B4)**:获胜系锚**从根下沉到 获胜生产中心**——
> 获胜包装在 07-03 拍板时是无子女叶子(fixture 实证),今天已是「生产+行政」双职能子树,
> 在根设锚违反本模块自己的设锚原则,会让海外子公司的 HR/行政人员拿到大陆生产伞组。

## 0. 落地窗口:语料真空期(rev3 新增,决定性事实)

prod-RO 实测:`document_meta` status 分布 = `{inactive 307, retired 1562, superseded 69}`——
**零 active 文档**;`chunk_meta.is_active=1` = **0**。全部 dept_internal 文档 `acl_mode='legacy'`(1067)。
⇒ **今天开 flag 的实际可读面 = 0 篇文档**。这使本次修复的现实爆炸半径为零,是
「先把 ACL 修对、再让语料回来」的最佳窗口;所有放权面数字(709/22/4 等)描述的是
**语料回灌之后**的状态,不是当下。

## 1. 目标

1. 消除名字漂移误伤:锚表补 3 个新顶层树锚,达成全覆盖。
2. `acl_policy_version()` 指纹补进锚表(flag ON 后锚表=活策略,改锚必须 bump 审计版本)+ **配套回归测试**。
3. 用 08-04 真实树做**精确值** parity 测试,把覆盖结论锁进 CI。
4. `RAG_ACL_ANCESTRY` 开启/回滚 runbook(user-gated,Sam 执行)——含**两步回滚**与收敛前置项。

## 2. 非目标

- **不改** `_DEPT_NAME_TO_GROUPS` / `_PRODUCTION_WORKSHOP_DEPTS`(退役方向的枚举表,保持 fallback 原样)。
- 不翻 `RAG_ACL_ANCESTRY` 默认值(仍默认关;开启=SAE 控制台 env,user-gated)。
- 不处理「其他」70 人(显式 [] 锚,有意仅 public——**decided-deny,非覆盖缺口**)。
- 不动死锚 `842763367`(工程,08-04 树中已不存在)——见 §8 拍板项。
- 不做「定时漂移告警」(真正防下次漂移的机制,见 §8;本次 fixture 只能防锚表被改坏)。

## 3. 当前行为与根因(摘要)

名字表按 2026-06/07 快照键控:不抗改名(资材部→采购部,同 id 728779788)、不给子树继承。
9/31 死键。三个海外系单位(获胜工厂/印尼公司/墨西哥公司)改名+**升顶层**(id 不变,脱离
海外中心锚子树),ancestry 现有锚也够不着——锚表唯一的真实覆盖缺口。

### 3.1 接线真实契约(rev2 修正——rev1 此处写错)

`dingtalk_identity.py:401-414` 的实现是**整体制**,不是逐支三态:

| ancestry 结果 | 行为 |
|---|---|
| 非空(哪怕只有一支解出) | **整体权威**:组码 CSV 顶替 dept_name;**未决定支的名字口径授权不再补上** |
| 空 + 全支 decided | 权威「有意仅 public」→ 存 deny 哨兵 |
| 空 + 存在未决定支 | 落名字口径兜底 |
| partial(任一跳失败) | 整体落名字口径,绝不缓存 |

rev1 写成「undecided 落名字口径兜底」是错的——混合支下**不会**并名字口径。该语义为既有设计
(代码注释 `:395-398` 本身准确),本次不改;但它意味着**加锚会让原本走名字口径的支被权威顶替**,
故必须逐部门证明新锚覆盖面的名字口径值不超出锚值(见 §4.1)。

## 4. 修改范围

### 4.1 `opensearch_pipeline/dept_ancestry.py` — 增 3 锚(rev3:获胜系下沉)

```python
1091525269: ["overseas", "production"],  # 获胜包装/获胜生产中心(原 海外中心/获胜工厂 子树的生产侧)
488554573:  ["overseas", "production"],  # 印尼富岭(原 海外中心/印尼公司;2026-08 改名+升顶层,id 不变)
488669494:  ["overseas", "production"],  # 墨西哥富岭(原 海外中心/墨西哥公司,同上)
```
另:728779788 注释补现名「采购部」(组值不变——改名免疫正是锚表卖点)。

**为何不在获胜包装根设锚(rev3,采纳安全评审 B4)**:07-03 fixture 实证 `1068136163` 当时是
**无子女叶子**,拍板对象是一个单点;今天它下辖 获胜生产中心 + 获胜行政中心 两条职能线,
在根设锚等于把叶子级拍板放大成 89 人双职能子树授权,与 `dept_ancestry.py:32-34` 自己写的
「多职能中心不在中心设锚,锚下沉到二级部门」直接冲突。实测两方案对比(全员 1167):

| 方案 | 涨 | 跌/交叉 | 无组残余 | 新得 production 人数 |
|---|---|---|---|---|
| A 根锚(rev1) | 247 | 0 / 0 | 70 | 132(含 获胜包装直挂 11 + 获胜行政中心 8) |
| **B 下沉获胜生产中心(rev3 采用)** | 226 | 0 / 0 | 89 | **111**(不含上述 19 名行政侧) |

B 是 A 的真子集,方向 fail-closed。代价:获胜包装直挂 11 人 + 获胜行政中心 8 人维持仅 public
(与今天一致,非回退)——**是否补锚列 §8 拍板项**。

**放权面确定性枚举**(prod-RO,`scratch/b2_subtree_enum_20260804.out.txt`):三子树共 6 个活跃部门,
名字口径值全部 ⊆ {overseas, production}(仅 `纸浆模塑事业部` = `['production']`,余为 `[]`)。
三子树 **89 名成员**(diag3 口径;含跨树兼职 生产中心2/内销监装1/行政部1/人力资源部1/吸塑办公室2,
均有更近锚)逐人对比:**跌落 0、交叉 0**。全员 dept_code CSV 最长
`marketing,production,overseas` = **29 字符**(限 64,安全)。

### 4.2 `opensearch_pipeline/versions.py`

`acl_policy_version()` payload 增 `"ancestry_anchors"`(键排序、值保序,`"*"` 哨兵原文)
**+ `"ancestry_enabled"`(flag 现态)**——rev3 采纳安全评审 M1:只加锚表的话,
flag ON 与 OFF 会算出**同一个 hash**,而两态对 ≥155 人给出不同的组,审计员无从分辨哪张表在活。
指纹因此随进程 env 变化,这正是 memory `flag-propagation-is-execution-path-dependent` 要暴露的事实。
docstring「5 个映射常量」→ 6 + flag。

### 4.2b `opensearch_pipeline/dingtalk_identity.py` — 祖先制 partial 可观测(rev3 新增)

`_resolve_groups_via_ancestry` 与 `dept_ancestry.py` 全文**零日志**,`:420` 那条 warning 是
**名字口径** partial,祖先制 partial 时不触发 ⇒ rev2 runbook 写的「SLS 盯 partial 回退告警」
根本无信号可盯(安全评审 B3)。补一条 warning(含 staff_id 与 dept_ids),使下述风险可观测。

**风险改写(安全评审 B2,rev3)**:祖先制 partial **不是**「瞬时、单向 fail-closed」——
`_anc_res is None` 时整块被跳过,`:415` 判的是名字口径的 `is_partial`;若名字路径本身成功,
结果**照常写入 `user_role` 并服务 6h**。后果:显式 `[]` 锚(如「其他」)的权威 deny,
可被一次 `department/get` 超时击穿成名字口径授权并被缓存(`tests/test_dept_ancestry.py:305-318`
锁的正是「deny 压过名字撞名」)。该机制既有、非本次引入,但 flag ON 后才真正带电,
故列入 runbook 与告警。

### 4.3 `tests/test_dept_ancestry.py` — 新增「2026-08-04 漂移批」

1. **改名免疫**:合成 生产中心→采购部 父链,728779788 → `{supply, pmc, production}`。
2. **子节点→新锚一跳**(rev2 修正:自锚在 `dept_ancestry.py:123` 循环顶即命中 break,父链 getter
   一次都不调用,rev1 的「合成 parent=1」测试是空转)——用 获胜生产中心→获胜包装 真实一跳。
3. **08-04 fixture 精确值 parity**(rev2 修正:rev1 的「非空」断言经变异测试证明对越权 0 检出——
   把 92 人子树误配 `["*"]` 全组哨兵也能通过):
   - `_DIVERGE_0804` 逐 path 精确字典相等(30 条,数据已算出:29 条 `[]`→非空的缺口修复 +
     1 条 `纸浆模塑事业部 ['production']→['overseas','production']`);
   - 87 个名字命中部门中除该 1 条外**逐一全等**(沿用 07-03 铁律 1 形制);
   - 空集合按 **dept_id** 键控(rev2 修正:名字豁免与"名字会漂移"的立论自相矛盾)——
     `{68112184 其他, 417762615 lzdqr, 920067054 实习生}`;
   - **undecided 断言**:`其他`=False(decided-deny)、`lzdqr/实习生`=True(覆盖缺口)——
     两者都表现为空集但语义相反,必须分开锁。
4. **锚 id 存在性**:每个锚 id 在 08-04 树中存在,**已知例外 `842763367`(工程)显式列出**并注释原因。

### 4.4 `tests/fixtures/dingtalk_org_snapshot_20260804.json`(新)

从 `dept_dim`(is_active=1,119 部门,无孤儿,最大深度 5)导出,shape 同 07-03。
⚠️ **必须剥掉导出首行的 `[prod_access] READONLY conn -> rm-…rds.aliyuncs.com` 横幅**——
否则生产 RDS 主机名进公开仓。仅含 dept_id/name/parent_id/path,无 PII(同 07-03 先例)。
⚠️ **口径边界**(rev2 补):运行时 ancestry 的输入是钉钉活 API(`dept_ids` 来自 `user/get`、
父链来自 `department/get`),fixture 是 `dept_dim` 日快照——它锁的是**快照树形态下的锚表正确性**,
不等于运行时覆盖率。

### 4.5 `tests/test_audit_log.py` — 补指纹回归(rev2 新增)

照 `:122` 形制补一条 monkeypatch `dept_ancestry.ANCHOR_GROUPS_BY_DEPT_ID` 的测试:改锚 → hash 变。
否则目标 2「改锚必须 bump」零测试覆盖(删掉 `ancestry_anchors` 键现有测试也照样绿)。

### 4.6 `scripts/verify_acl_coverage.py`(新,rev2 新增)

runbook 的验证步骤不能指向 `scratch/`(gitignored、不进镜像、Sam 执行不了)。提交一个
prod-RO 只读脚本:输出 supply 受众数 + 各组受众 + 无组员工数,供 flag 翻转前后对照。

### 4.7 `docs/ops/user_gated_checklist_2026-07-22.md` — `RAG_ACL_ANCESTRY` 开启项

- **前置核查**:`RAG_LIVE_ACL_REREAD=true`(默认 true,SAE env 未核实)、`RAG_ACL_CACHE_TTL`
  与 `RAG_BOT_DEPT_CACHE_TTL_SECONDS` 现网值(runbook 引的是代码默认 21600/90,须现查 env)。
- **收敛口径**(rev2 修正:非墙钟,是**活动触发**):employee 行 6h TTL 仅在用户再走**写路径**
  (机器人提问 / `/api/auth/dingtalk` 发令牌)时穿透;读路径 `_resolve_user_dept_cached` 是
  SELECT-only,永不触发重解析 ⇒ **不活跃用户永不收敛**。另有 45s `_live_acl_cache` 与
  令牌 2h 两条腿。
- **性能**(rev2 新增):flag ON 的 cache-miss 路径逐跳串行 `department/get`(timeout=5s,
  `_fetch_dept_parent` 无 memo、跨支不复用),与已并发化的名字口径打同一 endpoint。
  树深 ≤5 ⇒ 最坏 ~5 次串行 HTTP 压在首字延迟上。
- **回滚(两步,rev2 修正)**:关 flag **不足以**止血——flag ON 期写进 `user_role.dept_code` 的
  组码 CSV 关 flag 后仍被 `_normalize_dept_to_codes` 原样透传(实测 `overseas,production` →
  `['overseas','production']`)。必须 ①关 flag ②清/降级 employee 缓存行。
  清行姿势对比:
  - **推荐**:临时 `RAG_ACL_CACHE_TTL=300` 让 `_stale` 门自然放行——纯 env、可逆、零数据丢失;
  - 慎用 `DELETE ... WHERE role='employee'`:会**误删人工墓碑**(`is_active=0` 行,语义=撤销读权)。
    实测现网墓碑数 = **0**(`notify_data_annex` c_user_role_tombstone),故当前爆炸半径为零,
    但该表随时可能被运维手工写入,不应写成常规姿势。
- **验证**:`scripts/verify_acl_coverage.py` 翻转前后各跑一次(supply 受众 0→>0);SLS 盯 partial 回退告警。

### 4.8 诊断报告补实施记录

附:B2 枚举结论、`acl_policy_version()` old/new 两个 hash 值(hash 不可逆且仓库无注册表,
不留痕则审计侧只能看到"版本变了")。

## 5. 数据流/接口变化

零接口变化。flag OFF 下 dept_ancestry 不参与任何运行时解析(**已核实**:唯一读锚表的
`resolve_dept_ids` 唯一调用者被 `if _acl_ancestry_enabled() else None` 门死;`resolve_ancestor_chains`
/`resolve_descendant_ids` 均不读锚表)。唯一 flag-OFF 可见变化 = `acl_policy_version()` 值
(经 `audit_log.py:81` 进 ACL 审计行前缀,fail-open)。

## 6. 测试与验证

- `pytest tests/test_dept_ancestry.py tests/test_audit_log.py tests/test_acl_policy.py`,显式回显 exit code。
- 全量 `make test` 回归。
- **变异自检**(rev2 新增):对新 parity 测试做 4 个越权变异(`["*"]` / 漏 overseas / 错配 finance /
  删「其他」deny 锚),确认每个都被打红——测试有效性的元验证。
- CI:`ruff` 需注意 mid-file import 的 `# noqa: E402`(tests/ 不在 per-file-ignores);
  fixture 17KB 不进镜像(`.dockerignore` 排除 tests/)。

## 7. 风险与回滚

- 本补丁独立风险≈0(flag OFF 零运行时行为变化)。
- flag ON 放权面:1167 人仿真 涨 247/平 920/**跌 0/交叉 0**,三子树 91 人逐人复核跌 0;
  全部涨幅落在既有拍板口径内(2026-07-03 海外系条目),无新组值、无 kb_admin/写权变化。
- 代码回滚 = revert 单 commit;运行时回滚见 §4.7(两步)。
- 残余:锚表仍是静态快照,组织再漂移仍需人工跟进(fixture 是提交时快照,不会自己发现漂移)。

## 8. 未定项(实施不阻塞,单列拍板)

1. ~~获胜包装直挂 + 获胜行政中心是否补锚~~ **✅ 2026-08-04 拍板并落地(rev4)**:
   `1091358296 获胜行政中心 → ["admin"]`(刻意不叠 overseas/production——行政职能与生产内容无关);
   **树根 1068136163 仍不设锚**,直挂 11 人维持 fail-closed(树根是「公司」语义、职能不明)。
   落地后全员:涨 235 / 跌 0 / 交叉 0,无组残余 81 =「其他」70(有意)+ 获胜树根 11。
2. **纸浆模塑事业部的组织/内容分类分叉**(安全评审 M6,rev3 新增):dept_id `921614009`
   在 07-03 是 `生产中心/纸浆模塑事业部`,今天是 `获胜包装/获胜生产中心/纸浆模塑事业部`——
   **同 id 整建制迁入海外子公司树**,不是同名误授(诊断 §5.2 的「跨树误授」定性已作废,见 §10)。
   待拍板:该业务单元划入子公司后是否继续读大陆 production 全量;`retriever.py:373` 仍把
   `production_pulp_molding` 登记为大陆生产子线 owner,组织侧与内容侧分类已分叉。
3. 名字表同步补丁(加「采购部」等新名键)——默认不做;做则 flag 翻转前即可止血 15 人。
4. flag ON 时点与加速姿势(TTL 旋钮 vs 清行)——Sam。
5. **`_PRODUCTION_WORKSHOP_DEPTS` 全局名字键控无子树校验**(安全评审 M4):任意树下命名为
   `生产部`/`模具A` 的部门即得生产伞组。既存缺陷,本次不修(修法=加子树校验,影响面需单独评估)。
6. **外部组码防投毒**(安全评审 M5):`:384-391` 只挡名为 `"*"` 的部门,不挡 15 个组码——
   一个字面命名为 `production` 的钉钉部门即可提权。本次以**独立 commit** 补齐
   (08-04 树内 119 个部门名无一撞组码,零误伤),可单独 revert。
7. 死锚 `842763367`(工程)保留 or 删除——保留=id 若被回收则误授(理论);删除=部门恢复时静默失权。
8. **定时漂移告警**(结构性解法):对 `dept_dim` 定期跑覆盖扫描并告警
   (`scripts/scan_dept_mapping_gaps.py` 有骨架)——本次不做,列 backlog。
9. 锚表动态化(RDS 化)、`_ACL_AUDIT_ACTIONS` 补齐 `ACCESS_GRANT_DIRECT` 等——backlog。
10. 08-04 fixture 会首次披露子公司重组结构(获胜/印尼/墨西哥升顶层)进**公开仓**;
    07-03 先例成立、无 PII,但属新增披露,单列一句知会。
11. **cohort 暴露面**(安全评审 minor):`/api/hot-questions` 与「换个说法」候选池以
    `acl_groups` CSV 精确串为 cohort 键(`api.py:2364-2369`),组集合变化会改变非文档暴露面——
    本例中 33 人从 production 大池切到 `overseas,production` 小池(收紧)、15 人采购部进入真实问题池(新增)。

## 10. 诊断报告需更正项(rev3)

`dept_name_drift_supply_zero_diag_2026-08-04.md` §5.2 把「获胜子树 38 人有组」写成
「疑似跨树误授 production、安全方向存疑」——**已证伪**:唯一候选 `纸浆模塑事业部` 是同 dept_id
整建制迁移(07-03 fixture 实证),名字表命中的是它自己的历史部门名;另 2 人的 production
来自「吸塑办公室」兼职,正当。人数口径统一取 diag3 的 **89**(正文旧值 92 作废)。

## 9. 评审核验记录(Phase 5)

| 编号 | 意见 | 裁决 | 依据 |
|---|---|---|---|
| A1-B1 | 混合支下未决定支被吞,方案契约描述错 | **ACCEPTED** | `dingtalk_identity.py:403` 实读;方案 §3.1 已改写 |
| A1-B2 | 「零跌落」无仓库证据,新锚正是制造跌落的机制 | **ACCEPTED(证据补齐后 REFUTED 为缺陷)** | 已跑三子树逐部门枚举:6 部门全 ⊆ 锚值,91 人跌落 0 |
| A1-M1 | 量化主张全在 gitignored scratch | **ACCEPTED** | 结论表已内联进 §4.1;验证脚本移入 `scripts/`(§4.6) |
| A1-M2 | fixture 源 ≠ 运行时源 | **ACCEPTED** | §4.4 已加口径边界 |
| A1-M3 | 逐跳串行 department/get 无 memo | **ACCEPTED** | `_fetch_dept_parent:685` 无 lru;§4.7 已记 |
| A1-M4 / A2-B2 | 关 flag 非即时回滚 | **ACCEPTED** | 实测 `_normalize_dept_to_codes('overseas,production')` 透传;§4.7 改两步 |
| A1-minor | 自锚测试空转 | **ACCEPTED** | `dept_ancestry.py:123` 循环顶命中;测试改子节点一跳 |
| A1-minor | VARCHAR(64) union 无守卫 | **部分 REFUTED** | 实测全员最长 29 字符;既存潜在缺陷记 backlog |
| A2-B1 | 非空断言对越权 0 检出(4/4 变异通过) | **ACCEPTED** | 改精确值+undecided 断言(§4.3) |
| A2-B3 | 指纹主张零测试 | **ACCEPTED** | §4.5 新增 |
| A2-B4 | runbook 指向 gitignored 脚本 | **ACCEPTED** | §4.6 新增 `scripts/verify_acl_coverage.py` |
| A2-M1 | 清 employee 行误伤人工墓碑 | **ACCEPTED(爆炸半径经查为 0)** | 现网墓碑数=0;仍改推荐 TTL 旋钮 |
| A2-M2 | 08-04 可用精确相等,别只做非空 | **ACCEPTED** | 已算出 87 命中/1 发散/30 条发散表 |
| A2-M3 | 收敛是活动触发,漏 45s+2h 腿 | **ACCEPTED** | §4.7 已改写 |
| A2-minor | 豁免用名字自相矛盾 / banner 泄主机名 / 死锚 / hash 留痕 / 锚值顺序敏感 | **全 ACCEPTED** | §4.3、§4.4、§8.3、§4.8 |
| A2-minor | 「防下次漂移静默回归」是假的 | **ACCEPTED** | fixture 是静态快照;§2 已改口径,结构性解法列 §8.8 |
| A3-B1 | 关 flag 不撤销已发组 | **ACCEPTED**(与 A1-M4/A2-B2 三方独立同证) | §4.7 两步回滚 |
| A3-B2 | 祖先制 partial 会被缓存 6h,且权威 deny 可被一次超时击穿 | **ACCEPTED** | `:399-415` 实读:`_anc_res is None` 时整块跳过,`is_partial` 判的是名字口径;§4.2b 已改写风险 |
| A3-B3 | partial 无任何日志 ⇒ runbook 的 SLS 盯法不可执行 | **ACCEPTED** | `dept_ancestry.py` / `_resolve_groups_via_ancestry` 全文零 logger;§4.2b 补日志 |
| A3-B4 | 根锚把叶子级拍板放大成 89 人双职能子树 | **ACCEPTED(rev3 核心变更)** | 07-03 fixture 实证当时无子女;实测 A/B 两方案对比表(§4.1) |
| A3-M1 | 指纹不含 flag 态 ⇒ 两套活策略同 hash | **ACCEPTED** | §4.2 加 `ancestry_enabled` |
| A3-M2 | 「全部非空」是 fail-open 棘轮 | **已在 rev2 解决** | rev2 已改精确值+dept_id 键控空集,非"必须非空" |
| A3-M4/M5 | 名字表全局键控无子树校验 / 防投毒只挡 `*` | **ACCEPTED** | 列 §8.5、§8.6(M5 以独立 commit 补) |
| A3-M6 | 「跨树误授」定性错误,实为整建制迁移 | **ACCEPTED** | 07-03 fixture 实证同 id;§8.2 + §10 更正 |
| A3-缺证 1/2 | 放权面未按 acl_mode 拆分、overseas 文档数未知 | **已补齐** | dept_internal 全部 `acl_mode='legacy'`(1067);**现网零 active 文档**(§0) |
| A3-缺证 3/4 | 54 人逐部门分布、"交叉 0"支撑 | **已补齐** | §4.1 表 + `scratch/b4_decision_data_20260804.out.txt`;交叉桶实测为 0 |
