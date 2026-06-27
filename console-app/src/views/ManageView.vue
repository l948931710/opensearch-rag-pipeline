<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { Building2, MessagesSquare, Sparkles, LayoutDashboard, FolderOpen } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { consumePendingVersion } from '@/composables/useAuth'
import { useKb } from '@/composables/useKb'
import { useAsk } from '@/composables/useAsk'
import { deptLabel } from '@/lib/kb'
import UploadCard from '@/components/manage/UploadCard.vue'
import ApprovalQueue from '@/components/manage/ApprovalQueue.vue'
import AccessRequestQueue from '@/components/manage/AccessRequestQueue.vue'
import DocTable from '@/components/manage/DocTable.vue'
import VersionHistoryModal from '@/components/manage/VersionHistoryModal.vue'
import AccessRequestModal from '@/components/manage/AccessRequestModal.vue'
import KbAdminDashboard from '@/components/manage/KbAdminDashboard.vue'
import DeptDashboard from '@/components/manage/DeptDashboard.vue'

// 知识库入口：管理员 → 分 tab 管理台（概览看板 / 文档管理，设计稿 SUB-TAB SWITCHER）；
// 普通员工 → 只读基本概览（只用可访问数据：whoami + hot-questions，不打 admin-gated 接口）。
// AppShell 仅在 ready 后渲染，故身份已解析。
const { canManage, identity } = storeToRefs(useSession())
const { isKbAdmin, reviewCount, loadDocs, loadStats, loadConfig, loadApprovals, loadAccessRequests, applyPendingVersion } = useKb()
const { hotQuestions, loadHotQuestions, fillInput } = useAsk()
const router = useRouter()

// ── 管理台子 tab ──
type Tab = 'dash' | 'docs'
const activeTab = ref<Tab>('dash')
const tabs: { key: Tab; label: string; icon: any }[] = [
  { key: 'dash', label: '概览看板', icon: LayoutDashboard },
  { key: 'docs', label: '文档管理', icon: FolderOpen },
]
// 「文档管理」tab 角标 = 待你审核数（reviewCount，与侧栏入口红点同一来源）。

// ── 员工概览卡（只读，可访问数据）──
const myDepts = computed(() => (identity.value?.aclGroups || []).map(deptLabel).join('、') || '—')
const empCards = computed(() => [
  { key: 'dept', label: '我的部门', value: myDepts.value, icon: Building2, mono: false },
  { key: 'hot', label: '热门问题', value: String(hotQuestions.value.length), icon: Sparkles, mono: true },
])

function askHot(q: string) { fillInput(q); void router.push('/') }

onMounted(async () => {
  if (canManage.value) {
    await loadDocs()
    void loadStats()
    void loadConfig()
    void loadApprovals()
    void loadAccessRequests()
    const p = consumePendingVersion()   // 升版深链：切到「文档管理」tab 后再消费
    if (p) { activeTab.value = 'docs'; applyPendingVersion(p) }
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

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div v-for="c in empCards" :key="c.key" class="kb-card rounded-xl border border-border bg-card p-4">
        <div class="flex items-center justify-between">
          <span class="text-xs text-muted-foreground">{{ c.label }}</span>
          <component :is="c.icon" :size="15" :stroke-width="1.75" class="text-accent-text" />
        </div>
        <div class="mt-1.5 truncate text-lg font-semibold text-foreground" :class="c.mono ? 'font-mono tabular-nums' : ''">{{ c.value }}</div>
      </div>
    </div>

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
  </div>

  <!-- ───────── 管理员：分 tab 管理台 ───────── -->
  <div v-else class="mx-auto w-full max-w-5xl space-y-6 px-6 py-8">
    <header class="flex items-baseline justify-between border-b border-border pb-4">
      <h1 class="font-serif text-2xl tracking-tight text-foreground">知识库管理</h1>
      <span class="font-mono text-xs text-muted-foreground">{{ identity?.managedOwnerDepts.join(' · ') || '—' }}</span>
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

    <!-- 文档管理：待审批队列（上传放行，kb_admin）+ 授权申请队列（跨部门检索，本部门文档归属者）+ 上传 + 台账 -->
    <template v-else-if="activeTab === 'docs'">
      <ApprovalQueue />
      <AccessRequestQueue />
      <UploadCard />
      <DocTable />
    </template>

    <VersionHistoryModal />
    <AccessRequestModal />
  </div>
</template>
