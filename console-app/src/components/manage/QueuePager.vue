<script setup lang="ts">
import { computed } from 'vue'
import { ChevronLeft, ChevronRight } from '@lucide/vue'

// 共享翻页脚（设计稿 2026-07-19 components/queues.html 的 .qfoot/.pg，Tailwind 化）：
// 「第 x–y 条 · 共 N 条」+ 等宽页码（当前页绿底）+ 上一页/下一页（端点禁用态）。
// 只有超过一页才渲染（单页自隐，父组件无需再包 v-if）；页码窗口化（>7 页折叠为 1 … n）。
const props = defineProps<{ total: number; page: number; perPage: number }>()
const emit = defineEmits<{ (e: 'update:page', page: number): void }>()

const pages = computed(() => Math.max(1, Math.ceil(props.total / props.perPage)))
const first = computed(() => (props.page - 1) * props.perPage + 1)
const last = computed(() => Math.min(props.total, props.page * props.perPage))

// 页码窗口：≤7 页全显；更多则「1 … cur−1 cur cur+1 … N」（设计稿台账 1 2 3 … N 的同款收敛）。
const items = computed<(number | '…')[]>(() => {
  const n = pages.value
  if (n <= 7) return Array.from({ length: n }, (_, i) => i + 1)
  const want = [...new Set([1, props.page - 1, props.page, props.page + 1, n])]
    .filter((p) => p >= 1 && p <= n)
    .sort((a, b) => a - b)
  const out: (number | '…')[] = []
  let prev = 0
  for (const p of want) {
    if (p - prev > 1) out.push('…')
    out.push(p)
    prev = p
  }
  return out
})

function go(p: number) {
  const next = Math.max(1, Math.min(pages.value, p))
  if (next !== props.page) emit('update:page', next)
}

// 等宽页码（26px 方格 + mono/tabular-nums）；当前页绿底；禁用端点 40% 不可点。
const PG =
  'grid h-[26px] min-w-[26px] place-items-center rounded-md border border-border px-1.5 font-mono text-xs tabular-nums text-muted-foreground transition hover:bg-panel hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-muted-foreground'
const PG_CURRENT = 'border-primary bg-primary font-semibold text-primary-foreground hover:bg-primary hover:text-primary-foreground'
</script>

<template>
  <div
    v-if="pages > 1" data-testid="queue-pager"
    class="flex flex-wrap items-center justify-between gap-3 border-t border-border px-[18px] py-2"
  >
    <span class="font-mono text-[11.5px] tabular-nums text-muted-foreground" data-testid="pager-info">
      第 {{ first }}–{{ last }} 条 · 共 {{ total }} 条
    </span>
    <div class="flex items-center gap-1">
      <button type="button" :class="PG" :disabled="page <= 1" aria-label="上一页" @click="go(page - 1)">
        <ChevronLeft :size="13" :stroke-width="1.75" />
      </button>
      <template v-for="(it, i) in items" :key="i">
        <span v-if="it === '…'" class="min-w-[26px] text-center font-mono text-xs text-faint">…</span>
        <button
          v-else type="button" data-testid="pager-page"
          :class="[PG, it === page ? PG_CURRENT : '']"
          :data-current="it === page ? '1' : undefined"
          :aria-current="it === page ? 'page' : undefined"
          @click="go(it)"
        >{{ it }}</button>
      </template>
      <button type="button" :class="PG" :disabled="page >= pages" aria-label="下一页" @click="go(page + 1)">
        <ChevronRight :size="13" :stroke-width="1.75" />
      </button>
    </div>
  </div>
</template>
