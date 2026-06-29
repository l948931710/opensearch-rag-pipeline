<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { AlertTriangle } from 'lucide-vue-next'
import { useDialog } from '@/composables/useDialog'

// 全局自定义 确认/输入 对话框（替代原生 confirm/prompt）。在 ManageView 挂一份即可。
const { dialog, onConfirm, onCancel } = useDialog()
const taRef = ref<HTMLTextAreaElement | null>(null)
const showCount = computed(() => dialog.value.kind === 'prompt')

// 打开时聚焦输入框（prompt）；Esc/确认键由模板 @keydown 处理。
watch(() => dialog.value.open, (o) => {
  if (o && dialog.value.kind === 'prompt') nextTick(() => taRef.value?.focus())
})
</script>

<template>
  <div
    v-if="dialog.open"
    class="fixed inset-0 z-[90] flex items-center justify-center bg-black/40 p-6"
    role="dialog" aria-modal="true"
    @click="onCancel" @keydown.esc="onCancel"
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
        <button type="button" class="rounded-lg border border-border px-4 py-2 text-[13.5px] font-medium text-foreground transition hover:border-border-strong" @click="onCancel">{{ dialog.cancelText }}</button>
        <button
          type="button"
          class="rounded-lg px-4 py-2 text-[13.5px] font-semibold text-white transition hover:opacity-90"
          :class="dialog.danger ? 'bg-st-fail' : 'bg-primary text-primary-foreground'"
          @click="onConfirm"
        >{{ dialog.confirmText }}</button>
      </div>
    </div>
  </div>
</template>
