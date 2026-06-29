<script setup lang="ts">
import { HelpCircle, Clock } from 'lucide-vue-next'
import { deptLabel, gapKindLabel } from '@/lib/kb'
import { useContribute, type GapItem } from '@/composables/useContribute'
import LoadError from '@/components/manage/LoadError.vue'

// 「待回答」缺口列表：答不出的提问（NO_RESULT 缺文档 / REFUSAL 没答好）。
// 「回答」打开贡献弹窗并预填该问题；已有贡献待入库的标灰提示、不重复发起。
const { gaps, loadingGaps, loadErrors, gapsHasMore, loadGaps, openModal } = useContribute()

function onAnswer(g: GapItem) {
  openModal({ question: g.question, dept: g.dept, sourceMessageId: g.source_message_id, gapQuery: g.question })
}
</script>

<template>
  <section class="overflow-hidden rounded-[15px] border border-border bg-card">
    <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3">
      <HelpCircle :size="16" :stroke-width="1.75" class="text-st-warn" />
      <span class="text-sm font-semibold text-foreground">待回答</span>
      <span v-if="gaps.length" class="rounded-full bg-panel px-2 py-px text-[11px] font-bold tabular-nums text-muted-foreground">{{ gaps.length }}</span>
      <div class="flex-1" />
      <span class="hidden text-xs text-muted-foreground sm:inline">来自检索未命中 / 低置信度回答的聚合</span>
    </div>

    <LoadError class="m-[18px]" :message="loadErrors['gaps']" @retry="loadGaps()" />

    <div v-if="gaps.length">
      <div
        v-for="g in gaps" :key="g.question_hash"
        class="flex items-center gap-3 border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-medium text-foreground">{{ g.question }}</div>
          <div class="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[11.5px] text-faint">
            <span>{{ g.asks }} 次询问</span>
            <span v-if="g.dept">· {{ deptLabel(g.dept) }}</span>
            <span class="inline-flex items-center gap-1"><Clock :size="11" :stroke-width="2" /> {{ g.last_days }} 天未回答</span>
            <span v-if="gapKindLabel(g.kind)" class="rounded bg-panel px-1.5 py-px text-[10.5px] text-muted-foreground">{{ gapKindLabel(g.kind) }}</span>
          </div>
        </div>
        <span
          v-if="g.has_pending_contribution"
          class="shrink-0 rounded-lg bg-st-busy/10 px-3 py-[7px] text-[12px] font-medium text-st-busy"
        >已有贡献·待入库</span>
        <button
          v-else type="button"
          class="shrink-0 rounded-lg border border-border bg-transparent px-3.5 py-[7px] text-[12.5px] font-semibold text-accent-text transition hover:border-accent-strong hover:bg-accent-soft"
          @click="onAnswer(g)"
        >回答</button>
      </div>
    </div>

    <div v-else-if="loadingGaps" class="px-[18px] py-10 text-center text-sm text-muted-foreground">加载中…</div>
    <div v-else class="px-[18px] py-12 text-center">
      <p class="text-sm font-medium text-foreground">太棒了，暂无未答出的提问</p>
      <p class="mt-1 text-xs text-muted-foreground">大家的问题目前都能在知识库里找到答案。</p>
    </div>

    <div v-if="gapsHasMore" class="border-t border-border p-3 text-center">
      <button
        type="button" class="rounded-lg border border-border px-4 py-1.5 text-[12.5px] font-medium text-foreground transition hover:border-border-strong"
        @click="loadGaps(gaps.length)"
      >加载更多</button>
    </div>
  </section>
</template>
