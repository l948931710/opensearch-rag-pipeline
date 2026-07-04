<script setup lang="ts">
import { ThumbsDown } from 'lucide-vue-next'
import { deptLabel } from '@/lib/kb'
import { useKb } from '@/composables/useKb'
import LoadError from './LoadError.vue'

// 差评联动复核（看板卡片）：引用了本作用域文档的回答收到 👎 —— 逐条列出脱敏提问 +
// 涉及文档。这是「文档质量 → 答案质量」最直接的改进线索：修文档或去知识贡献补充。
const { feedbackReview, loadFeedbackReview, loadErrors } = useKb()
</script>

<template>
  <div>
    <LoadError :message="loadErrors['feedbackReview']" @retry="loadFeedbackReview()" />
    <div v-if="feedbackReview === null && !loadErrors['feedbackReview']" class="rounded-[14px] border border-dashed border-border bg-card/60 p-5 text-[12.5px] text-muted-foreground">
      差评复核拉取中…
    </div>
    <div v-else-if="feedbackReview && !feedbackReview.length" class="rounded-[14px] border border-border bg-card p-5 text-[12.5px] text-muted-foreground">
      近期无「引用本部门文档且被点踩」的回答 —— 保持。
    </div>
    <div v-else-if="feedbackReview" class="overflow-hidden rounded-[14px] border border-border bg-card">
      <div
        v-for="it in feedbackReview" :key="it.message_id"
        class="flex items-start gap-3 border-b border-border px-4 py-3 last:border-b-0"
      >
        <span class="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-st-fail/10 text-st-fail"><ThumbsDown :size="13" :stroke-width="1.75" /></span>
        <div class="min-w-0 flex-1">
          <div class="text-[13px] font-medium text-foreground">{{ it.question || '（无提问文本）' }}</div>
          <div class="mt-1 flex flex-wrap items-center gap-1.5">
            <span
              v-for="d in it.docs" :key="d.doc_id"
              class="rounded-full border border-border bg-panel px-2 py-0.5 text-[10.5px] text-muted-foreground"
              :title="d.doc_id"
            >{{ d.title || d.doc_id }} · {{ deptLabel(d.owner_dept) }}</span>
          </div>
        </div>
        <span class="shrink-0 font-mono text-[10.5px] text-faint">{{ (it.created_at || '').slice(0, 16) }}</span>
      </div>
    </div>
    <p class="ml-0.5 mt-2 text-[11.5px] text-faint">
      这些回答实际引用了本部门文档却被用户点踩——优先核对文档内容是否过时/表述不清；确有缺口可
      <RouterLink to="/contribute" class="font-semibold text-accent-text transition hover:underline">去知识贡献补充 →</RouterLink>
    </p>
  </div>
</template>
