<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { ShieldAlert, FileText, CheckCircle2, Loader, Clock } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { consumePendingVersion } from '@/composables/useAuth'
import { useKb } from '@/composables/useKb'
import UploadCard from '@/components/manage/UploadCard.vue'
import ApprovalQueue from '@/components/manage/ApprovalQueue.vue'
import DocTable from '@/components/manage/DocTable.vue'

// 视图内权限自检（深链 /manage 的非管理员落「无权限」）；AppShell 仅在 ready 后渲染，故 canManage 已解析。
const { canManage, identity } = storeToRefs(useSession())
const { docs, approvals, countOf, loadDocs, loadApprovals, applyPendingVersion } = useKb()

// 仪表盘卡片（基于已加载文档；my-docs 取前 50，作用域内概览）。
const stats = computed(() => [
  { key: 'total', label: '我的文档', value: docs.value.length, icon: FileText, tone: 'text-foreground' },
  { key: 'live', label: '已上线', value: countOf('已上线'), icon: CheckCircle2, tone: 'text-st-live' },
  { key: 'busy', label: '处理中 / 排队', value: countOf('处理中') + countOf('排队中'), icon: Loader, tone: 'text-st-busy' },
  { key: 'pending', label: '待审批', value: approvals.value.length, icon: Clock, tone: 'text-st-warn' },
])

onMounted(async () => {
  if (!canManage.value) return
  await loadDocs()
  void loadApprovals()
  // 升版深链：文档加载后消费一次（命中行→进升版态；列表外→合成 verCtx，perm 交后端继承）。
  const p = consumePendingVersion()
  if (p) applyPendingVersion(p)
})
</script>

<template>
  <div v-if="!canManage" class="mx-auto flex min-h-full max-w-md flex-col items-center justify-center px-6 text-center">
    <ShieldAlert :size="40" :stroke-width="1.75" class="text-st-busy" />
    <h2 class="mt-4 text-lg font-bold text-foreground">无管理权限</h2>
    <p class="mt-2 text-sm text-muted-foreground">
      知识库管理仅对部门管理员 / 知识库管理员开放。如需上传文档，请联系你的部门管理员。
    </p>
  </div>

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
