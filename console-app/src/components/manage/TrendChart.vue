<script setup lang="ts">
import { computed } from 'vue'

// 通用多序列日趋势折线（批次γ）：FeedbackTrend 是写死「赞/踩」双序列的专用件（全项目唯一折线），
// 本组件把 序列数/颜色/图例/量纲 泛化，几何与 tick 抽稀沿用同一套。
// 诚实性两则：
//  · percent 模式（比率 0-1）：y 域用真实 max（含 5% padding），不强拉 0-100%——0.05 级别的率
//    在满域下会糊成地平线；
//  · 缺日语义由调用方声明：fillZero=true（计数图：缺日=真 0，补 0 连线）/ false（比率图：缺日=
//    rollup 没跑，折线断开留空，绝不臆造 0 率假低谷）。
export interface TrendPoint { d: string; v: number | null }
export interface TrendSeries { key: string; label: string; color: string; points: TrendPoint[] }

const props = defineProps<{
  series: TrendSeries[]
  percent?: boolean
  unit?: string
  fillZero?: boolean
  empty?: string
}>()

const W = 300, H = 96

// 连续日历轴：跨全部序列取 [最早日, 最晚日]，逐日铺满（缺日 → fillZero ? 0 : null）
const calendar = computed<string[]>(() => {
  const ds = props.series.flatMap((s) => s.points.map((p) => p.d)).filter(Boolean).sort()
  if (!ds.length) return []
  const t0 = Date.parse(ds[0] + 'T00:00:00Z')
  const t1 = Date.parse(ds[ds.length - 1] + 'T00:00:00Z')
  if (isNaN(t0) || isNaN(t1)) return [...new Set(ds)]
  const out: string[] = []
  for (let t = t0; t <= t1; t += 86400000) out.push(new Date(t).toISOString().slice(0, 10))
  return out
})

interface Cell { d: string; label: string; vals: (number | null)[] }
const grid = computed<Cell[]>(() => {
  const maps = props.series.map((s) => new Map(s.points.map((p) => [p.d, p.v])))
  return calendar.value.map((d) => ({
    d,
    label: d.slice(5),
    vals: maps.map((m) => {
      const v = m.get(d)
      return v == null ? (props.fillZero ? 0 : null) : v
    }),
  }))
})

const maxV = computed(() => {
  const all = grid.value.flatMap((c) => c.vals).filter((v): v is number => v != null)
  const m = Math.max(...(all.length ? all : [0]))
  return m > 0 ? m * 1.05 : (props.percent ? 0.01 : 1)   // 全零也给个域，横线落底不除零
})

const x = (i: number) => (grid.value.length > 1 ? (i / (grid.value.length - 1)) * W : W / 2)
const y = (v: number) => H - (v / maxV.value) * H
const bandW = computed(() => (grid.value.length > 1 ? W / (grid.value.length - 1) : W))

/** 每序列切成若干连续段（null 断开）；每段是一串 "x,y" 点。 */
function segments(si: number): string[] {
  const segs: string[] = []
  let cur: string[] = []
  grid.value.forEach((c, i) => {
    const v = c.vals[si]
    if (v == null) {
      if (cur.length) { segs.push(cur.join(' ')); cur = [] }
    } else {
      cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`)
    }
  })
  if (cur.length) segs.push(cur.join(' '))
  return segs
}

const ticks = computed(() => {
  const n = grid.value.length
  if (!n) return [] as { pct: number; label: string }[]
  const want = Math.min(n, 6)
  const idxs = want <= 1 ? [0] : [...new Set(Array.from({ length: want }, (_, k) => Math.round((k * (n - 1)) / (want - 1))))]
  return idxs.map((i) => ({ pct: n > 1 ? (i / (n - 1)) * 100 : 50, label: grid.value[i].label }))
})

function fmt(v: number | null): string {
  if (v == null) return '—'
  if (props.percent) return (v * 100).toFixed(1) + '%'
  return v.toLocaleString('en-US') + (props.unit || '')
}
function cellTitle(c: Cell): string {
  return c.d + ' · ' + props.series.map((s, i) => `${s.label} ${fmt(c.vals[i])}`).join(' / ')
}
</script>

<template>
  <div>
    <template v-if="grid.length">
      <div class="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-faint">
        <span v-for="s in series" :key="s.key" class="flex items-center gap-1">
          <span class="h-0.5 w-3 rounded-full" :style="{ background: s.color }" />{{ s.label }}
        </span>
      </div>
      <div class="kb-in relative h-24 w-full">
        <svg :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none" class="h-full w-full overflow-visible" aria-hidden="true">
          <line v-for="g in 3" :key="g" :x1="0" :x2="W" :y1="((g - 1) / 2) * H" :y2="((g - 1) / 2) * H"
            stroke="var(--border)" stroke-width="1" :stroke-dasharray="g === 3 ? '0' : '3 4'" vector-effect="non-scaling-stroke" />
          <template v-for="(s, si) in series" :key="s.key">
            <polyline v-for="(seg, gi) in segments(si)" :key="gi" :points="seg" fill="none" :stroke="s.color"
              stroke-width="1.75" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke" />
          </template>
          <rect v-for="(c, i) in grid" :key="'h' + i"
            :x="Math.max(0, x(i) - bandW / 2)" :y="0" :width="bandW" :height="H" fill="transparent">
            <title>{{ cellTitle(c) }}</title>
          </rect>
        </svg>
      </div>
      <div class="relative mt-1 h-3.5">
        <span v-for="(t, i) in ticks" :key="i"
          class="absolute -translate-x-1/2 font-mono text-[10px] tabular-nums text-faint"
          :style="{ left: t.pct + '%' }">{{ t.label }}</span>
      </div>
    </template>
    <p v-else class="text-sm text-muted-foreground">{{ empty || '该区间暂无数据。' }}</p>
  </div>
</template>
