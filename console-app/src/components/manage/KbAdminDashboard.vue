<script setup lang="ts">
import { computed } from 'vue'
import {
  FileText, CheckCircle2, Archive, Clock, Database, GitBranch, Timer, Cpu,
  ShieldAlert, ShieldCheck, ThumbsUp, Headset, Percent,
} from 'lucide-vue-next'
import { useKb } from '@/composables/useKb'
import { deptLabel } from '@/lib/kb'
import StatusDistBar from './StatusDistBar.vue'
import BarList from './BarList.vue'

// 知识库管理员「概览看板」= 全库视角。资产/状态取 /api/kb/stats（kb_admin 不限作用域）+ 待审批
// /pending-approvals；运行健康/治理风险/部门覆盖取 /api/kb/governance；使用成效取 /api/kb/insights。
// 全部真实口径，无对应数据则如实显空 —— 绝不造数。
const { kbStats, approvals, kbGovernance, kbInsights } = useKb()
const b = (k: string) => kbStats.value?.by_badge?.[k] || 0
const cards = computed(() => [
  { key: 'total', label: '文档总数', value: kbStats.value?.total ?? 0, hint: '全部门 · 有效及处理中', icon: FileText, tone: 'text-foreground' },
  { key: 'live', label: '已上线', value: b('已上线'), hint: '当前可被检索', icon: CheckCircle2, tone: 'text-st-live' },
  { key: 'retired', label: '已退役', value: kbStats.value?.retired ?? 0, hint: '已下线文档', icon: Archive, tone: 'text-st-muted' },
  { key: 'pending', label: '待审批', value: approvals.value.length, hint: '公开/跨组 待放行', icon: Clock, tone: 'text-st-busy' },
])

const ms2s = (ms?: number) => (ms ? (ms / 1000).toFixed(1) + 's' : '—')
const pct = (x?: number) => (x === undefined ? '—' : (x * 100).toFixed(1) + '%')

// 运行健康
const healthCards = computed(() => {
  const g = kbGovernance.value
  const maxFail = Math.max(0, ...(g?.embed_runs || []).map((r) => r.fail_rate))
  return [
    { key: 'idx', label: '已索引文档', value: g?.docs_in_index ?? 0, hint: `共 ${g?.docs_active ?? 0} 在线`, icon: Database, tone: 'text-st-live' },
    { key: 'dual', label: '双版本残留', value: g?.dual_version_docs ?? 0, hint: (g?.dual_version_docs ? '需排查' : '不变量健康'), icon: GitBranch, tone: g?.dual_version_docs ? 'text-st-fail' : 'text-st-live' },
    { key: 'lat', label: '端到端延迟 p95', value: ms2s(g?.p95_latency_ms), hint: `检索 ${ms2s(g?.avg_retrieval_ms)}·生成 ${ms2s(g?.avg_llm_ms)}·含渲染`, icon: Timer, tone: 'text-foreground' },
    { key: 'embed', label: '嵌入失败率', value: pct(maxFail), hint: '近 8 次入库最差', icon: Cpu, tone: maxFail > 0 ? 'text-st-warn' : 'text-st-live' },
  ]
})
const embedItems = computed(() =>
  (kbGovernance.value?.embed_runs || []).map((r) => ({ label: r.bizdate, sub: `失败率 ${pct(r.fail_rate)}`, value: r.embedded })))

// 治理风险 / 知识效果
const riskCards = computed(() => {
  const g = kbGovernance.value
  const noAnswerRate = (g && g.answer_total) ? (g.answer_no_result + g.answer_refusal) / g.answer_total : undefined
  return [
    { key: 'redact', label: 'PII 已脱敏', value: g?.pii_redacted_docs ?? 0, hint: '含敏感信息文档', icon: ShieldCheck, tone: 'text-st-busy' },
    { key: 'quar', label: 'PII 隔离', value: g?.pii_quarantined_docs ?? 0, hint: '高风险未入库', icon: ShieldAlert, tone: (g?.pii_quarantined_docs ? 'text-st-warn' : 'text-st-muted') },
    { key: 'noans', label: '未答出率', value: pct(noAnswerRate), hint: '无结果 + 拒答', icon: Percent, tone: 'text-st-warn' },
    { key: 'esc', label: '转人工', value: g?.escalations ?? 0, hint: '用户求助工单 · 累计', icon: Headset, tone: 'text-foreground' },
  ]
})
const effectCards = computed(() => {
  const g = kbGovernance.value
  return [
    { key: 'eff', label: '有效回答率', value: pct(g?.effective_rate), hint: `近 ${g?.window_days ?? 30} 天全库`, icon: CheckCircle2, tone: 'text-st-live' },
    { key: 'fb', label: '好评率', value: pct(g?.helpful_rate), hint: `${g?.feedback_up ?? 0} 赞 / ${g?.feedback_total ?? 0} 反馈 · 累计`, icon: ThumbsUp, tone: 'text-accent-text' },
  ]
})

// 部门覆盖 / 使用失衡（文档数 vs 被检索热度，两条各自归一更直观）
const coverageDocs = computed(() =>
  [...(kbGovernance.value?.dept_coverage || [])].sort((a, c) => c.docs - a.docs)
    .map((d) => ({ label: deptLabel(d.owner_dept), value: d.docs })))
const coverageUsage = computed(() =>
  [...(kbGovernance.value?.dept_coverage || [])].sort((a, c) => c.qa_hits - a.qa_hits)
    .map((d) => ({ label: deptLabel(d.owner_dept), value: d.qa_hits })))

// 知识效果：全库最常被使用 / 高频未答好（取自 insights 全库口径）
const topDocItems = computed(() =>
  (kbInsights.value?.top_docs || []).map((d) => ({ label: d.title, sub: deptLabel(d.owner_dept), value: d.hits })))
const gapItems = computed(() =>
  (kbInsights.value?.gap_queries || []).map((g) => ({ label: g.query, sub: `平均相关度 ${g.avg_top.toFixed(2)}`, value: g.count })))
</script>

<template>
  <div class="space-y-6">
    <!-- 全库资产概览 -->
    <section>
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">全库资产概览</p>
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

    <!-- 状态分布（真实，全库） -->
    <section>
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">状态分布</p>
      <StatusDistBar :by-badge="kbStats?.by_badge || {}" />
    </section>

    <template v-if="kbGovernance">
      <!-- 运行健康 -->
      <section>
        <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">运行健康</p>
        <div class="kb-cards grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div v-for="s in healthCards" :key="s.key" class="kb-card rounded-[14px] border border-border bg-card p-[15px]">
            <div class="mb-2.5 flex items-center gap-2">
              <span class="grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft" :class="s.tone">
                <component :is="s.icon" :size="15" :stroke-width="1.75" />
              </span>
              <span class="truncate text-[12.5px] font-medium text-muted-foreground">{{ s.label }}</span>
            </div>
            <div class="font-mono text-[26px] font-bold leading-none tracking-tight tabular-nums" :class="s.tone">{{ s.value }}</div>
            <div class="mt-1.5 truncate text-[11.5px] text-faint">{{ s.hint }}</div>
          </div>
        </div>
        <p class="mb-2 ml-0.5 mt-4 text-[12.5px] font-medium text-muted-foreground">近期入库批次（嵌入块数）</p>
        <BarList :items="embedItems" unit=" 块" empty="近期无入库批次记录。" />
      </section>

      <!-- 治理风险 -->
      <section>
        <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">治理风险</p>
        <div class="kb-cards grid grid-cols-2 gap-3 sm:grid-cols-4">
          <div v-for="s in riskCards" :key="s.key" class="kb-card rounded-[14px] border border-border bg-card p-[15px]">
            <div class="mb-2.5 flex items-center gap-2">
              <span class="grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft" :class="s.tone">
                <component :is="s.icon" :size="15" :stroke-width="1.75" />
              </span>
              <span class="truncate text-[12.5px] font-medium text-muted-foreground">{{ s.label }}</span>
            </div>
            <div class="font-mono text-[26px] font-bold leading-none tracking-tight tabular-nums" :class="s.tone">{{ s.value }}</div>
            <div class="mt-1.5 truncate text-[11.5px] text-faint">{{ s.hint }}</div>
          </div>
        </div>
      </section>

      <!-- 部门覆盖 / 使用失衡 -->
      <section>
        <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">部门覆盖 / 使用失衡</p>
        <div class="grid gap-3 sm:grid-cols-2">
          <div>
            <p class="mb-2 ml-0.5 text-[12.5px] font-medium text-muted-foreground">文档覆盖（各部门文档数）</p>
            <BarList :items="coverageDocs" empty="暂无文档。" />
          </div>
          <div>
            <p class="mb-2 ml-0.5 text-[12.5px] font-medium text-muted-foreground">使用热度（各部门被检索次数）</p>
            <BarList :items="coverageUsage" tone="bg-st-busy" empty="近期无检索记录。" />
          </div>
        </div>
        <p class="ml-0.5 mt-2 text-[11.5px] text-faint">
          覆盖多≠用得多：两侧对照可看出「文档多但少人问」与「文档少却高频被检索」的失衡部门，指导补充优先级。
        </p>
      </section>
    </template>

    <!-- 知识效果（效果卡取自 governance；最常被使用 / 高频未答好取自 insights）。
         两数据源独立加载：卡片只在 kbGovernance 就绪时渲染（否则不显「0 赞 / 0 反馈」伪空），
         列表只在 kbInsights 就绪时渲染——各自缺数据时该子块整体不出，绝不造数。 -->
    <section v-if="kbGovernance || kbInsights">
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">知识效果</p>
      <div v-if="kbGovernance" class="kb-cards mb-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div v-for="s in effectCards" :key="s.key" class="kb-card rounded-[14px] border border-border bg-card p-[15px]">
          <div class="mb-2.5 flex items-center gap-2">
            <span class="grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft" :class="s.tone">
              <component :is="s.icon" :size="15" :stroke-width="1.75" />
            </span>
            <span class="truncate text-[12.5px] font-medium text-muted-foreground">{{ s.label }}</span>
          </div>
          <div class="font-mono text-[26px] font-bold leading-none tracking-tight tabular-nums" :class="s.tone">{{ s.value }}</div>
          <div class="mt-1.5 truncate text-[11.5px] text-faint">{{ s.hint }}</div>
        </div>
      </div>
      <div v-if="kbInsights" class="grid gap-3 sm:grid-cols-2">
        <div>
          <p class="mb-2 ml-0.5 text-[12.5px] font-medium text-muted-foreground">最常被使用的知识</p>
          <BarList :items="topDocItems" unit=" 问" empty="近期暂无检索记录。" />
        </div>
        <div>
          <p class="mb-2 ml-0.5 text-[12.5px] font-medium text-muted-foreground">高频未答好（待补充/改进）</p>
          <BarList :items="gapItems" tone="bg-st-warn" unit=" 次" empty="近期无「召回但未答好」的提问。" />
        </div>
      </div>
    </section>

    <!-- 治理数据加载中（端点未接入）→ 如实占位 -->
    <section v-if="!kbGovernance && !kbInsights">
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">全库治理看板</p>
      <div class="rounded-[14px] border border-dashed border-border bg-card/60 p-5 text-[12.5px] text-muted-foreground">
        运行健康 / 治理风险 / 部门覆盖数据加载中（需后端
        <code class="font-mono text-[11.5px]">/api/kb/governance</code> 与
        <code class="font-mono text-[11.5px]">/api/kb/insights</code>）；稍后自动呈现。
      </div>
    </section>
  </div>
</template>
