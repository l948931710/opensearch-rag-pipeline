<script setup lang="ts">
import { ref } from 'vue'
import { ChevronDown } from 'lucide-vue-next'
import type { SourceRow } from '@/composables/useAsk'

// 折叠的参考来源；高/中/低用状态色印记（以服务端 level 为准）。
defineProps<{ sources: SourceRow[] }>()
const open = ref(false)

const PILL: Record<string, string> = {
  high: 'text-st-live bg-st-live/12',
  mid: 'text-st-busy bg-st-busy/12',
  low: 'text-st-queue bg-st-queue/12',
}
</script>

<template>
  <div class="mt-3 overflow-hidden rounded-xl border border-border bg-panel/50">
    <button
      type="button"
      class="flex w-full items-center justify-between px-3.5 py-2.5 text-xs font-medium text-muted-foreground transition hover:text-foreground"
      @click="open = !open"
    >
      <span class="inline-flex items-center gap-1.5">
        参考来源
        <span class="rounded bg-accent-soft px-1.5 py-0.5 font-mono text-accent-text">{{ sources.length }}</span>
      </span>
      <ChevronDown :size="14" :stroke-width="2" class="transition-transform" :class="{ 'rotate-180': open }" />
    </button>
    <div v-show="open" class="border-t border-border px-1.5 py-1.5">
      <div
        v-for="s in sources" :key="s.idx"
        class="flex items-start gap-3 rounded-lg px-2 py-2 transition hover:bg-surface"
      >
        <span class="mt-px grid size-5 shrink-0 place-items-center rounded-md bg-accent-soft font-mono text-[11px] font-medium text-accent-text">{{ s.idx }}</span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-sm text-foreground">{{ s.title }}</div>
          <div v-if="s.section" class="truncate text-xs text-muted-foreground">{{ s.section }}</div>
        </div>
        <span class="shrink-0 rounded px-1.5 py-0.5 text-[11px] font-medium" :class="PILL[s.level]">相关度{{ s.levelLabel }}</span>
      </div>
    </div>
  </div>
</template>
