<script setup lang="ts">
import { computed } from 'vue'
import { ShieldAlert, Check, Ban, RotateCcw, CheckCircle2 } from '@lucide/vue'
import { deptLabel } from '@/lib/kb'
import { useKb } from '@/composables/useKb'
import { useDialog } from '@/composables/useDialog'
import LoadError from './LoadError.vue'

// 入库复审任务队列（盲区审计 P2-33）：spot_checker 权限泄露安全网/分类失败/成本隔离登记的
// PENDING 任务此前全仓无人出队——被标记"实时权限比 LLM 建议更宽松"的文档持续投放。
// kb_admin 专属。处置只写复审终态：实际整改用既有工具（可见范围/退役/重灌）。
const {
  reviewTasks, loadReviewTasks, loadErrors,
  showClosedReviewTasks, toggleShowClosedReviewTasks, resolveReviewTask, reviewTaskResolveBusy,
  reviewTasksHasMore,
} = useKb()
const { promptText } = useDialog()

const openCount = computed(() => (reviewTasks.value || []).filter((x) => !x.closed).length)
function busy(id: string) { return reviewTaskResolveBusy.value.has(id) }
function ageTone(d: number) { return d > 7 ? 'text-st-fail' : d > 2 ? 'text-st-busy' : 'text-faint' }

async function resolveWithComment(taskId: string) {
  const text = await promptText({
    title: '处置说明（可选）',
    message: '记录你核实/整改了什么（如：已改回 restricted / 已退役）。留空直接关闭。',
    placeholder: '处置说明…', confirmText: '标记已处理', maxlength: 500,
  })
  if (text === null) return
  await resolveReviewTask(taskId, 'resolve', text.trim())
}

const TYPE_LABEL: Record<string, string> = {
  spot_check_mismatch: '权限抽查不一致',
}
</script>

<template>
  <div>
    <div class="mb-2 flex items-center justify-between gap-2">
      <span class="text-[11.5px] text-faint">
        <template v-if="showClosedReviewTasks">含已处理</template>
        <template v-else>仅未处理<b v-if="openCount" class="ml-1 font-mono text-st-fail">{{ openCount }}</b>（老单在前）</template>
      </span>
      <button
        type="button"
        class="rounded-md border border-border px-2.5 py-1 text-[11.5px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground"
        @click="toggleShowClosedReviewTasks"
      >{{ showClosedReviewTasks ? '只看未处理' : '显示已处理' }}</button>
    </div>

    <LoadError :message="loadErrors['reviewTasks']" @retry="loadReviewTasks()" />
    <div v-if="reviewTasks === null && !loadErrors['reviewTasks']" class="rounded-[14px] border border-dashed border-border bg-card/60 p-5 text-[12.5px] text-muted-foreground">
      复审任务拉取中…
    </div>
    <div v-else-if="reviewTasks && !reviewTasks.length" class="rounded-[14px] border border-border bg-card p-5 text-[12.5px] text-muted-foreground">
      {{ showClosedReviewTasks ? '暂无复审任务。' : '没有待复审的安全任务 —— 安全网干净。' }}
    </div>
    <div v-else-if="reviewTasks" class="overflow-hidden rounded-[14px] border border-border bg-card">
      <div
        v-for="it in reviewTasks" :key="it.task_id"
        class="border-b border-border px-4 py-3 last:border-b-0"
        :class="it.closed ? 'bg-panel/40' : ''"
      >
        <div class="flex items-start gap-3">
          <span class="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg" :class="it.closed ? 'bg-st-live/10 text-st-live' : 'bg-st-fail/10 text-st-fail'">
            <CheckCircle2 v-if="it.closed" :size="13" :stroke-width="1.75" />
            <ShieldAlert v-else :size="13" :stroke-width="1.75" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="text-[13px] font-medium text-foreground" :class="it.closed ? 'line-through decoration-faint/60' : ''">
              {{ it.title || it.doc_id }} <span class="font-mono text-[10.5px] text-faint">v{{ it.version_no }}</span>
            </div>
            <div class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-faint">
              <span class="rounded bg-st-fail/10 px-1.5 py-0.5 font-medium text-st-fail">{{ TYPE_LABEL[it.review_type] || it.review_type }}</span>
              <span v-if="it.owner_dept">{{ deptLabel(it.owner_dept) }}</span>
              <span v-if="it.suggested_permission_level">建议级别：{{ it.suggested_permission_level }}</span>
              <span v-if="!it.closed" :class="ageTone(it.age_days)" class="font-medium">已等待 {{ it.age_days }} 天</span>
            </div>
            <div v-if="it.review_reason" class="mt-1.5 rounded-md bg-panel px-2 py-1.5 text-[11.5px] text-muted-foreground">
              {{ it.review_reason }}
            </div>
          </div>
          <span class="shrink-0 font-mono text-[10.5px] text-faint">{{ (it.created_at || '').slice(0, 16) }}</span>
        </div>
        <div class="mt-2 flex items-center justify-end gap-1.5 pl-10">
          <template v-if="!it.closed">
            <button
              type="button" :disabled="busy(it.task_id)"
              class="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground disabled:opacity-50"
              @click="resolveReviewTask(it.task_id, 'dismiss')"
            ><Ban :size="11" :stroke-width="1.75" /> 误报忽略</button>
            <button
              type="button" :disabled="busy(it.task_id)"
              class="inline-flex items-center gap-1 rounded-md bg-st-live/10 px-2.5 py-1 text-[11px] font-semibold text-st-live transition hover:bg-st-live/15 disabled:opacity-50"
              @click="resolveWithComment(it.task_id)"
            ><Check :size="11" :stroke-width="2.5" /> 已核实处理</button>
          </template>
          <template v-else>
            <span class="text-[10.5px] text-faint">
              {{ it.status === 'DISMISSED' ? '已忽略' : '已处理' }}<template v-if="it.reviewer_name"> · {{ it.reviewer_name }}</template>
            </span>
            <button
              type="button" :disabled="busy(it.task_id)"
              class="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground disabled:opacity-50"
              @click="resolveReviewTask(it.task_id, 'reopen')"
            ><RotateCcw :size="11" :stroke-width="1.75" /> 重开</button>
          </template>
        </div>
      </div>
    </div>
    <!-- P2-11：后端 limit=20 此前静默截断——复审任务是安全网承诺，第 21 条以后
         在界面上根本不存在。与审核队列/GapList 同款加载更多。 -->
    <div v-if="reviewTasksHasMore" class="mt-2 text-center">
      <button
        type="button" data-testid="review-task-load-more"
        class="rounded-lg border border-border px-4 py-1.5 text-[12.5px] font-medium text-foreground transition hover:border-border-strong"
        @click="loadReviewTasks((reviewTasks || []).length)"
      >加载更多</button>
    </div>
    <p class="ml-0.5 mt-2 text-[11.5px] text-faint">
      管线安全网登记的人工复审（权限抽查不一致等）——先用「谁能看到 / 调整可见范围 / 退役」核实并整改文档，再回来标记已处理。
    </p>
  </div>
</template>
