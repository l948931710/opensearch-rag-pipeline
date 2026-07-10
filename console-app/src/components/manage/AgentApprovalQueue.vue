<script setup lang="ts">
import { computed } from 'vue'
import { Bot, Loader2, ShieldAlert, Timer } from 'lucide-vue-next'
import { storeToRefs } from 'pinia'
import { useSession } from '@/stores/session'
import { deptLabel } from '@/lib/kb'
import { useAgentApprovals, type AgentApprovalItem } from '@/composables/useAgentApprovals'
import { useDialog } from '@/composables/useDialog'
import LoadError from './LoadError.vue'

// Agent 高风险操作审批队列（独立 tab 主体，非自隐区块——空态要显式告知「没有待办」）。
// 职责分离由服务端硬校验；前端对「自己发起的申请」预先禁用 批准/驳回（只留终止撤回），
// 避免点了才吃 403 的挫败感。参数展示的是服务端脱敏后的 proposed_args，渲染安全。
const { identity } = storeToRefs(useSession())
const {
  agentApprovals, agentApprovalError, isAgentApprovalBusy,
  loadAgentApprovals, decideAgentApproval,
} = useAgentApprovals()
const { promptText, confirm } = useDialog()

const myUserId = computed(() => identity.value?.userId || '')
function isMine(d: AgentApprovalItem): boolean { return d.requested_by === myUserId.value }

function argsPreview(d: AgentApprovalItem): string {
  const a = d.proposed_args
  if (!a || typeof a !== 'object') return '—'
  const parts = Object.entries(a).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
  return parts.join(' · ') || '—'
}
function ts(v: string | null): string { return v ? v.slice(0, 16) : '' }

async function onApprove(d: AgentApprovalItem) {
  const ok = await confirm({
    title: '批准执行',
    message: `批准 Agent 执行「${d.tool_name}」？参数：${argsPreview(d)}。批准后立即续跑执行，操作不可撤回。`,
    confirmText: '批准执行',
  })
  if (!ok) return
  void decideAgentApproval(d, 'approved')
}

async function onReject(d: AgentApprovalItem) {
  const reason = await promptText({
    title: '驳回并反馈',
    message: `驳回「${d.tool_name}」的执行申请？理由会回喂给 Agent 换方案续答（不执行该操作）。`,
    placeholder: '驳回理由（必填，Agent 据此调整方案）',
    confirmText: '驳回',
    danger: true,
  })
  if (reason === null) return
  void decideAgentApproval(d, 'rejected_feedback', reason || '审批未通过')
}

async function onTerminate(d: AgentApprovalItem) {
  const ok = await confirm({
    title: isMine(d) ? '撤回申请' : '终止运行',
    message: isMine(d)
      ? `撤回你发起的「${d.tool_name}」执行申请？该次 Agent 运行将终止。`
      : `直接终止这次 Agent 运行？「${d.tool_name}」不会执行，发起人会看到运行被终止。`,
    confirmText: isMine(d) ? '撤回' : '终止',
    danger: true,
  })
  if (!ok) return
  void decideAgentApproval(d, 'rejected_terminate')
}
</script>

<template>
  <section class="space-y-4">
    <LoadError :message="agentApprovalError" @retry="loadAgentApprovals(true)" />

    <div v-if="agentApprovals.length" class="overflow-hidden rounded-[15px] border border-border bg-card">
      <!-- 卡头：琥珀调（高风险写操作，与上传审批同族但独立图标） -->
      <div class="flex items-center gap-2.5 border-b border-border bg-accent-soft px-[18px] py-3">
        <Bot :size="16" :stroke-width="1.75" class="text-accent-text" />
        <span class="text-sm font-semibold text-foreground">Agent 高风险操作审批</span>
        <span class="rounded-full bg-accent-strong px-2 py-px text-[11px] font-bold text-primary-foreground">{{ agentApprovals.length }}</span>
        <div class="flex-1" />
        <span class="hidden text-xs text-muted-foreground sm:inline">批准前请核对工具与参数；过期未审视同拒绝</span>
      </div>

      <div
        v-for="d in agentApprovals" :key="d.request_id"
        class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3 first:border-t-0"
      >
        <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent-text">
          <ShieldAlert :size="16" :stroke-width="1.75" />
        </span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-semibold text-foreground">
            <span class="font-mono text-accent-text">{{ d.tool_name }}</span>
            <span v-if="d.tool_version" class="ml-1 text-[11px] font-normal text-faint">v{{ d.tool_version }}</span>
            <span v-if="isMine(d)" class="ml-2 rounded-full bg-panel px-2 py-px text-[10.5px] font-medium text-muted-foreground">我发起的</span>
          </div>
          <div class="mt-0.5 truncate font-mono text-[12px] text-muted-foreground" :title="argsPreview(d)">{{ argsPreview(d) }}</div>
          <div class="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11.5px] text-faint">
            <span>发起 {{ d.requested_by }}</span>
            <span v-if="d.approver_scope">· 归属 {{ deptLabel(d.approver_scope) }}</span>
            <span v-if="d.created_at">· {{ ts(d.created_at) }}</span>
            <span v-if="d.expires_at" class="inline-flex items-center gap-0.5"><Timer :size="11" :stroke-width="2" /> {{ ts(d.expires_at) }} 过期</span>
          </div>
        </div>

        <!-- 职责分离：自己发起的只能撤回（服务端硬校验，前端预先禁按钮防 403 挫败） -->
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isAgentApprovalBusy(`agent:${d.request_id}`)"
          @click="onTerminate(d)"
        ><Loader2 v-if="isAgentApprovalBusy(`agent:${d.request_id}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ isMine(d) ? '撤回' : '终止' }}</button>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isMine(d) || isAgentApprovalBusy(`agent:${d.request_id}`)"
          :title="isMine(d) ? '职责分离：不能审批自己发起的申请' : ''"
          @click="onReject(d)"
        >驳回</button>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg bg-primary px-3.5 py-[7px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          :disabled="isMine(d) || isAgentApprovalBusy(`agent:${d.request_id}`)"
          :title="isMine(d) ? '职责分离：不能审批自己发起的申请' : ''"
          @click="onApprove(d)"
        ><Loader2 v-if="isAgentApprovalBusy(`agent:${d.request_id}`)" :size="13" :stroke-width="2" class="animate-spin" />批准</button>
      </div>
    </div>

    <!-- 显式空态：独立 tab 不自隐，要告诉人「没有待办」而不是白屏 -->
    <div v-else-if="!agentApprovalError" class="rounded-xl border border-border bg-card px-6 py-10 text-center">
      <Bot :size="22" :stroke-width="1.5" class="mx-auto text-faint" />
      <p class="mt-3 text-sm font-medium text-foreground">当前没有待审批的 Agent 操作</p>
      <p class="mt-1 text-xs text-muted-foreground">
        Agent 提出高风险写操作（如 U8 写回）时会先挂起等审批，届时出现在这里；超时未审视同拒绝。
      </p>
    </div>

    <p class="ml-0.5 text-[11.5px] text-faint">
      审批即放行执行：批准后 Agent 立即续跑并执行该操作（一次性凭据，仅放行本次调用）；驳回会把理由回喂 Agent 换方案；终止直接结束该次运行。
    </p>
  </section>
</template>
