<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { ArrowUp, Square, Brain, Bot, Activity } from '@lucide/vue'

// 输入框：内嵌发送/停止按钮 + 深度思考开关；Enter 发送 / Shift+Enter 换行；单行自增高到上限。
// Agent canary：agentAvailable（能力探测通过）才渲染「Agent 模式」开关与「运行」入口——
// flag 未开/无权绝不留死入口。「深度思考」在 agent 模式下同样生效（后端把开关映射为
// 模型档 light→high，见 routes/agent.py），故两开关并存、不再互斥。
const props = defineProps<{
  modelValue: string; asking: boolean; hasMessages: boolean; thinking: boolean
  agentAvailable?: boolean; agentMode?: boolean
}>()
const emit = defineEmits<{
  'update:modelValue': [v: string]; submit: []; stop: []; 'toggle-thinking': []
  'toggle-agent': []; 'open-runs': []
}>()

const ta = ref<HTMLTextAreaElement | null>(null)

function grow() {
  nextTick(() => { const t = ta.value; if (t) { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 160) + 'px' } })
}
function onInput(e: Event) { emit('update:modelValue', (e.target as HTMLTextAreaElement).value); grow() }
function onKey(e: KeyboardEvent) {
  if (e.isComposing || (e as { keyCode?: number }).keyCode === 229) return   // 输入法选词回车不当作发送
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!props.asking) emit('submit') }
}
watch(() => props.modelValue, () => grow())
defineExpose({ focus: () => ta.value?.focus() })
</script>

<template>
  <div class="mx-auto w-full max-w-3xl xl:max-w-4xl px-4">
    <div class="rounded-2xl border border-input bg-card px-3 py-2 shadow-sm
                transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/15">
      <div class="flex items-end gap-2">
        <textarea
          ref="ta"
          data-testid="chat-input"
          :value="modelValue"
          rows="1"
          placeholder="问点什么…（Enter 发送 · Shift+Enter 换行）"
          class="max-h-40 min-h-9 flex-1 resize-none bg-transparent py-1.5 text-sm leading-6 text-foreground
                 placeholder:text-muted-foreground focus:outline-none"
          @input="onInput"
          @keydown="onKey"
        />
        <button
          type="button"
          data-testid="chat-send"
          class="grid size-9 shrink-0 place-items-center rounded-xl bg-primary text-primary-foreground transition
                 hover:opacity-90 active:scale-95 disabled:cursor-not-allowed disabled:opacity-30"
          :disabled="!asking && !modelValue.trim()"
          :title="asking ? '停止' : '发送'"
          :aria-label="asking ? '停止' : '发送'"
          @click="asking ? emit('stop') : emit('submit')"
        >
          <Square v-if="asking" :size="16" fill="currentColor" stroke-width="0" />
          <ArrowUp v-else :size="18" :stroke-width="2.2" />
        </button>
      </div>
      <!-- 深度思考 / Agent 模式 开关（内嵌左下）+ 运行中心入口（右下，仅 agent 可用时） -->
      <div class="mt-1 flex items-center gap-1.5">
        <button
          type="button"
          data-testid="think-toggle"
          class="flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition"
          :class="thinking ? 'border-accent-soft bg-accent text-accent-foreground' : 'border-border text-muted-foreground hover:bg-panel'"
          :aria-pressed="thinking"
          title="深度思考：更慢但更深入（逐问生效）"
          @click="emit('toggle-thinking')"
        >
          <Brain :size="14" :stroke-width="1.75" /> 深度思考
        </button>
        <button
          v-if="agentAvailable"
          type="button"
          data-testid="agent-toggle"
          class="flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition"
          :class="agentMode ? 'border-accent-soft bg-accent text-accent-foreground' : 'border-border text-muted-foreground hover:bg-panel'"
          :aria-pressed="agentMode"
          title="Agent 模式（试点）：可调用工具作答，高风险操作先送审批；随时可关回普通问答"
          @click="emit('toggle-agent')"
        >
          <Bot :size="14" :stroke-width="1.75" /> Agent 模式
        </button>
        <button
          v-if="agentAvailable"
          type="button"
          data-testid="agent-runs-entry"
          class="ml-auto flex items-center gap-1 rounded-full px-2.5 py-1 text-xs text-muted-foreground transition hover:bg-panel hover:text-foreground"
          title="Agent 运行中心：我的运行、审批挂起与工具回执"
          @click="emit('open-runs')"
        >
          <Activity :size="13" :stroke-width="1.75" /> 运行
        </button>
      </div>
    </div>
    <p v-if="hasMessages" class="mt-2 text-center text-xs text-muted-foreground">
      答案来自富岭内部文档；可见范围按你的部门权限过滤。
    </p>
  </div>
</template>
