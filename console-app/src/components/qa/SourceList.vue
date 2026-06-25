<script setup lang="ts">
import { ref } from 'vue'
import { ChevronDown } from 'lucide-vue-next'
import type { SourceRow } from '@/composables/useAsk'

// 折叠的参考来源；高/中/低用状态色印记（以服务端 level 为准）。
defineProps<{ sources: SourceRow[] }>()
const open = ref(false)

const PILL: Record<string, string> = {
  high: 'text-st-live bg-st-live/10',
  mid: 'text-st-busy bg-st-busy/10',
  low: 'text-st-queue bg-st-queue/10',
}
</script>

<template>
  <div class="mt-3 rounded-lg border border-border bg-secondary/40">
    <button
      type="button"
      class="flex w-full items-center justify-between px-3 py-2 text-xs text-muted-foreground transition hover:text-foreground"
      @click="open = !open"
    >
      <span>参考来源 · {{ sources.length }}</span>
      <ChevronDown :size="14" :stroke-width="2" class="transition-transform" :class="{ 'rotate-180': open }" />
    </button>
    <div v-show="open" class="space-y-1.5 px-2 pb-2">
      <div v-for="s in sources" :key="s.idx" class="flex items-start gap-2.5 rounded-md px-1.5 py-1.5">
        <span class="mt-0.5 grid size-5 shrink-0 place-items-center rounded bg-card font-mono text-[11px] text-muted-foreground">{{ s.idx }}</span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-sm text-foreground">{{ s.title }}</div>
          <div v-if="s.section" class="truncate text-xs text-muted-foreground">{{ s.section }}</div>
        </div>
        <span class="shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium" :class="PILL[s.level]">相关度{{ s.levelLabel }}</span>
      </div>
    </div>
  </div>
</template>
