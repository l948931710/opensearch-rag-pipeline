<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search, ArrowUpDown, FilePlus2, Archive, ArchiveRestore, History, Lock, Clock, Share2, Check, X, Eye, Download, Settings, Loader2 } from '@lucide/vue'
import { deptLabel, permLabel, PERM_LABEL } from '@/lib/kb'
import { useKb, type DocItem, type SortKey } from '@/composables/useKb'
import StatusPill from './StatusPill.vue'
import AccessSyncPill from './AccessSyncPill.vue'
import LoadError from './LoadError.vue'
import QueuePager from './QueuePager.vue'
import { useDialog } from '@/composables/useDialog'

const { confirm, notice } = useDialog()

const {
  docs, filtered, loadingDocs, docScope, q, filter, permFilter, ownerFilter, citedFilter, sortKey, sortDir, isDeptAdmin, isKbAdmin,
  ledgerBadgeChips, ledgerBadgeCount, ledgerOwnerOptions, ledgerTotal, setBadgeFilter, setPermFilter, setOwnerFilter, setCitedFilter, clearLedgerFilters,
  docsPage, docsTotal, docsPerPage, docsMaxPage, loadDocsPage,
  setQuery, sortBy, setScope, enterVersionMode, retire, restore, openHistory, openDocPreview,
  openAccessRequest, accessStateOf, accessNoteOf, loadDocs, loadErrors,
  openShare, openDocMeta, grantedLabelsByDoc, openVisibility,
  selectableVisible, selectedDocs, selectedCount, allVisibleSelected, isSelected, toggleSelect, toggleSelectAllVisible, clearSelection, bulkBusy, bulkMsg, bulkRetire, bulkSetVisibility,
} = useKb()

// 页码翻页（设计稿 doc-table.html 尾部 pager）：加载中不受理（防连点竞态，useKb 侧 docsSeq 是
// 第二道闸）；超出服务端深分页上界（api.py _KB_MAX_OFFSET 镜像 docsMaxPage）→ 如实提示不发请求。
function onPage(p: number) {
  if (loadingDocs.value) return
  if (p > docsMaxPage) {
    void notice({ message: `最多可翻到第 ${docsMaxPage} 页（服务端深分页上界）。请用搜索或筛选缩小范围。` })
    return
  }
  void loadDocsPage(p)
}

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
// 仅在 chips 真正就绪（>1 项）后生效——首帧只有「全部」时会把 URL 恢复的筛选误清掉。
watch(chips, (c) => { if (c.length > 1 && filter.value && !c.includes(filter.value)) setBadgeFilter('') })

// ── 筛选状态 ←→ URL（P2：刷新/深链不再丢筛选；replace 不产生历史）──
const route = useRoute()
const router = useRouter()
const rootEl = ref<HTMLElement | null>(null)   // 台账区根节点（筛选归位滚动锚点）
onMounted(() => {
  const s = (v: unknown) => (typeof v === 'string' ? v : '')
  const q0 = route?.query || {}   // 单测无 router 环境 → 可选链兜底（与 Sidebar 同约定）
  // 顺序：先 scope（其重置筛选的语义保留），后各筛选；仅应用与当前不同的值（setter 都会触发服务端重载）。
  if (isDeptAdmin.value && s(q0.scope) === 'all' && docScope.value !== 'all') setScope('all')
  if (s(q0.q) && s(q0.q) !== q.value) setQuery(s(q0.q))
  if (s(q0.badge) && s(q0.badge) !== filter.value) setBadgeFilter(s(q0.badge))
  if (s(q0.perm) && s(q0.perm) !== permFilter.value) setPermFilter(s(q0.perm))
  if (s(q0.owner) && s(q0.owner) !== ownerFilter.value) setOwnerFilter(s(q0.owner))
  if (s(q0.cited) && s(q0.cited) !== citedFilter.value) setCitedFilter(s(q0.cited))
})
watch([docScope, q, filter, permFilter, ownerFilter, citedFilter], () => {
  const query: Record<string, unknown> = { ...(route?.query || {}) }
  const put = (k: string, v: string, def = '') => { if (v && v !== def) query[k] = v; else delete query[k] }
  put('scope', docScope.value, 'managed')
  put('q', q.value)
  put('badge', filter.value)
  put('perm', permFilter.value)
  put('owner', ownerFilter.value)
  put('cited', citedFilter.value)
  void router?.replace({ query: query as Record<string, string> })
  // 筛选归位（2026-07-16 生产实测）：筛完结果变短 → 内层滚动容器保留旧 scrollTop 被钳到
  // 新内容底部——用户视口「跳到最下面」，筛选行反而滚出视野。任何筛选/搜索/范围变更后把
  // 台账区滚回容器顶（搜索框/筛选行保持可见；深链恢复时同样落到台账区，符合预期）。
  rootEl.value?.scrollIntoView({ block: 'start' })
})

const COLS: { key: SortKey; label: string }[] = [
  { key: 'title', label: '文档名' },
  { key: 'owner_dept', label: '归属' },
  { key: 'current_version_no', label: '版本' },
  { key: 'status_badge', label: '状态' },
  { key: 'updated_at', label: '更新' },
]

function arrow(k: SortKey) { return sortKey.value === k ? (sortDir.value === 1 ? '↑' : '↓') : '' }

// 退役/恢复的行级在途态：useKb 侧 retireBusy 只做全局互斥不进模板，此前按钮点了没任何
// 反馈（网络慢时用户会疑惑再点）。失败提示走 notice()——原生 alert 样式脱节且不可访问。
const retireRowId = ref('')

// ── 「更多操作」行菜单（2026-07-19 重设计：6 悬停图标收敛为 3 控件）──
// reka-ui 在依赖里但 src/ 尚无 DropdownMenu 用例 → 按本文件 bulkVisMenu 的手写下拉惯例实现，
// 补齐点击外部关闭 + Esc 关闭 + aria-haspopup/expanded。同一时刻至多展开一行的菜单。
const menuDocId = ref('')
function toggleMenu(id: string) { menuDocId.value = menuDocId.value === id ? '' : id }
function closeMenu() { menuDocId.value = '' }
function menuAct(fn: () => void) { closeMenu(); fn() }
function onGlobalPointerDown(e: Event) {
  if (!menuDocId.value) return
  const t = e.target as Element | null
  if (!t?.closest?.('[data-act-menu]')) closeMenu()   // 点在任一菜单锚点/弹层内不关（切换行由 toggle 处理）
}
function onGlobalKeydown(e: KeyboardEvent) { if (e.key === 'Escape') closeMenu() }
onMounted(() => {
  document.addEventListener('pointerdown', onGlobalPointerDown)
  document.addEventListener('keydown', onGlobalKeydown)
})
onUnmounted(() => {
  document.removeEventListener('pointerdown', onGlobalPointerDown)
  document.removeEventListener('keydown', onGlobalKeydown)
})

async function onRetire(d: DocItem) {
  const okGo = await confirm({
    title: '退役文档', confirmText: '退役', danger: true,
    message: `确认退役《${d.title || d.original_filename || d.doc_id}》？\n将标记下线、停止作为升版目标。从检索彻底移除会在下次维护完成（本操作可逆）。`,
  })
  if (!okGo) return
  retireRowId.value = d.doc_id
  const r = await retire(d)
  retireRowId.value = ''
  // 无 msg 的失败 = retireBusy 全局互斥挡下的并发第二单——原先静默丢弃，用户以为点了没生效（P2）。
  if (!r.ok) void notice({ title: '退役失败', message: r.msg || '另一项退役/恢复操作正在进行中，请稍候片刻再试。', danger: !!r.msg })
}

async function onRestore(d: DocItem) {
  const okGo = await confirm({
    title: '恢复上线', confirmText: '恢复上线',
    message: `确认恢复上线《${d.title || d.original_filename || d.doc_id}》？\n将重新激活并标记待重索引；若退役后 HA3 仍在则即时可检索，否则下次维护重索引后恢复。`,
  })
  if (!okGo) return
  retireRowId.value = d.doc_id
  const r = await restore(d)
  retireRowId.value = ''
  if (!r.ok) void notice({ title: '恢复失败', message: r.msg || '另一项退役/恢复操作正在进行中，请稍候片刻再试。', danger: !!r.msg })
}
</script>

<template>
  <section ref="rootEl" class="rounded-xl border border-border bg-card p-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center gap-3">
        <h2 class="text-[15px] font-semibold text-foreground">
          {{ docScope === 'all' ? '全部门文档' : '我的文档' }}
          <!-- faceted 真实总数（跟随全部筛选；此前显示已加载页行数——全库场景恒显分页上限 50，误导） -->
          <span class="font-mono text-xs text-muted-foreground">{{ ledgerTotal ?? docs.length }}</span>
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

    <!-- Atlas 台账网格（< 680px 自动卡片化，由 .led-* 媒体查询接管）。
         不再 overflow-hidden：行内「更多操作」菜单弹层需外溢容器（末行圆角由 .led-row:last-child 兜底）。 -->
    <div class="mt-4 rounded-xl border border-border bg-card">
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
            <!-- 驳回原因直出（反馈闭环）：原因一直落库却只有 kb_admin 的审批历史能看到，申请人只见红徽章 -->
            <div v-if="d.reject_reason" class="mt-0.5 truncate text-[11px] text-st-fail" :title="`驳回原因：${d.reject_reason}`">
              驳回原因：{{ d.reject_reason }}
            </div>
          </div>
        </div>
        <div class="led-cell text-sm text-muted-foreground" data-label="归属">
          {{ deptLabel(d.owner_dept) }}
          <span v-if="d.can_manage === false" class="ml-1.5 whitespace-nowrap rounded border border-border bg-panel px-1.5 py-px text-[10px] font-medium text-faint">其他部门</span>
        </div>
        <div class="led-cell font-mono text-xs text-muted-foreground" data-label="版本">v{{ d.current_version_no || 1 }}</div>
        <!-- 异常态给"下一步"提示：恢复路径其实存在（升版重灌 / 版本历史看原因），但行上原本无任何指引 -->
        <div
          class="led-cell" data-label="状态"
          :title="d.status_badge === '未入索引' || d.status_badge === '处理失败' ? '上传新版本（升版）可重灌；失败原因见「版本历史」' : undefined"
        ><StatusPill :badge="d.status_badge" /></div>
        <div class="led-cell font-mono text-xs text-muted-foreground" data-label="更新">{{ (d.updated_at || '').slice(0, 16) }}</div>
        <div class="led-cell led-actions doc-actions" data-label="操作" :data-open="menuDocId === d.doc_id ? '1' : '0'">
          <!-- 可操作（本部门 / kb_admin）收敛为 3 控件（2026-07-19 重设计定案，操作列 200px→110px）：
               版本历史 / 下载原始文件 / 「更多操作」菜单（可见范围 · 共享权限 · 升版 · 退役/恢复）。 -->
          <template v-if="d.can_manage !== false">
            <button
              type="button" aria-label="版本历史"
              class="grid size-7 place-items-center rounded-md text-muted-foreground transition hover:bg-panel hover:text-foreground"
              title="版本历史" @click="openHistory(d)"
            ><History :size="14" :stroke-width="1.75" /></button>
            <button
              type="button" data-testid="doc-preview" aria-label="下载原始文件"
              class="grid size-7 place-items-center rounded-md text-muted-foreground transition hover:bg-panel hover:text-foreground"
              title="下载原始文件" @click="openDocPreview(d.doc_id)"
            ><Download :size="14" :stroke-width="1.75" /></button>
            <div class="relative" data-act-menu>
              <button
                type="button" data-testid="doc-more" aria-label="更多操作" aria-haspopup="menu"
                :aria-expanded="menuDocId === d.doc_id"
                class="grid size-7 place-items-center rounded-md text-muted-foreground transition hover:bg-panel hover:text-foreground"
                title="更多操作" @click="toggleMenu(d.doc_id)"
              ><Loader2 v-if="retireRowId === d.doc_id" :size="14" :stroke-width="1.75" class="animate-spin" /><Settings v-else :size="14" :stroke-width="1.75" /></button>
              <!-- 弹层（设计稿 .menu-pop/.mi）：≤680px 卡片态锚点靠左 → 改左对齐防溢出屏幕左缘 -->
              <div
                v-if="menuDocId === d.doc_id" role="menu"
                class="absolute right-0 top-full z-20 mt-1 flex min-w-[148px] flex-col rounded-[10px] border border-border bg-card p-1 shadow-lg max-[680px]:left-0 max-[680px]:right-auto"
              >
                <button
                  type="button" role="menuitem" data-testid="doc-visibility"
                  class="flex items-center gap-2 whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-[12.5px] text-foreground transition hover:bg-panel"
                  @click="menuAct(() => openVisibility(d))"
                ><Eye :size="14" :stroke-width="1.75" class="text-muted-foreground" /> 可见范围</button>
                <!-- 已退役/已隔离不给共享·权限入口（canManagePerm，权限语义保持不变） -->
                <button
                  v-if="canManagePerm(d)"
                  type="button" role="menuitem" data-testid="doc-share"
                  class="flex items-center gap-2 whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-[12.5px] text-foreground transition hover:bg-panel"
                  @click="menuAct(() => openShare(d))"
                ><Share2 :size="14" :stroke-width="1.75" class="text-muted-foreground" /> 跨部门共享 / 权限</button>
                <button
                  type="button" role="menuitem" data-testid="doc-meta"
                  class="flex items-center gap-2 whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-[12.5px] text-foreground transition hover:bg-panel"
                  @click="menuAct(() => openDocMeta(d))"
                ><Settings :size="14" :stroke-width="1.75" class="text-muted-foreground" /> 编辑信息</button>
                <button
                  type="button" role="menuitem"
                  class="flex items-center gap-2 whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-[12.5px] text-foreground transition hover:bg-panel"
                  @click="menuAct(() => enterVersionMode(d))"
                ><FilePlus2 :size="14" :stroke-width="1.75" class="text-muted-foreground" /> 上传新版本</button>
                <div class="mx-1.5 my-1 h-px bg-border" role="separator" />
                <button
                  v-if="d.status_badge !== '已退役'"
                  type="button" role="menuitem" :disabled="retireRowId === d.doc_id"
                  class="flex items-center gap-2 whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-[12.5px] text-st-fail transition hover:bg-st-fail/10 disabled:opacity-50"
                  @click="menuAct(() => onRetire(d))"
                ><Archive :size="14" :stroke-width="1.75" /> 退役下线</button>
                <button
                  v-else
                  type="button" role="menuitem" :disabled="retireRowId === d.doc_id"
                  class="flex items-center gap-2 whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-[12.5px] text-st-live transition hover:bg-st-live/10 disabled:opacity-50"
                  @click="menuAct(() => onRestore(d))"
                ><ArchiveRestore :size="14" :stroke-width="1.75" /> 恢复上线</button>
              </div>
            </div>
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
            <!-- 已驳回（反馈闭环）：原先折叠回「申请授权」，被驳回这件事和原因都无从得知；悬停出原因，点击重申 -->
            <button
              v-else-if="accessStateOf(d.doc_id) === 'rejected'"
              type="button" data-testid="access-rejected"
              class="flex items-center gap-1 rounded-md border border-st-fail/30 bg-st-fail/5 px-2 py-1 text-xs font-medium text-st-fail transition hover:border-st-fail/50"
              :title="accessNoteOf(d.doc_id) ? `驳回原因：${accessNoteOf(d.doc_id)}；可重新申请` : '申请被驳回，可重新申请'"
              @click="openAccessRequest(d)"
            >
              <X :size="12" :stroke-width="2" /> 已驳回 · 重新申请
            </button>
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

      <!-- 尾部页码翻页器（设计稿 doc-table.html .pager）：「第 x–y 条 · 共 N 条」+ 页码。
           单页自隐（QueuePager 内建）；加载中禁点（onPage 守卫 + 视觉降透明）；翻页替换整页数据。 -->
      <QueuePager
        :total="docsTotal" :page="docsPage" :per-page="docsPerPage"
        :class="loadingDocs ? 'pointer-events-none opacity-60' : ''"
        @update:page="onPage"
      />
    </div>
  </section>
</template>
