<script setup lang="ts">
import { computed } from 'vue'
import { deptLabel } from '@/lib/kb'
import type { KbDeptCoverage } from '@/composables/useKb'

// 部门覆盖与失衡表（设计版）：部门 / 已上线 / 本月新增 / 使用量 / 无答案率 / 风险。
// 无答案率 = 命中本部门文档的提问里 REFUSAL 占比；风险 = 含 PII（脱敏/隔离）文档数。
const props = defineProps<{ rows: KbDeptCoverage[] }>()
const pct = (x: number) => (x * 100).toFixed(0) + '%'
const naTone = (x: number) => (x >= 0.2 ? 'text-st-fail' : x >= 0.1 ? 'text-st-busy' : 'text-muted-foreground')
const riskTone = (n: number) => (n >= 100 ? 'text-st-busy' : n > 0 ? 'text-muted-foreground' : 'text-faint')
const sorted = computed(() => [...(props.rows ?? [])].sort((a, b) => b.qa_hits - a.qa_hits))
</script>

<template>
  <div class="overflow-x-auto rounded-[14px] border border-border bg-card">
    <table class="w-full min-w-[520px] border-collapse text-[12.5px]">
      <thead>
        <tr class="border-b border-border text-[11px] uppercase tracking-wide text-faint">
          <th class="px-3.5 py-2.5 text-left font-semibold">部门</th>
          <th class="px-3 py-2.5 text-right font-semibold">已上线</th>
          <th class="px-3 py-2.5 text-right font-semibold">本月新增</th>
          <th class="px-3 py-2.5 text-right font-semibold">使用量</th>
          <th class="px-3 py-2.5 text-right font-semibold">无答案率</th>
          <th class="px-3.5 py-2.5 text-right font-semibold">风险</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="d in sorted" :key="d.owner_dept" class="border-b border-border/60 last:border-0">
          <td class="px-3.5 py-2.5 font-medium text-foreground">{{ deptLabel(d.owner_dept) }}</td>
          <td class="px-3 py-2.5 text-right font-mono tabular-nums text-muted-foreground">{{ d.docs }}</td>
          <td class="px-3 py-2.5 text-right font-mono tabular-nums" :class="d.new_month ? 'text-accent-text' : 'text-faint'">{{ d.new_month ? '+' + d.new_month : '—' }}</td>
          <td class="px-3 py-2.5 text-right font-mono tabular-nums text-muted-foreground">{{ d.qa_hits }}</td>
          <td class="px-3 py-2.5 text-right font-mono font-semibold tabular-nums" :class="naTone(d.no_answer_rate)">{{ pct(d.no_answer_rate) }}</td>
          <td class="px-3.5 py-2.5 text-right font-mono tabular-nums" :class="riskTone(d.pii_docs)">{{ d.pii_docs }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!sorted.length" class="px-4 py-6 text-center text-sm text-muted-foreground">暂无部门数据。</p>
  </div>
</template>
