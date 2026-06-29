<script setup lang="ts">
import { computed } from 'vue'
import { contribStateLabel, contribStateTone } from '@/lib/kb'

// 知识贡献 5 态徽章（state 码由后端 contribution_state 折叠 review/ingestion 两生命周期而来）。
const props = defineProps<{ state: string }>()
const TONE: Record<string, string> = {
  live: 'text-st-live bg-st-live/10',
  busy: 'text-st-busy bg-st-busy/10',
  warn: 'text-st-warn bg-st-warn/10',
  fail: 'text-st-fail bg-st-fail/10',
  muted: 'text-st-muted bg-st-muted/10',
}
const cls = computed(() => TONE[contribStateTone(props.state)] || TONE.muted)
const label = computed(() => contribStateLabel(props.state))
</script>

<template>
  <span class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium" :class="cls">{{ label }}</span>
</template>
