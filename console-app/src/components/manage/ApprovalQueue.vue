<script setup lang="ts">
import { Clock, FileText, Loader2, ExternalLink } from '@lucide/vue'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb, type PendingItem } from '@/composables/useKb'
import LoadError from './LoadError.vue'
import { useDialog } from '@/composables/useDialog'

// 待审批队列：仅 kb_admin 可见（后端 /pending-approvals 也会 403 兜底）。Atlas 式：带橙头的卡 + 行。
const { approvals, isBusy, isKbAdmin, approve, reject, loadApprovals, loadErrors, openDocPreview } = useKb()
const { promptText } = useDialog()
const rowKey = (d: PendingItem) => `appr:${d.doc_id}/${d.version_no}`

async function onReject(d: PendingItem) {
  const reason = await promptText({ title: '驳回上传', message: `驳回《${d.title || d.original_filename || d.doc_id}》的上传？`, placeholder: '驳回原因（可空）', confirmText: '驳回', danger: true })
  if (reason === null) return   // 取消
  void reject(d, reason || 'rejected')
}
</script>

<template>
  <!-- 卡头已带图标+标题+计数，不再另设分区眉标 -->
  <section v-if="isKbAdmin && (approvals.length || loadErrors['approvals'])">
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
        <!-- 预览原件：放行到全公司/跨组前先看一眼实物，别凭标题盲批 -->
        <button
          type="button" data-testid="approval-preview"
          class="inline-flex items-center justify-center gap-1 rounded-lg border border-border px-3 py-[7px] text-[12.5px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground"
          title="预览原始上传文件" @click="openDocPreview(d.doc_id, d.version_no)"
        ><ExternalLink :size="13" :stroke-width="1.75" /> 预览</button>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isBusy(rowKey(d))" @click="onReject(d)"
        ><Loader2 v-if="isBusy(rowKey(d))" :size="13" :stroke-width="2" class="animate-spin" />{{ isBusy(rowKey(d)) ? '驳回中…' : '驳回' }}</button>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 rounded-lg bg-primary px-3.5 py-[7px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          :disabled="isBusy(rowKey(d))" @click="approve(d)"
        ><Loader2 v-if="isBusy(rowKey(d))" :size="13" :stroke-width="2" class="animate-spin" />{{ isBusy(rowKey(d)) ? '通过中…' : '通过' }}</button>
      </div>
    </div>
  </section>
</template>
