<script setup lang="ts">
import { computed } from 'vue'

// 入库吞吐趋势（离散批次 → 竖条最诚实：跳过的日期不连线、不臆造中间值）。
// 每根条 = 当批嵌入块数（绿）；该批有失败时在条顶叠一段红「失败盖」，把「嵌入失败率」从悬浮提示
// 提升为可见信号。基线 + 平均参考线给出尺度感；条宽设上限，避免只有三两批时铺满成大色块。
const props = defineProps<{
  items: { label: string; value: number; failed?: number; failRate?: number; sub?: string }[]
  empty?: string
}>()

const max = computed(() => Math.max(1, ...props.items.map((i) => i.value || 0)))
const avg = computed(() => {
  const n = props.items.length
  return n ? props.items.reduce((s, i) => s + (i.value || 0), 0) / n : 0
})
const fmt = (n: number) => (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n))
const fmtPct = (x?: number) => (x === undefined ? '' : (x * 100).toFixed(x < 0.1 && x > 0 ? 1 : 0) + '%')
// 平均线高度（占条形绘图区，与条同一归一）。上不封顶到 100%（avg==max 时与最高条齐平，不再差 3%），
// 仅低端夹 3% 免贴底看不见。单批次（avg==该批）时不画均线（见模板 v-if）——一条的「平均」无意义。
const avgTop = computed(() => Math.min(100, Math.max(3, (avg.value / max.value) * 100)))
// 单条高度百分比（值/最大值），最低 2% 保证可见。
const barPct = (v: number) => Math.max(2, (v / max.value) * 100)
</script>

<template>
  <div v-if="items.length">
    <!-- 绘图区：顶部留 18px 浮动数值标签，绘图带 top-[18px]~bottom，条/网格/均线同一尺度 -->
    <div class="relative h-24">
      <div class="pointer-events-none absolute inset-x-0 bottom-0 top-[18px]">
        <div v-for="g in 3" :key="g" class="absolute inset-x-0 border-t border-dashed border-border/55" :style="{ top: ((g - 1) / 2) * 100 + '%' }" />
        <!-- 平均参考线（≥2 批才有意义）-->
        <div v-if="items.length > 1" class="absolute inset-x-0 flex items-center" :style="{ bottom: avgTop + '%' }">
          <div class="h-px flex-1" style="background-image:repeating-linear-gradient(to right,var(--st-busy) 0 5px,transparent 5px 9px);" />
          <span class="ml-1.5 shrink-0 rounded bg-st-busy/12 px-1 py-px font-mono text-[9.5px] font-bold tabular-nums text-st-busy">均 {{ fmt(Math.round(avg)) }}</span>
        </div>
      </div>
      <!-- 实底轴线 -->
      <div class="pointer-events-none absolute inset-x-0 bottom-0 h-px bg-border-strong" />
      <!-- 柱列：条宽设上限并居中，少数批次也不铺满 -->
      <div class="absolute inset-x-0 bottom-0 top-[18px] flex items-end justify-center gap-3 sm:gap-5">
        <div
          v-for="(it, i) in items" :key="i"
          class="kb-trend-col group relative flex h-full min-w-0 max-w-[76px] flex-1 items-end justify-center"
          :title="`${it.label} · ${fmt(it.value)} 块${it.failRate ? ` · 失败率 ${fmtPct(it.failRate)}` : ''}`"
        >
          <div
            class="kb-trend-bar kb-rise relative flex w-full max-w-[52px] flex-col-reverse overflow-hidden rounded-t-[5px]"
            :style="{ height: barPct(it.value) + '%', animationDelay: i * 60 + 'ms' }"
          >
            <div class="w-full flex-1" style="background-image:linear-gradient(to top,var(--accent),color-mix(in srgb,var(--accent) 60%,var(--surface)));" />
            <!-- 失败标记：该批有失败即在条顶显一道固定红条（不按比例 → 不暗示失败占比）；精确失败率见 hover。 -->
            <div v-if="it.failed" class="h-[3px] w-full shrink-0 bg-st-fail" />
          </div>
          <!-- 数值标签：浮在条顶上方（绝对定位，不占条高，保证条与均线同尺度） -->
          <span
            class="pointer-events-none absolute left-1/2 -translate-x-1/2 font-mono text-[11px] font-bold tabular-nums text-foreground"
            :style="{ bottom: 'calc(' + barPct(it.value) + '% + 2px)' }"
          >{{ fmt(it.value) }}</span>
        </div>
      </div>
    </div>
    <!-- 日期标签轴（与柱列同布局对齐） -->
    <div class="mt-1.5 flex justify-center gap-3 sm:gap-5">
      <span v-for="(it, i) in items" :key="i" class="min-w-0 max-w-[76px] flex-1 truncate text-center font-mono text-[10px] text-faint">{{ it.label }}</span>
    </div>
  </div>
  <p v-else class="py-2 text-sm text-muted-foreground">{{ empty || '暂无数据。' }}</p>
</template>
