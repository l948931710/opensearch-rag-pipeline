<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ThumbsUp, ThumbsDown, Copy, Check } from '@lucide/vue'
import { useAsk, type ChatMessage } from '@/composables/useAsk'

// 内嵌式反馈条：复制、赞/踩（一次性互斥），全部常驻可见。统一调 /api/feedback，靠 message_id 关联。
// （转人工已下线 2026-07：语料缺口由知识贡献系统兜底。）
// 点踩不直接提交（与小程序 feedback-bar 同语义）：先展开内联原因面板，原因多选 + 选填说明
// 随 downvote 一次性提交；取消只收起、草稿保留；失败面板保持打开并就地报错。
// 面板状态与草稿只存本组件局部——挂到 message 上会被 persist()（剔除式序列化）写进 localStorage。
const props = defineProps<{ message: ChatMessage }>()
const { vote, copyAns } = useAsk()
const m = props.message

// 与小程序 REASONS 完全同 code/顺序/文案；提交按此固定顺序序列化，与点击顺序无关
// （feedback_reason 逗号拼接是运营看板聚合的口径，两端保持一致）。
const REASONS = [
  { code: 'inaccurate', label: '内容不准确' },
  { code: 'irrelevant', label: '答非所问' },
  { code: 'incomplete', label: '答案不完整' },
  { code: 'not_found', label: '没找到我要的文档' },
  { code: 'wrong_image', label: '图片不对' },
  { code: 'outdated', label: '信息过时' },
] as const

const panelOpen = ref(false)
const selected = ref(new Set<string>())
const comment = ref('')
const submitting = ref(false)
const submitError = ref(false)
const panelId = `fb-panel-${m.messageId}`
const downBtn = ref<HTMLButtonElement | null>(null)
const panelEl = ref<HTMLElement | null>(null)

function onUpvote(): void {
  panelOpen.value = false   // 小程序同语义：转投赞收起点踩面板
  vote(m, 'upvote')
}

function onDownvote(): void {
  if (m.voted) return
  panelOpen.value = !panelOpen.value
  // QaView 的滚动跟随签名串看不到组件局部状态，面板撑高消息时自行滚进可视区
  if (panelOpen.value) nextTick(() => panelEl.value?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' }))
}

function toggleReason(code: string): void {
  if (submitting.value) return
  if (selected.value.has(code)) selected.value.delete(code)
  else selected.value.add(code)
}

function onCancel(): void {
  if (submitting.value) return
  panelOpen.value = false   // 草稿保留，再开还在
  downBtn.value?.focus?.()
}

const canSubmit = computed(() => (selected.value.size > 0 || comment.value.trim() !== '') && !submitting.value)

async function onSubmit(): Promise<void> {
  if (!canSubmit.value) return
  submitError.value = false
  submitting.value = true
  try {
    const reason = REASONS.filter((r) => selected.value.has(r.code)).map((r) => r.code).join(',')
    const ok = await vote(m, 'downvote', { reason, comment: comment.value })
    if (ok) panelOpen.value = false   // 票已锁，草稿留在内存但不再有入口
    else submitError.value = true     // 失败：面板保持打开、草稿保留、就地报错
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div>
    <div class="mt-2 flex items-center gap-0.5 text-muted-foreground">
      <button
        type="button" class="grid size-7 place-items-center rounded-md transition hover:bg-secondary hover:text-foreground"
        :class="{ '!text-st-live': m.copied }" :title="m.copied ? '已复制' : '复制'" aria-label="复制"
        @click="copyAns(m)"
      >
        <Check v-if="m.copied" :size="15" :stroke-width="2" />
        <Copy v-else :size="15" :stroke-width="1.75" />
      </button>

      <span class="mx-1 h-3.5 w-px bg-border" />
      <button
        type="button" class="grid size-7 place-items-center rounded-md transition hover:bg-secondary hover:text-foreground disabled:opacity-100"
        :class="{ '!text-st-live': m.voted === 'up' }" :disabled="!!m.voted" title="有用" aria-label="有用"
        @click="onUpvote"
      >
        <ThumbsUp :size="15" :stroke-width="1.75" />
      </button>
      <button
        ref="downBtn"
        type="button" class="grid size-7 place-items-center rounded-md transition hover:bg-secondary hover:text-foreground disabled:opacity-100"
        :class="{ '!text-st-fail': m.voted === 'down' || panelOpen }" :disabled="!!m.voted" title="没用" aria-label="没用"
        :aria-expanded="panelOpen" :aria-controls="panelId"
        @click="onDownvote"
      >
        <ThumbsDown :size="15" :stroke-width="1.75" />
      </button>
      <!-- 锁票后的宣告（role=status：出现时读屏播报，视觉与小程序「已反馈」对齐）。
           gate 在 !submitting：乐观置态在途不播报，失败回滚时不会出现「已反馈→提交失败」矛盾序列 -->
      <span v-if="m.voted && !submitting" role="status" class="ml-1.5 text-xs">已反馈</span>
    </div>

    <!-- 点踩原因面板：内联展开不包 Transition（与 SourceList/ThinkingDisclosure 现状模式一致） -->
    <div v-if="panelOpen" :id="panelId" ref="panelEl" class="mt-2 max-w-xl rounded-[10px] border border-border p-3">
      <p class="text-xs text-muted-foreground">请选择不满意的原因（可多选）：</p>
      <div class="mt-2 flex flex-wrap gap-1.5">
        <button
          v-for="r in REASONS" :key="r.code" type="button"
          class="rounded-full border px-2.5 py-1 text-[12.5px] transition"
          :class="selected.has(r.code)
            ? 'border-accent-strong bg-accent-soft font-medium text-accent-text'
            : 'border-border text-muted-foreground hover:border-ring'"
          :aria-pressed="selected.has(r.code)" :disabled="submitting"
          @click="toggleReason(r.code)"
        >{{ r.label }}</button>
      </div>
      <textarea
        v-model="comment" maxlength="200" rows="2" :disabled="submitting"
        aria-label="其他原因说明（选填）" placeholder="其他原因（选填）…"
        class="mt-2.5 w-full resize-none rounded-md border border-border bg-transparent px-2.5 py-1.5 text-[13px] text-foreground placeholder:text-muted-foreground/70 focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15"
      />
      <div class="mt-0.5 text-right text-[11px] text-muted-foreground">{{ comment.length }}/200</div>
      <p v-if="submitError" role="alert" aria-atomic="true" class="mt-1 text-xs text-st-fail">提交失败，请重试</p>
      <div class="mt-2 flex justify-end gap-2">
        <button
          type="button" class="rounded-lg border border-border px-3 py-1.5 text-xs text-muted-foreground transition hover:text-foreground"
          :disabled="submitting" @click="onCancel"
        >取消</button>
        <button
          type="button" class="rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition disabled:opacity-50"
          :disabled="!canSubmit" @click="onSubmit"
        >{{ submitting ? '提交中…' : '提交' }}</button>
      </div>
    </div>
  </div>
</template>
