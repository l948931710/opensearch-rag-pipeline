<script setup lang="ts">
import { ref } from 'vue'
import { FileText, RefreshCw, PencilLine } from 'lucide-vue-next'
import { useContribute, type ContributionItem } from '@/composables/useContribute'
import { fmtTs } from '@/lib/kb'
import LoadError from '@/components/manage/LoadError.vue'
import ContribBadge from './ContribBadge.vue'

// 我的贡献：4 态徽章（待审核 / 已采纳·待入库 / 已入库 / 入库失败）+ 入库失败可重试。
// 批次ε-2：行可展开看原稿（此前 content 完全不渲染，被驳回后原稿不可见=只能凭记忆重打）；
// 被驳回行「修改重交」带旧稿重开表单（新 contribution_id 重提，原驳回记录保留审计）；
// failed 行透出失败原因（ingestion_error，老后端缺字段→兜底句）。
const { myContribs, loadErrors, isBusy, loadMine, retryContribution, openModal } = useContribute()

const expanded = ref<Record<string, boolean>>({})
function toggleExpand(id: string) { expanded.value = { ...expanded.value, [id]: !expanded.value[id] } }

// 修改重交：带旧稿+原归属重开贡献弹窗；继承缺口溯源（rejected 行不占缺口坑位，
// 新提交接上原缺口的关闭链路）。提交走既有 POST（新 contribution_id）。
function reopen(c: ContributionItem) {
  openModal({
    question: c.question, content: c.content, dept: c.category_dept,
    sourceMessageId: c.source_message_id || undefined,
    gapQuery: c.gap_query || undefined,
  })
}
</script>

<template>
  <!-- 卡头已带图标+标题，不再另设分区眉标 -->
  <section>
    <LoadError class="mb-2.5" :message="loadErrors['mine']" @retry="loadMine()" />
    <div class="overflow-hidden rounded-[15px] border border-border bg-card">
      <div class="flex items-center gap-2.5 border-b border-border px-[18px] py-3">
        <FileText :size="16" :stroke-width="1.75" class="text-accent-text" />
        <span class="text-sm font-semibold text-foreground">我的贡献</span>
      </div>
      <div
        v-for="c in myContribs" :key="c.contribution_id"
        class="border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <div class="flex items-start gap-3">
          <button
            type="button" data-testid="mycontrib-toggle"
            class="min-w-0 flex-1 text-left"
            :aria-expanded="!!expanded[c.contribution_id]"
            @click="toggleExpand(c.contribution_id)"
          >
            <div class="truncate text-[13px] font-medium text-foreground">{{ c.question }}</div>
            <div class="mt-1 text-[11px] text-faint">
              {{ fmtTs(c.created_at) }}
              <span v-if="c.state === 'rejected'"> · {{ c.review_note || '未填写驳回理由' }}</span>
              <span v-else-if="c.review_note"> · {{ c.review_note }}</span>
            </div>
          </button>
          <div class="flex shrink-0 items-center gap-1.5">
            <!-- 被引用数（批次ε-2 R2）：已入库行的价值反馈；算不出（null/老后端）自隐，0=真零照显 -->
            <span
              v-if="c.state === 'searchable' && c.hits != null" data-testid="mycontrib-hits"
              class="rounded bg-accent-soft px-1.5 py-px text-[10.5px] font-medium tabular-nums text-accent-text"
              :title="`这条知识被引用进 ${c.hits} 次回答（累计）`"
            >被引用 {{ c.hits }} 次</span>
            <ContribBadge :state="c.state" />
            <button
              v-if="c.state === 'failed'" type="button" :disabled="isBusy(`ct:${c.contribution_id}`)"
              class="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
              @click="retryContribution(c)"
            ><RefreshCw :size="11" :stroke-width="2" /> 重试</button>
            <button
              v-if="c.state === 'rejected'" type="button" data-testid="mycontrib-reopen"
              class="inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 text-[11.5px] font-medium text-accent-text transition hover:border-accent-strong hover:bg-accent-soft"
              @click="reopen(c)"
            ><PencilLine :size="11" :stroke-width="2" /> 修改重交</button>
          </div>
        </div>
        <!-- failed：失败原因透出（ingestion_error 空 → 兜底句，不留空白） -->
        <p
          v-if="c.state === 'failed'" data-testid="mycontrib-fail-reason"
          class="mt-1.5 text-[11.5px] leading-relaxed text-st-fail"
        >{{ c.ingestion_error || '入库失败，可重试；反复失败请联系管理员。' }}</p>
        <!-- 展开：原稿全文（被驳回后修改重交前先能看到自己写了什么） -->
        <div
          v-if="expanded[c.contribution_id]" data-testid="mycontrib-content"
          class="mt-2 whitespace-pre-wrap rounded-[10px] bg-panel/60 px-3 py-2.5 text-[12px] leading-relaxed text-muted-foreground"
        >{{ c.content }}</div>
      </div>
      <p v-if="!myContribs.length" class="px-[18px] py-8 text-center text-[12.5px] text-muted-foreground">还没有贡献，去「待回答」挑一个问题回答吧。</p>
    </div>
  </section>
</template>
