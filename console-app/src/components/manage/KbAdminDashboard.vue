<script setup lang="ts">
import { computed } from 'vue'
import { FileText, CheckCircle2, Archive, Clock, Construction } from 'lucide-vue-next'
import { useKb } from '@/composables/useKb'
import StatusDistBar from './StatusDistBar.vue'

// 知识库管理员「概览看板」= 全库视角。仅用已有真实口径（/api/kb/stats，kb_admin 不限作用域）
// + 待审批数（/pending-approvals）。无对应接口的指标（运行健康/治理风险/部门失衡/知识效果/反馈）
// 一律【不造数】，以「建设中」如实占位，待分析接口落地后接入。
const { kbStats, approvals } = useKb()
const b = (k: string) => kbStats.value?.by_badge?.[k] || 0
const cards = computed(() => [
  { key: 'total', label: '文档总数', value: kbStats.value?.total ?? 0, hint: '全部门 · 有效及处理中', icon: FileText, tone: 'text-foreground' },
  { key: 'live', label: '已上线', value: b('已上线'), hint: '当前可被检索', icon: CheckCircle2, tone: 'text-st-live' },
  { key: 'retired', label: '已退役', value: kbStats.value?.retired ?? 0, hint: '已下线文档', icon: Archive, tone: 'text-st-muted' },
  { key: 'pending', label: '待审批', value: approvals.value.length, hint: '公开/跨组 待放行', icon: Clock, tone: 'text-st-busy' },
])
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

    <!-- 待接入的全库看板（不造数，如实占位） -->
    <section>
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">全库治理看板</p>
      <div class="rounded-[14px] border border-dashed border-border bg-card/60 p-5">
        <div class="flex items-center gap-2 text-sm font-medium text-foreground">
          <Construction :size="15" :stroke-width="1.75" class="text-st-busy" /> 看板建设中
        </div>
        <p class="mt-1.5 text-[12.5px] leading-relaxed text-muted-foreground">
          运行健康（入库成功率 / 检索可用率 / 数据一致性）、治理风险、部门覆盖与失衡、知识效果与用户反馈等看板，
          将在接入对应分析接口后上线。当前仅展示已有真实口径的资产总量与状态分布。
        </p>
      </div>
    </section>
  </div>
</template>
