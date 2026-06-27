<script setup lang="ts">
import { computed } from 'vue'
import { FileText, CheckCircle2, Loader, Clock, Construction } from 'lucide-vue-next'
import { useKb } from '@/composables/useKb'
import StatusDistBar from './StatusDistBar.vue'

// 部门管理员「概览看板」= 本部门视角。/api/kb/stats 已按 managed owner_dept 作用域聚合，
// 故此处全部口径都只覆盖本部门。「待审核」用 by_badge（= 我提交、待 kb_admin 放行的版本），
// 而非 approvals（pending-approvals 仅 kb_admin，部门管理员恒空 —— 修旧版恒显 0 的误导卡）。
const { kbStats } = useKb()
const b = (k: string) => kbStats.value?.by_badge?.[k] || 0
const cards = computed(() => [
  { key: 'total', label: '文档总数', value: kbStats.value?.total ?? 0, hint: '我管理范围内', icon: FileText, tone: 'text-foreground' },
  { key: 'live', label: '已上线', value: b('已上线'), hint: '可被检索', icon: CheckCircle2, tone: 'text-st-live' },
  { key: 'busy', label: '处理中 / 排队', value: b('处理中') + b('排队中'), hint: '入库处理中', icon: Loader, tone: 'text-st-busy' },
  { key: 'pending', label: '待审核', value: b('待审核'), hint: '我提交、待放行', icon: Clock, tone: 'text-st-warn' },
])
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

    <!-- 待接入：使用成效 / 知识缺口（不造数，如实占位） -->
    <section>
      <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">使用成效 · 知识缺口</p>
      <div class="rounded-[14px] border border-dashed border-border bg-card/60 p-5">
        <div class="flex items-center gap-2 text-sm font-medium text-foreground">
          <Construction :size="15" :stroke-width="1.75" class="text-st-busy" /> 看板建设中
        </div>
        <p class="mt-1.5 text-[12.5px] leading-relaxed text-muted-foreground">
          本部门使用成效（帮助用户数 / 有效回答率 / 被引用最多）与知识缺口（高频未命中、最需补充的知识），
          将在接入分析接口后上线。当前仅展示已有真实口径的文档资产与状态分布。
        </p>
      </div>
    </section>
  </div>
</template>
