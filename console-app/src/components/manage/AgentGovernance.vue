<script setup lang="ts">
import { computed } from 'vue'
import { AlertTriangle, Loader2, RefreshCw, ShieldCheck, Wrench } from 'lucide-vue-next'
import { useAgentGovernance, type AgentInvocationRow, type AgentToolRow } from '@/composables/useAgentGovernance'
import { useAgentAsk } from '@/composables/useAgentAsk'
import { useDialog } from '@/composables/useDialog'
import { fmtTs } from '@/lib/kb'
import LoadError from './LoadError.vue'
import { useRouter } from 'vue-router'

// Agent 治理（kb_admin 专属，批次β F1+F2）：工具 kill switch + 漂移告警 + uncertain 对账台。
// 纪律：无自动轮询（治理端点共用调用者 ask 限流桶）——刷新只走顶部手动按钮；
// 渲染真值源 = items[].status（disabled 数组有多实例一致性窗口，不作渲染源）；
// 停用按 tool_name 整体生效（全部版本一起翻转）——按工具分组渲染，单开关 + 明示影响面。
const {
  agentTools, agentToolDrift, agentUncertain, agentGovSupported, agentGovError,
  isAgentGovBusy, loadAgentGovernance, toggleTool, resolveInvocation,
} = useAgentGovernance()
const { confirm, promptText, notice } = useDialog()
const { openRunCenter } = useAgentAsk()
const router = useRouter()

// 按 tool_name 分组（后端支持同名多版本共存，toggle 是整组生效）
interface ToolGroup { tool_name: string; rows: AgentToolRow[]; status: string; risk_level: string }
const toolGroups = computed<ToolGroup[]>(() => {
  const m = new Map<string, AgentToolRow[]>()
  for (const t of agentTools.value || []) {
    const arr = m.get(t.tool_name) || []
    arr.push(t)
    m.set(t.tool_name, arr)
  }
  return [...m.entries()].map(([name, rows]) => ({
    tool_name: name,
    rows,
    // 任一版本 disabled 即视为已停用（服务端整组翻转，正常不会出现混态；混态=脏数据，按停用显示更安全）
    status: rows.some((r) => r.status === 'disabled') ? 'disabled' : rows[0]?.status || 'active',
    risk_level: rows[0]?.risk_level || 'read_only',
  }))
})

const RISK_LABEL: Record<string, string> = { read_only: '只读', low_write: '低风险写', high_write: '高风险写' }
const RISK_TONE: Record<string, string> = {
  read_only: 'bg-st-live/10 text-st-live', low_write: 'bg-st-busy/10 text-st-busy', high_write: 'bg-st-fail/10 text-st-fail',
}
const riskLabel = (r: string) => RISK_LABEL[r] || r
const riskTone = (r: string) => RISK_TONE[r] || 'bg-panel text-muted-foreground'

async function onToggle(g: ToolGroup) {
  if (g.status !== 'disabled') {
    // 停用 = 全局 kill switch：危险确认 + 理由（入审计）。明示多版本连带。
    const versions = g.rows.map((r) => `v${r.version}`).join('/')
    const reason = await promptText({
      title: '停用工具（全局）',
      message: `停用「${g.tool_name}」？将立即对全公司 Agent 生效（${g.rows.length > 1 ? `影响该工具全部版本 ${versions}` : versions}），执行中的调用不受影响，新调用一律拒绝。`,
      placeholder: '停用原因（将写入审计日志）',
      confirmText: '停用',
      danger: true,
    })
    if (reason === null) return
    const ok = await toggleTool(g.tool_name, true, reason || undefined)
    if (ok) void notice({ title: '已停用', message: `「${g.tool_name}」已全局停用（约 30s 内对所有实例生效）。` })
  } else {
    const ok2 = await confirm({
      title: '恢复工具',
      message: `恢复「${g.tool_name}」？新的 Agent 调用将重新允许使用该工具（高风险写操作仍需逐次审批）。`,
      confirmText: '恢复',
    })
    if (!ok2) return
    void toggleTool(g.tool_name, false)
  }
}

async function onResolve(inv: AgentInvocationRow, resolution: 'confirmed_succeeded' | 'confirmed_failed') {
  const label = resolution === 'confirmed_succeeded' ? '已成功' : '已失败'
  const note = await promptText({
    title: `核实为${label}`,
    message: `把「${inv.tool_name}」的这笔调用回填为「${label}」？请先到业务系统核实副作用是否真的发生（如 U8 单据是否落库），再填写核实依据。`,
    placeholder: '核实依据（必填，将写入审计日志）',
    confirmText: `回填${label}`,
    danger: resolution === 'confirmed_failed',
  })
  if (note === null) return
  if (!note.trim()) {   // 服务端 422 兜底；前端先拦，别让人白跑一趟
    void notice({ title: '需要核实依据', message: '对账处置必须填写核实依据（会进审计日志），本次未提交。', danger: true })
    return
  }
  void resolveInvocation(inv, resolution, note.trim())
}

/** uncertain 行没有原始参数（只有 digest）——完整参数在运行详情的 tool_call 步骤里。 */
function gotoRun(inv: AgentInvocationRow) {
  openRunCenter(inv.run_id)
  void router?.push('/')   // 运行中心抽屉挂在问答页；router?. = 单测无 router 环境的既有约定
}
</script>

<template>
  <div class="space-y-5">
    <!-- 功能未开/无权（深链可达此态；tab 自隐是另一道门）：诚实提示，不装空态 -->
    <div v-if="agentGovSupported === false" class="rounded-xl border border-border bg-card px-6 py-10 text-center">
      <ShieldCheck :size="22" :stroke-width="1.5" class="mx-auto text-faint" />
      <p class="mt-3 text-sm font-medium text-foreground">Agent 治理未开启或无权访问</p>
      <p class="mt-1 text-xs text-muted-foreground">需要生产开启 RAG_AGENT_ENABLE 且当前身份为知识库管理员。</p>
    </div>

    <!-- 探测未返回（深链直达的一帧）：占位而非闪一下内容骨架 -->
    <div v-else-if="agentGovSupported === null" class="rounded-xl border border-border bg-card px-6 py-10 text-center text-[12.5px] text-muted-foreground">
      正在探测 Agent 治理可用性…
    </div>

    <template v-else>
      <div class="flex items-center justify-between gap-3">
        <p class="text-[12.5px] text-muted-foreground">
          kill switch 全局生效；uncertain=超时/崩溃后副作用不明的调用，须人工核实后回填。本页不自动刷新。
        </p>
        <button
          type="button" data-testid="agent-gov-refresh"
          class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-border px-3 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong"
          @click="loadAgentGovernance(true)"
        ><RefreshCw :size="13" :stroke-width="1.75" /> 刷新</button>
      </div>

      <LoadError :message="agentGovError" @retry="loadAgentGovernance(true)" />

      <!-- 漂移告警：代码↔注册表不一致（部署/迁移遗漏的信号），纯文本列表 -->
      <div
        v-if="agentToolDrift.length" data-testid="agent-gov-drift"
        class="rounded-xl border border-st-warn/40 bg-st-warn/[0.06] px-4 py-3"
      >
        <div class="flex items-center gap-2 text-[12.5px] font-semibold text-st-warn">
          <AlertTriangle :size="14" :stroke-width="1.75" /> 代码↔注册表漂移 {{ agentToolDrift.length }} 项（部署或迁移可能有遗漏）
        </div>
        <ul class="mt-1.5 space-y-0.5">
          <li v-for="(d, i) in agentToolDrift" :key="i" class="font-mono text-[11.5px] text-muted-foreground">{{ d }}</li>
        </ul>
      </div>

      <!-- 工具注册表（kill switch） -->
      <section>
        <div class="overflow-hidden rounded-[15px] border border-border bg-card">
          <div class="flex items-center gap-2.5 border-b border-border bg-accent-soft px-[18px] py-3">
            <Wrench :size="16" :stroke-width="1.75" class="text-accent-text" />
            <span class="text-sm font-semibold text-foreground">工具注册表</span>
            <span class="rounded-full bg-accent-strong px-2 py-px text-[11px] font-bold text-primary-foreground">{{ toolGroups.length }}</span>
            <div class="flex-1" />
            <span class="hidden text-xs text-muted-foreground sm:inline">停用=全局 kill switch，按工具（全部版本）整体生效</span>
          </div>
          <div
            v-for="g in toolGroups" :key="g.tool_name"
            class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3 first:border-t-0"
            :class="g.status === 'disabled' ? 'bg-panel/40' : ''"
          >
            <div class="min-w-0 flex-1">
              <div class="truncate text-[13.5px] font-semibold text-foreground">
                <span class="font-mono">{{ g.tool_name }}</span>
                <span class="ml-1.5 text-[11px] font-normal text-faint">{{ g.rows.map((r) => 'v' + r.version).join(' / ') }}</span>
                <span class="ml-2 rounded-full px-2 py-px text-[10.5px] font-medium" :class="riskTone(g.risk_level)">{{ riskLabel(g.risk_level) }}</span>
                <span
                  class="ml-1.5 rounded-full px-2 py-px text-[10.5px] font-medium"
                  :class="g.status === 'disabled' ? 'bg-st-fail/10 text-st-fail' : g.status === 'deprecated' ? 'bg-panel text-muted-foreground' : 'bg-st-live/10 text-st-live'"
                >{{ g.status === 'disabled' ? '已停用' : g.status === 'deprecated' ? '已弃用' : '启用中' }}</span>
              </div>
              <div class="mt-0.5 truncate text-[11.5px] text-faint">
                <span v-if="g.rows[0]?.owner_team">归属 {{ g.rows[0].owner_team }}</span>
                <span v-if="g.rows[0]?.registered_by"> · 注册 {{ g.rows[0].registered_by }}</span>
                <span v-if="g.rows[0]?.created_at"> · {{ fmtTs(g.rows[0].created_at) }}</span>
              </div>
            </div>
            <button
              type="button" :data-testid="`tool-toggle-${g.tool_name}`"
              class="inline-flex items-center justify-center gap-1 self-start rounded-lg px-3.5 py-[7px] text-[12.5px] font-medium transition disabled:opacity-50"
              :class="g.status === 'disabled'
                ? 'bg-primary font-semibold text-primary-foreground hover:opacity-90'
                : 'border border-st-fail/45 text-st-fail hover:border-st-fail/70'"
              :disabled="isAgentGovBusy(`tool:${g.tool_name}`)"
              @click="onToggle(g)"
            ><Loader2 v-if="isAgentGovBusy(`tool:${g.tool_name}`)" :size="13" :stroke-width="2" class="animate-spin" />{{ g.status === 'disabled' ? '恢复' : '停用' }}</button>
          </div>
          <p v-if="!toolGroups.length && agentGovSupported === true" class="px-[18px] py-8 text-center text-[12.5px] text-muted-foreground">
            注册表为空——Agent 运行时启动时会自动同步代码内注册的工具。
          </p>
        </div>
      </section>

      <!-- uncertain 对账台 -->
      <section>
        <div class="overflow-hidden rounded-[15px] border border-border bg-card">
          <div class="flex items-center gap-2.5 border-b border-border bg-st-busy/[0.07] px-[18px] py-3">
            <AlertTriangle :size="16" :stroke-width="1.75" class="text-st-busy" />
            <span class="text-sm font-semibold text-foreground">uncertain 对账台</span>
            <span v-if="agentUncertain.length" class="rounded-full bg-st-busy px-2 py-px text-[11px] font-bold text-white">{{ agentUncertain.length }}</span>
            <div class="flex-1" />
            <span class="hidden text-xs text-muted-foreground sm:inline">超时/崩溃后副作用不明——先到业务系统核实，再回填结论</span>
          </div>
          <div
            v-for="inv in agentUncertain" :key="inv.invocation_id"
            class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3 first:border-t-0"
          >
            <div class="min-w-0 flex-1">
              <div class="truncate text-[13.5px] font-semibold text-foreground">
                <span class="font-mono text-accent-text">{{ inv.tool_name }}</span>
                <span class="ml-2 rounded-full bg-st-busy/10 px-2 py-px text-[10.5px] font-medium text-st-busy">待对账</span>
              </div>
              <div v-if="inv.error_text" class="mt-0.5 truncate text-[12px] text-muted-foreground" :title="inv.error_text">{{ inv.error_text }}</div>
              <div class="mt-0.5 flex flex-wrap items-center gap-x-2 font-mono text-[11px] text-faint">
                <span>run {{ inv.run_id.slice(0, 10) }}</span>
                <span>· step {{ inv.step_no }}</span>
                <span v-if="inv.args_digest">· 参数摘要 {{ inv.args_digest.slice(0, 10) }}</span>
                <span v-if="inv.started_at">· {{ fmtTs(inv.started_at) }}</span>
              </div>
            </div>
            <button
              type="button"
              class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-muted-foreground transition hover:border-border-strong hover:text-foreground"
              title="uncertain 行不带原始参数——完整参数与步骤时间线在运行详情里"
              @click="gotoRun(inv)"
            >查看运行</button>
            <button
              type="button"
              class="inline-flex items-center justify-center gap-1 self-start rounded-lg border border-border px-3.5 py-[7px] text-[12.5px] font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
              :disabled="isAgentGovBusy(`inv:${inv.invocation_id}`)"
              @click="onResolve(inv, 'confirmed_failed')"
            >核实为失败</button>
            <button
              type="button"
              class="inline-flex items-center justify-center gap-1 self-start rounded-lg bg-primary px-3.5 py-[7px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
              :disabled="isAgentGovBusy(`inv:${inv.invocation_id}`)"
              @click="onResolve(inv, 'confirmed_succeeded')"
            ><Loader2 v-if="isAgentGovBusy(`inv:${inv.invocation_id}`)" :size="13" :stroke-width="2" class="animate-spin" />核实为成功</button>
          </div>
          <p v-if="!agentUncertain.length && agentGovSupported === true" class="px-[18px] py-8 text-center text-[12.5px] text-muted-foreground">
            当前没有待对账的调用——超时/崩溃后副作用不明的工具调用会出现在这里。
          </p>
        </div>
        <p class="ml-0.5 mt-2 text-[11.5px] text-faint">
          对账=把人工核实的结论回填为权威状态（不会重跑操作，也不会撤销已发生的副作用）；「核实依据」必填并写入审计日志。
        </p>
      </section>
    </template>
  </div>
</template>
