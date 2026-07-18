<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { Activity, AlertTriangle, Check, Loader2, ShieldAlert, XCircle } from '@lucide/vue'
import { useSession } from '@/stores/session'
import { useDialog } from '@/composables/useDialog'
import type { ChatMessage } from '@/composables/useAsk'
import {
  RUN_TERMINAL, agentArgsPreview, agentToolLabel, invocationStatusLabel,
  runStatusLabel, useAgentAsk,
} from '@/composables/useAgentAsk'

// Agent run 结局卡（报告 §8②⑥「批准≠成功，用户必须看到真实执行结果」）：
//  · 挂起：工具+脱敏参数+审批单号+「等待审批人处置」+ 指路「审批」tab（有审批权者），可撤回；
//  · 断线/恢复执行：明确告知 run 仍在服务端运行，5s 轮询回写（不依赖原 SSE）；
//  · 终态：完成→工具回执（uncertain 显式「结果不确定，待对账」）/ 失败→下一步提示 / 撤回·过期如实展示。
const props = defineProps<{ message: ChatMessage }>()
const m = props.message
const { canManage } = storeToRefs(useSession())
const { agentRunDetails, ensureRunPolling, openRunCenter, withdrawRun, isAgentBusy } = useAgentAsk()
const { confirm } = useDialog()

const meta = computed(() => m.agent)
const runId = computed(() => meta.value?.runId || '')
const status = computed(() => meta.value?.status || '')
const detail = computed(() => (runId.value ? agentRunDetails.value[runId.value] : undefined))
const approvalRow = computed(() => detail.value?.approval || null)
const invocations = computed(() => detail.value?.invocations || [])
const uncertain = computed(() => invocations.value.filter((x) => x.status === 'uncertain'))
const failedInvs = computed(() => invocations.value.filter((x) => x.status === 'failed'))
const okInvs = computed(() => invocations.value.filter((x) => x.status === 'succeeded'))

// 卡片显隐：有 run 痕迹才出现；纯文本回答（无 runId 无审批）不加噪声。
const visible = computed(() => !!meta.value && (!!meta.value.approval || !!runId.value || !!status.value))
const suspended = computed(() => status.value === 'suspended')
const executing = computed(() => status.value === 'resuming' || status.value === 'running')

// 断线恢复：挂起/在途 run 的卡片挂载（含 localStorage 回灌重开页面）即确保轮询在跑，终态自停。
onMounted(() => {
  if (runId.value && status.value && !RUN_TERMINAL.has(status.value)) ensureRunPolling(runId.value)
})

const busy = computed(() => isAgentBusy(`agent-run:${runId.value}`))
async function onWithdraw() {
  if (!runId.value) return
  const ok = await confirm({
    title: '撤回申请',
    message: `撤回这次 Agent 运行的执行申请？「${agentToolLabel(meta.value?.approval?.toolName)}」不会执行，本次运行将终止。`,
    confirmText: '撤回',
    danger: true,
  })
  if (!ok) return
  void withdrawRun(runId.value, meta.value?.approval?.requestId)
}
function shortId(id: string): string { return id.length > 10 ? id.slice(0, 10) + '…' : id }
</script>

<template>
  <div v-if="visible" class="mt-2.5" data-testid="agent-run-card">
    <!-- 挂起等审批 -->
    <div v-if="suspended" class="rounded-xl border border-st-warn/35 bg-st-warn/5 p-3.5" data-testid="agent-suspended-card">
      <div class="flex items-center gap-2 text-[13px] font-semibold text-foreground">
        <ShieldAlert :size="15" :stroke-width="1.75" class="text-st-warn" />
        需要审批才能继续
        <Loader2 :size="12" :stroke-width="2" class="animate-spin text-st-warn" aria-hidden="true" />
      </div>
      <div class="mt-1.5 text-[12.5px] text-muted-foreground">
        Agent 提议执行
        <span class="font-mono text-foreground">{{ meta?.approval?.toolName || '—' }}</span>
        <span v-if="meta?.approval?.toolName">（{{ agentToolLabel(meta?.approval?.toolName) }}）</span>，
        已挂起等待审批人处置；审批通过后会自动继续执行，结果在这里同步（无需保持页面连接）。
      </div>
      <div class="mt-1 truncate font-mono text-[12px] text-muted-foreground" :title="agentArgsPreview(meta?.approval?.args)">
        {{ agentArgsPreview(meta?.approval?.args) }}
      </div>
      <div class="mt-1 flex flex-wrap items-center gap-x-2 text-[11.5px] text-faint">
        <span v-if="meta?.approval?.requestId">审批单 {{ shortId(meta.approval.requestId) }}</span>
        <span v-if="approvalRow?.expires_at">· {{ approvalRow.expires_at.slice(0, 16) }} 过期（超时视同拒绝）</span>
      </div>
      <div class="mt-2.5 flex flex-wrap items-center gap-2">
        <RouterLink
          v-if="canManage"
          to="/manage?tab=approvals"
          class="rounded-lg border border-border bg-card px-3 py-[6px] text-[12px] font-medium text-accent-text transition hover:border-accent-strong"
        >去「审批」处理</RouterLink>
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-[6px] text-[12px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="busy"
          data-testid="agent-withdraw"
          @click="onWithdraw"
        ><Loader2 v-if="busy" :size="12" :stroke-width="2" class="animate-spin" />撤回申请</button>
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-lg px-2.5 py-[6px] text-[12px] text-muted-foreground transition hover:bg-secondary hover:text-foreground"
          @click="openRunCenter(runId)"
        ><Activity :size="12" :stroke-width="1.75" />运行详情</button>
      </div>
    </div>

    <!-- 审批通过恢复执行 / 断线仍在后台跑 -->
    <div v-else-if="executing || meta?.disconnected" class="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-panel/60 px-3.5 py-2.5 text-[12.5px] text-muted-foreground">
      <Loader2 :size="13" :stroke-width="2" class="animate-spin text-accent-text" />
      <span v-if="executing">审批已通过，Agent 正在执行…结果会自动同步到这里。</span>
      <span v-else>实时连接已断开，Agent 仍在后台运行；结果会按运行状态自动同步。</span>
      <button type="button" class="ml-auto inline-flex items-center gap-1 rounded-lg px-2 py-1 text-[12px] text-muted-foreground transition hover:bg-secondary hover:text-foreground" @click="openRunCenter(runId)">
        <Activity :size="12" :stroke-width="1.75" />运行详情
      </button>
    </div>

    <!-- 终态 -->
    <div v-else-if="status" class="rounded-xl border border-border bg-panel/50 px-3.5 py-2.5 text-[12.5px]">
      <div class="flex flex-wrap items-center gap-1.5">
        <Check v-if="status === 'succeeded'" :size="13" :stroke-width="2.25" class="text-st-live" />
        <XCircle v-else-if="status === 'failed'" :size="13" :stroke-width="1.75" class="text-st-fail" />
        <AlertTriangle v-else :size="13" :stroke-width="1.75" class="text-muted-foreground" />
        <span class="font-medium text-foreground">
          {{ meta?.approval && status === 'succeeded' ? '审批已通过，运行完成' : `运行${runStatusLabel(status)}` }}
        </span>
        <span v-if="status === 'succeeded' && invocations.length" class="text-muted-foreground">
          · 工具回执：成功 {{ okInvs.length }}<template v-if="failedInvs.length">，失败 {{ failedInvs.length }}</template><template v-if="uncertain.length">，不确定 {{ uncertain.length }}</template>
        </span>
        <button type="button" class="ml-auto inline-flex items-center gap-1 rounded-lg px-2 py-1 text-[12px] text-muted-foreground transition hover:bg-secondary hover:text-foreground" @click="openRunCenter(runId)">
          <Activity :size="12" :stroke-width="1.75" />运行详情
        </button>
      </div>
      <!-- 失败：给下一步，不是只丢状态码 -->
      <p v-if="status === 'failed'" class="mt-1 text-muted-foreground">
        本次运行失败：可调整问法重新提问；反复失败请到「运行详情」查看步骤与工具回执后反馈管理员。
      </p>
      <p v-else-if="status === 'cancelled'" class="mt-1 text-muted-foreground">
        本次运行已被终止（撤回或审批人终止），提议的操作未执行。
      </p>
      <p v-else-if="status === 'expired'" class="mt-1 text-muted-foreground">
        审批超时未处理，运行已过期（超时视同拒绝），提议的操作未执行；如仍需要请重新发起。
      </p>
      <!-- uncertain 回执：显式、带行为指引（勿重复发起） -->
      <div v-if="uncertain.length" class="mt-1.5 rounded-lg border border-st-warn/35 bg-st-warn/10 px-2.5 py-2 text-[12px] text-foreground" data-testid="agent-uncertain">
        <span class="font-medium">{{ uncertain.map((x) => agentToolLabel(x.tool_name)).join('、') }} 结果不确定</span>（超时/中断），已进入人工对账，请勿重复发起相同操作；对账结论以运行详情为准。
      </div>
      <div v-else-if="failedInvs.length" class="mt-1.5 text-[12px] text-muted-foreground">
        {{ failedInvs.map((x) => `${agentToolLabel(x.tool_name)}（${invocationStatusLabel(x.status)}）`).join('、') }}——详情见运行中心。
      </div>
    </div>
  </div>
</template>
