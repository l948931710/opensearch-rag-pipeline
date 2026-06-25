<script setup lang="ts">
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { ShieldAlert } from 'lucide-vue-next'
import { useSession } from '@/stores/session'
import { useKb } from '@/composables/useKb'
import UploadCard from '@/components/manage/UploadCard.vue'
import ApprovalQueue from '@/components/manage/ApprovalQueue.vue'
import DocTable from '@/components/manage/DocTable.vue'

// 视图内权限自检（深链 /manage 的非管理员落「无权限」）；AppShell 仅在 ready 后渲染，故 canManage 已解析。
const { canManage, identity } = storeToRefs(useSession())
const { loadDocs, loadApprovals } = useKb()

onMounted(() => {
  if (!canManage.value) return
  void loadDocs()
  void loadApprovals()
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
      <h1 class="text-xl font-extrabold tracking-tight text-foreground">知识库管理</h1>
      <span class="font-mono text-xs text-muted-foreground">{{ identity?.managedOwnerDepts.join(' · ') || '—' }}</span>
    </header>

    <ApprovalQueue />
    <UploadCard />
    <DocTable />
  </div>
</template>
