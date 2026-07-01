<script setup lang="ts">
import { computed, ref } from 'vue'
import { History } from 'lucide-vue-next'
import { useKb, type ApprovalHistoryItem } from '@/composables/useKb'
import { deptLabel } from '@/lib/kb'
import LoadError from './LoadError.vue'

// 审批历史（只读时间线）：四条审批流的历史决策合并展示，数据来自 /api/kb/approval-history。
// 后端已按角色作用域（dept_admin 本部门 access+contribution、kb_admin 全库四类）+ PII 脱敏；
// 前端只做类型 chip 的本地过滤，不再请求。时间线轴复用 VersionHistoryModal 视觉。
const { approvalHistory, loadApprovalHistory, loadErrors, isKbAdmin } = useKb()

// 类型 → 中文短标（badge）。
const KIND_LABEL: Record<string, string> = {
  access: '访问授权', contribution: '知识贡献', upload: '上传审批', admin_grant: '成员授权',
}
// 决策动作 → 文案 + 徽标色 + 时间线点色。通过/采纳/授予→绿；驳回→红；撤销→琥珀。
const ACTION: Record<string, { label: string; pill: string; dot: string }> = {
  approved: { label: '通过', pill: 'text-st-live bg-st-live/10', dot: 'bg-st-live' },
  accepted: { label: '采纳', pill: 'text-st-live bg-st-live/10', dot: 'bg-st-live' },
  granted: { label: '授予', pill: 'text-st-live bg-st-live/10', dot: 'bg-st-live' },
  rejected: { label: '驳回', pill: 'text-st-fail bg-st-fail/10', dot: 'bg-st-fail' },
  revoked: { label: '撤销', pill: 'text-st-busy bg-st-busy/10', dot: 'bg-st-busy' },
}
const act = (a: string) => ACTION[a] || { label: a || '—', pill: 'text-st-muted bg-st-muted/10', dot: 'bg-border-strong' }
// 贡献采纳后的入库结果（extra=ingestion_status）→ 次要 pill；仅有意义态才显。
const INGEST: Record<string, { label: string; pill: string }> = {
  searchable: { label: '已入库', pill: 'text-st-live bg-st-live/10' },
  registered: { label: '待索引', pill: 'text-st-busy bg-st-busy/10' },
  registering: { label: '入库中', pill: 'text-st-busy bg-st-busy/10' },
  failed: { label: '入库失败', pill: 'text-st-fail bg-st-fail/10' },
}

// 类型筛选 chip（kb_admin 见四类，dept_admin 只见前两类）。
const chips = computed(() => {
  const base = [{ key: 'all', label: '全部' }, { key: 'access', label: '访问授权' }, { key: 'contribution', label: '知识贡献' }]
  return isKbAdmin.value ? [...base, { key: 'upload', label: '上传审批' }, { key: 'admin_grant', label: '成员授权' }] : base
})
const activeKind = ref('all')
const rows = computed(() =>
  activeKind.value === 'all' ? approvalHistory.value : approvalHistory.value.filter((r) => r.kind === activeKind.value))

// 元信息行：申请人/作者/目标 · 归属部门 · 决策人（缺失项自动省略）。
function subjectLabel(r: ApprovalHistoryItem): string {
  const p = r.kind === 'contribution' ? '作者' : r.kind === 'admin_grant' ? '目标' : '申请人'
  return `${p} ${r.subject}`
}
function metaOf(r: ApprovalHistoryItem): string {
  const parts: string[] = []
  if (r.subject) parts.push(subjectLabel(r))
  if (r.owner_dept) parts.push('归属 ' + deptLabel(r.owner_dept))
  if (r.decided_by_name) parts.push('决策人 ' + r.decided_by_name)
  return parts.join(' · ')
}
</script>

<template>
  <section data-testid="approval-history">
    <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">审批历史</p>
    <LoadError class="mb-3" :message="loadErrors['approvalHistory']" @retry="loadApprovalHistory()" />

    <div class="overflow-hidden rounded-[15px] border border-border bg-card">
      <!-- 头 + 类型筛选 chip -->
      <div class="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-accent-soft px-[18px] py-3">
        <History :size="16" :stroke-width="1.75" class="shrink-0 text-accent-text" />
        <span class="text-sm font-semibold text-foreground">审批历史</span>
        <span class="rounded-full bg-accent-strong px-2 py-px text-[11px] font-bold text-white">{{ approvalHistory.length }}</span>
        <div class="flex-1" />
        <div class="flex flex-wrap gap-1" role="tablist" aria-label="审批类型筛选">
          <button
            v-for="c in chips" :key="c.key" type="button" role="tab"
            :aria-selected="activeKind === c.key"
            class="rounded-full border px-2.5 py-1 text-[12px] font-medium transition"
            :class="activeKind === c.key ? 'border-accent-strong bg-card text-accent-text' : 'border-border text-muted-foreground hover:text-foreground'"
            @click="activeKind = c.key"
          >{{ c.label }}</button>
        </div>
      </div>

      <!-- 空态 -->
      <div v-if="!approvalHistory.length" class="px-[18px] py-10 text-center text-[13px] text-muted-foreground">
        暂无审批历史 —— 通过 / 驳回 / 撤销 / 采纳后，记录会在这里留痕。
      </div>
      <div v-else-if="!rows.length" class="px-[18px] py-10 text-center text-[13px] text-muted-foreground">
        当前筛选无记录。
      </div>

      <!-- 时间线 -->
      <div v-else class="px-[18px] py-4">
        <div
          v-for="(r, i) in rows" :key="i"
          class="flex gap-3.5 pb-4 last:pb-0"
        >
          <!-- 时间线轴（点色随决策动作） -->
          <div class="flex shrink-0 flex-col items-center pt-1">
            <span class="size-2.5 rounded-full" :class="act(r.action).dot" />
            <span v-if="i < rows.length - 1" class="mt-1 w-px flex-1 bg-border" />
          </div>
          <!-- 该条内容 -->
          <div class="min-w-0 flex-1 pb-1">
            <div class="flex flex-wrap items-center gap-2">
              <span class="rounded bg-secondary px-1.5 py-0.5 text-[11px] font-medium text-muted-foreground">{{ KIND_LABEL[r.kind] || r.kind }}</span>
              <span class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium" :class="act(r.action).pill">{{ act(r.action).label }}</span>
              <span class="min-w-0 truncate text-[13.5px] font-semibold text-foreground">{{ r.title }}</span>
              <span v-if="r.kind === 'contribution' && INGEST[r.extra]" class="inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium" :class="INGEST[r.extra].pill">{{ INGEST[r.extra].label }}</span>
              <div class="flex-1" />
              <span class="shrink-0 font-mono text-[11.5px] text-faint">{{ (r.decided_at || '').slice(0, 16) }}</span>
            </div>
            <div v-if="metaOf(r)" class="mt-1 truncate text-[11.5px] text-faint">{{ metaOf(r) }}</div>
            <div v-if="r.detail" class="mt-1 line-clamp-2 text-[12px] text-muted-foreground">“{{ r.detail }}”</div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>
