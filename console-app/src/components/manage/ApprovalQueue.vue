<script setup lang="ts">
import { Check, X } from 'lucide-vue-next'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb, type PendingItem } from '@/composables/useKb'

// 待审批队列：仅 kb_admin 可见（后端 /pending-approvals 也会 403 兜底）。
const { approvals, apprBusy, isKbAdmin, approve, reject } = useKb()

function onReject(d: PendingItem) {
  const reason = prompt('驳回原因（可空）：', '')
  if (reason === null) return   // 取消
  void reject(d, reason || 'rejected')
}
</script>

<template>
  <section v-if="isKbAdmin && approvals.length" class="rounded-xl border border-st-warn/30 bg-st-warn/5 p-5">
    <h2 class="flex items-center gap-2 text-sm font-bold text-foreground">
      待审批
      <span class="rounded px-1.5 py-0.5 text-[11px] font-medium text-st-warn bg-st-warn/15">{{ approvals.length }}</span>
    </h2>
    <p class="mt-1 text-xs text-muted-foreground">公开 / 跨组上传，需知识库管理员放行后才进入入库。</p>

    <div class="mt-3 space-y-2">
      <div v-for="d in approvals" :key="d.doc_id + '/' + d.version_no" class="flex items-center gap-3 rounded-lg border border-border bg-card px-3 py-2.5">
        <div class="min-w-0 flex-1">
          <div class="truncate text-sm font-medium text-foreground">{{ d.title || d.original_filename || d.doc_id }}</div>
          <div class="truncate text-xs text-muted-foreground">
            {{ deptLabel(d.owner_dept) }} · {{ permLabel(d.permission_level) }} · v{{ d.version_no }}
            <span v-if="d.owner_name"> · {{ d.owner_name }}</span>
          </div>
        </div>
        <button type="button" class="flex items-center gap-1 rounded-md px-2.5 py-1.5 text-xs text-st-live transition hover:bg-st-live/10 disabled:opacity-50" :disabled="apprBusy" @click="approve(d)">
          <Check :size="14" :stroke-width="2" /> 通过
        </button>
        <button type="button" class="flex items-center gap-1 rounded-md px-2.5 py-1.5 text-xs text-st-fail transition hover:bg-st-fail/10 disabled:opacity-50" :disabled="apprBusy" @click="onReject(d)">
          <X :size="14" :stroke-width="2" /> 驳回
        </button>
      </div>
    </div>
  </section>
</template>
