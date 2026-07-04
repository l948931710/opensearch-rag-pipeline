<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Search, ArrowUpDown, FilePlus2, Archive, ArchiveRestore, History, Lock, Clock, Share2, Check, X, Eye, ExternalLink } from 'lucide-vue-next'
import { deptLabel, permLabel, PERM_LABEL } from '@/lib/kb'
import { useKb, type DocItem, type SortKey } from '@/composables/useKb'
import StatusPill from './StatusPill.vue'
import AccessSyncPill from './AccessSyncPill.vue'
import LoadError from './LoadError.vue'
import { useDialog } from '@/composables/useDialog'

const { confirm } = useDialog()

const {
  docs, filtered, loadingDocs, loadingMoreDocs, hasMoreDocs, docScope, q, filter, permFilter, ownerFilter, citedFilter, sortKey, sortDir, isDeptAdmin, isKbAdmin,
  ledgerBadgeChips, ledgerBadgeCount, ledgerOwnerOptions, setBadgeFilter, setPermFilter, setOwnerFilter, setCitedFilter, clearLedgerFilters,
  setQuery, sortBy, setScope, enterVersionMode, retire, restore, openHistory, openDocPreview,
  openAccessRequest, accessStateOf, loadMoreDocs, loadDocs, loadErrors,
  openShare, grantedLabelsByDoc, openVisibility,
  selectableVisible, selectedDocs, selectedCount, allVisibleSelected, isSelected, toggleSelect, toggleSelectAllVisible, clearSelection, bulkBusy, bulkMsg, bulkRetire, bulkSetVisibility,
} = useKb()

// 「权限」入口条件：可管理 + 非退役 + 非隔离。弹窗内含 基础可见范围（改级别）+ 跨部门共享。
// 放宽到全部可见级别（不再只 dept_internal）：public 文档也要能被改回本部门/受限。
// 已隔离 = 安全隔离（PII 等）：后端对可见范围/恢复一律 409（唯一出路是脱敏重灌），入口直接不给。
function canManagePerm(d: DocItem): boolean {
  return d.can_manage !== false && d.status_badge !== '已退役' && d.status_badge !== '已隔离'
}

// 共享部门名（去 count，直接列名字，>2 个折 +N）——回答"文档共享给了哪些部门"。
// O(1)：useKb 侧 computed Map（grants 变更才重算），模板每行 4 次调用不再各自全量扫。
function sharedLabels(docId: string): string[] { return grantedLabelsByDoc.value.get(docId) || [] }

// 利用度副行文案：0=真·从未被引用（退役候选，amber 提示）；>0=引用 N 次；null/undefined=数据不可用不显示。
function usageText(d: DocItem): string {
  if (d.cited_count === 0) return '从未被引用'
  if (d.cited_count && d.cited_count > 0) return `引用 ${d.cited_count} 次`
  return ''
}

// 可见范围筛选选项（下拉）
const PERM_OPTS = Object.keys(PERM_LABEL)   // dept_internal / public / restricted

// 批量：可选目标可见范围（public 涉全公司需 kb_admin，dept_admin 只给 本部门/受限）
const bulkVisMenu = ref(false)
const bulkVisOpts = computed(() =>
  (isKbAdmin.value ? ['dept_internal', 'public', 'restricted'] : ['dept_internal', 'restricted']))

async function onBulkRetire() {
  const n = selectedDocs.value.filter((d) => d.status_badge !== '已退役').length
  if (!n) return
  const okGo = await confirm({
    title: '批量退役', confirmText: `退役 ${n} 篇`, danger: true,
    message: `确认退役选中的 ${n} 篇文档？\n将逐篇标记下线、停止作为升版目标（可逆，从检索移除在下次维护完成）。`,
  })
  if (okGo) void bulkRetire()
}
async function onBulkSetVis(level: string) {
  bulkVisMenu.value = false
  const n = selectedDocs.value.filter((d) => d.permission_level !== level).length
  if (!n) return
  const okGo = await confirm({
    title: '批量改可见范围', confirmText: `改 ${n} 篇`,
    danger: level === 'restricted',
    message: `把选中的 ${n} 篇文档改为「${PERM_LABEL[level]}」？${level === 'restricted' ? '\n受限 = 下线归档、离开检索（可再改回）。' : ''}`,
  })
  if (okGo) void bulkSetVisibility(level)
}

// 状态筛选 chip + 计数：全库口径来自 /api/kb/stats（本部门台账不受 50 页上限影响）；
// 「全部门」浏览无对应全库聚合 → useKb 内部自动回退已加载页派生（ledgerBadgeChips/ledgerBadgeCount）。
const chips = ledgerBadgeChips

// 自愈：当前筛选的徽章不在可选集里（如换 scope 后徽章分布变化）→ 回退「全部」并重载，避免死角。
watch(chips, (c) => { if (filter.value && !c.includes(filter.value)) setBadgeFilter('') })

const COLS: { key: SortKey; label: string }[] = [
  { key: 'title', label: '文档名' },
  { key: 'owner_dept', label: '归属' },
  { key: 'current_version_no', label: '版本' },
  { key: 'status_badge', label: '状态' },
  { key: 'updated_at', label: '更新' },
]

function arrow(k: SortKey) { return sortKey.value === k ? (sortDir.value === 1 ? '↑' : '↓') : '' }

async function onRetire(d: DocItem) {
  const okGo = await confirm({
    title: '退役文档', confirmText: '退役', danger: true,
    message: `确认退役《${d.title || d.original_filename || d.doc_id}》？\n将标记下线、停止作为升版目标。从检索彻底移除会在下次维护完成（本操作可逆）。`,
  })
  if (!okGo) return
  const r = await retire(d)
  if (!r.ok && r.msg) alert('退役失败：' + r.msg)
}

async function onRestore(d: DocItem) {
  const okGo = await confirm({
    title: '恢复上线', confirmText: '恢复上线',
    message: `确认恢复上线《${d.title || d.original_filename || d.doc_id}》？\n将重新激活并标记待重索引；若退役后 HA3 仍在则即时可检索，否则下次维护重索引后恢复。`,
  })
  if (!okGo) return
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

    <LoadError class="mt-3" :message="loadErrors['docs']" @retry="loadDocs()" />

    <!-- 状态筛选 chips + 归属/可见范围下拉 -->
    <div class="mt-3 flex flex-wrap items-center justify-between gap-2">
      <div class="flex flex-wrap gap-1.5">
        <button
          v-for="c in chips" :key="c || 'all'"
          type="button"
          class="rounded-full border px-2.5 py-1 text-xs transition"
          :class="filter === c ? 'border-accent-soft bg-accent text-accent-foreground' : 'border-border text-muted-foreground hover:bg-panel'"
          @click="setBadgeFilter(c)"
        >
          {{ c || '全部' }} <span class="font-mono">{{ ledgerBadgeCount(c) }}</span>
        </button>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <select
          :value="ownerFilter" aria-label="按归属部门筛选"
          class="ui-select rounded-md border border-input bg-card py-1.5 pl-2.5 pr-7 text-xs text-foreground focus:border-ring focus:outline-none"
          @change="setOwnerFilter(($event.target as HTMLSelectElement).value)"
        >
          <option value="">全部归属</option>
          <option v-for="o in ledgerOwnerOptions" :key="o" :value="o">{{ deptLabel(o) }}</option>
        </select>
        <select
          :value="permFilter" aria-label="按可见范围筛选"
          class="ui-select rounded-md border border-input bg-card py-1.5 pl-2.5 pr-7 text-xs text-foreground focus:border-ring focus:outline-none"
          @change="setPermFilter(($event.target as HTMLSelectElement).value)"
        >
          <option value="">全部范围</option>
          <option v-for="p in PERM_OPTS" :key="p" :value="p">{{ PERM_LABEL[p] }}</option>
        </select>
        <select
          :value="citedFilter" aria-label="按利用度筛选"
          class="ui-select rounded-md border border-input bg-card py-1.5 pl-2.5 pr-7 text-xs text-foreground focus:border-ring focus:outline-none"
          @change="setCitedFilter(($event.target as HTMLSelectElement).value)"
        >
          <option value="">全部利用度</option>
          <option value="never">从未被引用</option>
          <option value="used">有引用</option>
        </select>
        <button
          v-if="ownerFilter || permFilter || filter || citedFilter"
          type="button" class="rounded-md px-2 py-1 text-xs text-muted-foreground transition hover:bg-panel hover:text-foreground"
          @click="clearLedgerFilters()"
        >清除筛选</button>
      </div>
    </div>

    <!-- 批量操作条（选中任意行时浮现；仅作用于可见可管理行） -->
    <div
      v-if="selectedCount"
      class="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-accent-strong/40 bg-accent-soft px-3 py-2"
    >
      <span class="text-[12.5px] font-semibold text-accent-text">已选 {{ selectedCount }} 篇</span>
      <span v-if="bulkMsg" class="text-[11.5px] text-muted-foreground">· {{ bulkMsg }}</span>
      <div class="flex-1" />
      <button
        type="button" :disabled="bulkBusy"
        class="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1 text-xs font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
        @click="onBulkRetire"
      ><Archive :size="12" :stroke-width="1.75" /> 批量退役</button>
      <div class="relative">
        <button
          type="button" :disabled="bulkBusy"
          class="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1 text-xs font-medium text-foreground transition hover:border-border-strong disabled:opacity-50"
          @click="bulkVisMenu = !bulkVisMenu"
        ><Lock :size="12" :stroke-width="1.75" /> 改可见范围</button>
        <div
          v-if="bulkVisMenu"
          class="absolute right-0 top-full z-10 mt-1 w-32 overflow-hidden rounded-lg border border-border bg-card py-1 shadow-lg"
        >
          <button
            v-for="lv in bulkVisOpts" :key="lv" type="button"
            class="block w-full px-3 py-1.5 text-left text-xs text-foreground transition hover:bg-panel"
            @click="onBulkSetVis(lv)"
          >{{ PERM_LABEL[lv] }}</button>
        </div>
      </div>
      <button
        type="button" :disabled="bulkBusy"
        class="grid size-6 place-items-center rounded text-muted-foreground transition hover:bg-card hover:text-foreground disabled:opacity-50"
        aria-label="取消选择" @click="clearSelection"
      ><X :size="13" :stroke-width="2" /></button>
    </div>

    <!-- Atlas 台账网格（< 680px 自动卡片化，由 .led-* 媒体查询接管） -->
    <div class="mt-4 overflow-hidden rounded-xl border border-border bg-card">
      <div class="led-head">
        <span class="inline-flex items-center gap-2">
          <button
            type="button" role="checkbox" :aria-checked="allVisibleSelected"
            :disabled="!selectableVisible.length"
            class="grid size-4 shrink-0 place-items-center rounded border transition disabled:opacity-30"
            :class="allVisibleSelected ? 'border-accent-strong bg-accent-strong text-primary-foreground' : 'border-border-strong bg-surface hover:border-ring'"
            aria-label="全选可见文档" title="全选可见文档" @click="toggleSelectAllVisible"
          ><Check v-if="allVisibleSelected" :size="11" :stroke-width="3" /></button>
          <button
            type="button" class="led-sort inline-flex items-center gap-1"
            :aria-label="`按${COLS[0].label}排序`"
            :aria-sort="sortKey === COLS[0].key ? (sortDir === 1 ? 'ascending' : 'descending') : 'none'"
            @click="sortBy(COLS[0].key)"
          >{{ COLS[0].label }}<ArrowUpDown :size="11" :stroke-width="1.75" class="opacity-40" /><span class="text-accent-text">{{ arrow(COLS[0].key) }}</span></button>
        </span>
        <button
          v-for="col in COLS.slice(1)" :key="col.key" type="button"
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
        :data-selected="isSelected(d.doc_id) ? '1' : '0'"
      >
        <div class="led-cell led-cell-main flex min-w-0 items-start gap-2.5" data-label="文档名">
          <!-- 行选择框（仅可管理行；外部门只读行不给选框） -->
          <button
            v-if="d.can_manage !== false"
            type="button" role="checkbox" :aria-checked="isSelected(d.doc_id)"
            class="mt-0.5 grid size-4 shrink-0 place-items-center rounded border transition"
            :class="isSelected(d.doc_id) ? 'border-accent-strong bg-accent-strong text-primary-foreground' : 'border-border-strong bg-surface hover:border-ring'"
            :aria-label="`选择：${d.title || d.doc_id}`" @click="toggleSelect(d.doc_id)"
          ><Check v-if="isSelected(d.doc_id)" :size="11" :stroke-width="3" /></button>
          <span v-else class="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <div class="min-w-0 flex-1">
            <div class="truncate text-[13.5px] font-semibold text-foreground" :title="d.title || d.original_filename || d.doc_id">{{ d.title || d.original_filename || d.doc_id }}</div>
            <div class="truncate text-[11px] text-faint">
              {{ permLabel(d.permission_level) }}<template v-if="sharedLabels(d.doc_id).length"><span class="text-accent-text"> · 共享 {{ sharedLabels(d.doc_id).slice(0, 2).join('、') }}<span v-if="sharedLabels(d.doc_id).length > 2"> +{{ sharedLabels(d.doc_id).length - 2 }}</span></span></template><template v-if="usageText(d)"> · <span :class="d.cited_count === 0 ? 'text-st-warn' : ''" :title="d.last_cited_at ? `最近被引用 ${d.last_cited_at.slice(0, 16)}` : ''">{{ usageText(d) }}</span></template><span v-if="d.original_filename && d.original_filename !== d.title"> · {{ d.original_filename }}</span>
            </div>
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
          <!-- 可操作（本部门 / kb_admin）：可见 / 权限 / 预览 / 历史 / 升版 / 退役。
               6 个操作 → 图标按钮 + tooltip（原 icon+文字 6 个会溢出 200px 操作列、压到「更新」列上）。 -->
          <template v-if="d.can_manage !== false">
            <button
              type="button" data-testid="doc-visibility" aria-label="谁能看到这篇文档"
              class="grid size-7 place-items-center rounded-md text-muted-foreground transition hover:bg-panel hover:text-foreground"
              title="谁能看到这篇文档" @click="openVisibility(d)"
            ><Eye :size="14" :stroke-width="1.75" /></button>
            <button
              v-if="canManagePerm(d)"
              type="button" data-testid="doc-share" aria-label="跨部门共享 / 权限"
              class="grid size-7 place-items-center rounded-md text-muted-foreground transition hover:bg-accent-soft hover:text-accent-text"
              title="跨部门共享 / 权限" @click="openShare(d)"
            ><Share2 :size="14" :stroke-width="1.75" /></button>
            <button
              type="button" data-testid="doc-preview" aria-label="预览原始上传文件"
              class="grid size-7 place-items-center rounded-md text-muted-foreground transition hover:bg-panel hover:text-foreground"
              title="预览原始上传文件" @click="openDocPreview(d.doc_id)"
            ><ExternalLink :size="14" :stroke-width="1.75" /></button>
            <button
              type="button" aria-label="版本历史"
              class="grid size-7 place-items-center rounded-md text-muted-foreground transition hover:bg-panel hover:text-foreground"
              title="版本历史" @click="openHistory(d)"
            ><History :size="14" :stroke-width="1.75" /></button>
            <button
              type="button" aria-label="上传新版本"
              class="grid size-7 place-items-center rounded-md text-muted-foreground transition hover:bg-panel hover:text-foreground"
              title="上传新版本（升版）" @click="enterVersionMode(d)"
            ><FilePlus2 :size="14" :stroke-width="1.75" /></button>
            <button
              v-if="d.status_badge !== '已退役'"
              type="button" aria-label="退役下线"
              class="grid size-7 place-items-center rounded-md text-muted-foreground transition hover:bg-st-fail/10 hover:text-st-fail"
              title="退役下线" @click="onRetire(d)"
            ><Archive :size="14" :stroke-width="1.75" /></button>
            <button
              v-else
              type="button" aria-label="恢复上线"
              class="grid size-7 place-items-center rounded-md text-st-live transition hover:bg-st-live/10"
              title="恢复上线" @click="onRestore(d)"
            ><ArchiveRestore :size="14" :stroke-width="1.75" /></button>
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
