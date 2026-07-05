<script setup lang="ts">
import { computed } from 'vue'
import { Headset, Check, Ban, RotateCcw, CheckCircle2, Reply } from 'lucide-vue-next'
import { deptLabel } from '@/lib/kb'
import { useKb } from '@/composables/useKb'
import { useDialog } from '@/composables/useDialog'
import LoadError from './LoadError.vue'

// 转人工工单队列（盲区审计 P1-2）：钉钉「转人工」此前只写不读——工单落库后无人出队、
// 用户被告知"会有人跟进"却永远没有下文。这里是消费端：未处置按龄升序（老单顶置），
// 「答复并关闭」弹输入框收人工答案 → 后端 1 对 1 推回提问者钉钉，闭环信任恢复。
const {
  escalations, loadEscalations, loadErrors,
  showClosedEscalations, toggleShowClosedEscalations, resolveEscalation, escalationResolveBusy,
} = useKb()
const { promptText } = useDialog()

const openCount = computed(() => (escalations.value || []).filter((x) => !x.closed).length)
function busy(id: string) { return escalationResolveBusy.value.has(id) }

// 答复并关闭：收人工答案（可空——空答案=仅标记已跟进，不推钉钉消息）
async function answerAndClose(it: { ticket_id: string; question: string }) {
  const text = await promptText({
    title: '人工答复',
    message: `「${it.question}」\n答复内容会以钉钉消息发给提问者；留空则仅标记已跟进（不发消息）。`,
    placeholder: '输入人工答复…',
    confirmText: '答复并关闭',
    maxlength: 2000,
  })
  if (text === null) return
  await resolveEscalation(it.ticket_id, 'resolve', text.trim())
}

// 工单龄标色：≤2 天正常，3-7 天提醒，>7 天告急（SLA 视角，未处置才显）
function ageTone(d: number) { return d > 7 ? 'text-st-fail' : d > 2 ? 'text-st-busy' : 'text-faint' }
</script>

<template>
  <div>
    <div class="mb-2 flex items-center justify-between gap-2">
      <span class="text-[11.5px] text-faint">
        <template v-if="showClosedEscalations">含已处理</template>
        <template v-else>仅未处理<b v-if="openCount" class="ml-1 font-mono text-st-fail">{{ openCount }}</b>（老单在前）</template>
      </span>
      <button
        type="button"
        class="rounded-md border border-border px-2.5 py-1 text-[11.5px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground"
        @click="toggleShowClosedEscalations"
      >{{ showClosedEscalations ? '只看未处理' : '显示已处理' }}</button>
    </div>

    <LoadError :message="loadErrors['escalations']" @retry="loadEscalations()" />
    <div v-if="escalations === null && !loadErrors['escalations']" class="rounded-[14px] border border-dashed border-border bg-card/60 p-5 text-[12.5px] text-muted-foreground">
      转人工工单拉取中…
    </div>
    <div v-else-if="escalations && !escalations.length" class="rounded-[14px] border border-border bg-card p-5 text-[12.5px] text-muted-foreground">
      {{ showClosedEscalations ? '暂无转人工工单。' : '没有待处理的转人工工单 —— 用户的求助都被接住了。' }}
    </div>
    <div v-else-if="escalations" class="overflow-hidden rounded-[14px] border border-border bg-card">
      <div
        v-for="it in escalations" :key="it.ticket_id"
        class="border-b border-border px-4 py-3 last:border-b-0"
        :data-closed="it.closed ? '1' : '0'"
        :class="it.closed ? 'bg-panel/40' : ''"
      >
        <div class="flex items-start gap-3">
          <span class="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg" :class="it.closed ? 'bg-st-live/10 text-st-live' : 'bg-st-busy/10 text-st-busy'">
            <CheckCircle2 v-if="it.closed" :size="13" :stroke-width="1.75" />
            <Headset v-else :size="13" :stroke-width="1.75" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="text-[13px] font-medium text-foreground" :class="it.closed ? 'line-through decoration-faint/60' : ''">{{ it.question || '（无提问文本）' }}</div>
            <div class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-faint">
              <span v-if="it.user_name">提问：{{ it.user_name }}<template v-if="it.user_dept">（{{ it.user_dept }}）</template></span>
              <span v-if="!it.closed" :class="ageTone(it.age_days)" class="font-medium">
                已等待 {{ it.age_days }} 天
              </span>
            </div>
            <!-- AI 原答案节选（判断哪里没答好的上下文；空=当时没答上来） -->
            <div v-if="it.ai_answer_excerpt" class="mt-1.5 rounded-md bg-panel px-2 py-1.5 text-[11.5px] text-muted-foreground">
              AI 曾答：{{ it.ai_answer_excerpt }}
            </div>
            <!-- 人工答复回显（已处置） -->
            <div v-if="it.expert_answer" class="mt-1.5 flex items-start gap-1.5 rounded-md bg-st-live/5 px-2 py-1.5 text-[11.5px] text-muted-foreground">
              <Reply :size="12" :stroke-width="1.75" class="mt-0.5 shrink-0 text-st-live" />
              <span class="min-w-0">{{ it.expert_answer }}</span>
            </div>
            <!-- 涉及文档 chips（该回答实际引用的作用域内文档，答前先核对语料） -->
            <div v-if="it.docs.length" class="mt-1.5 flex flex-wrap items-center gap-1.5">
              <span
                v-for="d in it.docs" :key="d.doc_id"
                class="rounded-full border border-border bg-panel px-2 py-0.5 text-[10.5px] text-muted-foreground"
                :title="d.doc_id"
              >{{ d.title || d.doc_id }} · {{ deptLabel(d.owner_dept) }}</span>
            </div>
          </div>
          <span class="shrink-0 font-mono text-[10.5px] text-faint">{{ (it.created_at || '').slice(0, 16) }}</span>
        </div>
        <div class="mt-2 flex items-center justify-end gap-1.5 pl-10">
          <template v-if="!it.closed">
            <button
              type="button" :disabled="busy(it.ticket_id)"
              class="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground disabled:opacity-50"
              @click="resolveEscalation(it.ticket_id, 'dismiss')"
            ><Ban :size="11" :stroke-width="1.75" /> 忽略</button>
            <button
              type="button" :disabled="busy(it.ticket_id)"
              class="inline-flex items-center gap-1 rounded-md bg-st-live/10 px-2.5 py-1 text-[11px] font-semibold text-st-live transition hover:bg-st-live/15 disabled:opacity-50"
              @click="answerAndClose(it)"
            ><Check :size="11" :stroke-width="2.5" /> 答复并关闭</button>
          </template>
          <template v-else>
            <span class="text-[10.5px] text-faint">
              {{ it.status === 'DISMISSED' ? '已忽略' : '已答复' }}<template v-if="it.assigned_user_name"> · {{ it.assigned_user_name }}</template>
            </span>
            <button
              type="button" :disabled="busy(it.ticket_id)"
              class="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 text-[11px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground disabled:opacity-50"
              @click="resolveEscalation(it.ticket_id, 'reopen')"
            ><RotateCcw :size="11" :stroke-width="1.75" /> 重开</button>
          </template>
        </div>
      </div>
    </div>
    <p class="ml-0.5 mt-2 text-[11.5px] text-faint">
      用户点「转人工」的求助工单——「答复并关闭」会把你的答复以钉钉消息发给提问者；若语料本身有缺口，答完顺手
      <RouterLink to="/contribute" class="font-semibold text-accent-text transition hover:underline">去知识贡献补充 →</RouterLink>
    </p>
  </div>
</template>
