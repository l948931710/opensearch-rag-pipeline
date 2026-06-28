<script setup lang="ts">
import { computed } from 'vue'

// 分类「占比」环图（part-to-whole）：比横条更适合表达「分布/构成」。
// 配色：静默分类序列，刻意避开正向绿（点踩语境），全用 CSS 变量 → 自适应暗色。
const props = defineProps<{
  items: { label: string; value: number }[]
  centerValue?: string | number
  centerLabel?: string
  empty?: string
}>()

const PALETTE = ['var(--c-str)', 'var(--st-warn)', 'var(--c-num)', 'var(--st-busy)', 'var(--st-queue)', 'var(--st-muted)', 'var(--faint)']
const R = 42
const C = 2 * Math.PI * R

const segs = computed(() => {
  const items = props.items.filter((i) => i.value > 0).sort((a, b) => b.value - a.value)
  const total = items.reduce((s, i) => s + i.value, 0) || 1
  let acc = 0
  return items.map((it, i) => {
    const frac = it.value / total
    const seg = { ...it, pct: Math.round(frac * 100), color: PALETTE[i % PALETTE.length], len: frac * C, off: -acc }
    acc += frac * C
    return seg
  })
})
const hasData = computed(() => segs.value.length > 0)
</script>

<template>
  <div v-if="hasData" class="flex items-center gap-5">
    <div class="relative size-[112px] shrink-0">
      <svg viewBox="0 0 100 100" class="size-full -rotate-90" aria-hidden="true">
        <circle cx="50" cy="50" :r="R" fill="none" stroke="var(--panel)" stroke-width="13" />
        <circle
          v-for="(s, i) in segs" :key="i"
          cx="50" cy="50" :r="R" fill="none" :stroke="s.color" stroke-width="13"
          stroke-linecap="butt"
          :stroke-dasharray="`${s.len} ${C - s.len}`" :stroke-dashoffset="s.off"
        />
      </svg>
      <div class="absolute inset-0 flex flex-col items-center justify-center">
        <span class="font-mono text-[20px] font-bold leading-none tabular-nums text-foreground">{{ centerValue }}</span>
        <span v-if="centerLabel" class="mt-1 text-[10.5px] text-faint">{{ centerLabel }}</span>
      </div>
    </div>
    <ul class="min-w-0 flex-1 space-y-[5px]">
      <li v-for="(s, i) in segs" :key="i" class="flex items-center gap-2 text-[12px]">
        <span class="size-2.5 shrink-0 rounded-[3px]" :style="{ background: s.color }" />
        <span class="min-w-0 flex-1 truncate text-foreground" :title="s.label">{{ s.label }}</span>
        <span class="shrink-0 font-mono tabular-nums text-muted-foreground">{{ s.value }}</span>
        <span class="w-9 shrink-0 text-right font-mono tabular-nums text-faint">{{ s.pct }}%</span>
      </li>
    </ul>
  </div>
  <p v-else class="text-sm text-muted-foreground">{{ empty || '暂无数据。' }}</p>
</template>
