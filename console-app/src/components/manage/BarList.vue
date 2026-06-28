<script setup lang="ts">
import { computed } from 'vue'

// 通用「标签 + 比例条 + 值」排行列表（最常被检索 / 知识缺口 / 部门覆盖 / 点踩原因 复用）。
// 排行用→不显份额；完整分布用→ show-share 显「占比 %」，便于读「谁撑起大头」。
// 真实口径直显，无数据交由 empty 文案如实占位；比例条按本列表最大值归一。
const props = defineProps<{
  items: { label: string; sub?: string; value: number; value2?: number }[]
  unit?: string
  tone?: string        // 主条颜色（默认 bg-accent-strong）
  tone2?: string       // value2 第二条颜色（部门覆盖：文档数 vs 使用数）
  empty?: string
  bare?: boolean       // 嵌在共享面板里时去掉自身边框/底色（避免框中框）
  showShare?: boolean  // 显示份额（占比）
  shareBase?: number   // 份额分母（默认=本列表各项之和）。当列表非互斥/不完整（如点踩原因多选、含未注明）
                       // 时传外部权威分母（如点踩总数），使占比与上方「共 N 条」同口径、可对账。
}>()

const max = computed(() => Math.max(1, ...props.items.map((i) => Math.max(i.value || 0, i.value2 || 0))))
const total = computed(() => props.shareBase ?? props.items.reduce((s, i) => s + (i.value || 0), 0))
const fmt = (n: number) => (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n))
const share = (v: number) => {
  if (!total.value) return ''
  const p = (v / total.value) * 100
  return (p < 1 && p > 0 ? '<1' : String(Math.round(p))) + '%'
}
</script>

<template>
  <div :class="bare ? '' : 'rounded-[14px] border border-border bg-card p-[15px]'">
    <template v-if="items.length">
      <div v-for="(it, i) in items" :key="i" class="py-1.5">
        <div class="flex items-baseline justify-between gap-3">
          <span class="min-w-0 truncate text-[12.5px] text-foreground" :title="it.label">{{ it.label }}</span>
          <span class="flex shrink-0 items-baseline gap-1.5">
            <span class="font-mono text-[12px] font-bold tabular-nums text-foreground">{{ fmt(it.value) }}{{ unit || '' }}</span>
            <span v-if="showShare" class="w-9 text-right font-mono text-[11px] tabular-nums text-faint">{{ share(it.value) }}</span>
          </span>
        </div>
        <div class="mt-1 h-1.5 overflow-hidden rounded-full bg-panel">
          <div class="kb-grow h-full rounded-full" :class="tone || 'bg-accent-strong'" :style="{ width: ((it.value / max) * 100) + '%', animationDelay: i * 45 + 'ms' }" />
        </div>
        <div v-if="it.value2 !== undefined" class="mt-0.5 h-1.5 overflow-hidden rounded-full bg-panel">
          <div class="kb-grow h-full rounded-full" :class="tone2 || 'bg-st-busy'" :style="{ width: ((it.value2 / max) * 100) + '%', animationDelay: i * 45 + 'ms' }" />
        </div>
        <div v-if="it.sub" class="mt-0.5 truncate text-[11px] text-faint">{{ it.sub }}</div>
      </div>
    </template>
    <p v-else class="text-sm text-muted-foreground">{{ empty || '暂无数据。' }}</p>
  </div>
</template>
