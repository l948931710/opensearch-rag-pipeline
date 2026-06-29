<script setup lang="ts">
import { Lock, FileText } from 'lucide-vue-next'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb, type AccessRequestItem } from '@/composables/useKb'
import LoadError from './LoadError.vue'

// 授权申请队列（审批人侧，Phase C）：其他部门申请检索本部门文档 → 由文档所属部门管理员审批。
// 与「待审批队列」（上传放行，橙头）区分：此处绿头。数据空时整块不渲染（无后端 = 自然隐藏，不造占位噪声）。
const { accessRequests, apprBusy, approveAccess, rejectAccess, loadAccessRequests, loadErrors } = useKb()

function onReject(d: AccessRequestItem) {
  const reason = prompt('驳回原因（可空，将通知申请人）：', '')
  if (reason === null) return   // 取消
  void rejectAccess(d, reason || 'rejected')
}
</script>

<template>
  <section v-if="accessRequests.length || loadErrors['accessRequests']">
    <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">授权申请</p>
    <LoadError class="mb-2.5" :message="loadErrors['accessRequests']" @retry="loadAccessRequests()" />
    <div v-if="accessRequests.length" class="overflow-hidden rounded-[15px] border border-border bg-card">
      <!-- 绿头（区别于上传审批的橙头） -->
      <div class="flex items-center gap-2.5 border-b border-border bg-accent-soft px-[18px] py-3">
        <Lock :size="16" :stroke-width="1.75" class="text-accent-text" />
        <span class="text-sm font-semibold text-foreground">授权申请</span>
        <span class="rounded-full bg-accent-strong px-2 py-px text-[11px] font-bold text-primary-foreground">{{ accessRequests.length }}</span>
        <div class="flex-1" />
        <span class="hidden text-xs text-muted-foreground sm:inline">其他部门申请检索本部门文档，由你审批</span>
      </div>
      <!-- 行 -->
      <div
        v-for="d in accessRequests" :key="d.id"
        class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent-text">
          <FileText :size="16" :stroke-width="1.75" />
        </span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-semibold text-foreground">
            <span class="text-accent-text">{{ deptLabel(d.requester_dept) }}</span> 申请访问《{{ d.doc_title }}》
          </div>
          <div class="truncate text-[11.5px] text-faint">
            归属 {{ deptLabel(d.owner_dept) }} · {{ permLabel(d.permission_level) }} · 申请人 {{ d.requester_name }}
            <span v-if="d.created_at"> · {{ d.created_at }}</span>
          </div>
          <div v-if="d.reason" class="mt-1 line-clamp-2 text-[12px] text-muted-foreground">“{{ d.reason }}”</div>
        </div>
        <button
          type="button"
          class="self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="apprBusy" @click="onReject(d)"
        >驳回</button>
        <button
          type="button"
          class="self-start rounded-lg bg-primary px-3.5 py-[7px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          :disabled="apprBusy" @click="approveAccess(d)"
        >授权</button>
      </div>
    </div>
  </section>
</template>
