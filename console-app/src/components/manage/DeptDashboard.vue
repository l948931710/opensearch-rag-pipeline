<script setup lang="ts">
import { computed } from 'vue'
import { FileText, CheckCircle2, Loader, Clock, MessageSquare, Percent, Users, Quote } from 'lucide-vue-next'
import { useKb } from '@/composables/useKb'
import { deptLabel } from '@/lib/kb'
import StatusDistBar from './StatusDistBar.vue'
import BarList from './BarList.vue'

// 部门管理员「概览看板」= 本部门视角。/api/kb/stats 已按 managed owner_dept 作用域聚合，
// 故资产/状态口径只覆盖本部门。「待审核」用 by_badge（= 我提交、待 kb_admin 放行的版本）。
// 使用成效/知识缺口取自 /api/kb/insights（retrieved_docs_json→doc_id→owner_dept 归属，本部门文档）。
const { kbStats, kbInsights } = useKb()
const b = (k: string) => kbStats.value?.by_badge?.[k] || 0
const cards = computed(() => [
  { key: 'total', label: '文档总数', value: kbStats.value?.total ?? 0, hint: '我管理范围内', icon: FileText, tone: 'text-foreground' },
  { key: 'live', label: '已上线', value: b('已上线'), hint: '可被检索', icon: CheckCircle2, tone: 'text-st-live' },
  { key: 'busy', label: '处理中 / 排队', value: b('处理中') + b('排队中'), hint: '入库处理中', icon: Loader, tone: 'text-st-busy' },
  { key: 'pending', label: '待审核', value: b('待审核'), hint: '我提交、待放行', icon: Clock, tone: 'text-st-warn' },
])

const pct = (x?: number) => (x === undefined ? '—' : (x * 100).toFixed(1) + '%')
const usageCards = computed(() => {
  const i = kbInsights.value
  return [
    { key: 'q', label: '被提问', value: i?.questions ?? 0, hint: '命中本部门文档', icon: MessageSquare, tone: 'text-foreground' },
    { key: 'rate', label: '有效回答率', value: pct(i?.effective_rate), hint: '成功 / 提问', icon: Percent, tone: 'text-st-live' },
    { key: 'askers', label: '提问人数', value: i?.askers ?? 0, hint: '不同员工', icon: Users, tone: 'text-foreground' },
    { key: 'cited', label: '被引用', value: i?.cited ?? 0, hint: '实际作答引用', icon: Quote, tone: 'text-accent-text' },
  ]
})
const topDocItems = computed(() =>
  (kbInsights.value?.top_docs || []).map((d) => ({ label: d.title, sub: deptLabel(d.owner_dept), value: d.hits })))
const gapItems = computed(() =>
  (kbInsights.value?.gap_queries || []).map((g) => ({ label: g.query, sub: `平均相关度 ${g.avg_top.toFixed(2)}`, value: g.count })))
</script>

<template>
  <div class="space-y-6">
    <!-- 本部门概览 -->
    <section>
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">概览</p>
      <div class="kb-cards grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div v-for="s in cards" :key="s.key" class="kb-card rounded-[14px] border border-border bg-card p-[15px]">
          <div class="mb-2.5 flex items-center gap-2">
            <span class="grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft" :class="s.tone">
              <component :is="s.icon" :size="15" :stroke-width="1.75" />
            </span>
            <span class="truncate text-[12.5px] font-medium text-muted-foreground">{{ s.label }}</span>
          </div>
          <div class="font-mono text-[26px] font-bold leading-none tracking-tight tabular-nums" :class="s.tone">{{ s.value }}</div>
          <div class="mt-1.5 text-[11.5px] text-faint">{{ s.hint }}</div>
        </div>
      </div>
    </section>

    <!-- 状态分布（真实，本部门） -->
    <section>
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">状态分布</p>
      <StatusDistBar :by-badge="kbStats?.by_badge || {}" />
    </section>

    <!-- 使用成效（真实，近 N 天，本部门文档被使用情况） -->
    <section v-if="kbInsights">
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">
        使用成效 · 近 {{ kbInsights.window_days }} 天（本部门文档）
      </p>
      <div class="kb-cards grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div v-for="s in usageCards" :key="s.key" class="kb-card rounded-[14px] border border-border bg-card p-[15px]">
          <div class="mb-2.5 flex items-center gap-2">
            <span class="grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft" :class="s.tone">
              <component :is="s.icon" :size="15" :stroke-width="1.75" />
            </span>
            <span class="truncate text-[12.5px] font-medium text-muted-foreground">{{ s.label }}</span>
          </div>
          <div class="font-mono text-[26px] font-bold leading-none tracking-tight tabular-nums" :class="s.tone">{{ s.value }}</div>
          <div class="mt-1.5 text-[11.5px] text-faint">{{ s.hint }}</div>
        </div>
      </div>
      <p class="mb-2 ml-0.5 mt-4 text-[12.5px] font-medium text-muted-foreground">最常被检索的文档</p>
      <BarList :items="topDocItems" unit=" 问" empty="近期本部门文档暂无检索记录。" />
    </section>

    <!-- 知识缺口：未答好的提问（建议补充/改进对应文档） -->
    <section v-if="kbInsights">
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">
        知识缺口 · 未答好的提问
      </p>
      <BarList
        :items="gapItems" tone="bg-st-warn" unit=" 次"
        empty="近期本部门文档无「召回但未答好」的提问 —— 覆盖良好。"
      />
      <p class="ml-0.5 mt-2 text-[11.5px] text-faint">
        这些问题命中了本部门文档却未能答好（拒答）—— 多为文档内容缺漏或表述不清，是最直接的补充/改进线索。
      </p>
    </section>

    <!-- 使用数据尚未就绪（端点未接入/加载中）→ 如实占位，不显 0 误导 -->
    <section v-else>
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">使用成效 · 知识缺口</p>
      <div class="rounded-[14px] border border-dashed border-border bg-card/60 p-5 text-[12.5px] text-muted-foreground">
        使用成效与知识缺口数据加载中（需后端 <code class="font-mono text-[11.5px]">/api/kb/insights</code>）；稍后自动呈现。
      </div>
    </section>
  </div>
</template>
