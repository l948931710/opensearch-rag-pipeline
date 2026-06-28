<script setup lang="ts">
import { computed, ref } from 'vue'
import type { KbFeedbackDay } from '@/composables/useKb'

// 反馈趋势（设计版「以中线为零」：上方赞绿、下方踩红）。可在 近7天 / 近30天 间切换窗口。
const props = defineProps<{ days: KbFeedbackDay[]; last7: number; total: number; bare?: boolean }>()
const win = ref<7 | 30>(30)
const allDays = computed(() => props.days ?? [])
const shown = computed(() => (win.value === 7 ? allDays.value.slice(-7) : allDays.value))
const max = computed(() => Math.max(1, ...shown.value.map((d) => Math.max(d.up, d.down))))
const sumShown = computed(() => (win.value === 7 ? props.last7 : props.total))
</script>

<template>
  <div :class="bare ? '' : 'rounded-[14px] border border-border bg-card p-[15px]'">
    <div class="mb-3 flex items-center gap-3">
      <!-- 窗口切换 -->
      <div class="flex gap-0.5 rounded-lg border border-border bg-panel p-0.5">
        <button
          v-for="w in ([7, 30] as const)" :key="w" type="button"
          class="rounded-md px-2.5 py-1 text-[11.5px] font-medium transition"
          :class="win === w ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'"
          @click="win = w"
        >近 {{ w }} 天</button>
      </div>
      <span class="font-mono text-[13px] font-bold tabular-nums text-foreground">{{ sumShown }}</span>
      <span class="text-[11.5px] text-muted-foreground">条反馈</span>
      <span class="ml-auto flex items-center gap-3 text-[11px] text-faint">
        <span class="flex items-center gap-1"><span class="size-2 rounded-sm bg-st-live" />赞</span>
        <span class="flex items-center gap-1"><span class="size-2 rounded-sm bg-st-fail" />踩</span>
      </span>
    </div>
    <template v-if="shown.length">
      <div class="relative flex h-20 items-stretch gap-[3px]">
        <div class="pointer-events-none absolute inset-x-0 top-1/2 h-px bg-border" />
        <div
          v-for="(d, i) in shown" :key="i"
          class="flex h-full min-w-[4px] flex-1 flex-col"
          :title="`${d.day} · ${d.up} 赞 / ${d.down} 踩`"
        >
          <div class="flex flex-1 flex-col justify-end pb-px">
            <div class="rounded-t-sm bg-st-live transition-[height]" :style="{ height: (d.up / max * 100) + '%' }" />
          </div>
          <div class="flex flex-1 flex-col justify-start pt-px">
            <div class="rounded-b-sm bg-st-fail transition-[height]" :style="{ height: (d.down / max * 100) + '%' }" />
          </div>
        </div>
      </div>
      <p class="mt-2 text-[11px] text-faint">以中线为零 · 上方点赞、下方点踩</p>
    </template>
    <p v-else class="text-sm text-muted-foreground">该区间暂无反馈。</p>
  </div>
</template>
