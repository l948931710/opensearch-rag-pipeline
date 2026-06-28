<script setup lang="ts">
import { computed } from 'vue'
import { badgeTone } from '@/lib/kb'

// 状态分布条：把 /api/kb/stats 的 by_badge（真实口径，按作用域聚合）画成一条堆叠比例条 + 图例。
// 配色复用 lib/kb.badgeTone（与台账徽章同一真相），绝不在此另造色表。
const props = defineProps<{ byBadge: Record<string, number> }>()

const TONE_BG: Record<string, string> = {
  live: 'bg-st-live', busy: 'bg-st-busy', queue: 'bg-st-queue',
  warn: 'bg-st-warn', fail: 'bg-st-fail', muted: 'bg-st-muted',
}
// 稳定展示顺序（未列出的徽章排末尾）。
const ORDER = ['已上线', '处理中', '排队中', '待审核', '已驳回', '已隔离', '处理失败', '内容未变', '已退役']

const segs = computed(() => {
  const bb = props.byBadge || {}
  const entries = Object.entries(bb).filter(([, n]) => (n || 0) > 0)
  entries.sort((a, b) => ((ORDER.indexOf(a[0]) + 1) || 99) - ((ORDER.indexOf(b[0]) + 1) || 99))
  const total = entries.reduce((s, [, n]) => s + (n || 0), 0)
  return {
    total,
    items: entries.map(([k, n]) => {
      const pct = total ? (n * 100) / total : 0
      return { k, n, pct, label: pct >= 1 ? Math.round(pct) + '%' : '<1%', bg: TONE_BG[badgeTone(k)] || 'bg-st-muted' }
    }),
  }
})
</script>

<template>
  <div class="rounded-[14px] border border-border bg-card p-[15px]">
    <template v-if="segs.total">
      <!-- 段宽 ∝ 占比；精确读数交给下方图例（label+计数+占比，主题安全色）与 hover tooltip，
           不在段内叠字——段底色覆盖 6 种状态色，任何单一前景色都无法在亮/暗双主题保证对比度。 -->
      <div class="kb-grow mb-3 flex h-4 gap-0.5 overflow-hidden rounded-md">
        <div
          v-for="s in segs.items" :key="s.k"
          class="h-full transition-[filter] first:rounded-l-md last:rounded-r-md hover:brightness-110"
          :class="s.bg" :style="{ width: s.pct + '%' }"
          :title="`${s.k} ${s.n}（${s.label}）`"
        />
      </div>
      <div class="flex flex-wrap gap-x-4 gap-y-1.5">
        <div v-for="s in segs.items" :key="s.k" class="flex items-center gap-1.5">
          <span class="size-2 rounded-sm" :class="s.bg" />
          <span class="text-[12.5px] text-muted-foreground">{{ s.k }}</span>
          <span class="font-mono text-[12.5px] font-bold tabular-nums text-foreground">{{ s.n }}</span>
          <span class="font-mono text-[11px] tabular-nums text-faint">{{ s.label }}</span>
        </div>
      </div>
    </template>
    <p v-else class="text-sm text-muted-foreground">暂无文档数据。</p>
  </div>
</template>
