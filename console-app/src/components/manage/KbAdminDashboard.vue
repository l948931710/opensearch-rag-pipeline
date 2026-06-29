<script setup lang="ts">
import { computed } from 'vue'
import {
  Database, CheckCircle2, Archive, Clock, GitBranch, Timer, Cpu,
  ShieldAlert, ShieldCheck, ThumbsUp, ThumbsDown, Headset, Percent, Quote, MessageSquare, Ban,
  Search, AlertTriangle,
} from 'lucide-vue-next'
import { useKb } from '@/composables/useKb'
import { deptLabel } from '@/lib/kb'
import StatusDistBar from './StatusDistBar.vue'
import StatCard from './StatCard.vue'
import BarList from './BarList.vue'
import ColumnChart from './ColumnChart.vue'
import DeptTable from './DeptTable.vue'
import FeedbackTrend from './FeedbackTrend.vue'
import DonutChart from './DonutChart.vue'
import MiniTrend from './MiniTrend.vue'
import LoadError from './LoadError.vue'

// 知识库管理员「概览看板」= 全库视角（对齐 Atlas 设计分区）。资产/状态取 /api/kb/stats、待审批
// /pending-approvals；运行健康+治理风险+部门覆盖取 /api/kb/governance；知识效果取 /api/kb/insights。
// 全部真实口径，无对应数据则如实显空 —— 绝不造数。
const { kbStats, approvals, kbGovernance, kbInsights, loadStats, loadGovernance, loadInsights, loadErrors } = useKb()
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

// ── 全库运行健康：入库成功率 / 数据一致性 / 嵌入失败率 / 问答延迟 p95（检索可用率移入「服务可用性」）──
const healthCards = computed<Card[]>(() => {
  const g = kbGovernance.value
  const maxFail = Math.max(0, ...(g?.embed_runs || []).map((r) => r.fail_rate))
  const ingest = (g && g.docs_active) ? g.docs_in_index / g.docs_active : undefined
  const dual = g?.dual_version_docs ?? 0
  const consistency = (g && g.docs_in_index) ? (g.docs_in_index - dual) / g.docs_in_index : undefined
  return [
    { label: '入库成功率', value: pct(ingest), icon: CheckCircle2, tone: 'text-st-live', hint: `${g?.docs_in_index ?? 0}/${g?.docs_active ?? 0} 已索引上线` },
    { label: '数据一致性', value: pct(consistency), icon: GitBranch, tone: dual ? 'text-st-warn' : 'text-st-live', hint: dual ? `${dual} 文档双版本残留` : '无双版本残留' },
    { label: '嵌入失败率', value: pct(maxFail), icon: Cpu, tone: maxFail > 0 ? 'text-st-warn' : 'text-st-live', hint: '近 8 次入库最差' },
    { label: '问答延迟 p95', value: ms2s(g?.p95_latency_ms), icon: Timer, tone: 'text-foreground', hint: `p50 ${ms2s(g?.p50_latency_ms)} · 含流式渲染` },
  ]
})
// 近期入库趋势（纵向柱，最新在右）：bizdate 取 MM-DD，值 = 嵌入块数；带失败计数 → 条顶红盖。
const embedTrend = computed(() =>
  [...(kbGovernance.value?.embed_runs || [])].reverse().map((r) => {
    const d = (r.bizdate || '').replace(/\D/g, '')
    return { label: d.length >= 4 ? `${d.slice(-4, -2)}-${d.slice(-2)}` : (r.bizdate || ''), value: r.embedded, failed: r.failed, failRate: r.fail_rate }
  }))
// ── 服务可用性 ──
const availabilityCards = computed<Card[]>(() => {
  const g = kbGovernance.value
  return [
    { label: '问答 API 成功率', value: pct(g?.qa_api_success_rate), icon: CheckCircle2, tone: 'text-st-live', hint: `近 ${g?.window_days ?? 30} 天 · ${fmtN(g?.qa_total_30d)} 次` },
    { label: '检索 API 成功率', value: pct(g?.retrieval_api_success_rate), icon: Search, tone: 'text-st-live', hint: '检索正常返回占比' },
    { label: '近 24h 错误数', value: g?.errors_24h ?? 0, icon: AlertTriangle, tone: g?.errors_24h ? 'text-st-fail' : 'text-st-live', hint: '失败请求 · DashScope/HA3' },
  ]
})
// ── 全库资产概览扩展：各部门文档数分布（识别偏科）+ 文件类型分布 ──
const deptDocItems = computed(() =>
  [...(kbGovernance.value?.dept_coverage || [])].sort((a, c) => c.docs - a.docs)
    .map((d) => ({ label: deptLabel(d.owner_dept), value: d.docs, delta: d.wow_net ?? null, deltaPct: d.wow_total ?? null })))
// 各部门使用量（命中提问数，近 30 天）+ 使用量周环比（本周 vs 上周）——按使用量降序。
const deptUsageItems = computed(() =>
  [...(kbGovernance.value?.dept_coverage || [])].sort((a, c) => c.qa_hits - a.qa_hits)
    .map((d) => ({ label: deptLabel(d.owner_dept), value: d.qa_hits, delta: d.qa_wow_net ?? null, deltaPct: d.qa_wow ?? null })))
const fileTypeItems = computed(() => (kbGovernance.value?.file_types || []).map((f) => ({ label: f.ftype, value: f.count })))
const fileTotal = computed(() => (kbGovernance.value?.file_types || []).reduce((s, f) => s + f.count, 0))
const riskCards = computed<Card[]>(() => {
  const g = kbGovernance.value
  // 未答出率移除：与「全库知识效果 · 无答案率」重复（同一 (无结果+拒答)/总 口径）。
  return [
    { label: 'PII 已脱敏', value: g?.pii_redacted_docs ?? 0, icon: ShieldCheck, tone: 'text-st-busy', hint: '含敏感信息文档' },
    { label: 'PII 隔离', value: g?.pii_quarantined_docs ?? 0, icon: ShieldAlert, tone: g?.pii_quarantined_docs ? 'text-st-warn' : 'text-st-muted', hint: '高风险未入库' },
    { label: '转人工', value: g?.escalations ?? 0, icon: Headset, tone: 'text-foreground', hint: '用户求助工单 · 累计' },
  ]
})

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
  (kbInsights.value?.top_docs || []).map((d) => ({ label: d.title, sub: deptLabel(d.owner_dept), value: d.hits })))
const gapItems = computed(() =>
  (kbInsights.value?.gap_queries || []).map((g) => ({ label: g.query, sub: `平均相关度 ${g.avg_top.toFixed(2)}`, value: g.count })))

// ── 用户反馈与回答质量 ──
const feedbackCards = computed<Card[]>(() => {
  const g = kbGovernance.value
  const coverage = (g && g.answer_total) ? g.feedback_total / g.answer_total : undefined
  return [
    { label: '点赞', value: fmtN(g?.feedback_up), icon: ThumbsUp, tone: 'text-st-live', hint: '用户认可的回答' },
    { label: '点踩', value: fmtN(g?.feedback_down), icon: ThumbsDown, tone: 'text-st-fail', hint: '用户标记的问题' },
    { label: '正反馈率', value: pct(g?.helpful_rate), icon: Percent, tone: 'text-accent-text', hint: '赞 /(赞+踩)' },
    { label: '反馈覆盖率', value: pct(coverage), icon: MessageSquare, tone: 'text-foreground', hint: '反馈数 / 回答数' },
  ]
})
const downvoteItems = computed(() =>
  (kbGovernance.value?.downvote_reasons || []).map((r) => ({ label: r.reason, value: r.count })))

// 每个主分区 = 一个有边框的「区域面板」（暖底 bg-panel + 描边），白色指标卡浮于其上（卡上区的标准看板分层）。
const SECTION = 'rounded-2xl border border-border bg-panel/60 p-4 sm:p-5'
// 区域标题：绿色竖条 + 区名 + 下方细分隔线（替代原 uppercase「眉标」，更像真标题而非装饰）。
const ZONE_HEAD = 'mb-4 flex items-center gap-2 border-b border-border/70 pb-3 text-[13px] font-semibold tracking-tight text-foreground'
const ZONE_TICK = 'h-3.5 w-1 shrink-0 rounded-full bg-accent-strong'
const SUBHEAD = 'mb-2 text-[12.5px] font-medium text-muted-foreground'
const GRID = 'kb-cards grid grid-cols-2 gap-3 sm:grid-cols-4'
const GRID3 = 'kb-cards grid grid-cols-2 gap-3 sm:grid-cols-3'   // 3 张卡的区（服务可用性 / 治理风险）：N 项 N 格，不留空格
// 成对子项收进「一个框、两半、中间竖线分隔」的共享面板（对齐设计：趋势|原因、最常用|未答好）。
// 嵌在区域面板里 → 用纯白 bg-surface 与暖底面板拉开层次。
const SPLIT = 'grid overflow-hidden rounded-2xl border border-border bg-surface divide-y divide-border sm:grid-cols-2 sm:divide-y-0 sm:divide-x'
</script>

<template>
  <div class="space-y-6">
    <!-- 全库资产概览（含状态分布 + 部门覆盖情况） -->
    <section :class="SECTION">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>全库资产概览</header>
      <LoadError class="mb-3" :message="loadErrors['stats']" @retry="loadStats()" />
      <div :class="GRID">
        <StatCard v-for="s in assetCards" :key="s.label" v-bind="s" />
      </div>
      <!-- 各部门文档数分布（偏科）| 文件类型分布 —— 一个框两半 -->
      <div v-if="kbGovernance" :class="SPLIT" class="mt-4">
        <div class="p-[15px]">
          <div class="mb-2 flex items-baseline justify-between gap-2">
            <span class="text-[12.5px] font-medium text-muted-foreground">各部门文档数分布</span>
            <span class="text-[11px] text-faint">▲▼ = 本周净变化（篇）</span>
          </div>
          <ColumnChart :items="deptDocItems" unit=" 篇" empty="暂无文档。" />
          <p class="mt-1 text-[11px] text-faint">柱高悬殊 = 知识偏科：少数部门撑起大部分语料。</p>
        </div>
        <div class="flex flex-col p-[15px]">
          <p :class="SUBHEAD">文件类型分布</p>
          <DonutChart :items="fileTypeItems" :center-value="fmtN(fileTotal)" center-label="篇" class="my-auto" empty="暂无文件。" />
        </div>
      </div>
    </section>

    <!-- 全库运行健康（状态分布 + 健康卡 + 近期入库趋势 + 部门覆盖与失衡 + 治理风险） -->
    <!-- 状态分布按 kbStats（治理未就绪也显）；其余指标治理就绪才显（无对应数据如实空，不造数）。 -->
    <section v-if="kbGovernance || kbStats" :class="SECTION">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>全库运行健康</header>
      <p :class="SUBHEAD" class="ml-0.5">状态分布</p>
      <StatusDistBar :by-badge="kbStats?.by_badge || {}" />
      <template v-if="kbGovernance">
        <div :class="GRID" class="mt-4">
          <StatCard v-for="s in healthCards" :key="s.label" v-bind="s" />
        </div>
        <p :class="SUBHEAD" class="ml-0.5 mt-4">近期入库趋势（嵌入块数）</p>
        <div class="rounded-2xl border border-border bg-surface p-[15px]">
          <MiniTrend :items="embedTrend" empty="近期无入库批次记录。" />
        </div>
        <p :class="SUBHEAD" class="ml-0.5 mt-5">部门覆盖与失衡</p>
        <DeptTable :rows="kbGovernance.dept_coverage" />
        <p class="ml-0.5 mb-1 mt-1.5 text-[11.5px] text-faint">
          覆盖多≠用得多：对照「已上线 vs 使用量」找出失衡部门；「无答案率」高 = 该部门文档被问到却答不好，「风险」= 含敏感信息文档数。
        </p>
        <p :class="SUBHEAD" class="ml-0.5 mt-5">治理风险</p>
        <div :class="GRID3">
          <StatCard v-for="s in riskCards" :key="s.label" v-bind="s" />
        </div>
      </template>
    </section>

    <!-- 服务可用性 -->
    <section v-if="kbGovernance" :class="SECTION">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>服务可用性</header>
      <div :class="GRID3">
        <StatCard v-for="s in availabilityCards" :key="s.label" v-bind="s" />
      </div>
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
          <p :class="SUBHEAD">最常被使用的知识</p>
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
      <!-- 各部门使用量 + 使用量周环比（命中提问数；柱=近30天、徽标=本周净变化） -->
      <div v-if="deptUsageItems.length" class="mt-3 rounded-2xl border border-border bg-surface p-[15px]">
        <div class="mb-2 flex items-baseline justify-between gap-2">
          <span class="text-[12.5px] font-medium text-muted-foreground">各部门使用量（近 30 天）</span>
          <span class="text-[11px] text-faint">▲▼ = 本周使用量净变化（次）</span>
        </div>
        <ColumnChart :items="deptUsageItems" unit=" 次" empty="近期无检索记录。" />
        <p class="mt-1 text-[11px] text-faint">使用量 = 命中本部门文档的提问数；对照「各部门文档数」找覆盖多但用得少的部门。</p>
      </div>
    </section>

    <!-- 用户反馈与回答质量（卡 + 趋势|原因 收在同一个框里） -->
    <section v-if="kbGovernance" :class="SECTION">
      <header :class="ZONE_HEAD"><span :class="ZONE_TICK"></span>用户反馈与回答质量</header>
      <div :class="GRID" class="mb-3">
        <StatCard v-for="s in feedbackCards" :key="s.label" v-bind="s" />
      </div>
      <div :class="SPLIT">
        <div class="p-[15px]">
          <p :class="SUBHEAD">反馈趋势</p>
          <FeedbackTrend bare :days="kbGovernance.feedback_daily" :last7="kbGovernance.feedback_last7" :total="kbGovernance.feedback_total" />
        </div>
        <div class="p-[15px]">
          <div class="mb-2 flex items-baseline justify-between gap-2">
            <span class="text-[12.5px] font-medium text-muted-foreground">点踩原因分布</span>
            <span class="font-mono text-[11px] tabular-nums text-faint">共 {{ fmtN(kbGovernance.feedback_down) }} 条</span>
          </div>
          <ColumnChart :items="downvoteItems" show-share :share-base="kbGovernance.feedback_down" color="var(--st-fail)" unit=" 次" empty="近期无点踩反馈。" />
          <p v-if="downvoteItems.length" class="mt-1 text-[11px] text-faint">占比 = 该原因 / 点踩总数；点踩可多选，故合计可超 100%。</p>
        </div>
      </div>
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
