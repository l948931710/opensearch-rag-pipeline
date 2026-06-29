<script setup lang="ts">
import { FileText, RefreshCw } from 'lucide-vue-next'
import { useContribute } from '@/composables/useContribute'
import LoadError from '@/components/manage/LoadError.vue'
import ContribBadge from './ContribBadge.vue'

// 我的贡献：4 态徽章（待审核 / 已采纳·待入库 / 已入库 / 入库失败）+ 入库失败可重试。
const { myContribs, loadErrors, isBusy, loadMine, retryContribution } = useContribute()
</script>

<template>
  <section>
    <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">我的贡献</p>
    <LoadError class="mb-2.5" :message="loadErrors['mine']" @retry="loadMine()" />
    <div class="overflow-hidden rounded-[15px] border border-border bg-card">
      <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3">
        <FileText :size="16" :stroke-width="1.75" class="text-accent-text" />
        <span class="text-sm font-semibold text-foreground">我的贡献</span>
      </div>
      <div
        v-for="c in myContribs" :key="c.contribution_id"
        class="flex items-start gap-3 border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13px] font-medium text-foreground">{{ c.question }}</div>
          <div class="mt-1 text-[11px] text-faint">{{ c.created_at }}<span v-if="c.review_note"> · {{ c.review_note }}</span></div>
        </div>
        <div class="flex shrink-0 items-center gap-1.5">
          <ContribBadge :state="c.state" />
          <button
            v-if="c.state === 'failed'" type="button" :disabled="isBusy(`ct:${c.contribution_id}`)"
            class="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
            @click="retryContribution(c)"
          ><RefreshCw :size="11" :stroke-width="2" /> 重试</button>
        </div>
      </div>
      <p v-if="!myContribs.length" class="px-[18px] py-8 text-center text-[12.5px] text-muted-foreground">还没有贡献，去「待回答」挑一个问题回答吧。</p>
    </div>
  </section>
</template>
