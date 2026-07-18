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
/** 有效期相对化（「3 天后过期」）；绝对时间挂 title。解析失败回退绝对展示（时区兜底）。 */
function relExpire(v: string | null): string {
  if (!v) return ''
  const t = Date.parse(String(v).replace(' ', 'T'))
  if (Number.isNaN(t)) return `${ts(v)} 过期`
  const diff = t - Date.now()
  if (diff <= 0) return '已过期'
  const m = Math.round(diff / 60000)
  if (m < 60) return `${Math.max(1, m)} 分钟后过期`
  const hr = Math.round(m / 60)
  if (hr < 48) return `${hr} 小时后过期`
  return `${Math.round(hr / 24)} 天后过期`
}
/** 已过期判定：卡头承诺「过期未审视同拒绝」，故过期单禁用 批准/改参/驳回（放行与留档处置都无意义），
 *  只留 终止/撤回 清卡。解析失败按未过期处理（fail-open：时区/格式异常不误伤成禁批，交服务端裁决）。 */
function isExpired(d: AgentApprovalItem): boolean {
  if (!d.expires_at) return false
  const t = Date.parse(String(d.expires_at).replace(' ', 'T'))
  return !Number.isNaN(t) && t <= Date.now()
}
const EXPIRED_TITLE = '已过期：超时视同拒绝，无需处置（可「终止」清卡）'

async function onApprove(d: AgentApprovalItem) {
  if (isExpired(d)) return   // 按钮已禁用；兜底防其它路径触发
  const ok = await confirm({
    title: '批准执行',
    message: `批准 Agent 执行「${d.tool_name}」？参数：${argsPreview(d)}。批准后立即续跑执行，操作不可撤回。`,
    confirmText: '批准执行',
  })
  if (!ok) return
  void decideAgentApproval(d, 'approved')
}

/** 批次8（ultra AgentApprovalQueue:73）：递归找出仍含脱敏掩码占位（3+ 个 *，对应
 *  REDACTION_MAP 的 138****5678 / ab***@ / 前缀**** 形态）的字段路径。 */
function findMaskedFields(obj: Record<string, unknown>, prefix = ''): string[] {
  const hits: string[] = []
  for (const [k, v] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${k}` : k
    if (typeof v === 'string' && /\*{3,}/.test(v)) hits.push(path)
    else if (Array.isArray(v)) {
      v.forEach((x, i) => {
        if (typeof x === 'string' && /\*{3,}/.test(x)) hits.push(`${path}[${i}]`)
        else if (x && typeof x === 'object') hits.push(...findMaskedFields(x as Record<string, unknown>, `${path}[${i}]`))
      })
    } else if (v && typeof v === 'object') hits.push(...findMaskedFields(v as Record<string, unknown>, path))
  }
  return hits
}

/** 第四处置「修改后批准」：编辑脱敏参数 JSON 后以 kind=edited 放行——服务端重过
 *  jsonschema+Policy、按改后参数算 digest（重放安全），执行的就是你看到的这份参数。 */
async function onEdited(d: AgentApprovalItem) {
  if (isExpired(d)) return   // 同 onApprove：过期单不接受任何放行形态
  const { notice } = useDialog()
  const raw = await promptText({
    title: '修改参数后批准',
    message: `编辑「${d.tool_name}」的执行参数（JSON 对象）。提交即批准执行改后参数：会重新过参数校验与策略，操作不可撤回。注意：预填值是脱敏后的展示值（*** 为掩码）——请为要保留的敏感字段填入真实值，或删除该字段。`,
    placeholder: '{ "qty": 100 }',
    initial: JSON.stringify(d.proposed_args ?? {}, null, 2),
    maxlength: 2000,
    confirmText: '改参并批准',
  })
  if (raw === null) return
  let parsed: Record<string, unknown>
  try {
    const v = JSON.parse(raw)
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('not object')
    parsed = v as Record<string, unknown>
  } catch {
    void notice({ title: '参数格式错误', message: '需要合法的 JSON 对象（如 {"qty": 100}），本次未提交。', danger: true })
    return
  }
  // 批次8：预填来自服务端**脱敏后**的 proposed_args（原文不出库），kind=edited 按提交值
  // 原样执行——若保留 "138****5678" 这类掩码占位，高危写操作会把掩码当真参跑。
  // 含掩码字段一律拒绝提交（填真实值或删掉该字段后重试）。
  const masked = findMaskedFields(parsed)
  if (masked.length) {
    void notice({
      title: '参数仍含脱敏占位符',
      message: `字段 ${masked.slice(0, 5).join('、')} 仍是脱敏掩码值（含 ***）。改参会按提交值原样执行——请填入真实值，或删除不需要修改的字段后重试。`,
      danger: true,
    })
    return
  }
  void decideAgentApproval(d, 'edited', undefined, parsed)
}

async function onReject(d: AgentApprovalItem) {
  if (isExpired(d)) return   // 与 onApprove/onEdited 同款兜底：过期即视同拒绝，无需留档驳回
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
            <span v-if="d.expires_at" class="inline-flex items-center gap-0.5" :class="isExpired(d) ? 'font-medium text-st-fail' : ''" :title="`${d.expires_at} 过期（超时视同拒绝）`"><Timer :size="11" :stroke-width="2" /> {{ relExpire(d.expires_at) }}</span>
          </div>
        </div>

        <!-- 职责分离：自己发起的只能撤回（服务端硬校验，前端预先禁按钮防 403 挫败） -->
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isAgentApprovalBusy(`agent:${d.request_id}`)"
          @click="onTerminate(d)"
        ><Loader2 v-if="isAgentApprovalBusy(`agent:${d.request_id}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ isMine(d) ? '撤回' : '终止' }}</button>
        <!-- 修改后批准（第四处置）：own 申请直接不显（职责分离下无可用场景，不摆禁用按钮占位） -->
        <button
          v-if="!isMine(d)"
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isExpired(d) || isAgentApprovalBusy(`agent:${d.request_id}`)"
          :title="isExpired(d) ? EXPIRED_TITLE : '修改参数后批准执行（重新过校验与策略）'"
          @click="onEdited(d)"
        >改参</button>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          :disabled="isMine(d) || isExpired(d) || isAgentApprovalBusy(`agent:${d.request_id}`)"
          :title="isMine(d) ? '职责分离：不能审批自己发起的申请' : isExpired(d) ? EXPIRED_TITLE : ''"
          @click="onReject(d)"
        >驳回</button>
        <button
          type="button"
          class="inline-flex items-center justify-center gap-1 self-start rounded-lg bg-primary px-3.5 py-[7px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          :disabled="isMine(d) || isExpired(d) || isAgentApprovalBusy(`agent:${d.request_id}`)"
          :title="isMine(d) ? '职责分离：不能审批自己发起的申请' : isExpired(d) ? EXPIRED_TITLE : ''"
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
      审批即放行执行：批准后 Agent 立即续跑并执行该操作（一次性凭据，仅放行本次调用）；改参=修改参数后批准（重过校验与策略，执行改后参数）；驳回会把理由回喂 Agent 换方案；终止直接结束该次运行。
    </p>
  </section>
</template>
