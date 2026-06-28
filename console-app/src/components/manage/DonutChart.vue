<script setup lang="ts">
import { computed, ref } from 'vue'

// 占比环图（part-to-whole）：用于「构成/分布」——总量有意义、切片即整体的份额（如语料的文件类型构成）。
// 配色：静默分类序列，刻意避开正向绿（避免与点赞语境混淆），全用 CSS 变量 → 自适应暗色。
// 图例 ↔ 切片 hover 联动聚焦；不足 1% 显「<1%」而非误读为 0%。
const props = defineProps<{
  items: { label: string; value: number }[]
  centerValue?: string | number
  centerLabel?: string
  empty?: string
}>()

const PALETTE = ['var(--c-str)', 'var(--st-warn)', 'var(--c-num)', 'var(--st-busy)', 'var(--st-queue)', 'var(--st-muted)', 'var(--faint)']
const R = 42
const C = 2 * Math.PI * R
const hovered = ref<number | null>(null)

const segs = computed(() => {
  const items = props.items.filter((i) => i.value > 0).sort((a, b) => b.value - a.value)
  const total = items.reduce((s, i) => s + i.value, 0) || 1
  const gap = items.length > 1 ? 1.4 : 0   // 切片间细缝（C 单位），单切片不开缝
  let acc = 0
  return items.map((it, i) => {
    const frac = it.value / total
    const rounded = Math.round(frac * 100)
    // 任何非零切片至少留一道发丝弧（≥0.8）→ 图例显「<1%」时环上不会真消失（自相矛盾）。
    const len = Math.max(0.8, frac * C - gap)
    // <1% 显「<1」；99.x% 不四舍成「100」(否则把非整体说成整体)，封 99。
    const pctLabel = rounded === 0 ? '<1' : rounded === 100 && frac < 1 ? '99' : String(rounded)
    const seg = { ...it, pctLabel, color: PALETTE[i % PALETTE.length], len, off: -acc }
    acc += frac * C
    return seg
  })
})
const hasData = computed(() => segs.value.length > 0)
const dim = (i: number) => hovered.value !== null && hovered.value !== i
</script>

<template>
  <div v-if="hasData" class="flex items-center gap-5">
    <div class="kb-in relative size-[116px] shrink-0">
      <svg viewBox="0 0 100 100" class="size-full -rotate-90" aria-hidden="true">
        <circle cx="50" cy="50" :r="R" fill="none" stroke="var(--panel)" stroke-width="13" />
        <circle
          v-for="(s, i) in segs" :key="i"
          class="kb-seg" cx="50" cy="50" :r="R" fill="none" :stroke="s.color"
          :stroke-width="hovered === i ? 16 : 13" :opacity="dim(i) ? 0.32 : 1"
          stroke-linecap="butt"
          :stroke-dasharray="`${s.len} ${C - s.len}`" :stroke-dashoffset="s.off"
          @mouseenter="hovered = i" @mouseleave="hovered = null"
        />
      </svg>
      <div class="absolute inset-0 flex flex-col items-center justify-center">
        <span class="font-mono text-[20px] font-bold leading-none tabular-nums text-foreground">{{ centerValue }}</span>
        <span v-if="centerLabel" class="mt-1 text-[10.5px] text-faint">{{ centerLabel }}</span>
      </div>
    </div>
    <ul class="min-w-0 flex-1 space-y-[5px]">
      <li
        v-for="(s, i) in segs" :key="i"
        class="flex cursor-default items-center gap-2 text-[12px] transition-opacity"
        :class="dim(i) ? 'opacity-45' : ''"
        @mouseenter="hovered = i" @mouseleave="hovered = null"
      >
        <span class="size-2.5 shrink-0 rounded-[3px]" :style="{ background: s.color }" />
        <span class="min-w-0 flex-1 truncate text-foreground" :title="s.label">{{ s.label }}</span>
        <span class="shrink-0 font-mono tabular-nums text-muted-foreground">{{ s.value }}</span>
        <span class="w-9 shrink-0 text-right font-mono tabular-nums text-faint">{{ s.pctLabel }}%</span>
      </li>
    </ul>
  </div>
  <p v-else class="text-sm text-muted-foreground">{{ empty || '暂无数据。' }}</p>
</template>
