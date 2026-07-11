<script setup lang="ts">
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { Activity, ArrowLeft, Bot, Brain, Loader2, RefreshCw, ShieldAlert, Wrench, X } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { useDialog } from '@/composables/useDialog'
import {
  RUN_TERMINAL, agentToolLabel, invocationStatusLabel, invocationStatusTone,
  runStatusLabel, runStatusTone, useAgentAsk, type AgentRunRow, type AgentRunStep,
} from '@/composables/useAgentAsk'

// 运行中心（P0-F run center）：「我的 runs」列表 + 单 run 详情（状态/步骤时间线/工具回执/审批指向）。
// 放在问答侧抽屉而非 ManageView：runs 属于每个提问者（员工也有），ManageView 的管理 tab
// 对员工不可达；入口=Composer「运行」按钮 + 消息结局卡「运行详情」。
// 断线恢复：聚焦非终态 run 时 5s 轮询（useAgentAsk.ensureRunPolling），终态自停；
// 「批准≠成功」：invocations 回执逐条展示，uncertain 显式标注并给对账指引。
const { identity } = storeToRefs(useSession())
const {
  agentRuns, agentRunDetails, runCenterOpen, runCenterRunId,
  closeRunCenter, loadAgentRuns, refreshRunDetail, ensureRunPolling, withdrawRun, isAgentBusy,
} = useAgentAsk()
const { confirm } = useDialog()

const focusedId = computed(() => runCenterRunId.value)
const detail = computed(() => (focusedId.value ? agentRunDetails.value[focusedId.value] : undefined))
const run = computed<AgentRunRow | undefined>(() =>
  detail.value?.run || agentRuns.value.find((r) => r.run_id === focusedId.value))
const isMineRun = computed(() => {
  const uid = identity.value?.userId || ''
  const owner = detail.value?.run?.user_id
  // 列表接口只返回本人 runs；详情带 user_id（kb_admin 可看他人）→ 有 user_id 以它为准
  return owner == null ? true : owner === uid
})
const canWithdraw = computed(() => run.value?.status === 'suspended' && isMineRun.value)

const TONE: Record<string, string> = {
  live: 'text-st-live bg-st-live/10',
  busy: 'text-st-busy bg-st-busy/10',
  queue: 'text-st-queue bg-st-queue/10',
  warn: 'text-st-warn bg-st-warn/10',
  fail: 'text-st-fail bg-st-fail/10',
  muted: 'text-st-muted bg-st-muted/10',
}
function pillCls(tone: string): string { return TONE[tone] || TONE.muted }

const KIND: Record<string, { label: string; icon: any }> = {
  model_call: { label: '模型调用', icon: Brain },
  tool_call: { label: '工具调用', icon: Wrench },
  approval: { label: '审批', icon: ShieldAlert },
}
function kindLabel(k: string): string { return KIND[k]?.label || k }
function kindIcon(k: string) { return KIND[k]?.icon || Activity }

function ts(v?: string | null): string { return v ? String(v).slice(0, 16) : '—' }
function shortId(id?: string | null): string { return id ? (id.length > 12 ? id.slice(0, 12) + '…' : id) : '—' }
/** 模型档标注：light=默认档不标（少即是多）；high 用用户语言「深度思考」；未知档原样透出。 */
function tierLabel(t?: string | null): string { return !t || t === 'light' ? '' : t === 'high' ? '深度思考' : t }
function pretty(v: unknown): string {
  if (v == null) return ''
  try { return typeof v === 'string' ? v : JSON.stringify(v, null, 2) } catch { return String(v) }
}

function focusRun(r: AgentRunRow) {
  runCenterRunId.value = r.run_id
  if (RUN_TERMINAL.has(r.status || '')) void refreshRunDetail(r.run_id)
  else ensureRunPolling(r.run_id)
}
function backToList() { runCenterRunId.value = '' }

async function onWithdraw() {
  const r = run.value
  if (!r) return
  const ok = await confirm({
    title: '撤回申请',
    message: '撤回这次运行的执行申请？挂起中的操作不会执行，本次运行将终止。',
    confirmText: '撤回',
    danger: true,
  })
  if (!ok) return
  void withdrawRun(r.run_id, detail.value?.approval?.request_id || undefined)
}

// 步骤 payload 一句话摘要（详情可展开完整 JSON；payload 服务端已脱敏）
function stepSummary(s: AgentRunStep): string {
  const p = s.payload as Record<string, unknown> | null | undefined
  if (!p || typeof p !== 'object') return ''
  const name = (p.tool_name as string) || ''
  if (name) return agentToolLabel(name)
  const txt = (p.text as string) || (p.content as string) || ''
  return typeof txt === 'string' ? txt.slice(0, 60) : ''
}
</script>

<template>
  <div v-if="runCenterOpen" class="fixed inset-0 z-[80]" role="dialog" aria-modal="true" aria-label="Agent 运行中心" data-testid="agent-run-center">
    <!-- 遮罩 -->
    <div class="absolute inset-0 bg-black/30" @click="closeRunCenter" />
    <!-- 右侧抽屉 -->
    <div class="absolute inset-y-0 right-0 flex w-[440px] max-w-full flex-col border-l border-border bg-card shadow-2xl">
      <div class="flex h-14 shrink-0 items-center gap-2.5 border-b border-border px-4">
        <button
          v-if="focusedId" type="button" class="grid size-7 place-items-center rounded-lg text-muted-foreground transition hover:bg-secondary hover:text-foreground"
          aria-label="返回列表" @click="backToList"
        ><ArrowLeft :size="16" :stroke-width="1.75" /></button>
        <Bot v-else :size="17" :stroke-width="1.75" class="text-accent-text" />
        <span class="text-sm font-semibold text-foreground">{{ focusedId ? '运行详情' : 'Agent 运行中心' }}</span>
        <div class="flex-1" />
        <button
          type="button" class="grid size-7 place-items-center rounded-lg text-muted-foreground transition hover:bg-secondary hover:text-foreground"
          :aria-label="focusedId ? '刷新详情' : '刷新列表'"
          @click="focusedId ? void refreshRunDetail(focusedId) : void loadAgentRuns(true)"
        ><RefreshCw :size="14" :stroke-width="1.75" /></button>
        <button type="button" class="grid size-7 place-items-center rounded-lg text-muted-foreground transition hover:bg-secondary hover:text-foreground" aria-label="关闭运行中心" @click="closeRunCenter">
          <X :size="16" :stroke-width="1.75" />
        </button>
      </div>

      <!-- ── 列表 ── -->
      <div v-if="!focusedId" class="min-h-0 flex-1 overflow-y-auto">
        <button
          v-for="r in agentRuns" :key="r.run_id" type="button"
          class="flex w-full flex-wrap items-center gap-x-2.5 gap-y-1 border-b border-border px-4 py-3 text-left transition hover:bg-panel/60"
          data-testid="agent-run-row"
          @click="focusRun(r)"
        >
          <span class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium" :class="pillCls(runStatusTone(r.status))">{{ runStatusLabel(r.status) }}</span>
          <span class="font-mono text-[12px] text-foreground">{{ shortId(r.run_id) }}</span>
          <span v-if="r.agent_profile" class="text-[11.5px] text-faint">{{ r.agent_profile }}</span>
          <span v-if="tierLabel(r.model_profile)" class="text-[11.5px] text-accent-text" data-testid="run-tier">{{ tierLabel(r.model_profile) }}</span>
          <span class="ml-auto text-[11.5px] text-faint">{{ ts(r.started_at) }}</span>
          <span class="w-full text-[11.5px] text-faint">
            轮次 {{ r.turns_used ?? '—' }} · 工具 {{ r.tool_calls_used ?? '—' }} · tokens {{ r.tokens_used ?? '—' }}
          </span>
        </button>
        <div v-if="!agentRuns.length" class="px-6 py-12 text-center">
          <Bot :size="22" :stroke-width="1.5" class="mx-auto text-faint" />
          <p class="mt-3 text-sm font-medium text-foreground">还没有 Agent 运行记录</p>
          <p class="mt-1 text-xs text-muted-foreground">在输入框开启「Agent 模式」提问后，每次运行都会在这里留痕：状态、步骤、工具回执与审批。</p>
        </div>
        <p v-if="agentRuns.length" class="px-4 py-3 text-[11.5px] text-faint">挂起/运行中的 run 会自动每 5 秒刷新，终态即停；审批处置后无需保持原对话连接。</p>
      </div>

      <!-- ── 详情 ── -->
      <div v-else class="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
        <!-- run 头 -->
        <div class="rounded-xl border border-border bg-panel/50 p-3.5">
          <div class="flex flex-wrap items-center gap-2">
            <span class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium" :class="pillCls(runStatusTone(run?.status))" data-testid="run-status-pill">{{ runStatusLabel(run?.status) }}</span>
            <span class="font-mono text-[12px] text-foreground" :title="focusedId">{{ shortId(focusedId) }}</span>
            <Loader2 v-if="run && !RUN_TERMINAL.has(run.status || '')" :size="12" :stroke-width="2" class="animate-spin text-muted-foreground" aria-label="轮询中" />
            <button
              v-if="canWithdraw" type="button"
              class="ml-auto inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-[5px] text-[12px] font-medium text-st-fail transition hover:border-border-strong disabled:opacity-50"
              :disabled="isAgentBusy(`agent-run:${focusedId}`)"
              data-testid="run-withdraw"
              @click="onWithdraw"
            ><Loader2 v-if="isAgentBusy(`agent-run:${focusedId}`)" :size="12" :stroke-width="2" class="animate-spin" />撤回</button>
          </div>
          <div class="mt-1.5 text-[11.5px] text-faint">
            开始 {{ ts(run?.started_at) }} <template v-if="run?.ended_at">· 结束 {{ ts(run?.ended_at) }}</template>
            <template v-if="run?.agent_profile"> · {{ run.agent_profile }}</template>
            <template v-if="tierLabel(run?.model_profile)"> · {{ tierLabel(run?.model_profile) }}</template>
          </div>
          <p v-if="run?.status === 'failed'" class="mt-1.5 text-[12px] text-muted-foreground">运行失败：可回到对话调整问法重新提问；反复失败请把本页信息反馈管理员。</p>
          <p v-else-if="run?.status === 'suspended'" class="mt-1.5 text-[12px] text-muted-foreground">挂起等待审批：审批通过后自动继续执行；也可撤回终止本次运行。</p>
        </div>

        <!-- 审批指向 -->
        <div v-if="detail?.approval" class="rounded-xl border border-border bg-card p-3.5">
          <div class="flex items-center gap-2 text-[12.5px] font-semibold text-foreground"><ShieldAlert :size="14" :stroke-width="1.75" class="text-st-warn" />审批</div>
          <div class="mt-1.5 space-y-0.5 text-[12px] text-muted-foreground">
            <div>工具 <span class="font-mono text-foreground">{{ detail.approval.tool_name || '—' }}</span>（{{ agentToolLabel(detail.approval.tool_name) }}）· 状态 {{ detail.approval.status || '—' }}</div>
            <div v-if="detail.approval.render_summary" class="font-mono">{{ detail.approval.render_summary }}</div>
            <div>审批单 {{ shortId(detail.approval.request_id) }} <template v-if="detail.approval.approver_scope">· 审批域 {{ detail.approval.approver_scope }}</template></div>
            <div v-if="detail.approval.expires_at">{{ ts(detail.approval.expires_at) }} 过期（超时视同拒绝）</div>
            <div v-if="detail.approval.decided_at">已处置于 {{ ts(detail.approval.decided_at) }}</div>
          </div>
        </div>

        <!-- 工具回执（批准≠成功） -->
        <div v-if="detail?.invocations?.length" class="rounded-xl border border-border bg-card p-3.5">
          <div class="text-[12.5px] font-semibold text-foreground">工具回执</div>
          <div
            v-for="inv in detail.invocations" :key="inv.invocation_id"
            class="mt-2 rounded-lg bg-panel px-2.5 py-2 text-[12px]"
            data-testid="run-invocation"
          >
            <div class="flex flex-wrap items-center gap-2">
              <span class="font-mono text-foreground">{{ inv.tool_name }}</span>
              <span class="text-faint">{{ agentToolLabel(inv.tool_name) }}</span>
              <span class="ml-auto inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium" :class="pillCls(invocationStatusTone(inv.status))">{{ invocationStatusLabel(inv.status) }}</span>
            </div>
            <p v-if="inv.status === 'uncertain'" class="mt-1 text-muted-foreground">
              结果不确定（超时/中断），已进入人工对账，请勿重复发起相同操作；对账结论出来前以本页状态为准。
            </p>
            <p v-else-if="inv.status === 'failed' && inv.error_text" class="mt-1 break-all text-muted-foreground">{{ inv.error_text }}</p>
          </div>
        </div>

        <!-- 步骤时间线 -->
        <div v-if="detail?.steps?.length" class="rounded-xl border border-border bg-card p-3.5">
          <div class="text-[12.5px] font-semibold text-foreground">步骤时间线</div>
          <div v-for="s in detail.steps" :key="s.step_no" class="mt-2 flex gap-2.5" data-testid="run-step">
            <span class="grid size-6 shrink-0 place-items-center rounded-md bg-panel text-muted-foreground">
              <component :is="kindIcon(s.kind)" :size="12" :stroke-width="1.75" />
            </span>
            <div class="min-w-0 flex-1 text-[12px]">
              <div class="flex flex-wrap items-center gap-x-2 text-foreground">
                <span class="font-medium">#{{ s.step_no }} {{ kindLabel(s.kind) }}</span>
                <span v-if="stepSummary(s)" class="truncate text-muted-foreground">{{ stepSummary(s) }}</span>
                <span class="ml-auto text-[11px] text-faint">{{ ts(s.created_at) }}</span>
              </div>
              <div v-if="s.tokens_prompt != null || s.tokens_completion != null" class="mt-0.5 text-[11px] text-faint">
                tokens {{ s.tokens_prompt ?? 0 }}+{{ s.tokens_completion ?? 0 }}
              </div>
              <details v-if="s.payload != null" class="mt-1">
                <summary class="cursor-pointer text-[11px] text-faint">载荷（已脱敏）</summary>
                <pre class="mt-1 max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-md bg-panel p-2 font-mono text-[11px] text-foreground">{{ pretty(s.payload) }}</pre>
              </details>
            </div>
          </div>
        </div>

        <div v-if="!detail" class="rounded-xl border border-border bg-card px-6 py-10 text-center text-sm text-muted-foreground">
          <Loader2 :size="16" :stroke-width="2" class="mx-auto animate-spin text-faint" />
          <p class="mt-2">正在加载运行详情…（他人 run 或功能未开时会提示不可见）</p>
        </div>
      </div>
    </div>
  </div>
</template>
