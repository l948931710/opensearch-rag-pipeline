<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { History, X } from 'lucide-vue-next'
import { useKb } from '@/composables/useKb'
import StatusPill from './StatusPill.vue'

// 版本历史弹窗（Atlas 时间线）：数据来自 /api/kb/version-history（后端现成）；每版显示徽章 + 时间 + 报错。
const { verHistory, closeHistory } = useKb()
function onKey(e: KeyboardEvent) { if (e.key === 'Escape') closeHistory() }
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div
    v-if="verHistory"
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6"
    @click.self="closeHistory"
  >
    <div class="flex max-h-[84vh] w-[480px] max-w-full flex-col overflow-hidden rounded-2xl border border-border bg-card shadow-2xl">
      <!-- 头 -->
      <div class="flex items-center gap-2.5 border-b border-border px-[22px] py-[18px]">
        <History :size="18" :stroke-width="1.8" class="shrink-0 text-accent-text" />
        <div class="min-w-0 flex-1">
          <div class="text-[15px] font-semibold text-foreground">版本历史</div>
          <div class="truncate text-xs text-muted-foreground">{{ verHistory.doc?.title || verHistory.doc?.original_filename || verHistory.doc?.doc_id }}</div>
        </div>
        <button type="button" class="grid size-7 shrink-0 place-items-center rounded-lg text-muted-foreground transition hover:bg-secondary hover:text-foreground" aria-label="关闭" @click="closeHistory">
          <X :size="16" :stroke-width="2" />
        </button>
      </div>

      <!-- 体 -->
      <div class="min-h-0 flex-1 overflow-y-auto px-[22px] py-4">
        <div v-if="verHistory.loading" class="py-10 text-center text-sm text-muted-foreground">加载中…</div>
        <div v-else-if="verHistory.error" class="py-10 text-center text-sm text-destructive">{{ verHistory.error }}</div>
        <div v-else-if="!verHistory.versions.length" class="py-10 text-center text-sm text-muted-foreground">暂无版本记录</div>
        <template v-else>
          <div v-for="(h, i) in verHistory.versions" :key="h.version_no" class="flex gap-3.5 pb-4 last:pb-0">
            <!-- 时间线轴 -->
            <div class="flex shrink-0 flex-col items-center pt-1">
              <span class="size-2.5 rounded-full" :class="i === 0 ? 'bg-accent-strong' : 'bg-border-strong'" />
              <span v-if="i < verHistory.versions.length - 1" class="mt-1 w-px flex-1 bg-border" />
            </div>
            <!-- 该版内容 -->
            <div class="min-w-0 flex-1 pb-1">
              <div class="flex items-center gap-2.5">
                <span class="font-mono text-[13px] font-bold text-foreground">v{{ h.version_no }}</span>
                <StatusPill :badge="h.status_badge" />
                <div class="flex-1" />
                <span class="shrink-0 font-mono text-[11.5px] text-faint">{{ (h.created_at || '').slice(0, 16) }}</span>
              </div>
              <p v-if="h.error_message" class="mt-1.5 text-[11.5px] leading-relaxed text-st-fail">{{ h.error_message }}</p>
            </div>
          </div>
        </template>
      </div>

      <!-- 脚 -->
      <div class="border-t border-border px-[22px] py-2.5 text-center text-[11px] text-faint">
        仅展示该文档的版本演进 · 数据来自 /api/kb/version-history
      </div>
    </div>
  </div>
</template>
