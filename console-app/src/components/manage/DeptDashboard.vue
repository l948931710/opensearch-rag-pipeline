<script setup lang="ts">
import { computed } from 'vue'
import { Database, CheckCircle2, Loader, Clock, MessageSquare, Percent, Users, Quote } from 'lucide-vue-next'
import { useKb } from '@/composables/useKb'
import { deptLabel } from '@/lib/kb'
import StatusDistBar from './StatusDistBar.vue'
import StatCard from './StatCard.vue'
import BarList from './BarList.vue'
import LoadError from './LoadError.vue'

// 部门管理员「概览看板」= 本部门视角。/api/kb/stats 已按 managed owner_dept 作用域聚合，
// 故资产/状态口径只覆盖本部门。「待审核」用 by_badge（= 我提交、待 kb_admin 放行的版本）。
// 使用成效/知识缺口取自 /api/kb/insights（retrieved_docs_json→doc_id→owner_dept 归属，本部门文档）。
const { kbStats, kbInsights, loadStats, loadInsights, loadErrors } = useKb()
const b = (k: string) => kbStats.value?.by_badge?.[k] || 0
const fmtN = (n?: number) => (n || 0).toLocaleString('en-US')
const pct = (x?: number) => (x === undefined ? '—' : (x * 100).toFixed(1) + '%')

interface Card {
  label: string; value: string | number; icon: any; tone?: string; hint?: string
  pill?: string; pillLabel?: string; subValue?: string; subLabel?: string
}

const cards = computed<Card[]>(() => {
  const st = kbStats.value
  const nm = st?.new_this_month ?? 0
  return [
    {
      label: '文档总数', value: st?.total ?? 0, icon: Database, tone: 'text-foreground', hint: '我管理范围内',
      pill: nm > 0 ? `+${fmtN(nm)}` : '', pillLabel: '本月新增',
      subValue: fmtN(st?.chunks ?? 0), subLabel: '已索引分块',
    },
    { label: '已上线', value: b('已上线'), icon: CheckCircle2, tone: 'text-st-live', hint: '可被检索' },
    { label: '处理中 / 排队', value: b('处理中') + b('排队中'), icon: Loader, tone: 'text-st-busy', hint: '入库处理中' },
    { label: '待审核', value: b('待审核'), icon: Clock, tone: 'text-st-warn', hint: '我提交、待放行' },
  ]
})

const usageCards = computed<Card[]>(() => {
  const i = kbInsights.value
  return [
    { label: '被提问', value: i?.questions ?? 0, icon: MessageSquare, tone: 'text-foreground', hint: '命中本部门文档' },
    { label: '有效回答率', value: pct(i?.effective_rate), icon: Percent, tone: 'text-st-live', hint: '成功 / 提问' },
    { label: '提问人数', value: i?.askers ?? 0, icon: Users, tone: 'text-foreground', hint: '不同员工' },
    { label: '被引用', value: i?.cited ?? 0, icon: Quote, tone: 'text-accent-text', hint: '实际作答引用' },
  ]
})
const topDocItems = computed(() =>
  (kbInsights.value?.top_docs || []).map((d) => ({ label: d.title, sub: deptLabel(d.owner_dept), value: d.hits })))
const gapItems = computed(() =>
  (kbInsights.value?.gap_queries || []).map((g) => ({ label: g.query, sub: `平均相关度 ${g.avg_top.toFixed(2)}`, value: g.count })))

const HEADER = 'mb-3 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint'
const SUBHEAD = 'mb-2 ml-0.5 text-[12.5px] font-medium text-muted-foreground'
const GRID = 'kb-cards grid grid-cols-2 gap-3 sm:grid-cols-4'
</script>

<template>
  <div class="space-y-7">
    <!-- 本部门概览（含状态分布） -->
    <section>
      <p :class="HEADER">概览</p>
      <LoadError class="mb-3" :message="loadErrors['stats']" @retry="loadStats()" />
      <div :class="GRID">
        <StatCard v-for="s in cards" :key="s.label" v-bind="s" />
      </div>
      <p :class="SUBHEAD" class="mt-4">状态分布</p>
      <StatusDistBar :by-badge="kbStats?.by_badge || {}" />
    </section>

    <!-- 使用成效（真实，近 N 天，本部门文档被使用情况） -->
    <section v-if="kbInsights">
      <p :class="HEADER">使用成效 · 近 {{ kbInsights.window_days }} 天（本部门文档）</p>
      <div :class="GRID">
        <StatCard v-for="s in usageCards" :key="s.label" v-bind="s" />
      </div>
      <p :class="SUBHEAD" class="mt-4">最常被检索的文档</p>
      <BarList :items="topDocItems" unit=" 问" empty="近期本部门文档暂无检索记录。" />
    </section>

    <!-- 知识缺口：未答好的提问（建议补充/改进对应文档） -->
    <section v-if="kbInsights">
      <p :class="HEADER">知识缺口 · 未答好的提问</p>
      <BarList
        :items="gapItems" tone="bg-st-warn" unit=" 次"
        empty="近期本部门文档无「召回但未答好」的提问 —— 覆盖良好。"
      />
      <p class="ml-0.5 mt-2 text-[11.5px] text-faint">
        这些问题命中了本部门文档却未能答好（拒答）—— 多为文档内容缺漏或表述不清，是最直接的补充/改进线索。
        <RouterLink to="/contribute" class="font-semibold text-accent-text transition hover:underline">去知识贡献补充 →</RouterLink>
      </p>
    </section>

    <!-- 使用数据尚未就绪（端点未接入/加载中）→ 如实占位，不显 0 误导；真实失败（5xx）→ 错误条 + 重试 -->
    <section v-else>
      <p :class="HEADER">使用成效 · 知识缺口</p>
      <LoadError :message="loadErrors['insights']" @retry="loadInsights()" />
      <div v-if="!loadErrors['insights']" class="rounded-[14px] border border-dashed border-border bg-card/60 p-5 text-[12.5px] text-muted-foreground">
        使用成效与知识缺口数据加载中（需后端 <code class="font-mono text-[11.5px]">/api/kb/insights</code>）；稍后自动呈现。
      </div>
    </section>
  </div>
</template>
