<script setup lang="ts">
import { Clock, FileText } from 'lucide-vue-next'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb, type PendingItem } from '@/composables/useKb'
import LoadError from './LoadError.vue'
import { useDialog } from '@/composables/useDialog'

// 待审批队列：仅 kb_admin 可见（后端 /pending-approvals 也会 403 兜底）。Atlas 式：带橙头的卡 + 行。
const { approvals, isBusy, isKbAdmin, approve, reject, loadApprovals, loadErrors } = useKb()
const { promptText } = useDialog()
const rowKey = (d: PendingItem) => `appr:${d.doc_id}/${d.version_no}`

async function onReject(d: PendingItem) {
  const reason = await promptText({ title: '驳回上传', message: `驳回《${d.title || d.original_filename || d.doc_id}》的上传？`, placeholder: '驳回原因（可空）', confirmText: '驳回', danger: true })
  if (reason === null) return   // 取消
  void reject(d, reason || 'rejected')
}
</script>

<template>
  <section v-if="isKbAdmin && (approvals.length || loadErrors['approvals'])">
    <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">待审批</p>
    <LoadError class="mb-2.5" :message="loadErrors['approvals']" @retry="loadApprovals()" />
    <div v-if="approvals.length" class="overflow-hidden rounded-[15px] border border-border bg-card">
      <!-- 橙头 -->
      <div class="flex items-center gap-2.5 border-b border-border bg-st-busy/[0.07] px-[18px] py-3">
        <Clock :size="16" :stroke-width="1.75" class="text-st-busy" />
        <span class="text-sm font-semibold text-foreground">待审批队列</span>
        <span class="rounded-full bg-st-busy px-2 py-px text-[11px] font-bold text-white">{{ approvals.length }}</span>
        <div class="flex-1" />
        <span class="hidden text-xs text-muted-foreground sm:inline">公开 / 跨组上传，需放行后入库</span>
      </div>
      <!-- 行 -->
      <div
        v-for="d in approvals" :key="d.doc_id + '/' + d.version_no"
        class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent-text">
          <FileText :size="16" :stroke-width="1.75" />
        </span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-semibold text-foreground">{{ d.title || d.original_filename || d.doc_id }}</div>
          <div class="truncate text-[11.5px] text-faint">
            {{ deptLabel(d.owner_dept) }} · {{ permLabel(d.permission_level) }} · v{{ d.version_no }}
            <span v-if="d.owner_name"> · 上传人 {{ d.owner_name }}</span>
          </div>
        </div>
        <button
          type="button"
          class="rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isBusy(rowKey(d))" @click="onReject(d)"
        >驳回</button>
        <button
          type="button"
          class="rounded-lg bg-primary px-3.5 py-[7px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          :disabled="isBusy(rowKey(d))" @click="approve(d)"
        >通过</button>
      </div>
    </div>
  </section>
</template>
