<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { FileText, CheckCircle2, Loader, Clock, Building2, MessagesSquare, Sparkles } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { consumePendingVersion } from '@/composables/useAuth'
import { useKb } from '@/composables/useKb'
import { useAsk } from '@/composables/useAsk'
import { deptLabel } from '@/lib/kb'
import UploadCard from '@/components/manage/UploadCard.vue'
import ApprovalQueue from '@/components/manage/ApprovalQueue.vue'
import DocTable from '@/components/manage/DocTable.vue'

// 知识库入口：管理员 → 完整管理台；普通员工 → 只读基本概览（只用可访问数据：whoami + hot-questions，
// 不打 admin-gated 接口）。AppShell 仅在 ready 后渲染，故身份已解析。
const { canManage, identity } = storeToRefs(useSession())
const { docs, approvals, countOf, loadDocs, loadApprovals, applyPendingVersion } = useKb()
const { hotQuestions, loadHotQuestions, fillInput } = useAsk()
const router = useRouter()

// 管理员仪表盘（基于已加载文档；my-docs 取前 50，作用域内概览）。
const stats = computed(() => [
  { key: 'total', label: '我的文档', value: docs.value.length, icon: FileText, tone: 'text-foreground' },
  { key: 'live', label: '已上线', value: countOf('已上线'), icon: CheckCircle2, tone: 'text-st-live' },
  { key: 'busy', label: '处理中 / 排队', value: countOf('处理中') + countOf('排队中'), icon: Loader, tone: 'text-st-busy' },
  { key: 'pending', label: '待审批', value: approvals.value.length, icon: Clock, tone: 'text-st-warn' },
])

// 员工概览卡（只读，可访问数据）。
const myDepts = computed(() => (identity.value?.aclGroups || []).map(deptLabel).join('、') || '—')
const empCards = computed(() => [
  { key: 'dept', label: '我的部门', value: myDepts.value, icon: Building2, mono: false },
  { key: 'hot', label: '热门问题', value: String(hotQuestions.value.length), icon: Sparkles, mono: true },
])

function askHot(q: string) { fillInput(q); void router.push('/') }

onMounted(async () => {
  if (canManage.value) {
    await loadDocs()
    void loadApprovals()
    const p = consumePendingVersion()   // 升版深链：文档加载后消费一次
    if (p) applyPendingVersion(p)
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
        <MessagesSquare :size="15" :stroke-width="1.9" /> 去问答
      </button>
    </section>
  </div>

  <!-- ───────── 管理员：完整管理台 ───────── -->
  <div v-else class="mx-auto w-full max-w-5xl space-y-5 px-6 py-8">
    <header class="flex items-baseline justify-between border-b border-border pb-4">
      <h1 class="font-serif text-2xl tracking-tight text-foreground">知识库管理</h1>
      <span class="font-mono text-xs text-muted-foreground">{{ identity?.managedOwnerDepts.join(' · ') || '—' }}</span>
    </header>

    <!-- 仪表盘卡片 -->
    <div class="kb-cards grid grid-cols-2 gap-3 sm:grid-cols-4">
      <div v-for="s in stats" :key="s.key" class="kb-card rounded-xl border border-border bg-card p-4">
        <div class="flex items-center justify-between">
          <span class="text-xs text-muted-foreground">{{ s.label }}</span>
          <component :is="s.icon" :size="15" :stroke-width="1.75" :class="s.tone" />
        </div>
        <div class="mt-1.5 font-mono text-2xl font-semibold tabular-nums" :class="s.tone">{{ s.value }}</div>
      </div>
    </div>

    <ApprovalQueue />
    <UploadCard />
    <DocTable />
  </div>
</template>
