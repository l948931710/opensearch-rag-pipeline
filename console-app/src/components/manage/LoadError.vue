<script setup lang="ts">
import { AlertTriangle, RotateCw } from 'lucide-vue-next'

// 分区加载失败提示条：仅在 message 非空（5xx/网络，非 404 未上线）时显示，带「重试」回调。
defineProps<{ message?: string }>()
defineEmits<{ retry: [] }>()
</script>

<template>
  <div
    v-if="message"
    class="flex items-center justify-between gap-2 rounded-lg border border-st-fail/30 bg-st-fail/5 px-3 py-2 text-xs text-st-fail"
    role="alert"
  >
    <span class="flex items-center gap-1.5"><AlertTriangle :size="13" :stroke-width="1.75" /> {{ message }}</span>
    <button
      type="button"
      class="inline-flex items-center gap-1 rounded-md border border-st-fail/40 px-2 py-0.5 font-medium transition hover:bg-st-fail/10"
      @click="$emit('retry')"
    ><RotateCw :size="12" :stroke-width="1.75" /> 重试</button>
  </div>
</template>
