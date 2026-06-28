<script setup lang="ts">
import { computed } from 'vue'

// 通用纵向柱状趋势（近期入库批次等）：高度 ∝ 值，按全列表最大值归一；顶值 + 底标。
const props = defineProps<{
  items: { label: string; value: number; sub?: string }[]
  tone?: string
  empty?: string
}>()
const max = computed(() => Math.max(1, ...props.items.map((i) => i.value || 0)))
const fmt = (n: number) => (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n))
</script>

<template>
  <div v-if="items.length" class="flex items-end gap-2.5">
    <div v-for="(it, i) in items" :key="i" class="flex min-w-0 flex-1 flex-col items-center gap-1" :title="it.sub">
      <span class="font-mono text-[11px] font-bold tabular-nums text-foreground">{{ fmt(it.value) }}</span>
      <div class="flex h-16 w-full items-end">
        <div class="w-full rounded-t-md transition-[height]" :class="tone || 'bg-accent-strong'" :style="{ height: (it.value / max * 100) + '%' }" />
      </div>
      <span class="w-full truncate text-center text-[10.5px] text-faint">{{ it.label }}</span>
    </div>
  </div>
  <p v-else class="text-sm text-muted-foreground">{{ empty || '暂无数据。' }}</p>
</template>
