<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  Database, CheckCircle2, Archive, Clock,
  ThumbsUp, ThumbsDown, Percent, Quote, MessageSquare, Ban, AlertTriangle,
} from '@lucide/vue'
import { onMounted } from 'vue'
import { useKb } from '@/composables/useKb'
import { deptLabel } from '@/lib/kb'
import { SECTION, ZONE_HEAD, ZONE_TICK, SUBHEAD, GRID, SPLIT } from '@/lib/section'
import { fetchOrgSnapshot } from '@/composables/useOrgSnapshot'
import { resolveOwnerBucket } from '@/lib/orgTree'
import type { OrgNode } from '@/composables/useOrgSnapshot'
import StatusDistBar from './StatusDistBar.vue'
import StatCard from './StatCard.vue'
import BarList from './BarList.vue'
import ColumnChart from './ColumnChart.vue'
import OrgCoverageTable from './OrgCoverageTable.vue'
import OrgTreeSelect from './OrgTreeSelect.vue'
import type { PickedNode } from './OrgTreePicker.vue'
import VitalsList, { type VitalItem } from './VitalsList.vue'
import FeedbackTrend from './FeedbackTrend.vue'
import MiniTrend from './MiniTrend.vue'
import LoadError from './LoadError.vue'
import FeedbackReviewList from './FeedbackReviewList.vue'
import ReviewTaskQueue from './ReviewTaskQueue.vue'

// 归属键 kind-aware 解析（top_docs 副标签用；快照缓存命中零额外请求）
const snapById = ref<Map<number, OrgNode>>(new Map())
onMounted(async () => {
  try { snapById.value = new Map((await fetchOrgSnapshot()).nodes.map((n) => [n.dept_id, n])) } catch { /* 兜底走 owner_label/deptLabel */ }
})
const ownerText = (key: string) => resolveOwnerBucket(key, undefined, snapById.value, deptLabel).label

// 知识库管理员「概览看板」= 全库视角（对齐 Atlas 设计分区）。资产/状态取 /api/kb/stats、待审批
// /pending-approvals；运行健康+治理风险+部门覆盖取 /api/kb/governance；知识效果取 /api/kb/insights。
// 全部真实口径，无对应数据则如实显空 —— 绝不造数。
const { kbStats, approvals, kbGovernance, kbInsights, feedbackReview, loadStats, loadGovernance, loadInsights, loadErrors, fbStats, loadFeedbackStats } = useKb()

// 「待你处理」置顶条（P2）：差评复核是看板里唯一的行动区，却沉在页尾 ~2900px 深——
// 有未处理差评时在首屏给一枚计数 chip，点击平滑定位；清零即隐，与文档管理 tab 的待办条同语言。
// 计数来源区分「0 条」与「加载失败」（staging 2026-07-11 P1）：接口真错误时挂「加载失败」chip
// 而非整条消失——0=真没有，失败=数量未知，两者对管理员是完全不同的信号。
const feedbackOpenCount = computed(() => (feedbackReview.value || []).filter((x) => !x.handled).length)
const feedbackLoadFailed = computed(() => !!loadErrors.value['feedbackReview'])
function scrollToSec(id: string) { document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
// 资产概览卡的加载态：stats 尚未返回且无错误 → 显骨架（避免闪 0）。
const statsLoading = computed(() => !kbStats.value && !loadErrors.value['stats'])
const b = (k: string) => kbStats.value?.by_badge?.[k] || 0
const fmtN = (n?: number) => (n || 0).toLocaleString('en-US')
const ms2s = (ms?: number) => (ms ? (ms / 1000).toFixed(1) + 's' : '—')
const pct = (x?: number) => (x === undefined ? '—' : (x * 100).toFixed(1) + '%')

interface Card {
  label: string; value: string | number; icon: any; tone?: string; hint?: string
  box?: string; pill?: string; pillLabel?: string; subValue?: string; subLabel?: string
}

// ── 全库资产概览：文档总数（数据库图标 + 本月新增徽标 + 已索引分块子行）/ 已上线 / 已退役 / 待审批 ──
const assetCards = computed<Card[]>(() => {
  const st = kbStats.value
  const nm = st?.new_this_month ?? 0
  return [
    {
      label: '文档总数', value: st?.total ?? 0, icon: Database, tone: 'text-foreground',
      hint: '全部门 · 有效及处理中',
      pill: nm > 0 ? `+${fmtN(nm)}` : '', pillLabel: '本月新增',
      subValue: fmtN(st?.chunks ?? 0), subLabel: '已索引分块',
    },
    { label: '已上线', value: b('已上线'), icon: CheckCircle2, tone: 'text-st-live', hint: '当前可被检索' },
    { label: '已退役', value: st?.retired ?? 0, icon: Archive, tone: 'text-st-muted', hint: '已下线文档' },
    // 待审批 = 唯一「待你处理」的行动卡：有积压时整卡橙框高亮（去「文档管理」放行）；清空回常态。
    {
      label: '待审批', value: approvals.value.length, icon: Clock, tone: 'text-st-busy', hint: '公开/跨组 待放行',
      box: approvals.value.length ? 'border-st-busy/45 bg-st-busy/[0.06]' : '',
    },
  ]
})

// ── 运行 vitals（2026-08-03 重设计：9 张同权重卡压成扁平列表；tone 语义逐项照搬原卡,
//    不发明新阈值——codex 共识）。分组小标题保留「运行健康/服务可用性/治理风险」词面。──
const vitalsHealth = computed<VitalItem[]>(() => {
  const g = kbGovernance.value
  const maxFail = Math.max(0, ...(g?.embed_runs || []).map((r) => r.fail_rate))
  const ingest = (g && g.docs_active) ? g.docs_in_index / g.docs_active : undefined
  const dual = g?.dual_version_docs ?? 0
  const consistency = (g && g.docs_in_index) ? (g.docs_in_index - dual) / g.docs_in_index : undefined
  return [
    { label: '入库成功率', value: pct(ingest), tone: 'text-st-live', hint: `${g?.docs_in_index ?? 0}/${g?.docs_active ?? 0} 已索引上线` },
    { label: '数据一致性', value: pct(consistency), tone: dual ? 'text-st-warn' : 'text-st-live', hint: dual ? `${dual} 文档双版本残留` : '无双版本残留' },
    { label: '嵌入失败率', value: pct(maxFail), tone: maxFail > 0 ? 'text-st-warn' : 'text-st-live', hint: '近 8 次入库最差' },
    { label: '问答延迟 p95', value: ms2s(g?.p95_latency_ms), tone: 'text-foreground', hint: `p50 ${ms2s(g?.p50_latency_ms)} · 含流式渲染` },
  ]
})
const vitalsAvailability = computed<VitalItem[]>(() => {
  const g = kbGovernance.value
  return [
    { label: '问答 API 成功率', value: pct(g?.qa_api_success_rate), tone: 'text-st-live', hint: `近 ${g?.window_days ?? 30} 天 · ${fmtN(g?.qa_total_30d)} 次` },
    { label: '检索 API 成功率', value: pct(g?.retrieval_api_success_rate), tone: 'text-st-live', hint: '检索正常返回占比' },
    { label: '近 24h 错误数', value: g?.errors_24h ?? 0, tone: g?.errors_24h ? 'text-st-fail' : 'text-st-live', hint: '失败请求 · DashScope/HA3' },
  ]
})
const vitalsRisk = computed<VitalItem[]>(() => {
  const g = kbGovernance.value
  return [
    { label: 'PII 已脱敏', value: g?.pii_redacted_docs ?? 0, tone: 'text-st-busy', hint: '含敏感信息文档' },
    { label: 'PII 隔离', value: g?.pii_quarantined_docs ?? 0, tone: g?.pii_quarantined_docs ? 'text-st-warn' : 'text-st-muted', hint: '高风险未入库' },
  ]
})
// 近期入库趋势（纵向柱，最新在右）：bizdate 取 MM-DD，值 = 嵌入块数；带失败计数 → 条顶红盖。
const embedTrend = computed(() =>
  [...(kbGovernance.value?.embed_runs || [])].reverse().map((r) => {
    const d = (r.bizdate || '').replace(/\D/g, '')
    return { label: d.length >= 4 ? `${d.slice(-4, -2)}-${d.slice(-2)}` : (r.bizdate || ''), value: r.embedded, failed: r.failed, failRate: r.fail_rate }
  }))
// ── 文件类型：DonutChart → 内联堆叠条（<1% 段保底可见,占比按真实值不四舍五入到 100）──
const fileTypeItems = computed(() => (kbGovernance.value?.file_types || []).map((f) => ({ label: f.ftype, value: f.count })))
const fileTotal = computed(() => (kbGovernance.value?.file_types || []).reduce((s, f) => s + f.count, 0))
const fileSegments = computed(() => fileTypeItems.value.map((f, i) => ({
  ...f,
  sharePct: fileTotal.value ? (f.value / fileTotal.value) * 100 : 0,
  style: `background: color-mix(in srgb, var(--accent) ${Math.max(14, 52 - i * 13)}%, var(--panel))`,
})))

// ── 全库知识效果：效果卡（按数据源就绪与否纳入，绝不显伪 0）+ 最常被使用 / 高频未答好 ──
const effectCards = computed<Card[]>(() => {
  const g = kbGovernance.value, i = kbInsights.value
  const out: Card[] = []
  if (g) {
    out.push({ label: '有效回答率', value: pct(g.effective_rate), icon: CheckCircle2, tone: 'text-st-live', hint: `近 ${g.window_days} 天 · 有依据答案占比` })
    const na = g.answer_total ? (g.answer_no_result + g.answer_refusal) / g.answer_total : undefined
    out.push({ label: '无答案率', value: pct(na), icon: Percent, tone: 'text-st-warn', hint: '无结果 + 拒答 占比' })
    const refusal = g.answer_total ? g.answer_refusal / g.answer_total : undefined
    out.push({ label: '拒答率', value: pct(refusal), icon: Ban, tone: 'text-st-warn', hint: '命中文档但拒答（语料弱/召回不足）' })
  }
  if (i) out.push({ label: '近 30 天引用', value: fmtN(i.cited), icon: Quote, tone: 'text-accent-text', hint: '文档进入最终回答的提问数' })
  return out
})
const topDocItems = computed(() =>
  (kbInsights.value?.top_docs || []).map((d) => ({ label: d.title, sub: ownerText(d.owner_dept), value: d.hits })))
const gapItems = computed(() =>
  (kbInsights.value?.gap_queries || []).map((g) => ({ label: g.query, sub: `平均相关度 ${g.avg_top.toFixed(2)}`, value: g.count })))

// ── 反馈区按部门筛选（2026-08-03 Sam 需求，codex 两轮共识）────────────────────
// 归属口径=答案实际引用（cited=1）该部门文档；选择颗粒度随 OrgTreeSelect 默认止于二级。
// 置顶「差评未处理」chip 保持全库收件箱计数（行动区语义），筛选只作用于本区视图。
const fbFilterNode = ref<PickedNode[]>([])
const fbOwnerKey = computed(() => (fbFilterNode.value[0] ? `node:${fbFilterNode.value[0].dept_id}` : ''))
const fbFilterName = computed(() => {
  const id = fbFilterNode.value[0]?.dept_id
  return id ? (snapById.value.get(id)?.name ?? `#${id}`) : ''
})
watch(fbOwnerKey, (k) => { void loadFeedbackStats(k) })
const fbFiltered = computed(() => !!fbOwnerKey.value && !!fbStats.value)
function clearFbFilter() { fbFilterNode.value = [] }

// ── 用户反馈与回答质量 ──
const feedbackCards = computed<Card[]>(() => {
  // 筛选态：数据源切 /api/kb/feedback-stats（分母 answer_total 同为筛选口径，绝不混窗）
  if (fbFiltered.value) {
    const st = fbStats.value!
    const coverage = st.answer_total ? st.total / st.answer_total : undefined
    const hint = `筛选：${fbFilterName.value} · 近 ${st.window_days} 天`
    return [
      { label: '点赞', value: fmtN(st.up), icon: ThumbsUp, tone: 'text-st-live', hint },
      { label: '点踩', value: fmtN(st.down), icon: ThumbsDown, tone: 'text-st-fail', hint },
      { label: '正反馈率', value: pct(st.total ? st.helpful_rate : undefined), icon: Percent, tone: 'text-accent-text', hint: '赞 /(赞+踩)' },
      { label: '反馈覆盖率', value: pct(coverage), icon: MessageSquare, tone: 'text-foreground', hint: `反馈数 / 命中该部门文档的回答数` },
    ]
  }
  const g = kbGovernance.value
  // 覆盖率 = 反馈数 / 回答数，两者【同为近 window_days 天】口径（#10 修复：此前分子全量、分母 30 天
  // 混窗，覆盖率可 >100% 且无意义）。
  const win = g?.window_days ?? 30
  const coverage = (g && g.answer_total) ? g.feedback_total / g.answer_total : undefined
  return [
    { label: '点赞', value: fmtN(g?.feedback_up), icon: ThumbsUp, tone: 'text-st-live', hint: `近 ${win} 天 · 用户认可` },
    { label: '点踩', value: fmtN(g?.feedback_down), icon: ThumbsDown, tone: 'text-st-fail', hint: `近 ${win} 天 · 用户标记` },
    { label: '正反馈率', value: pct(g?.helpful_rate), icon: Percent, tone: 'text-accent-text', hint: '赞 /(赞+踩)' },
    { label: '反馈覆盖率', value: pct(coverage), icon: MessageSquare, tone: 'text-foreground', hint: `反馈数 / 回答数（近 ${win} 天）` },
  ]
})
const downvoteItems = computed(() =>
  (kbGovernance.value?.downvote_reasons || []).map((r) => ({ label: r.reason, value: r.count })))

// 分区视觉常量已抽到 @/lib/section（此前三个看板各写一遍，SUBHEAD 已因此漂移过）。
</script>

<template>
  <div class="space-y-6">
    <!-- P2-14：监控链路心跳超时红条——ops_monitor >26h 未跑 = 所有 parity/SLO 检查停摆，
         看板下方的健康数字全是旧的；serving 作被动死人开关，首屏可见。 -->
    <div
      v-if="kbGovernance?.monitor_stale"
      class="flex flex-wrap items-center gap-2 rounded-xl border border-st-fail/40 bg-st-fail/5 px-4 py-3"
    >
      <span class="text-[12.5px] font-bold text-st-fail">⚠ 监控链路停摆</span>
      <span class="text-[12px] text-muted-foreground">
        ops_monitor 心跳已 {{ kbGovernance?.monitor_heartbeat_age_h }} 小时未刷新——巡检
        crontab / 凭据 / 网络可能失效，下方健康指标可能已过期。
      </span>
    </div>

    <!-- 待你处理：有未处理差评时首屏可见、一键直达（看板唯一行动区在页尾，不能只靠滚动发现）；
         差评复核接口真错误时同样置顶显「加载失败」——数量未知 ≠ 0 条，点击直达区块内重试。 -->
    <div
      v-if="feedbackOpenCount || feedbackLoadFailed"
      class="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-panel/60 px-4 py-3"
    >
      <span class="text-[12.5px] font-semibold text-foreground">待你处理</span>
      <button
        v-if="feedbackOpenCount"
        type="button"
        class="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1 text-[12px] font-medium text-st-fail transition hover:border-border-strong"
        @click="scrollToSec('kb-dash-feedback')"
      >差评未处理 <b class="font-mono tabular-nums">{{ feedbackOpenCount }}</b></button>
      <button
        v-if="feedbackLoadFailed"
        type="button" data-testid="feedback-load-failed-chip"
        class="inline-flex items-center gap-1.5 rounded-full border border-st-warn/40 bg-card px-3 py-1 text-[12px] font-medium text-st-warn transition hover:border-st-warn/70"
        @click="scrollToSec('kb-dash-feedback')"
      ><AlertTriangle :size="12" :stroke-width="1.75" /> 差评复核加载失败 · 数量未知</button>
    </div>

    <!-- ① 全库资产与运行（2026-08-03 重设计：资产 hero 卡 + 状态分布 + 扁平 vitals + 趋势|文件类型）
         设计原则：只有决策数字配大卡,运行体征降为安静的 hairline 行——异常靠语义色点浮出。 -->
    <section :class="SECTION">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>全库资产与运行</header>
      <LoadError class="mb-3" :message="loadErrors['stats']" @retry="loadStats()" />
      <div :class="GRID">
        <StatCard v-for="s in assetCards" :key="s.label" v-bind="s" :loading="statsLoading" />
      </div>
      <!-- ml-0.5 已移除（2026-08-04）：本处曾是全应用唯一一个在调用点给 SUBHEAD 加 2px 左边距的
           实例，而 DeptDashboard 是把同样的 2px 烤进了它自己的常量——同一偏移两条互不知情的路径。
           抽取共享常量时统一取多数（17:3）的无偏移版，20 个 SUBHEAD 实例自此同一基准。 -->
      <p :class="SUBHEAD" class="mt-4">状态分布</p>
      <StatusDistBar :by-badge="kbStats?.by_badge || {}" />
      <template v-if="kbGovernance">
        <div class="mt-4 grid gap-x-10 lg:grid-cols-2">
          <div>
            <p :class="SUBHEAD">运行健康</p>
            <VitalsList :items="vitalsHealth" />
          </div>
          <div class="mt-4 lg:mt-0">
            <p :class="SUBHEAD">服务可用性</p>
            <VitalsList :items="vitalsAvailability" />
            <p :class="SUBHEAD" class="mt-3">治理风险</p>
            <VitalsList :items="vitalsRisk" />
          </div>
        </div>
        <div class="mt-5 grid gap-x-8 gap-y-4 border-t border-border/70 pt-4 lg:grid-cols-2">
          <div>
            <p :class="SUBHEAD">近期入库趋势（嵌入块数）</p>
            <MiniTrend :items="embedTrend" empty="近期无入库批次记录。" />
          </div>
          <div>
            <div class="mb-2 flex items-baseline justify-between gap-2">
              <span class="text-[12.5px] font-medium text-muted-foreground">文件类型分布</span>
              <span class="font-mono text-[11px] tabular-nums text-faint">共 {{ fmtN(fileTotal) }} 篇</span>
            </div>
            <div v-if="fileSegments.length" class="flex h-4 w-full overflow-hidden rounded-md" role="img"
                 :aria-label="`文件类型：${fileSegments.map((f) => `${f.label} ${f.value} 篇`).join('，')}`">
              <div v-for="f in fileSegments" :key="f.label" :style="`width:${Math.max(f.sharePct, 1.5)}%; ${f.style}`"
                   :title="`${f.label} · ${f.value} 篇`" />
            </div>
            <p v-if="fileSegments.length" class="mt-1.5 text-[11px] text-faint">
              {{ fileSegments.map((f) => `${f.label} ${f.sharePct < 1 ? '<1' : f.sharePct.toFixed(0)}%`).join(' · ') }}
            </p>
            <p v-else class="text-[12px] text-muted-foreground">暂无文件。</p>
          </div>
        </div>
      </template>
    </section>

    <!-- ② 组织覆盖（签名区）：归属轴=组织树。中心行卷积可展开;覆盖条+树表合并原三件套。 -->
    <section v-if="kbGovernance" :class="SECTION">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>组织覆盖</header>
      <OrgCoverageTable :rows="kbGovernance.dept_coverage" />
    </section>

    <!-- 全库知识效果 -->
    <section v-if="kbGovernance || kbInsights" :class="SECTION">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>全库知识效果</header>
      <div v-if="effectCards.length" :class="GRID" class="mb-3">
        <StatCard v-for="s in effectCards" :key="s.label" v-bind="s" />
      </div>
      <!-- 最常被使用 | 高频未答好 —— 一个框两半 -->
      <div v-if="kbInsights" :class="SPLIT">
        <div class="p-[15px]">
          <p :class="SUBHEAD">最常被使用的知识<span class="ml-1.5 font-normal text-faint">近 30 天</span></p>
          <BarList bare :items="topDocItems" unit=" 问" empty="近期暂无检索记录。" />
        </div>
        <div class="p-[15px]">
          <div class="flex items-baseline justify-between gap-2">
            <p :class="SUBHEAD">高频未答好（待补充/改进）</p>
            <RouterLink to="/contribute" class="shrink-0 text-[11.5px] font-semibold text-accent-text transition hover:underline">去补充 →</RouterLink>
          </div>
          <BarList bare :items="gapItems" tone="bg-st-warn" unit=" 次" empty="近期无「召回但未答好」的提问。" />
        </div>
      </div>
    </section>

    <!-- 用户反馈与回答质量（卡 + 趋势|原因 收在同一个框里） -->
    <section v-if="kbGovernance" id="kb-dash-feedback" :class="SECTION" class="scroll-mt-4">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>用户反馈与回答质量</header>
      <!-- 按部门筛选（止于二级；归属=答案实际引用该部门文档；chip 计数恒全库不随筛选） -->
      <div class="mb-3 flex flex-wrap items-center gap-2" data-testid="fb-filter">
        <span class="text-[12px] text-muted-foreground">按部门筛选</span>
        <div class="w-64"><OrgTreeSelect v-model="fbFilterNode" mode="owner" placeholder="全部部门" /></div>
        <template v-if="fbOwnerKey">
          <span class="rounded-full border border-accent-strong bg-accent-soft px-2.5 py-0.5 text-[11.5px] font-medium text-accent-text">
            筛选：{{ fbFilterName }}</span>
          <button type="button" class="text-[11.5px] text-muted-foreground underline transition hover:text-foreground"
                  @click="clearFbFilter">清除</button>
        </template>
      </div>
      <p v-if="loadErrors['feedbackStats']" class="mb-3 rounded-lg border border-st-busy/40 bg-st-busy/10 px-3 py-2 text-[12px] text-st-busy"
         data-testid="fb-filter-error">
        {{ loadErrors['feedbackStats'] }}
      </p>
      <div :class="GRID" class="mb-3">
        <StatCard v-for="s in feedbackCards" :key="s.label" v-bind="s" />
      </div>
      <div :class="SPLIT">
        <div class="p-[15px]">
          <p :class="SUBHEAD">反馈趋势</p>
          <FeedbackTrend bare :days="fbFiltered ? fbStats!.daily : kbGovernance.feedback_daily" :last7="fbFiltered ? fbStats!.last7 : kbGovernance.feedback_last7" :total="fbFiltered ? fbStats!.total : kbGovernance.feedback_total" />
        </div>
        <div class="p-[15px]">
          <div class="mb-2 flex items-baseline justify-between gap-2">
            <span class="text-[12.5px] font-medium text-muted-foreground">点踩原因分布</span>
            <span class="font-mono text-[11px] tabular-nums text-faint">共 {{ fmtN(fbFiltered ? fbStats!.down : kbGovernance.feedback_down) }} 条</span>
          </div>
          <ColumnChart :items="fbFiltered ? fbStats!.reasons.map((r) => ({ label: r.reason, value: r.count })) : downvoteItems" show-share :share-base="fbFiltered ? fbStats!.down : kbGovernance.feedback_down" color="var(--st-fail)" unit=" 次" empty="近期无点踩反馈。" />
          <p v-if="downvoteItems.length" class="mt-1 text-[11px] text-faint">占比 = 该原因 / 点踩总数；点踩可多选，故合计可超 100%。</p>
        </div>
      </div>
      <!-- 差评复核：逐条被点踩回答 + 涉及文档（全库；与上方聚合互补——这里能落到具体该修哪篇） -->
      <p :class="SUBHEAD" class="mt-4">差评复核 · 逐条（引用了库内文档）</p>
      <FeedbackReviewList :owner-key="fbOwnerKey" />
      <!-- 入库复审任务：spot_checker 权限抽查等安全网登记（P2-33 消费端，kb_admin 专属） -->
      <p :class="SUBHEAD" class="mt-4">入库复审 · 安全网登记的人工任务</p>
      <ReviewTaskQueue />
    </section>

    <!-- 治理/洞察数据加载中（端点未接入）→ 如实占位；真实失败（5xx）→ 错误条 + 重试 -->
    <section v-if="!kbGovernance && !kbInsights" :class="SECTION">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>全库治理看板</header>
      <div v-if="loadErrors['governance'] || loadErrors['insights']" class="space-y-2">
        <LoadError :message="loadErrors['governance']" @retry="loadGovernance()" />
        <LoadError :message="loadErrors['insights']" @retry="loadInsights()" />
      </div>
      <div v-else class="rounded-[14px] border border-dashed border-border bg-surface/60 p-5 text-[12.5px] text-muted-foreground">
        运行健康 / 治理风险 / 部门覆盖 / 知识效果数据加载中（需后端
        <code class="font-mono text-[11.5px]">/api/kb/governance</code> 与
        <code class="font-mono text-[11.5px]">/api/kb/insights</code>）；稍后自动呈现。
      </div>
    </section>
  </div>
</template>
