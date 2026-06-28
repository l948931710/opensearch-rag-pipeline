<script setup lang="ts">
import { computed } from 'vue'

// 纵向柱状图（分类分布）：各部门文档数 / 点踩原因等。高度 ∝ 值（按列表最大值归一）；
// 数值浮于柱顶上方（绝对定位，不占柱高 → 柱与基线/网格同尺度）；可选 占比% 与 周环比 delta 徽标。
// 类目多时容器横向滚动（每柱保底宽度），长标签截断 + hover 显全。
const props = defineProps<{
  items: { label: string; value: number; delta?: number | null; deltaPct?: number | null }[]
  color?: string       // 柱主色（CSS 变量串），默认 --accent
  unit?: string
  showShare?: boolean  // 显示占比
  shareBase?: number   // 占比分母（默认=各项之和）；非互斥/不完整分布传外部权威分母
  empty?: string
}>()

const max = computed(() => Math.max(1, ...props.items.map((i) => i.value || 0)))
const totalBase = computed(() => props.shareBase ?? props.items.reduce((s, i) => s + (i.value || 0), 0))
const grad = computed(() => {
  const c = props.color || 'var(--accent)'
  return `linear-gradient(to top, ${c}, color-mix(in srgb, ${c} 58%, var(--surface)))`
})
const fmt = (n: number) => (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n))
const sharePct = (v: number) => {
  if (!totalBase.value) return ''
  const p = (v / totalBase.value) * 100
  return (p < 1 && p > 0 ? '<1' : String(Math.round(p))) + '%'
}
// 徽标显「净变化篇数」（箭头表方向；0 中性无箭头）——比四舍五入到 0% 的比率更可读、不自相矛盾。
const deltaTxt = (c: number) => (c > 0 ? '▲+' + c : c < 0 ? '▼' + Math.abs(c) : '0')
// 比率（周环比 %）只进 tooltip：|p|<0.1% 仍保 1 位小数，免显误导的 0%。
const pctTxt = (p: number) => (p > 0 ? '+' : '') + (p * 100).toFixed(Math.abs(p) < 0.1 ? 1 : 0) + '%'
const barPct = (v: number) => Math.max(2, (v / max.value) * 100)
</script>

<template>
  <div v-if="items.length" class="overflow-x-auto">
    <div :style="{ minWidth: items.length * 46 + 'px' }">
      <!-- 绘图区：顶部留 36px 给 值 + 占比/环比；网格/柱同尺度（top-9~bottom）-->
      <div class="relative h-28">
        <div class="pointer-events-none absolute inset-x-0 bottom-0 top-9">
          <div v-for="g in 3" :key="g" class="absolute inset-x-0 border-t border-dashed border-border/55" :style="{ top: ((g - 1) / 2) * 100 + '%' }" />
        </div>
        <div class="pointer-events-none absolute inset-x-0 bottom-0 h-px bg-border-strong" />
        <div class="absolute inset-x-0 bottom-0 top-9 flex items-end gap-2 sm:gap-3">
          <div
            v-for="(it, i) in items" :key="i"
            class="kb-trend-col group relative flex h-full min-w-0 flex-1 items-end justify-center"
            :title="`${it.label} · ${fmt(it.value)}${unit || ''}${showShare ? ' · ' + sharePct(it.value) : ''}${it.delta != null ? ' · 本周净变化 ' + deltaTxt(it.delta) + (unit || '') : ''}${it.deltaPct != null ? ' · 周环比 ' + pctTxt(it.deltaPct) : ''}`"
          >
            <div
              class="kb-trend-bar kb-rise w-full max-w-[40px] rounded-t-[5px]"
              :style="{ height: barPct(it.value) + '%', animationDelay: i * 50 + 'ms', backgroundImage: grad }"
            />
            <div class="pointer-events-none absolute left-1/2 flex -translate-x-1/2 flex-col items-center leading-none" :style="{ bottom: 'calc(' + barPct(it.value) + '% + 3px)' }">
              <span class="font-mono text-[11px] font-bold tabular-nums text-foreground">{{ fmt(it.value) }}</span>
              <span v-if="showShare || it.delta != null" class="mt-0.5 flex items-center gap-1">
                <span v-if="showShare" class="font-mono text-[10px] tabular-nums text-faint">{{ sharePct(it.value) }}</span>
                <span
                  v-if="it.delta != null"
                  class="font-mono text-[10px] font-semibold tabular-nums"
                  :class="it.delta > 0 ? 'text-st-live' : it.delta < 0 ? 'text-st-fail' : 'text-faint'"
                >{{ deltaTxt(it.delta) }}</span>
              </span>
            </div>
          </div>
        </div>
      </div>
      <!-- 类目标签轴（与柱列同布局对齐）-->
      <div class="mt-1.5 flex gap-2 sm:gap-3">
        <span v-for="(it, i) in items" :key="i" class="min-w-0 flex-1 truncate text-center text-[10.5px] text-faint" :title="it.label">{{ it.label }}</span>
      </div>
    </div>
  </div>
  <p v-else class="py-2 text-sm text-muted-foreground">{{ empty || '暂无数据。' }}</p>
</template>
