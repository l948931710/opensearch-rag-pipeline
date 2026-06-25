<script setup lang="ts">
import { computed } from 'vue'
import { badgeTone } from '@/lib/kb'

// 文档状态徽章：文案来自后端 status_badge，颜色由 badgeTone 映射到 st-* 语义色。
const props = defineProps<{ badge: string }>()
const TONE: Record<string, string> = {
  live: 'text-st-live bg-st-live/10',
  busy: 'text-st-busy bg-st-busy/10',
  queue: 'text-st-queue bg-st-queue/10',
  warn: 'text-st-warn bg-st-warn/10',
  fail: 'text-st-fail bg-st-fail/10',
  muted: 'text-st-muted bg-st-muted/10',
}
const cls = computed(() => TONE[badgeTone(props.badge)] || TONE.muted)
</script>

<template>
  <span class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium" :class="cls">{{ badge || '—' }}</span>
</template>
