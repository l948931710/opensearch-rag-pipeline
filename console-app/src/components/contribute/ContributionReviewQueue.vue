<script setup lang="ts">
import { ClipboardCheck, User } from 'lucide-vue-next'
import { deptLabel } from '@/lib/kb'
import { useContribute, type ContributionItem } from '@/composables/useContribute'
import LoadError from '@/components/manage/LoadError.vue'
import { useDialog } from '@/composables/useDialog'

// 贡献审核队列（审批人侧）：本部门/全库待采纳的员工贡献 → 采纳即合成文档走管线入库，或驳回。
// 与「待回答」是两件事：这里是【管理员】审别人提交的答案。橙头（待办性质）。
const { pendingContribs, loadErrors, isBusy, loadPending, acceptContribution, rejectContribution } = useContribute()
const { promptText } = useDialog()

async function onReject(c: ContributionItem) {
  const reason = await promptText({ title: '驳回贡献', message: `驳回「${c.author_name || c.author_id}」提交的《${c.question}》？`, placeholder: '驳回原因（可空）', confirmText: '驳回', danger: true })
  if (reason === null) return
  void rejectContribution(c, reason || '')
}
</script>

<template>
  <section v-if="pendingContribs.length || loadErrors['pending']">
    <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">贡献审核</p>
    <LoadError class="mb-2.5" :message="loadErrors['pending']" @retry="loadPending()" />
    <div v-if="pendingContribs.length" class="overflow-hidden rounded-[15px] border border-border bg-card">
      <!-- 橙头（待办） -->
      <div class="flex items-center gap-2.5 border-b border-border bg-st-warn/8 px-[18px] py-3">
        <ClipboardCheck :size="16" :stroke-width="1.75" class="text-st-warn" />
        <span class="text-sm font-semibold text-foreground">贡献审核</span>
        <span class="rounded-full bg-st-warn px-2 py-px text-[11px] font-bold text-white">{{ pendingContribs.length }}</span>
      </div>
      <div
        v-for="c in pendingContribs" :key="c.contribution_id"
        class="border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <div class="text-[13.5px] font-semibold text-foreground">{{ c.question }}</div>
        <div class="mt-1.5 line-clamp-2 whitespace-pre-wrap text-[12px] leading-relaxed text-muted-foreground">{{ c.content }}</div>
        <div class="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
          <span class="inline-flex items-center gap-1 text-[11px] text-faint">
            <User :size="11" :stroke-width="2" /> {{ c.author_name || c.author_id }}
          </span>
          <span class="text-[11px] text-faint">· {{ deptLabel(c.category_dept) }}</span>
          <span v-if="c.created_at" class="text-[11px] text-faint">· {{ c.created_at }}</span>
          <div class="flex-1" />
          <button
            type="button" :disabled="isBusy(`ct:${c.contribution_id}`)"
            class="rounded-lg border border-border px-3.5 py-[6px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
            @click="onReject(c)"
          >驳回</button>
          <button
            type="button" :disabled="isBusy(`ct:${c.contribution_id}`)"
            class="rounded-lg bg-primary px-3.5 py-[6px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
            @click="acceptContribution(c)"
          >采纳</button>
        </div>
      </div>
    </div>
  </section>
</template>
