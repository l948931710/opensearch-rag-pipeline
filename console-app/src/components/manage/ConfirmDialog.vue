<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { AlertTriangle } from '@lucide/vue'
import { useDialog } from '@/composables/useDialog'

// 全局自定义 确认/输入/告知 对话框（替代原生 confirm/prompt/alert）。在 ManageView 挂一份即可。
const { dialog, onConfirm, onCancel } = useDialog()
const taRef = ref<HTMLTextAreaElement | null>(null)
const cancelRef = ref<HTMLButtonElement | null>(null)
const confirmRef = ref<HTMLButtonElement | null>(null)
const showCount = computed(() => dialog.value.kind === 'prompt')

// Esc 监听挂 window：模板 @keydown 只在焦点落于弹窗内时收得到事件——
// 纯确认框打开时焦点仍留在触发按钮上，Esc 会石沉大海（键盘用户关不掉）。
function onKey(e: KeyboardEvent) { if (e.key === 'Escape') { e.stopPropagation(); onCancel() } }

// 打开时的初始焦点：prompt→输入框；confirm→「取消」（危险操作的安全默认，回车不误确认）；notice→唯一按钮。
watch(() => dialog.value.open, (o) => {
  if (o) {
    window.addEventListener('keydown', onKey)
    void nextTick(() => {
      if (dialog.value.kind === 'prompt') taRef.value?.focus()
      else if (dialog.value.kind === 'notice') confirmRef.value?.focus()
      else cancelRef.value?.focus()
    })
  } else {
    window.removeEventListener('keydown', onKey)
  }
})
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div
    v-if="dialog.open"
    class="fixed inset-0 z-[90] flex items-center justify-center bg-black/40 p-6"
    role="dialog" aria-modal="true"
    @click="onCancel"
  >
    <div class="w-[440px] max-w-full overflow-hidden rounded-2xl border border-border bg-card shadow-xl" @click.stop>
      <div class="p-[22px] pb-0">
        <div class="mb-2.5 flex items-center gap-2.5">
          <span
            class="grid size-9 place-items-center rounded-[10px]"
            :class="dialog.danger ? 'bg-st-fail/10 text-st-fail' : 'bg-accent-soft text-accent-text'"
          ><AlertTriangle :size="18" :stroke-width="1.75" /></span>
          <span class="text-base font-semibold text-foreground">{{ dialog.title }}</span>
        </div>
        <p class="whitespace-pre-line text-[13px] leading-relaxed text-muted-foreground">{{ dialog.message }}</p>
        <div v-if="dialog.kind === 'prompt'" class="mt-3">
          <textarea
            ref="taRef" v-model="dialog.value" rows="3" :maxlength="dialog.maxlength" :placeholder="dialog.placeholder"
            class="w-full resize-none rounded-[10px] border border-input bg-surface px-3 py-2.5 text-[13px] leading-relaxed text-foreground focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15"
            @keydown.ctrl.enter="onConfirm" @keydown.meta.enter="onConfirm"
          />
          <div v-if="showCount" class="mt-1 text-right font-mono text-[10.5px] tabular-nums text-faint">{{ dialog.value.length }}/{{ dialog.maxlength }}</div>
        </div>
      </div>
      <div class="flex justify-end gap-2.5 px-[22px] py-4">
        <button
          v-if="dialog.kind !== 'notice'"
          ref="cancelRef" type="button"
          class="rounded-lg border border-border px-4 py-2 text-[13.5px] font-medium text-foreground transition hover:border-border-strong focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          @click="onCancel"
        >{{ dialog.cancelText }}</button>
        <button
          ref="confirmRef" type="button"
          class="rounded-lg px-4 py-2 text-[13.5px] font-semibold text-white transition hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :class="dialog.danger ? 'bg-st-fail' : 'bg-primary text-primary-foreground'"
          @click="onConfirm"
        >{{ dialog.confirmText }}</button>
      </div>
    </div>
  </div>
</template>
