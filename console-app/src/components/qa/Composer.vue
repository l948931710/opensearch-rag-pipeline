<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { ArrowUp, Square, Brain } from 'lucide-vue-next'

// 输入框：内嵌发送/停止按钮 + 深度思考开关；Enter 发送 / Shift+Enter 换行；单行自增高到上限。
const props = defineProps<{ modelValue: string; asking: boolean; hasMessages: boolean; thinking: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [v: string]; submit: []; stop: []; 'toggle-thinking': [] }>()

const ta = ref<HTMLTextAreaElement | null>(null)

function grow() {
  nextTick(() => { const t = ta.value; if (t) { t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 160) + 'px' } })
}
function onInput(e: Event) { emit('update:modelValue', (e.target as HTMLTextAreaElement).value); grow() }
function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!props.asking) emit('submit') }
}
watch(() => props.modelValue, () => grow())
defineExpose({ focus: () => ta.value?.focus() })
</script>

<template>
  <div class="mx-auto w-full max-w-3xl px-4">
    <div class="rounded-2xl border border-input bg-card px-3 py-2 shadow-sm
                transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring/15">
      <div class="flex items-end gap-2">
        <textarea
          ref="ta"
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
      <!-- 深度思考开关（逐问，内嵌左下） -->
      <div class="mt-1 flex items-center">
        <button
          type="button"
          class="flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition"
          :class="thinking ? 'border-primary/40 bg-accent text-accent-foreground' : 'border-border text-muted-foreground hover:bg-secondary'"
          :aria-pressed="thinking"
          title="深度思考：更慢但更深入（逐问生效）"
          @click="emit('toggle-thinking')"
        >
          <Brain :size="14" :stroke-width="1.75" /> 深度思考
        </button>
      </div>
    </div>
    <p v-if="hasMessages" class="mt-2 text-center text-xs text-muted-foreground">
      答案来自富岭内部文档；可见范围按你的部门权限过滤。
    </p>
  </div>
</template>
