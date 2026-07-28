# xlsx 图→步骤绑定对 VLM 措辞过度敏感(2026-07-20 定位;当日修复落地)

**状态(2026-07-20 晚更新)**:P1/P2/P3 全部落地——main `63f0f45`(证据制 P0+兄弟比例守卫+
动态 redirect;GT 加权 0.833→0.936,xlsx_sop 1.0)+ `6ba487b`(P2 哨兵:VLM 指纹入 regime);
ontology-p0 摘取 `02878d6`+`a083328`(复现同款回归 0.6818→1.0)。均未 push。
**注意:实际修复方向与本文 §四 P1 草案不同**——深挖后发现真病根是 P0 单侧 IDF 的杂词竞价
(「上的+操作」df=1 拿满分抢位挤出真信号「归零」),而 anchor 在 xlsx_sop 上主动误导
(anchor-first 只能 3/6),故弃 anchor-first、改证据制评分。§二/§三 的证据链与结构性结论仍然成立。
**触发**:RB-05 refreeze 评测 `l4ing.jaccard.xlsx` 0.891665 → 0.716665(−0.175)

---

## 一、结论

**不是逻辑错误,是绑定输入变了。**`63de8bf` 把图片内容哈希 MD5→SHA-256、VLM 缓存命名空间
默认 `""`→`"2"`,存量缓存整体失效 ⇒ 全部图片重过 VLM ⇒ 生成措辞不同的 `visual_summary`
⇒ 而 xlsx 图→步骤绑定靠这段文字做消歧 ⇒ 绑定结果改变。

**哈希切换本身是必须保留的安全修复**:该哈希兼任 VLM 安全判定(CLEAN/QUARANTINE)缓存主键,
MD5 可构造碰撞让恶意图继承他图的 CLEAN 结论。**不要为评测分数回滚它。**

## 二、证据链(全部实测)

| 环节 | 方法 | 结果 |
|---|---|---|
| 确认真回归 | 同 GT/同文档/同台架跑基线代码 | 基线 `01575d9` = **1.0000**;HEAD = **0.6818**;其余三篇 xlsx 分毫不差 |
| 责任提交 | `git bisect run`(判据:xlsx_sop==1.0) | **`63de8bf`** |
| 责任文件 | 在坏提交上逐文件回退到父版本 | 只回退 `extraction/unified_extractor.py` → 恢复 **1.0000**;另外 3 个文件回退**无影响** |
| 变化形态 | 逐 chunk 对比父 vs 坏提交 | 父提交 11 chunk 全 1.00;坏提交连续 **5 个步骤**变化(一张图从步骤2 溢到步骤3,其后全体错位) |

### 已排除的四个假设(别再重查)

1. **PII 脱敏扩到 visual_summary**(`2e1be81`)——`RAG_IMAGE_OCR_PII_FAILCLOSED` 开/关 A/B,
   两臂结果**完全相同**(0.6818)。
2. **XML 硬化拒绝 drawing**——两次运行日志均**无**「XML 硬化拒绝」警告,抽图数一致(6/7/9)。
3. **标点正则丢字符**(`annotation_parser.py`)——从 git 取原文 `ast.literal_eval` 求值比对,
   字符**集合完全一致**(55→54 仅为 ASCII 双引号去重)。
4. **VLM 漏斗路由改变**——两次运行路由计数逐行相同(6→6 / 7→6 / 29→28 / 9→9,
   ROUTE_TO_VECTOR 计数一致)。

⇒ 同样的图、同样的数量、同样的路由,但绑定变了 ⇒ **只可能是喂给绑定的文字变了**。

## 三、为什么这是个结构性问题,不是一次性事故

**分数一直在漂,从来不是稳定台阶**:`63de8bf` 当时把 xlsx_sop 打到 **0.5909**,后续提交
又漂回 **0.6818**。任何 VLM 模型升级、funnel prompt 调整、缓存失效(含正常的 ns 版本号提升)
都会再次触发同类漂移。**绑定精度事实上依赖于一个非确定、会随供应商变化的文本源。**

## 四、待办(未做)

- [ ] **P1 绑定去文字依赖**:让 xlsx 图→步骤绑定不再单靠 `visual_summary` 词面匹配。
      候选方向:优先用 `anchor_row`(结构信号,不随 VLM 变)+ 文件名序号;文字匹配降为兜底。
      ⚠️ 注意 [[xlsx-equipment-cleaning-binding-fix-2026-06-18]] 记载:`xlsx_clean` 那种
      figure-grid 版式 anchor_row 是聚簇的、**不能**单靠 anchor——两类版式要分别处理。
- [ ] **P2 加漂移哨兵**:L4 绑定分数纳入基线回归网时,同时记录 VLM 缓存 ns 与模型指纹;
      ns/模型变化时自动标注「本次分数不可与前次直接比较」,避免再花一晚做二分。
- [ ] **P3 复查 ontology-p0**:`63de8bf` 是从 ontology-p0 摘取到 main 的,**那个分支很可能
      有同样的回归**,其 L4 基线需一并复查。

## 五、对 RB-05 refreeze 的处置建议(等 Sam 定)

1. **接受新值 + 记账**(建议):基线记 `partial_refresh`,写明本文件为根因说明。
2. 修绑定后再冻 —— 治本但是独立工程,会把 refreeze 阻塞很久。
3. 回滚哈希切换 —— **不建议**,为评测分数回滚真安全修复。

## 六(新增 2026-07-20 晚)、xlsx_clean 0.729 战役调查结论:不修,给出 GT 复核建议

Sam 授权推翻 06-18「near-dups 有意」后展开;实际发现 GT 自带注记已把 over-attach/cross-bind
标为 "the follow-up fix target, not a GT loosening"——但**三条候选修复规则全部被数据判死**:

| 候选规则 | 修 | 破 | 判决 |
|---|---|---|---|
| annotation_num↔行序号 redirect | 链条总成 +0.06 | 主机温度 1.0→0(GT 把 ann3 图绑给序号2 行)、控制面板/齿轮油同险 | 净负,毙 |
| 近重复对只留首张(image_index) | 电机螺丝 0.33→0.5 | 链条 0.5→0(GT 要第二张 img0007) | 互斥,毙 |
| 相似度阈值分离 | — | img4-12=0.463 vs img6-7=0.437,无阈值可分 | 无信号,毙 |

**结构性事实**:GT 的图↔行选择跨 sheet、不随 anchor、不随标注编号、近重复对二选一无规律——
是人工 gestalt 标注。继续拟合 = GT 硬编码。serving 可达性无缺口(未绑图走兜底 image chunk)。

**GT 复核建议(数据仓,等 Sam 定)**:
1. 液压泵行 expected ref **内部自相矛盾**:该 ref 自己的 `binding_target="油压系统-压力表"`、
   `nearby_text` 是压力表行(序号14),却挂在液压泵 gt_chunk(序号10)下——疑标注错位;
2. 电机螺丝行 GT 选 img0004,但工作簿自身的圆圈编号 ⑧=img0017(每班清扫序号8 的作者配图);
3. 链条总成行 GT 在两张近重复链条图(sim 0.437)里选了 img0007,依据未记录。

## 七、复现方法(供后续验证)

```bash
# 临时 worktree 跑任意提交的 xlsx 绑定(不动主工作树)
git worktree add -f /tmp/wt_x <commit>
cd /tmp/wt_x && ln -sfn <repo>/.env .env && ln -sfn <repo>/.env.prod_ro .env.prod_ro
RAG_SIMULATE=false RAG_ENV=prod_ro python3 -c "
import sys,os; sys.path.insert(0,'.')
from eval_harness.binding import ingestion_binding as ib
D=os.path.expanduser('~/Downloads/opensearch-rag-data/eval_samples')
r=ib.run([f'{D}/ground_truth/gt_xlsx_pptx_analysis.json'], f'{D}/documents')
print({d['label']: d['mean_jaccard'] for d in r['per_doc'] if d.get('fmt')=='xlsx'})"
```

⚠️ 必须 `RAG_SIMULATE=false`,否则 VLM 走 mock、visual_summary 变成 `[Simulated]`,
结果完全不可比(实测 mock 下 xlsx_clean=0.0)。
⚠️ 本机**没有 `timeout` 命令**,别写进判定脚本(会让 `git bisect run` 全程静默跳过)。
