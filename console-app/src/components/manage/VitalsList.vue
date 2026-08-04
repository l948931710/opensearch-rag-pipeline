<script setup lang="ts">
/**
 * 运行指标扁平列表（2026-08-03 看板重设计）：把 9 张同权重 StatCard 压成 hairline 行
 * ——状态点 + 标签 + mono 数值 + 口径 hint。tone 由调用方显式传入（沿用各指标现有
 * 语义判定,本组件不发明 SLO 阈值——codex 共识）。
 */
export interface VitalItem { label: string; value: string | number; tone?: string; hint?: string }
defineProps<{ items: VitalItem[] }>()
const dotClass = (tone?: string) =>
  (tone || 'text-st-muted').replace('text-', 'bg-').replace('bg-foreground', 'bg-st-muted')
</script>

<template>
  <div class="grid grid-cols-1 sm:grid-cols-2 sm:gap-x-8">
    <div v-for="it in items" :key="it.label"
         class="flex items-baseline gap-2 border-b border-border/60 py-2 last:border-0 sm:[&:nth-last-child(2)]:border-0">
      <span class="size-1.5 shrink-0 self-center rounded-full" :class="dotClass(it.tone)" />
      <span class="text-[12.5px] text-muted-foreground">{{ it.label }}</span>
      <span class="ml-auto font-mono text-[13px] font-semibold tabular-nums text-foreground">{{ it.value }}</span>
      <span v-if="it.hint" class="hidden max-w-[45%] truncate text-[11px] text-faint lg:inline">{{ it.hint }}</span>
    </div>
  </div>
</template>
