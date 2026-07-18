<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import { Building2, MessagesSquare, Sparkles, LayoutDashboard, FolderOpen, UserCog, History, Lightbulb, Gauge } from '@lucide/vue'
import { useSession } from '@/stores/session'
import { consumePendingVersion } from '@/composables/useAuth'
import { useKb } from '@/composables/useKb'
import { useAsk } from '@/composables/useAsk'
import { deptLabel } from '@/lib/kb'
import UploadCard from '@/components/manage/UploadCard.vue'
import ApprovalQueue from '@/components/manage/ApprovalQueue.vue'
import AccessRequestQueue from '@/components/manage/AccessRequestQueue.vue'
import AccessGrantList from '@/components/manage/AccessGrantList.vue'
import DocTable from '@/components/manage/DocTable.vue'
import VersionHistoryModal from '@/components/manage/VersionHistoryModal.vue'
import AccessRequestModal from '@/components/manage/AccessRequestModal.vue'
import ShareDocModal from '@/components/manage/ShareDocModal.vue'
import VisibilityModal from '@/components/manage/VisibilityModal.vue'
import KbAdminDashboard from '@/components/manage/KbAdminDashboard.vue'
import DeptDashboard from '@/components/manage/DeptDashboard.vue'
import MemberRoleManager from '@/components/manage/MemberRoleManager.vue'
import ApprovalHistory from '@/components/manage/ApprovalHistory.vue'
import OpsMetricsPanel from '@/components/manage/OpsMetricsPanel.vue'

// 知识库入口：管理员 → 分 tab 管理台（概览看板 / 文档管理，设计稿 SUB-TAB SWITCHER）；
// 普通员工 → 只读基本概览（只用可访问数据：whoami + hot-questions，不打 admin-gated 接口）。
// AppShell 仅在 ready 后渲染，故身份已解析。
const { canManage, identity } = storeToRefs(useSession())
const { isKbAdmin, reviewCount, anomalyCount, approvals, accessRequests, queuesSettled, accessGrants, setBadgeFilter, loadDocs, loadStats, loadConfig, loadInsights, loadGovernance, loadOpsMetrics, loadApprovals, loadAccessRequests, loadAccessGrants, loadApprovalHistory, loadAdminGrants, loadFeedbackReview, loadReviewTasks, applyPendingVersion } = useKb()
const { hotQuestions, loadHotQuestions, fillInput } = useAsk()
const router = useRouter()
const route = useRoute()

// ── 「文档管理」信息架构：待办摘要条 + 分区（待办审批 → 上传 → 台账 → 授权治理）──
// 分区眉标与看板 HEADER 同一视觉语言；各队列组件自带空态自隐，眉标随内容一起隐藏。
const ZONE = 'mb-3 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint'
// 异常文档数取自 useKb 的全库口径（/api/kb/stats by_badge），不再只数已加载页（#7）。
const hasQueues = computed(() => (isKbAdmin.value && approvals.value.length > 0) || accessRequests.value.length > 0)
interface TodoChip { key: string; label: string; n: number; anchor: string; tone: string }
const todoChips = computed<TodoChip[]>(() => {
  const chips: TodoChip[] = []
  if (isKbAdmin.value && approvals.value.length) chips.push({ key: 'appr', label: '待审批上传', n: approvals.value.length, anchor: 'kb-sec-queues', tone: 'text-st-busy' })
  if (accessRequests.value.length) chips.push({ key: 'req', label: '授权申请', n: accessRequests.value.length, anchor: 'kb-sec-queues', tone: 'text-accent-text' })
  if (anomalyCount.value) chips.push({ key: 'anom', label: '异常文档', n: anomalyCount.value, anchor: 'kb-sec-ledger', tone: 'text-st-fail' })
  return chips
})
function scrollToSec(id: string) { document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' }) }
// 异常 chip：滚动 + 顺带设「异常」聚合筛选（原先只滚动，还得自己再挑一个坏徽章点）
function onTodoChip(c: TodoChip) { if (c.key === 'anom') setBadgeFilter('异常'); scrollToSec(c.anchor) }

// ── 管理台子 tab（成员管理仅 kb_admin 可见）──
type Tab = 'dash' | 'docs' | 'history' | 'ops' | 'members'
const VALID_TABS = ['dash', 'docs', 'history', 'ops', 'members'] as const
// kb_admin 专属 tab 名单：深链门与 tab bar 渲染门共用一张表——此前是手写字面量排除
// 表达式（t !== 'members'），每加一个 kb_admin tab 都要人肉同步，漏一处 = 非管理员
// 深链直达空白页（批次γ 审计风险 1）。收敛成单一名单。
const KB_ADMIN_TABS: readonly Tab[] = ['members', 'ops'] as const
const activeTab = ref<Tab>('dash')
// tab ←→ URL（P2：刷新/深链不再落回默认 tab）。身份在 AppShell ready 后已解析，可安全校验 members。
// route?. 可选链 = 单测无 router 环境的既有约定（同 Sidebar）。
{
  const t = route?.query?.tab
  if (typeof t === 'string' && (VALID_TABS as readonly string[]).includes(t) && (!(KB_ADMIN_TABS as readonly string[]).includes(t) || isKbAdmin.value)) activeTab.value = t as Tab
}
watch(activeTab, (t) => { void router?.replace({ query: { ...(route?.query || {}), tab: t === 'dash' ? undefined : t } }) })
// 反向：URL tab 变化 → 切 tab（差评复核「定位文档」等站内导航靠它；同值 no-op 防回环）
watch(() => route?.query?.tab, (t) => {
  if (typeof t === 'string' && (VALID_TABS as readonly string[]).includes(t) && (!(KB_ADMIN_TABS as readonly string[]).includes(t) || isKbAdmin.value) && t !== activeTab.value) activeTab.value = t as Tab
})
const tabs = computed<{ key: Tab; label: string; icon: any }[]>(() => [
  { key: 'dash', label: '概览看板', icon: LayoutDashboard },
  { key: 'docs', label: '文档管理', icon: FolderOpen },
  { key: 'history', label: '审批历史', icon: History },
  // 运营指标（批次γ）：members 档纯角色门（端点无 flag 语义，三块 available 是业务降级非功能开关）。
  // 只读观测面板——绝不并入「待你处理」chip/reviewCount 的行动语义。
  ...(isKbAdmin.value ? [{ key: 'ops' as Tab, label: '运营指标', icon: Gauge }] : []),
  ...(isKbAdmin.value ? [{ key: 'members' as Tab, label: '成员管理', icon: UserCog }] : []),
])
// 「文档管理」tab 角标 = 待你审核数（reviewCount，与侧栏入口红点同一来源）。

// ── 员工概览（只读，可访问数据）──
const myDeptChips = computed(() => (identity.value?.aclGroups || []).map(deptLabel))
// ── 管理员头部的管辖范围：kb_admin 全库收成一枚（10 个组码逐一列出只是噪声），dept_admin 列中文名 chips ──
const managedDepts = computed(() => identity.value?.managedOwnerDepts || [])

function askHot(q: string) { fillInput(q); void router.push('/') }

// ── 按 tab 惰性加载（perf 2026-07-16 ①）：此前挂载即并发拉全部 ~13 个接口——首屏被最慢
// 端点拖住（stats 实测 4.6-5.2s），还制造了 429 雪崩的请求源。现在挂载只拉三类：
//   a) 探测（tab 自隐判据，不拉就没入口）：ontology / agent 审批 / agent 治理；
//   b) 角标（「审批」badge + 侧栏红点，60s pollQueues 同款三队列）；
//   c) 当前 tab 所需数据。
// 其余 tab 首次激活时补拉（ensureTabLoaded；loader 各自的 30s staleness/LoadError 手动
// 重试语义不变——失败后面板内重试按钮直调 load*，不经本表）。
const _loadedTabs = new Set<Tab>()
function ensureTabLoaded(t: Tab): Promise<unknown> {
  if (_loadedTabs.has(t)) return Promise.resolve()
  _loadedTabs.add(t)
  const jobs: unknown[] = []
  if (t === 'dash') {
    jobs.push(loadStats(), loadInsights(), loadFeedbackReview())
    if (isKbAdmin.value) jobs.push(loadGovernance(), loadReviewTasks())
  } else if (t === 'docs') {
    // stats 兼供台账（归属下拉全库口径）；accessGrants=台账底部「授权治理」区
    jobs.push(loadDocs(), loadConfig(), loadStats(), loadAccessGrants())
  } else if (t === 'history') {                      // main 旧 IA：独立「审批历史」tab
    jobs.push(loadApprovalHistory())                 // 待办队列已全局预载+轮询
  } else if (t === 'ops') {
    if (isKbAdmin.value) jobs.push(loadOpsMetrics())
  } else if (t === 'members') {
    if (isKbAdmin.value) jobs.push(loadAdminGrants())
  }
  // （main 版无 ontology/agent_gov tab）
  return Promise.allSettled(jobs.map((j) => Promise.resolve(j)))
}
watch(activeTab, (t) => { if (canManage.value) void ensureTabLoaded(t) })

onMounted(async () => {
  if (canManage.value) {
    // （main 热应用版：无 agent/ontology 探测——该 IA 在 ontology-p0 分支）
    void loadApprovals()                             // 角标/红点队列（非 force：ready 预载 30s 内不重拉，#82）
    void loadAccessRequests()
    const tabReady = ensureTabLoaded(activeTab.value)
    const p = consumePendingVersion()   // 升版深链：切到「文档管理」tab 后再消费（docs 就绪后）
    if (p) { activeTab.value = 'docs'; await ensureTabLoaded('docs'); applyPendingVersion(p) }
    await tabReady
  } else {
    if (!hotQuestions.value.length) void loadHotQuestions()
  }
})
</script>

<template>
  <!-- ───────── 普通员工：只读基本概览 ───────── -->
  <div v-if="!canManage" class="mx-auto w-full max-w-3xl space-y-5 px-6 py-8">
    <header class="border-b border-border pb-4">
      <h1 class="font-serif text-2xl tracking-tight text-foreground">知识库概览</h1>
      <p class="mt-1 text-sm text-muted-foreground">你以员工身份访问，可查看概览并直接提问；文档上传与管理由部门管理员负责。</p>
    </header>

    <!-- 我的可检索范围：部门 chips + 口径说明（替代原先「热门问题 6」这类无信息量的计数卡） -->
    <section class="kb-card rounded-xl border border-border bg-card p-5">
      <h2 class="flex items-center gap-2 text-sm font-bold text-foreground"><Building2 :size="15" :stroke-width="1.75" class="text-accent-text" /> 我的可检索范围</h2>
      <div class="mt-3 flex flex-wrap items-center gap-1.5">
        <span v-for="d in myDeptChips" :key="d" class="rounded-full bg-accent-soft px-3 py-1 text-[12.5px] font-medium text-accent-text">{{ d }}</span>
        <span v-if="!myDeptChips.length" class="text-sm text-muted-foreground">—</span>
      </div>
      <p class="mt-2.5 text-xs text-muted-foreground">可检索以上部门的内部文档，以及全公司公开文档；需要其他部门资料时，答案会提示你找对应管理员。</p>
    </section>

    <section class="rounded-xl border border-border bg-card p-5">
      <h2 class="flex items-center gap-2 text-sm font-bold text-foreground"><Sparkles :size="15" :stroke-width="1.75" class="text-accent-text" /> 热门问题</h2>
      <p class="mt-1 text-xs text-muted-foreground">点一个直接带去「问答」。</p>
      <div v-if="hotQuestions.length" class="mt-3 flex flex-wrap gap-2">
        <button
          v-for="(h, i) in hotQuestions" :key="i"
          type="button"
          class="rounded-full border border-border bg-card px-3.5 py-1.5 text-sm text-foreground transition hover:border-ring hover:bg-panel"
          @click="askHot(h)"
        >{{ h }}</button>
      </div>
      <p v-else class="mt-3 text-sm text-muted-foreground">暂无热门问题。</p>
      <button
        type="button"
        class="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90"
        @click="router.push('/')"
      >
        <MessagesSquare :size="15" :stroke-width="1.75" /> 去问答
      </button>
    </section>

    <!-- 交叉入口：没答上的问题 → 知识贡献 -->
    <section class="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-panel/60 px-5 py-4">
      <span class="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent-soft text-accent-text"><Lightbulb :size="17" :stroke-width="1.75" /></span>
      <div class="min-w-0 flex-1">
        <div class="text-[13.5px] font-semibold text-foreground">遇到没答上来的问题？</div>
        <div class="mt-0.5 text-xs text-muted-foreground">把你知道的答案贡献出来，采纳后全部门都能搜到。</div>
      </div>
      <RouterLink
        to="/contribute"
        class="shrink-0 rounded-lg border border-border bg-card px-3.5 py-2 text-[12.5px] font-semibold text-accent-text transition hover:border-accent-strong hover:bg-accent-soft"
      >去知识贡献</RouterLink>
    </section>
  </div>

  <!-- ───────── 管理员：分 tab 管理台 ───────── -->
  <div v-else class="mx-auto w-full max-w-5xl space-y-6 px-6 py-8">
    <header class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border pb-4">
      <h1 class="font-serif text-2xl tracking-tight text-foreground">知识库管理</h1>
      <!-- 管辖范围：kb_admin 全库收成一枚，dept_admin 逐个列中文名（组码只在悬停提示里） -->
      <div class="flex flex-wrap items-center justify-end gap-1.5">
        <span
          v-if="isKbAdmin"
          class="rounded-full border border-border bg-panel px-2.5 py-1 text-[11.5px] font-medium text-muted-foreground"
          :title="managedDepts.join(' · ')"
        >全库 · {{ managedDepts.length }} 个部门</span>
        <template v-else>
          <span
            v-for="d in managedDepts" :key="d"
            class="rounded-full border border-border bg-panel px-2.5 py-1 text-[11.5px] font-medium text-muted-foreground" :title="d"
          >{{ deptLabel(d) }}</span>
        </template>
        <span v-if="!managedDepts.length" class="text-xs text-muted-foreground">—</span>
      </div>
    </header>

    <!-- 子 tab：概览看板 / 文档管理 -->
    <div class="-mt-2 flex gap-1 border-b border-border" role="tablist" aria-label="管理台分区">
      <button
        v-for="t in tabs" :key="t.key" type="button" role="tab"
        :aria-selected="activeTab === t.key"
        class="relative -mb-px flex items-center gap-2 border-b-2 px-3.5 py-2.5 text-sm font-medium transition"
        :class="activeTab === t.key ? 'border-accent-strong text-accent-text' : 'border-transparent text-muted-foreground hover:text-foreground'"
        @click="activeTab = t.key"
      >
        <component :is="t.icon" :size="15" :stroke-width="1.75" />
        {{ t.label }}
        <span
          v-if="t.key === 'docs' && reviewCount"
          class="grid h-[17px] min-w-[17px] place-items-center rounded-full bg-st-busy px-1.5 text-[10px] font-bold tabular-nums text-white"
        >{{ reviewCount }}</span>
      </button>
    </div>

    <!-- 概览看板：按角色分流（kb_admin 全库 / dept_admin 本部门） -->
    <KbAdminDashboard v-if="activeTab === 'dash' && isKbAdmin" />
    <DeptDashboard v-else-if="activeTab === 'dash'" />

    <!-- 文档管理：待办摘要条 → 待办审批（自隐）→ 上传 → 台账（主体）→ 授权治理（存量参考置底） -->
    <template v-else-if="activeTab === 'docs'">
      <!-- 待办摘要条：一眼看清今天要处理什么；点击滚动到对应区块。有待办时台账被推下首屏 →
           条尾常备「跳到台账」一键直达；全空且队列已拉取过 → 一行确认文案（区分「处理完了」与「功能没开」）。 -->
      <div
        v-if="todoChips.length"
        class="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-panel/60 px-4 py-3"
      >
        <span class="text-[12.5px] font-semibold text-foreground">待办</span>
        <button
          v-for="c in todoChips" :key="c.key" type="button"
          class="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-3 py-1 text-[12px] font-medium transition hover:border-border-strong"
          :class="c.tone"
          @click="onTodoChip(c)"
        >{{ c.label }} <b class="font-mono tabular-nums">{{ c.n }}</b></button>
        <div class="flex-1" />
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[12px] font-medium text-muted-foreground transition hover:bg-card hover:text-foreground"
          title="直达文档台账" @click="scrollToSec('kb-sec-ledger')"
        >跳到台账 ↓</button>
      </div>
      <p v-else-if="queuesSettled" class="ml-0.5 text-[12px] text-faint">
        当前无待办 —— 新的{{ isKbAdmin ? '上传审批 / ' : '' }}授权申请会先出现在这里。
      </p>

      <section v-if="hasQueues" id="kb-sec-queues" class="space-y-4 scroll-mt-4">
        <p :class="ZONE">待办审批</p>
        <ApprovalQueue />
        <AccessRequestQueue />
      </section>

      <section id="kb-sec-upload" class="scroll-mt-4">
        <p :class="ZONE">上传入库</p>
        <UploadCard />
      </section>

      <section id="kb-sec-ledger" class="scroll-mt-4">
        <p :class="ZONE">文档台账</p>
        <DocTable />
      </section>

      <section v-if="accessGrants.length" id="kb-sec-grants" class="scroll-mt-4">
        <p :class="ZONE">授权治理 · 已放行的跨部门检索</p>
        <AccessGrantList />
      </section>
    </template>

    <!-- 审批历史（两角色）：四条审批流的历史决策合并时间线（只读） -->
    <ApprovalHistory v-else-if="activeTab === 'history'" />

    <!-- 运营指标（仅 kb_admin，批次γ）：LLM 用量/SLO 日趋势/限流准入——只读观测，独立于看板行动区 -->
    <OpsMetricsPanel v-else-if="activeTab === 'ops' && isKbAdmin" />

    <!-- 成员管理（仅 kb_admin）：维护部门管理员 + 其可管理 owner_dept（写授权） -->
    <MemberRoleManager v-else-if="activeTab === 'members' && isKbAdmin" />

    <VersionHistoryModal />
    <AccessRequestModal />
    <ShareDocModal />
    <VisibilityModal />
    <!-- 确认/输入/告知 框已上移 AppShell 全局挂载（贡献页审核失败等 notice 也要能渲染） -->
  </div>
</template>
