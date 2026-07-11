<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { Ban, Check, Hourglass, Loader2, X } from 'lucide-vue-next'
import type { ChatMessage } from '@/composables/useAsk'
import { RUN_TERMINAL, agentArgsPreview, agentToolLabel } from '@/composables/useAgentAsk'

// Agent 过程可视（P0-F 阶段化状态）：
//  · 阶段条——只映射真实事件（已提交/回答中/调用工具/工具完成/等待审批/完成/失败），
//    每阶段显示真实耗时（本地计时），不编造管线里不存在的假阶段；
//  · 工具 chips——tool_call 提案即现（spinner），tool_result 收敛为明确结局
//    （完成+耗时 / 失败 / 被策略拒绝 / 需审批），参数（服务端已脱敏）挂 title 可核对。
const props = defineProps<{ message: ChatMessage }>()
const m = props.message

// 当前阶段活跃 → 1s 心跳驱动「进行中」阶段的耗时安静推进；终态/错误即停。
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | null = null
const active = computed(() =>
  !m.error && (m.streaming || (!!m.agent?.status && !RUN_TERMINAL.has(m.agent.status))))
function ensureTick() {
  if (timer || typeof setInterval !== 'function') return
  timer = setInterval(() => {
    now.value = Date.now()
    if (!active.value && timer) { clearInterval(timer); timer = null }
  }, 1000)
}
// 活跃即起搏（immediate：localStorage 回灌的挂起消息重挂载也续跳）；不活跃由心跳自停。
watch(active, (a) => { if (a) ensureTick() }, { immediate: true })
onUnmounted(() => { if (timer) { clearInterval(timer); timer = null } })

function fmtDur(ms: number): string {
  if (!(ms >= 0)) return ''
  const s = ms / 1000
  if (s < 10) return `${s.toFixed(1)}s`
  if (s < 60) return `${Math.round(s)}s`
  return `${Math.floor(s / 60)}m${Math.round(s % 60)}s`
}

interface StageView { key: string; label: string; dur: string; live: boolean }
const stages = computed<StageView[]>(() => {
  const list = m.agent?.stages || []
  return list.map((st, i) => {
    const next = list[i + 1]
    const last = i === list.length - 1
    const terminal = st.key === 'done' || st.key === 'error'
    return {
      key: st.key + ':' + i,
      label: st.label,
      // 已过阶段=真实区间；进行中阶段=至今（now 心跳推进）；终态阶段与"结束时刻未知"的收尾阶段不计时
      dur: terminal ? '' : (next ? fmtDur(next.at - st.at) : (active.value ? fmtDur(now.value - st.at) : '')),
      live: last && !terminal && active.value,
    }
  })
})

const TOOL_TONE: Record<string, string> = {
  succeeded: 'text-st-live',
  failed: 'text-st-fail',
  denied: 'text-st-fail',
  pending_approval: 'text-st-warn',
}
function toolText(t: { toolName: string; status?: string; elapsedMs?: number }): string {
  const label = agentToolLabel(t.toolName)
  switch (t.status) {
    case 'succeeded': return `${label} 完成${t.elapsedMs ? `（${fmtDur(t.elapsedMs)}）` : ''}`
    case 'failed': return `${label} 失败`
    case 'denied': return `${label} 被策略拒绝`
    case 'pending_approval': return `${label} 等待审批`
    default: return `${label}…`
  }
}
</script>

<template>
  <div v-if="m.agent" class="mb-2 space-y-1.5" data-testid="agent-flow">
    <!-- 阶段化状态条：真实事件序列 + 每阶段耗时 -->
    <div v-if="stages.length" class="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-[11.5px] text-faint" data-testid="agent-stages">
      <template v-for="(st, i) in stages" :key="st.key">
        <span v-if="i > 0" class="text-border-strong" aria-hidden="true">›</span>
        <span class="inline-flex items-center gap-1" :class="st.live ? 'text-muted-foreground' : ''">
          <Loader2 v-if="st.live" :size="10" :stroke-width="2" class="animate-spin" />
          {{ st.label }}<span v-if="st.dur" class="font-mono tabular-nums">{{ st.dur }}</span>
        </span>
      </template>
    </div>

    <!-- 工具调用 chips：提案即现，tool_result 收敛结局 -->
    <div v-if="m.agent.tools && m.agent.tools.length" class="flex flex-wrap gap-1.5">
      <span
        v-for="(t, i) in m.agent.tools" :key="t.callId || i"
        class="inline-flex items-center gap-1.5 rounded-full border border-border bg-panel px-2.5 py-1 text-[11.5px] font-medium text-muted-foreground"
        :title="`${t.toolName}(${agentArgsPreview(t.args)})`"
        data-testid="agent-tool-chip"
      >
        <Loader2 v-if="!t.status || t.status === 'proposed'" :size="12" :stroke-width="2" class="animate-spin" />
        <Check v-else-if="t.status === 'succeeded'" :size="12" :stroke-width="2.25" class="text-st-live" />
        <Ban v-else-if="t.status === 'denied'" :size="12" :stroke-width="2" class="text-st-fail" />
        <Hourglass v-else-if="t.status === 'pending_approval'" :size="12" :stroke-width="2" class="text-st-warn" />
        <X v-else :size="12" :stroke-width="2.25" class="text-st-fail" />
        <span :class="TOOL_TONE[t.status || ''] || ''">{{ toolText(t) }}</span>
      </span>
    </div>
  </div>
</template>
