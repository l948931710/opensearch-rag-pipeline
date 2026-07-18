<script setup lang="ts">
import { computed } from 'vue'
import { Trophy } from '@lucide/vue'
import { useSession } from '@/stores/session'
import { useContribute } from '@/composables/useContribute'

// 知识贡献英雄榜：按【已入库(searchable)】贡献数排名（真正闭环才计入）。
const { heroes } = useContribute()
const me = computed(() => useSession().identity?.userId || '')
// 名次奖牌调：金（琥珀）/ 银（灰）/ 铜（暖橙，借调 --c-str），4 名以后素灰。
const RANK_TONE: Record<number, string> = {
  1: 'bg-st-busy/15 text-st-busy',
  2: 'bg-panel text-muted-foreground',
  3: 'bg-[color-mix(in_srgb,var(--c-str)_13%,transparent)] text-[var(--c-str)]',
}
function rankCls(r: number) { return RANK_TONE[r] || 'bg-panel text-faint' }
function initial(name: string) { return (name || '?').trim().charAt(0) || '?' }
</script>

<template>
  <!-- 卡头已带图标+标题，不再另设分区眉标 -->
  <section v-if="heroes.length">
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
        <!-- 被引用数（批次ε-2 R2）：次级价值信号，排名仍按入库篇数；算不出（null）自隐不用 0 顶替 -->
        <span
          v-if="h.hits != null" data-testid="hero-hits"
          class="shrink-0 rounded bg-accent-soft px-1.5 py-px text-[10.5px] font-medium tabular-nums text-accent-text"
          :title="`TA 的贡献被引用进 ${h.hits} 次回答（累计）`"
        >引用 {{ h.hits }}</span>
        <span class="shrink-0 font-mono text-[13px] font-bold tabular-nums text-foreground" title="已入库篇数（排名依据）">{{ h.count }}</span>
      </div>
    </div>
  </section>
</template>
