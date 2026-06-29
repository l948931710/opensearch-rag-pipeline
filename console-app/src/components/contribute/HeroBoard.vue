<script setup lang="ts">
import { computed } from 'vue'
import { Trophy } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { useContribute } from '@/composables/useContribute'

// 知识贡献英雄榜：按【已入库(searchable)】贡献数排名（真正闭环才计入）。
const { heroes } = useContribute()
const me = computed(() => useSession().identity?.userId || '')
const RANK_TONE: Record<number, string> = {
  1: 'bg-st-warn/15 text-st-warn', 2: 'bg-panel text-muted-foreground', 3: 'bg-accent-soft text-accent-text',
}
function rankCls(r: number) { return RANK_TONE[r] || 'bg-panel text-muted-foreground' }
function initial(name: string) { return (name || '?').trim().charAt(0) || '?' }
</script>

<template>
  <section v-if="heroes.length">
    <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">知识贡献英雄榜</p>
    <div class="overflow-hidden rounded-[15px] border border-border bg-card">
      <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3">
        <Trophy :size="16" :stroke-width="1.75" class="text-st-warn" />
        <span class="text-sm font-semibold text-foreground">英雄榜</span>
      </div>
      <div
        v-for="h in heroes" :key="h.author_id"
        class="flex items-center gap-3 border-t border-border px-[18px] py-2.5 first:border-t-0"
        :class="h.author_id === me ? 'bg-accent-soft/40' : ''"
      >
        <span class="grid size-6 shrink-0 place-items-center rounded-md font-mono text-[12px] font-bold tabular-nums" :class="rankCls(h.rank)">{{ h.rank }}</span>
        <span class="grid size-7 shrink-0 place-items-center rounded-full bg-accent-soft text-[12px] font-semibold text-accent-text">{{ initial(h.author_name) }}</span>
        <span class="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">{{ h.author_name || h.author_id }}<span v-if="h.author_id === me" class="ml-1 text-[11px] text-accent-text">（我）</span></span>
        <span class="shrink-0 font-mono text-[13px] font-bold tabular-nums text-foreground">{{ h.count }}</span>
      </div>
    </div>
  </section>
</template>
