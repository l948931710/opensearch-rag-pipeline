<script setup lang="ts">
import { computed } from 'vue'
import { Search, ArrowUpDown, FilePlus2, Archive, ArchiveRestore, History, Lock, Clock } from 'lucide-vue-next'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb, type DocItem, type SortKey } from '@/composables/useKb'
import StatusPill from './StatusPill.vue'
import AccessSyncPill from './AccessSyncPill.vue'

const {
  docs, filtered, loadingDocs, loadingMoreDocs, hasMoreDocs, docScope, q, filter, sortKey, sortDir, isDeptAdmin,
  setQuery, sortBy, countOf, setScope, enterVersionMode, retire, restore, openHistory,
  openAccessRequest, accessStateOf, loadMoreDocs,
} = useKb()

// 状态筛选 chip：从已加载文档里取出现过的徽章（+ 全部）。
const chips = computed(() => {
  const present = Array.from(new Set(docs.value.map((d) => d.status_badge).filter(Boolean)))
  return ['', ...present]
})

const COLS: { key: SortKey; label: string }[] = [
  { key: 'title', label: '文档名' },
  { key: 'owner_dept', label: '归属' },
  { key: 'current_version_no', label: '版本' },
  { key: 'status_badge', label: '状态' },
  { key: 'updated_at', label: '更新' },
]

function arrow(k: SortKey) { return sortKey.value === k ? (sortDir.value === 1 ? '↑' : '↓') : '' }

async function onRetire(d: DocItem) {
  if (!confirm(`确认退役《${d.title || d.original_filename || d.doc_id}》？\n将标记下线、停止作为升版目标。从检索彻底移除会在下次维护完成（本操作可逆）。`)) return
  const r = await retire(d)
  if (!r.ok && r.msg) alert('退役失败：' + r.msg)
}

async function onRestore(d: DocItem) {
  if (!confirm(`确认恢复上线《${d.title || d.original_filename || d.doc_id}》？\n将重新激活并标记待重索引；若退役后 HA3 仍在则即时可检索，否则下次维护重索引后恢复。`)) return
  const r = await restore(d)
  if (!r.ok && r.msg) alert('恢复失败：' + r.msg)
}
</script>

<template>
  <section class="rounded-xl border border-border bg-card p-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-3">
        <h2 class="text-[15px] font-semibold text-foreground">
          {{ docScope === 'all' ? '全部门文档' : '我的文档' }}
          <span class="font-mono text-xs text-muted-foreground">{{ docs.length }}</span>
        </h2>
        <!-- 本部门 / 全部门 切换（仅部门管理员；kb_admin 本就全见，无需切换） -->
        <div v-if="isDeptAdmin" class="flex gap-0.5 rounded-lg border border-border bg-panel p-0.5">
          <button
            type="button" :data-active-item="docScope === 'managed' ? '1' : '0'"
            class="rounded-md px-3 py-1 text-xs font-medium text-muted-foreground transition"
            @click="setScope('managed')"
          >本部门</button>
          <button
            type="button" :data-active-item="docScope === 'all' ? '1' : '0'"
            class="rounded-md px-3 py-1 text-xs font-medium text-muted-foreground transition"
            @click="setScope('all')"
          >全部门</button>
        </div>
      </div>
      <div class="relative">
        <Search :size="14" :stroke-width="1.75" class="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <input
          :value="q" type="search" placeholder="搜索文档名…"
          class="w-56 rounded-md border border-input bg-card py-1.5 pl-8 pr-2.5 text-sm text-foreground focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15"
          @input="setQuery(($event.target as HTMLInputElement).value)"
        />
      </div>
    </div>

    <!-- 全部门只读提示 -->
    <div
      v-if="docScope === 'all'"
      class="mt-3 flex items-start gap-2 rounded-lg border border-border bg-panel px-3 py-2 text-xs text-muted-foreground"
    >
      <Lock :size="13" :stroke-width="1.75" class="mt-0.5 shrink-0 text-faint" />
      <span>全部门为只读视图：其他部门文档不可直接管理；如需让本部门可检索，点「申请授权」由文档所属部门管理员审批。（不含受限文档）</span>
    </div>

    <!-- 状态筛选 chips -->
    <div class="mt-3 flex flex-wrap gap-1.5">
      <button
        v-for="c in chips" :key="c || 'all'"
        type="button"
        class="rounded-full border px-2.5 py-1 text-xs transition"
        :class="filter === c ? 'border-accent-soft bg-accent text-accent-foreground' : 'border-border text-muted-foreground hover:bg-panel'"
        @click="filter = c"
      >
        {{ c || '全部' }} <span class="font-mono">{{ countOf(c) }}</span>
      </button>
    </div>

    <!-- Atlas 台账网格（< 680px 自动卡片化，由 .led-* 媒体查询接管） -->
    <div class="mt-4 overflow-hidden rounded-xl border border-border bg-card">
      <div class="led-head">
        <button
          v-for="col in COLS" :key="col.key" type="button"
          class="led-sort inline-flex items-center gap-1"
          :aria-label="`按${col.label}排序`"
          :aria-sort="sortKey === col.key ? (sortDir === 1 ? 'ascending' : 'descending') : 'none'"
          @click="sortBy(col.key)"
        >
          {{ col.label }}<ArrowUpDown :size="11" :stroke-width="1.75" class="opacity-40" /><span class="text-accent-text">{{ arrow(col.key) }}</span>
        </button>
        <span class="text-right">操作</span>
      </div>

      <div
        v-for="d in filtered" :key="d.doc_id"
        class="led-row" :data-retired="d.status_badge === '已退役' ? '1' : '0'" :data-foreign="d.can_manage === false ? '1' : '0'"
      >
        <div class="led-cell led-cell-main min-w-0" data-label="文档名">
          <div class="truncate text-[13.5px] font-semibold text-foreground">{{ d.title || d.original_filename || d.doc_id }}</div>
          <div class="truncate text-[11px] text-faint">
            {{ permLabel(d.permission_level) }}<span v-if="d.original_filename && d.original_filename !== d.title"> · {{ d.original_filename }}</span>
          </div>
        </div>
        <div class="led-cell text-sm text-muted-foreground" data-label="归属">
          {{ deptLabel(d.owner_dept) }}
          <span v-if="d.can_manage === false" class="ml-1.5 whitespace-nowrap rounded border border-border bg-panel px-1.5 py-px text-[10px] font-medium text-faint">其他部门</span>
        </div>
        <div class="led-cell font-mono text-xs text-muted-foreground" data-label="版本">v{{ d.current_version_no || 1 }}</div>
        <div class="led-cell" data-label="状态"><StatusPill :badge="d.status_badge" /></div>
        <div class="led-cell font-mono text-xs text-muted-foreground" data-label="更新">{{ (d.updated_at || '').slice(0, 16) }}</div>
        <div class="led-cell led-actions doc-actions" data-label="操作">
          <!-- 可操作（本部门 / kb_admin）：历史 / 升版 / 退役 -->
          <template v-if="d.can_manage !== false">
            <button type="button" class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-panel hover:text-foreground" @click="openHistory(d)">
              <History :size="13" :stroke-width="1.75" /> 历史
            </button>
            <button type="button" class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-panel hover:text-foreground" @click="enterVersionMode(d)">
              <FilePlus2 :size="13" :stroke-width="1.75" /> 升版
            </button>
            <button
              v-if="d.status_badge !== '已退役'"
              type="button" class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-st-fail/10 hover:text-st-fail"
              @click="onRetire(d)"
            >
              <Archive :size="13" :stroke-width="1.75" /> 退役
            </button>
            <button
              v-else
              type="button" class="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-st-live transition hover:bg-st-live/10"
              @click="onRestore(d)"
            >
              <ArchiveRestore :size="13" :stroke-width="1.75" /> 恢复上线
            </button>
          </template>
          <!-- 其他部门（只读）：申请授权 / 审批中 / 同步中 / 已放行 -->
          <template v-else>
            <AccessSyncPill
              v-if="accessStateOf(d.doc_id) === 'projected' || accessStateOf(d.doc_id) === 'approved_pending_sync'"
              :state="(accessStateOf(d.doc_id) as 'approved_pending_sync' | 'projected')"
            />
            <span
              v-else-if="accessStateOf(d.doc_id) === 'pending'"
              class="flex items-center gap-1 rounded-md bg-st-busy/10 px-2 py-1 text-xs font-medium text-st-busy"
            >
              <Clock :size="12" :stroke-width="2" /> 审批中
            </span>
            <button
              v-else
              type="button"
              class="flex items-center gap-1 rounded-md border border-border px-2 py-1 text-xs font-medium text-accent-text transition hover:border-accent-strong hover:bg-accent-soft"
              @click="openAccessRequest(d)"
            >
              <Lock :size="13" :stroke-width="1.75" /> 申请授权
            </button>
          </template>
        </div>
      </div>

      <div v-if="!filtered.length" class="px-4 py-10 text-center text-sm text-muted-foreground">
        {{ loadingDocs ? '加载中…' : (q ? '无匹配文档' : (docScope === 'all' ? '暂无可浏览的文档' : '暂无文档，先上传一篇吧')) }}
      </div>
    </div>

    <!-- 分页：服务端还有下一页时显「加载更多」（单页 50 条；管理大量文档时尾部不再被静默截断） -->
    <div v-if="hasMoreDocs" class="mt-3 flex items-center justify-center gap-2">
      <button
        type="button" :disabled="loadingMoreDocs"
        class="rounded-lg border border-border px-4 py-1.5 text-xs font-medium text-muted-foreground transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-60"
        @click="loadMoreDocs()"
      >{{ loadingMoreDocs ? '加载中…' : `加载更多（已显示 ${docs.length} 条）` }}</button>
    </div>
  </section>
</template>
