<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRoute, useRouter } from 'vue-router'
import { Building2, MessagesSquare, Sparkles, LayoutDashboard, FolderOpen, UserCog, ClipboardCheck, Lightbulb, Fingerprint } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { consumePendingVersion } from '@/composables/useAuth'
import { useKb } from '@/composables/useKb'
import { useAgentApprovals } from '@/composables/useAgentApprovals'
import { useOntology } from '@/composables/useOntology'
import { useAsk } from '@/composables/useAsk'
import { deptLabel } from '@/lib/kb'
import UploadCard from '@/components/manage/UploadCard.vue'
import AccessGrantList from '@/components/manage/AccessGrantList.vue'
import DocTable from '@/components/manage/DocTable.vue'
import VersionHistoryModal from '@/components/manage/VersionHistoryModal.vue'
import AccessRequestModal from '@/components/manage/AccessRequestModal.vue'
import ShareDocModal from '@/components/manage/ShareDocModal.vue'
import VisibilityModal from '@/components/manage/VisibilityModal.vue'
import KbAdminDashboard from '@/components/manage/KbAdminDashboard.vue'
import DeptDashboard from '@/components/manage/DeptDashboard.vue'
import MemberRoleManager from '@/components/manage/MemberRoleManager.vue'
import ApprovalCenter from '@/components/manage/ApprovalCenter.vue'
import OntologyWorkbench from '@/components/manage/OntologyWorkbench.vue'

// 知识库入口：管理员 → 分 tab 管理台（概览看板 / 文档管理 / 审批 / [本体消解] / [成员管理]）；
// 普通员工 → 只读基本概览（只用可访问数据：whoami + hot-questions，不打 admin-gated 接口）。
// AppShell 仅在 ready 后渲染，故身份已解析。
const { canManage, identity } = storeToRefs(useSession())
const { isKbAdmin, reviewCount, accessGrants, loadDocs, loadStats, loadConfig, loadInsights, loadGovernance, loadApprovals, loadAccessRequests, loadAccessGrants, loadApprovalHistory, loadAdminGrants, loadFeedbackReview, loadEscalations, loadReviewTasks, applyPendingVersion } = useKb()
const { loadAgentApprovals } = useAgentApprovals()
const { ontologySupported, loadOntology } = useOntology()
const { hotQuestions, loadHotQuestions, fillInput } = useAsk()
const router = useRouter()
const route = useRoute()

// ── 「文档管理」信息架构：上传 → 台账 → 授权治理（纯台账职责）──
// 待办审批全部收进「审批」tab（ApprovalCenter）；异常文档信号由台账自己的徽章筛选 chips 承担。
// 分区眉标与看板 HEADER 同一视觉语言。
const ZONE = 'mb-3 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint'

// ── 管理台子 tab（成员管理仅 kb_admin 可见）──
// 「审批」= 审批中心（待办/历史同址），常驻：Agent 区块随端点探测自隐，tab 本身不消失。
type Tab = 'dash' | 'docs' | 'approvals' | 'ontology' | 'members'
const VALID_TABS = ['dash', 'docs', 'approvals', 'ontology', 'members'] as const
const activeTab = ref<Tab>('dash')
const approvalsView = ref<'pending' | 'history'>('pending')
// 旧深链兼容：tab=agent（独立 Agent 审批 tab 时代）→ 审批待办面；tab=history（独立审批
// 历史 tab 时代）→ 审批历史面。站内引用已无这两个 key（唯一站内引用是 tab=docs），
// 别名只为外部书签/习惯留后路。
function resolveTab(t: unknown): { tab: Tab; view?: 'pending' | 'history' } | null {
  if (typeof t !== 'string') return null
  if (t === 'agent') return { tab: 'approvals', view: 'pending' }
  if (t === 'history') return { tab: 'approvals', view: 'history' }
  if ((VALID_TABS as readonly string[]).includes(t) && (t !== 'members' || isKbAdmin.value)) return { tab: t as Tab }
  return null
}
// tab ←→ URL（P2：刷新/深链不再落回默认 tab）。身份在 AppShell ready 后已解析，可安全校验 members。
// route?. 可选链 = 单测无 router 环境的既有约定（同 Sidebar）。
{
  const r = resolveTab(route?.query?.tab)
  if (r) { activeTab.value = r.tab; if (r.view) approvalsView.value = r.view }
  if (activeTab.value === 'approvals' && route?.query?.view === 'history') approvalsView.value = 'history'
}
// 文档管理 tab 的专属筛选键（DocTable 写入 URL / 挂载时恢复）。切到其他 tab 时从 URL 摘除——
// 深链/分享只携带当前 tab 的状态（staging 2026-07-11 §3-P3「审批 URL 残留 q=」）。白名单式：
// 只清这六键，绝不触碰 debug/preview/corpId 等全局键。切回 docs 时筛选仍在（useKb 模块态保持），
// 仅「切走再切回后 F5」不再从 URL 恢复——换取审批深链不携带无关搜索词。
const DOC_URL_KEYS = ['scope', 'q', 'badge', 'perm', 'owner', 'cited'] as const
watch([activeTab, approvalsView], ([t, v]) => {
  const q: Record<string, unknown> = { ...(route?.query || {}) }
  if (t !== 'docs') for (const k of DOC_URL_KEYS) delete q[k]
  if (t === 'dash') delete q.tab; else q.tab = t
  if (t === 'approvals' && v === 'history') q.view = 'history'; else delete q.view
  void router?.replace({ query: q as Record<string, string> })
})
// 反向：URL tab 变化 → 切 tab（差评复核「定位文档」等站内导航靠它；同值 no-op 防回环）
watch(() => route?.query?.tab, (t) => {
  const r = resolveTab(t)
  if (!r) return
  if (r.tab !== activeTab.value) activeTab.value = r.tab
  if (r.view && r.view !== approvalsView.value) approvalsView.value = r.view
})
const tabs = computed<{ key: Tab; label: string; icon: any }[]>(() => [
  { key: 'dash', label: '概览看板', icon: LayoutDashboard },
  { key: 'docs', label: '文档管理', icon: FolderOpen },
  { key: 'approvals', label: '审批', icon: ClipboardCheck },
  // 本体消解：RAG_ONTOLOGY_ENABLE 未开（404）或非管理角色（403）→ tab 自隐
  ...(ontologySupported.value === true ? [{ key: 'ontology' as Tab, label: '本体消解', icon: Fingerprint }] : []),
  ...(isKbAdmin.value ? [{ key: 'members' as Tab, label: '成员管理', icon: UserCog }] : []),
])
// 「审批」tab 角标 = 待你审核数（reviewCount 聚合口径：角色职责队列 + Agent 审批，
// 与侧栏入口红点同一来源）。

// ── 员工概览（只读，可访问数据）──
const myDeptChips = computed(() => (identity.value?.aclGroups || []).map(deptLabel))
// ── 管理员头部的管辖范围：kb_admin 全库收成一枚（10 个组码逐一列出只是噪声），dept_admin 列中文名 chips ──
const managedDepts = computed(() => identity.value?.managedOwnerDepts || [])

function askHot(q: string) { fillInput(q); void router.push('/') }

onMounted(async () => {
  if (canManage.value) {
    // 全并发（Perf-7）：此前 `await loadDocs()` 把后面 8 个加载都挡在 docs 一个 RTT 之后，
    // 看板/队列白等。docs 的 promise 只留给升版深链（applyPendingVersion 要在 docs 就绪后）。
    const docsReady = loadDocs()
    void loadStats()
    void loadConfig()
    void loadInsights()                              // 概览看板：使用成效 + 知识缺口（两角色）
    void loadFeedbackReview()                        // 差评复核（两角色，看板卡片）
    void loadEscalations()                           // 转人工工单队列（两角色，看板卡片）
    void loadApprovals()                             // 非 force：App ready 已为红点预载，30s 内不重拉（#82）
    void loadAccessRequests()                        // 同上（staleness 门在 useKb 内）
    void loadAccessGrants()
    void loadApprovalHistory()                       // 审批历史（两角色，只读聚合）
    void loadAgentApprovals()                        // Agent 审批队列（404/403 → tab 自隐）
    void loadOntology()                              // 本体消解工作台（404/403 → tab 自隐）
    if (isKbAdmin.value) { void loadGovernance(); void loadAdminGrants(); void loadReviewTasks() }   // 全库治理 + 成员管理 + 复审任务（kb_admin）
    await docsReady
    const p = consumePendingVersion()   // 升版深链：切到「文档管理」tab 后再消费
    if (p) { activeTab.value = 'docs'; applyPendingVersion(p) }
  } else {
    if (!hotQuestions.value.length) void loadHotQuestions()
  }
})

// ── 审批/行动队列轻轮询（批次α-②）：此前全部 load-once——挂着管理台整个会话都看不到新单
// （另一位管理员处置了同一单也不知情）。每 60s 补拉三个审批队列：非 force 调用天然复用各 loader
// 的 30s staleness 门与身份/角色守卫（与预载、LoadError 手动重试互相去重，语义不变）；页签
// 后台化（visibilityState）与非管理员不打。只碰审批类队列，绝不触达 loadDocs——
// 「切子 tab 不整树重挂载：my-docs 恰一次」的硬门断言保持成立。
const QUEUE_POLL_MS = 60_000
let queuePollTimer: number | undefined
function pollQueues() {
  if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
  if (!canManage.value) return
  void loadApprovals()
  void loadAccessRequests()
  void loadAgentApprovals()
}
onMounted(() => { queuePollTimer = window.setInterval(pollQueues, QUEUE_POLL_MS) })
onUnmounted(() => { if (queuePollTimer !== undefined) window.clearInterval(queuePollTimer) })
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
          v-if="t.key === 'approvals' && reviewCount"
          class="grid h-[17px] min-w-[17px] place-items-center rounded-full bg-st-busy px-1.5 text-[10px] font-bold tabular-nums text-white"
        >{{ reviewCount }}</span>
      </button>
    </div>

    <!-- 概览看板：按角色分流（kb_admin 全库 / dept_admin 本部门） -->
    <KbAdminDashboard v-if="activeTab === 'dash' && isKbAdmin" />
    <DeptDashboard v-else-if="activeTab === 'dash'" />

    <!-- 文档管理：上传 → 台账（主体）→ 授权治理（存量参考置底）。
         待办审批全部在「审批」tab；台账不再被队列推下首屏（审计问题3）。 -->
    <template v-else-if="activeTab === 'docs'">
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

    <!-- 审批中心（两角色）：待办（Agent 高风险 → 上传入库 → 跨部门授权）/ 历史 同址切换 -->
    <ApprovalCenter v-else-if="activeTab === 'approvals'" v-model:view="approvalsView" />
    <OntologyWorkbench v-else-if="activeTab === 'ontology'" />

    <!-- 成员管理（仅 kb_admin）：维护部门管理员 + 其可管理 owner_dept（写授权） -->
    <MemberRoleManager v-else-if="activeTab === 'members' && isKbAdmin" />

    <VersionHistoryModal />
    <AccessRequestModal />
    <ShareDocModal />
    <VisibilityModal />
    <!-- 确认/输入/告知 框已上移 AppShell 全局挂载（贡献页审核失败等 notice 也要能渲染） -->
  </div>
</template>
