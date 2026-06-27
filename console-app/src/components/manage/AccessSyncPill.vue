<!--
  AccessSyncPill — 申请人侧「跨部门授权」同步态徽章。
  独立于文档 StatusPill（lib/kb.ts 明示两套状态机勿合并）：本徽章只表达「授权已批准后，是否真正
  放行检索」——approved_pending_sync=已批准但投影未落地（生效前本部门仍检索不到）；projected=已放行。
-->
<script setup lang="ts">
import { computed } from 'vue'
import { CheckCircle2, RefreshCw } from 'lucide-vue-next'
import type { AccessState } from '@/composables/useKb'

const props = defineProps<{ state: Extract<AccessState, 'approved_pending_sync' | 'projected'> }>()
const projected = computed(() => props.state === 'projected')
</script>

<template>
  <span
    class="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium"
    :class="projected ? 'bg-st-live/10 text-st-live' : 'bg-st-busy/10 text-st-busy'"
    :title="projected ? '已放行：本部门现可检索该文档' : '已批准，正在同步到检索（生效前本部门仍检索不到）'"
  >
    <component :is="projected ? CheckCircle2 : RefreshCw" :size="12" :stroke-width="2" />
    {{ projected ? '已放行' : '同步中' }}
  </span>
</template>
