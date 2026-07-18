<script setup lang="ts">
import { computed } from 'vue'
import { ClipboardCheck, Lightbulb } from '@lucide/vue'
import { useKb } from '@/composables/useKb'
import { useAgentApprovals } from '@/composables/useAgentApprovals'
import { useContribute } from '@/composables/useContribute'
import AgentApprovalQueue from './AgentApprovalQueue.vue'
import ApprovalQueue from './ApprovalQueue.vue'
import AccessRequestQueue from './AccessRequestQueue.vue'
import ApprovalHistory from './ApprovalHistory.vue'

// 审批中心(单一审批入口):待办/历史同址切换,替代原先散在三处的入口
// (文档管理内嵌队列 + Agent 审批独立 tab + 审批历史独立 tab)。
// 待办面按风险降序:Agent 高风险执行(批准即放行,不可撤回)→ 上传入库 → 跨部门授权;
// 各区块沿用既有自隐约定,全空才给聚合空态——空态只看审批队列,不再与异常文档共享
// 显隐开关(异常信号归台账自己的徽章筛选)。历史面 = ApprovalHistory 原样(四条流时间线)。
// 知识贡献审核不搬家(它与缺口列表同上下文),这里只留带计数的跳转入口。
const props = defineProps<{ view: 'pending' | 'history' }>()
const emit = defineEmits<{ (e: 'update:view', v: 'pending' | 'history'): void }>()

const { approvals, accessRequests, queuesSettled, isKbAdmin, reviewCount, loadErrors } = useKb()
const { agentApprovalCount, agentApprovalError } = useAgentApprovals()
const { reviewCount: contribReviewCount } = useContribute()

// 三区块显隐与各组件内部的自隐条件保持同构(错误态也算"有内容",LoadError 要可见可重试)。
const hasAgentBlock = computed(() => agentApprovalCount.value > 0 || !!agentApprovalError.value)
const hasKbBlocks = computed(() =>
  (isKbAdmin.value && (approvals.value.length > 0 || !!loadErrors.value['approvals']))
  || accessRequests.value.length > 0 || !!loadErrors.value['accessRequests'])
// 聚合空态等 kb 队列拉取过(queuesSettled)再显,避免加载间隙闪"没有待办";
// Agent 队列后到时区块自然浮现(渐进呈现,与其余队列一致)。
const allEmpty = computed(() => !hasAgentBlock.value && !hasKbBlocks.value && queuesSettled.value)

const VIEWS = [
  { key: 'pending', label: '待办' },
  { key: 'history', label: '历史' },
] as const
</script>

<template>
  <section class="space-y-4" data-testid="approval-center">
    <!-- 待办/历史 切换:复用审批历史类型筛选的 chip 视觉(rounded-full + accent 激活态) -->
    <div class="flex flex-wrap items-center gap-1.5" role="tablist" aria-label="审批分区">
      <button
        v-for="v in VIEWS" :key="v.key" type="button" role="tab"
        :aria-selected="props.view === v.key"
        class="inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1.5 text-[12.5px] font-medium transition"
        :class="props.view === v.key ? 'border-accent-strong bg-card text-accent-text' : 'border-border text-muted-foreground hover:text-foreground'"
        @click="emit('update:view', v.key)"
      >
        {{ v.label }}
        <b v-if="v.key === 'pending' && reviewCount" class="font-mono text-[11.5px] tabular-nums">{{ reviewCount }}</b>
      </button>
    </div>

    <!-- ── 待办面:风险降序,区块自隐 ── -->
    <template v-if="props.view === 'pending'">
      <AgentApprovalQueue v-if="hasAgentBlock" />
      <ApprovalQueue />
      <AccessRequestQueue />

      <!-- 聚合空态:三条流都清空(或功能未开)时显式确认,不留白屏 -->
      <div v-if="allEmpty" data-testid="approval-empty" class="rounded-xl border border-border bg-card px-6 py-10 text-center">
        <ClipboardCheck :size="22" :stroke-width="1.5" class="mx-auto text-faint" />
        <p class="mt-3 text-sm font-medium text-foreground">当前没有待你处理的审批</p>
        <p class="mt-1 text-xs text-muted-foreground">
          新的{{ isKbAdmin ? '上传入库审批' : '跨部门授权申请' }}和 Agent 高风险操作会先出现在这里。
        </p>
      </div>

      <!-- 交叉入口:知识贡献审核留在贡献页(与缺口列表同上下文),这里只给带计数的直达 -->
      <div
        v-if="contribReviewCount"
        data-testid="approval-contrib-link"
        class="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-panel/60 px-5 py-3.5"
      >
        <span class="grid size-8 shrink-0 place-items-center rounded-[9px] bg-accent-soft text-accent-text"><Lightbulb :size="15" :stroke-width="1.75" /></span>
        <div class="min-w-0 flex-1 text-[13px] text-foreground">
          知识贡献待审 <b class="font-mono tabular-nums text-accent-text">{{ contribReviewCount }}</b> 条
          <span class="text-xs text-muted-foreground">· 在知识贡献页审核采纳</span>
        </div>
        <RouterLink
          to="/contribute"
          class="shrink-0 rounded-lg border border-border bg-card px-3.5 py-1.5 text-[12.5px] font-semibold text-accent-text transition hover:border-accent-strong hover:bg-accent-soft"
        >去处理</RouterLink>
      </div>
    </template>

    <!-- ── 历史面:四条审批流合并时间线(只读) ── -->
    <ApprovalHistory v-else />
  </section>
</template>
