<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ThumbsDown, Check, Ban, RotateCcw, CheckCircle2, MessageSquareText } from 'lucide-vue-next'
import { deptLabel } from '@/lib/kb'
import { useKb } from '@/composables/useKb'
import LoadError from './LoadError.vue'

// 差评联动复核（看板卡片）：引用了本作用域文档的回答收到 👎 —— 逐条列出脱敏提问 +
// 点踩原因 + 用户补充说明 + 涉及文档，并可一键处置（已修复/忽略/重开）。这是「文档质量 →
// 答案质量」最直接的改进线索：看清用户嫌哪儿不对 → 修文档或去知识贡献补充 → 标记闭环。
const {
  feedbackReview, loadFeedbackReview, loadErrors,
  showResolvedFeedback, toggleShowResolvedFeedback, resolveFeedback, feedbackResolveBusy,
} = useKb()

const openCount = computed(() => (feedbackReview.value || []).filter((x) => !x.handled).length)
function busy(id: string) { return feedbackResolveBusy.value.has(id) }

// 「差评 → 修文档」补断层：涉及文档 chip 点击 = 切「文档管理」tab + 台账按标题定位。
// 走 URL（tab+q）→ ManageView 路由 watcher 切 tab → DocTable 挂载时从 URL 恢复搜索,零新机制。
const router = useRouter()
const route = useRoute()
function gotoDoc(d: { doc_id: string; title?: string }) {
  void router?.replace({ query: { ...(route?.query || {}), tab: 'docs', q: d.title || d.doc_id } })
  setTimeout(() => document.getElementById('kb-sec-ledger')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 400)
}
</script>

<template>
  <div>
    <div class="mb-2 flex items-center justify-between gap-2">
      <span class="text-[11.5px] text-faint">
        <template v-if="showResolvedFeedback">含已处理</template>
        <template v-else>仅未处理<b v-if="openCount" class="ml-1 font-mono text-st-fail">{{ openCount }}</b></template>
      </span>
      <button
        type="button"
        class="rounded-md border border-border px-2.5 py-1 text-[11.5px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground"
        @click="toggleShowResolvedFeedback"
      >{{ showResolvedFeedback ? '只看未处理' : '显示已处理' }}</button>
    </div>

    <LoadError :message="loadErrors['feedbackReview']" @retry="loadFeedbackReview()" />
    <div v-if="feedbackReview === null && !loadErrors['feedbackReview']" class="rounded-[14px] border border-dashed border-border bg-card/60 p-5 text-[12.5px] text-muted-foreground">
      差评复核拉取中…
    </div>
    <div v-else-if="feedbackReview && !feedbackReview.length" class="rounded-[14px] border border-border bg-card p-5 text-[12.5px] text-muted-foreground">
      {{ showResolvedFeedback ? '近期无「引用本部门文档且被点踩」的回答 —— 保持。' : '未处理的差评已清空 —— 干得漂亮。' }}
    </div>
    <div v-else-if="feedbackReview" class="overflow-hidden rounded-[14px] border border-border bg-card">
      <div
        v-for="it in feedbackReview" :key="it.message_id"
        class="border-b border-border px-4 py-3 last:border-b-0"
        :data-handled="it.handled ? '1' : '0'"
        :class="it.handled ? 'bg-panel/40' : ''"
      >
        <div class="flex items-start gap-3">
          <span class="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg" :class="it.handled ? 'bg-st-live/10 text-st-live' : 'bg-st-fail/10 text-st-fail'">
            <CheckCircle2 v-if="it.handled" :size="13" :stroke-width="1.75" />
            <ThumbsDown v-else :size="13" :stroke-width="1.75" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="text-[13px] font-medium text-foreground" :class="it.handled ? 'line-through decoration-faint/60' : ''">{{ it.question || '（无提问文本）' }}</div>
            <!-- 点踩原因（用户多选）：一眼看清「嫌哪儿不对」 -->
            <div v-if="it.reasons.length" class="mt-1 flex flex-wrap items-center gap-1">
              <span
                v-for="r in it.reasons" :key="r"
                class="rounded bg-st-fail/10 px-1.5 py-0.5 text-[10.5px] font-medium text-st-fail"
              >{{ r }}</span>
            </div>
            <!-- 用户补充说明（已脱敏）：修文档最关键的一手线索 -->
            <div v-if="it.comment" class="mt-1.5 flex items-start gap-1.5 rounded-md bg-panel px-2 py-1.5 text-[11.5px] text-muted-foreground">
              <MessageSquareText :size="12" :stroke-width="1.75" class="mt-0.5 shrink-0 text-faint" />
              <span class="min-w-0">{{ it.comment }}</span>
            </div>
            <!-- 涉及文档 chips：点击直达台账该文档（修文档是差评闭环的下一步，原先是死文本要自己去搜） -->
            <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
              <button
                v-for="d in it.docs" :key="d.doc_id" type="button"
                class="rounded-full border border-border bg-panel px-2 py-0.5 text-[10.5px] text-muted-foreground transition hover:border-accent-strong hover:bg-accent-soft hover:text-accent-text"
                :title="`在文档台账中定位（${d.doc_id}）`"
                @click="gotoDoc(d)"
              >{{ d.title || d.doc_id }} · {{ deptLabel(d.owner_dept) }}</button>
            </div>
          </div>
          <span class="shrink-0 font-mono text-[10.5px] text-faint">{{ (it.created_at || '').slice(0, 16) }}</span>
        </div>
        <!-- 处置动作条：闭环最后一步 -->
        <div class="mt-2 flex items-center justify-end gap-1.5 pl-10">
          <template v-if="!it.handled">
            <button
              type="button" :disabled="busy(it.message_id)"
              class="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground disabled:opacity-50"
              @click="resolveFeedback(it.message_id, 'dismiss')"
            ><Ban :size="11" :stroke-width="1.75" /> 忽略</button>
            <button
              type="button" :disabled="busy(it.message_id)"
              class="inline-flex items-center gap-1 rounded-md bg-st-live/10 px-2.5 py-1 text-[11px] font-semibold text-st-live transition hover:bg-st-live/15 disabled:opacity-50"
              @click="resolveFeedback(it.message_id, 'resolve')"
            ><Check :size="11" :stroke-width="2.5" /> 标记已处理</button>
          </template>
          <template v-else>
            <span class="text-[10.5px] text-faint">{{ it.handled_status === 'DISMISSED' ? '已忽略' : '已处理' }}</span>
            <button
              type="button" :disabled="busy(it.message_id)"
              class="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground disabled:opacity-50"
              @click="resolveFeedback(it.message_id, 'reopen')"
            ><RotateCcw :size="11" :stroke-width="1.75" /> 重开</button>
          </template>
        </div>
      </div>
    </div>
    <p class="ml-0.5 mt-2 text-[11.5px] text-faint">
      这些回答实际引用了本部门文档却被用户点踩——按原因/补充说明核对文档是否过时/表述不清；改好后点「标记已处理」；确有缺口可
      <RouterLink to="/contribute" class="font-semibold text-accent-text transition hover:underline">去知识贡献补充 →</RouterLink>
    </p>
  </div>
</template>
