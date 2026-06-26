<script setup lang="ts">
import { computed } from 'vue'
import { badgeTone, qBadgeTone } from '@/lib/kb'

// 状态徽章：文案直显，颜色按 kind 取语义色 —— 文档徽章走 badgeTone，批量上传队列态走 qBadgeTone。
const props = defineProps<{ badge: string; kind?: 'doc' | 'queue' }>()
const TONE: Record<string, string> = {
  live: 'text-st-live bg-st-live/10',
  busy: 'text-st-busy bg-st-busy/10',
  queue: 'text-st-queue bg-st-queue/10',
  warn: 'text-st-warn bg-st-warn/10',
  fail: 'text-st-fail bg-st-fail/10',
  muted: 'text-st-muted bg-st-muted/10',
}
const cls = computed(() => TONE[(props.kind === 'queue' ? qBadgeTone : badgeTone)(props.badge)] || TONE.muted)
</script>

<template>
  <span class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium" :class="cls">{{ badge || '—' }}</span>
</template>
