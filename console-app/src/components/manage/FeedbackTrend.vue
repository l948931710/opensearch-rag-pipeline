<script setup lang="ts">
import { computed, ref } from 'vue'
import type { KbFeedbackDay } from '@/composables/useKb'

// 反馈趋势（折线图）：赞（绿）/ 踩（红）两条线随日期走。连续日历轴——以数据最晚一天为右端向前补满
// win 天，缺反馈的日补 0（折线在无反馈日如实落 0、不跨空隙臆造插值）；x 轴显部分日期刻度。
const props = defineProps<{ days: KbFeedbackDay[]; last7: number; total: number; bare?: boolean }>()
const win = ref<7 | 30>(30)
const sumShown = computed(() => (win.value === 7 ? props.last7 : props.total))

const W = 300, H = 96
// 连续日历序列（含补 0 日）
const series = computed(() => {
  const days = props.days ?? []
  if (!days.length) return [] as { day: string; up: number; down: number; label: string }[]
  const map = new Map(days.map((d) => [d.day, d]))
  const endTs = Date.parse(days[days.length - 1].day + 'T00:00:00Z')   // 已按升序，末项=最晚日
  if (isNaN(endTs)) return days.map((d) => ({ day: d.day, up: d.up, down: d.down, label: d.day.slice(5) }))
  const out: { day: string; up: number; down: number; label: string }[] = []
  for (let i = win.value - 1; i >= 0; i--) {
    const dt = new Date(endTs - i * 86400000)
    const key = dt.toISOString().slice(0, 10)
    const m = map.get(key)
    out.push({ day: key, up: m?.up || 0, down: m?.down || 0,
      label: String(dt.getUTCMonth() + 1).padStart(2, '0') + '-' + String(dt.getUTCDate()).padStart(2, '0') })
  }
  return out
})
const max = computed(() => Math.max(1, ...series.value.flatMap((d) => [d.up, d.down])))
const net = computed(() => series.value.reduce((s, d) => s + (d.up - d.down), 0))
const x = (i: number) => (series.value.length > 1 ? (i / (series.value.length - 1)) * W : W / 2)
const bandW = computed(() => (series.value.length > 1 ? W / (series.value.length - 1) : W))
const path = (key: 'up' | 'down') =>
  series.value.map((d, i) => `${x(i).toFixed(1)},${(H - (d[key] / max.value) * H).toFixed(1)}`).join(' ')
// x 轴日期刻度：均匀取 ≤6 个（含首尾），30 天只显部分
const ticks = computed(() => {
  const n = series.value.length
  if (!n) return [] as { pct: number; label: string }[]
  const want = Math.min(n, 6)
  const idxs = want <= 1 ? [0] : [...new Set(Array.from({ length: want }, (_, k) => Math.round((k * (n - 1)) / (want - 1))))]
  return idxs.map((i) => ({ pct: n > 1 ? (i / (n - 1)) * 100 : 50, label: series.value[i].label }))
})
</script>

<template>
  <div :class="bare ? '' : 'rounded-[14px] border border-border bg-card p-[15px]'">
    <div class="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
      <div class="flex gap-0.5 rounded-lg border border-border bg-panel p-0.5">
        <button
          v-for="w in ([7, 30] as const)" :key="w" type="button"
          class="rounded-md px-2.5 py-1 text-[11.5px] font-medium transition"
          :class="win === w ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'"
          @click="win = w"
        >近 {{ w }} 天</button>
      </div>
      <!-- 7天=近7天计数、30天=累计总数（后端口径不同），标签随窗口如实切换。 -->
      <span class="font-mono text-[13px] font-bold tabular-nums text-foreground">{{ sumShown }}</span>
      <span class="-ml-1.5 text-[11.5px] text-muted-foreground">{{ win === 7 ? '条 · 近 7 天' : '条 · 累计' }}</span>
      <span
        v-if="series.length"
        class="rounded-full px-2 py-px font-mono text-[11px] font-bold tabular-nums"
        :class="net >= 0 ? 'bg-st-live/12 text-st-live' : 'bg-st-fail/12 text-st-fail'"
        :title="`近 ${win} 天 赞 − 踩 之和（与左侧累计数非同口径）`"
      >近 {{ win }} 天净 {{ net >= 0 ? '+' : '' }}{{ net }}</span>
      <span class="ml-auto flex items-center gap-3 text-[11px] text-faint">
        <span class="flex items-center gap-1"><span class="h-0.5 w-3 rounded-full bg-st-live" />赞</span>
        <span class="flex items-center gap-1"><span class="h-0.5 w-3 rounded-full bg-st-fail" />踩</span>
      </span>
    </div>
    <template v-if="series.length">
      <div class="kb-in relative h-24 w-full">
        <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="h-full w-full overflow-visible" aria-hidden="true">
          <!-- 网格基线 -->
          <line v-for="g in 3" :key="g" :x1="0" :x2="W" :y1="((g - 1) / 2) * H" :y2="((g - 1) / 2) * H"
            stroke="var(--border)" stroke-width="1" :stroke-dasharray="g === 3 ? '0' : '3 4'" vector-effect="non-scaling-stroke" />
          <polyline :points="path('up')" fill="none" stroke="var(--st-live)" stroke-width="1.75"
            stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
          <polyline :points="path('down')" fill="none" stroke="var(--st-fail)" stroke-width="1.75"
            stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
          <!-- 逐日命中区（透明）：hover 读当日 赞/踩，恢复改折线前每根柱的 title 读数 -->
          <rect v-for="(d, i) in series" :key="'h' + i"
            :x="Math.max(0, x(i) - bandW / 2)" :y="0" :width="bandW" :height="H" fill="transparent">
            <title>{{ d.day }} · 赞 {{ d.up }} / 踩 {{ d.down }}</title>
          </rect>
        </svg>
      </div>
      <!-- x 轴日期刻度（30 天显部分）-->
      <div class="relative mt-1 h-3.5">
        <span v-for="(t, i) in ticks" :key="i"
          class="absolute -translate-x-1/2 font-mono text-[10px] tabular-nums text-faint"
          :style="{ left: t.pct + '%' }">{{ t.label }}</span>
      </div>
    </template>
    <p v-else class="text-sm text-muted-foreground">该区间暂无反馈。</p>
  </div>
</template>
