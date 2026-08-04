<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { X, Loader2, Lock } from '@lucide/vue'
import { deptLabel } from '@/lib/kb'
import { apiJson } from '@/lib/api'
import { useKb, type AccessGrantItem, type DocItem } from '@/composables/useKb'
import { useDialog } from '@/composables/useDialog'
import OrgTreePicker, { type PickedNode } from './OrgTreePicker.vue'

// 文档权限弹窗（owner 侧）：
//   上 = 基础可见范围（仅本部门 / 全公司 / 受限，POST set-visibility；公开需 kb_admin）；
//   下 = 跨部门共享（仅 dept_internal 时显示）：已共享逐行可撤销 + 新增目标 chips。
// ⚠️ `isBusy` 是**函数** `isBusy(key)`（useKb.ts:242，按行/用户加锁），**必须带 key 调用**
// （见下方撤销按钮 `isBusy(\`grant:${g.id}\`)`）。
// 2026-07-31 实测踩过：「保存可见范围」曾写成 `:disabled="shareBusy || isBusy"` —— shareBusy 为
// false 时表达式求值成那个**函数对象**（truthy）⇒ 按钮**永久禁用**，管理员根本存不了节点可见
// 范围。vue-tsc 当场就报 TS2322，但被当噪声放过。`saveNodeGrants` 只用 shareBusy、不往 inflight
// 注册，故那里根本不该出现 isBusy。
const { shareCtx, shareBusy, shareTargets, isBusy, isKbAdmin, grantedDeptsOf, docGrantRows, submitShare, saveNodeGrants, revokeAccess, setVisibility, closeShare } = useKb()
const { confirm, promptText, dialog } = useDialog()

const picked = ref<string[]>([])
const reason = ref('')
const err = ref('')
// node-ACL：按组织树勾选的可见范围（整体替换语义 —— 取消勾选即撤销）
const nodePicked = ref<PickedNode[]>([])
const nodeErr = ref('')
// 阶段 B：开弹窗先拉 GET /api/kb/doc-meta 预填——此前每次打开都清空 nodePicked 再整体
// 替换保存 = **静默抹掉存量授权**（codex 评审点名的隐患）。预填失败禁保存（宁可存不了，
// 不可空集覆盖）；CAS revision 也从这里来（后端已必填）。
const docMeta = ref<null | {
  acl_mode: string; acl_revision: number; owner_label: string
  node_grants: { dept_id: number; scope: string; name: string; active: boolean }[]
}>(null)
const metaLoading = ref(false)
const metaErr = ref('')
async function saveNodes() {
  const d = shareCtx.value
  if (!d || !docMeta.value) return
  nodeErr.value = ''
  const e = await saveNodeGrants(d.doc_id, nodePicked.value, docMeta.value.acl_revision,
                                 reason.value.trim())
  if (e) nodeErr.value = e
}
const visBusy = ref('')           // 正在切换的目标级别（禁用按钮 + 转圈）

watch(shareCtx, async (d) => {
  picked.value = []; reason.value = ''; err.value = ''; visBusy.value = ''
  nodePicked.value = []; nodeErr.value = ''; docMeta.value = null; metaErr.value = ''
  if (!d) return
  metaLoading.value = true
  try {
    const m = await apiJson<any>(
      `/api/kb/doc-meta?doc_id=${encodeURIComponent(d.doc_id)}`, { auth: true })
    docMeta.value = m
    nodePicked.value = ((m?.node_grants || []) as any[]).map(
      (g) => ({ dept_id: g.dept_id, subtree: (g.scope || 'subtree') === 'subtree' }))
  } catch (e: any) {
    metaErr.value = e?.detail || '加载当前可见范围失败——为防误覆盖，已禁用保存'
  } finally { metaLoading.value = false }
})

const curLevel = computed(() => shareCtx.value?.permission_level || '')
const isDeptInternal = computed(() => curLevel.value === 'dept_internal')
// 基础可见范围三档；public 涉及全公司 → dept_admin 不可选（后端也 403，这里做前置禁用 + 提示）。
const LEVELS: { key: string; label: string; hint: string; kbAdminOnly?: boolean }[] = [
  { key: 'dept_internal', label: '仅本部门', hint: '本部门 + 已共享部门可检索' },
  { key: 'public', label: '全公司', hint: '全员可检索', kbAdminOnly: true },
  { key: 'restricted', label: '受限', hint: '仅归档，不进检索' },
]
function levelDisabled(l: { key: string; kbAdminOnly?: boolean }): boolean {
  if (visBusy.value) return true
  // public 相关（目标=public 或当前=public 的任何变更）需 kb_admin
  if (!isKbAdmin.value && (l.key === 'public' || curLevel.value === 'public')) return true
  return false
}

const granted = computed(() => (shareCtx.value ? grantedDeptsOf(shareCtx.value.doc_id) : []))
const rows = computed<AccessGrantItem[]>(() => (shareCtx.value ? docGrantRows(shareCtx.value.doc_id) : []))
// 可选目标 = 组码 − 归属自身 − 已放行。伞组/共享面冗余判定交后端闭集 taxonomy（不再前端
// startswith 预判：闭集外子线如 production_papercup 检索 fail-closed，"共享给 production"
// 恰是唯一放行通道，前缀预判会把救济入口藏掉；真冗余由后端 skipped 返回并如实提示）。
const options = computed(() => {
  const d = shareCtx.value
  if (!d) return []
  const covered = new Set(granted.value)
  return shareTargets.filter((t) => t !== d.owner_dept && !covered.has(t))
})

function toggle(t: string) {
  picked.value = picked.value.includes(t) ? picked.value.filter((x) => x !== t) : [...picked.value, t]
}
const rowLabel = (csv: string) => csv.split(',').map((c) => deptLabel(c.trim())).filter(Boolean).join('、')

// 改级别先过确认（与台账批量路径同词表：受限 = danger）。此前单击即生效——
// 误触「受限」文档立刻离开检索、误触「全公司」立刻全员可见，是全页唯一裸奔的高危写操作。
const LEVEL_CONFIRM_MSG: Record<string, (title: string) => string> = {
  restricted: (t) => `把《${t}》改为「受限」？\n受限 = 下线归档、离开检索：所有部门（含本部门）将搜不到本文档（可再改回恢复）。`,
  public: (t) => `把《${t}》改为「全公司」？\n全员（含仅有公开权限的账号）都将能检索到本文档。`,
  dept_internal: (t) => `把《${t}》改为「仅本部门」？\n此后仅归属部门与已共享部门可检索。`,
}
async function onPickLevel(key: string) {
  const d = shareCtx.value
  if (!d || key === curLevel.value || visBusy.value) return
  const title = d.title || d.original_filename || d.doc_id
  const okGo = await confirm({
    title: '改可见范围',
    confirmText: `改为「${LEVELS.find((l) => l.key === key)?.label || key}」`,
    danger: key === 'restricted',
    message: (LEVEL_CONFIRM_MSG[key] || ((t: string) => `把《${t}》改为「${key}」？`))(title),
  })
  if (!okGo) return
  err.value = ''; visBusy.value = key
  // R1：本弹窗开启即拉 doc-meta，手上有权威 acl_revision ⇒ 送上做 CAS（零额外请求）。
  const e = await setVisibility(d as DocItem, key, '权限弹窗调整',
                                { expectedAclRevision: docMeta.value?.acl_revision ?? null })
  visBusy.value = ''
  if (e) { err.value = e; return }
  // 成功：受限 = 已离开检索，直接关闭（列表会刷新）；其余留在弹窗以便继续调共享。
  if (key === 'restricted') closeShare()
}

async function onSubmit() {
  err.value = ''
  const e = await submitShare(picked.value, reason.value.trim())
  if (e) err.value = e
}

// 与「已授权」清单同一撤销确认模式（P2 统一）：单个 danger 原因弹窗,替代原行内二段确认。
async function onRevoke(g: AccessGrantItem) {
  const r = await promptText({
    title: '撤销授权', confirmText: '确认撤销', danger: true,
    message: `撤销「${rowLabel(g.requester_dept)}」对《${shareCtx.value?.title || g.doc_title || g.doc_id}》的检索授权？\n撤销后该部门将不再能检索此文档（即时生效），申请人可重新申请。`,
    placeholder: '撤销原因（可空，记录于审计）',
  })
  if (r === null) return   // 取消
  await revokeAccess(g, r || 'revoked')
}

function close() { if (!shareBusy.value && !visBusy.value) { err.value = ''; closeShare() } }

// Esc 关闭（对齐 ConfirmDialog/VersionHistoryModal）。全局确认框叠在上层时不响应——
// stopPropagation 拦不住同一 window target 上的其他监听器，必须显式让位，否则一次 Esc 连关两层。
function onKey(e: KeyboardEvent) { if (e.key === 'Escape' && shareCtx.value && !dialog.value.open) close() }
watch(shareCtx, (v) => {
  if (v) window.addEventListener('keydown', onKey)
  else window.removeEventListener('keydown', onKey)
})
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div
    v-if="shareCtx"
    class="kb-scrim fixed inset-0 z-[80] flex items-center justify-center bg-black/40 p-6"
    @click="close"
  >
    <div class="kb-pop w-[500px] max-w-full overflow-hidden rounded-2xl border border-border bg-card shadow-xl" @click.stop>
      <!-- 头 -->
      <div class="flex items-start gap-3 border-b border-border px-[22px] py-4">
        <span class="grid size-9 shrink-0 place-items-center rounded-[10px] bg-accent-soft text-accent-text"><Lock :size="17" :stroke-width="1.75" /></span>
        <div class="min-w-0 flex-1">
          <div class="text-base font-semibold text-foreground">文档权限</div>
          <div class="mt-0.5 truncate text-[12.5px] text-muted-foreground">
            《{{ shareCtx.title || shareCtx.original_filename || shareCtx.doc_id }}》 · 归属 {{ deptLabel(shareCtx.owner_dept) }}
          </div>
        </div>
        <button
          type="button" aria-label="关闭"
          class="grid size-[30px] shrink-0 place-items-center rounded-lg text-faint transition hover:bg-panel hover:text-foreground"
          @click="close"
        ><X :size="16" :stroke-width="2" /></button>
      </div>

      <div class="max-h-[60vh] overflow-y-auto px-[22px] py-[18px]">
        <!-- 基础可见范围（三档单选；public 相关需 kb_admin，dept_admin 禁用 + 提示） -->
        <p class="mb-2 text-[11px] font-bold uppercase tracking-[0.04em] text-faint">基础可见范围</p>
        <div class="mb-1.5 grid grid-cols-3 gap-1.5">
          <button
            v-for="l in LEVELS" :key="l.key" type="button"
            :data-testid="`vis-${l.key}`"
            class="flex flex-col items-start gap-0.5 rounded-[10px] border px-3 py-2 text-left transition disabled:cursor-not-allowed disabled:opacity-45"
            :class="curLevel === l.key ? 'border-accent-strong bg-accent-soft' : 'border-border hover:border-ring'"
            :disabled="levelDisabled(l)" :aria-pressed="curLevel === l.key"
            @click="onPickLevel(l.key)"
          >
            <span class="flex items-center gap-1 text-[13px] font-semibold" :class="curLevel === l.key ? 'text-accent-text' : 'text-foreground'">
              <Loader2 v-if="visBusy === l.key" :size="12" :stroke-width="2" class="animate-spin" />{{ l.label }}
            </span>
            <span class="text-[10.5px] leading-tight text-faint">{{ l.hint }}</span>
          </button>
        </div>
        <p v-if="!isKbAdmin" class="mb-4 text-[11px] text-faint">「全公司」涉及全员可见，需知识库管理员操作。</p>
        <p v-else class="mb-4 text-[11px] text-faint">改「受限」= 下线归档、离开检索（可再改回恢复）。</p>

        <!-- node-ACL：按组织架构设可见范围（整体替换；取消勾选即撤销）——仅已迁移 node 文档 -->
        <template v-if="isDeptInternal && metaLoading">
          <p class="mb-4 flex items-center gap-1.5 text-[11px] text-faint">
            <Loader2 :size="12" class="animate-spin" /> 加载当前可见范围…
          </p>
        </template>
        <template v-else-if="isDeptInternal && metaErr">
          <p class="mb-4 text-[11px] text-danger">{{ metaErr }}</p>
        </template>
        <template v-else-if="isDeptInternal && docMeta?.acl_mode === 'node'">
          <p class="mb-2 text-[11px] font-bold uppercase tracking-[0.04em] text-faint">
            按组织架构设置可见范围
          </p>
          <div class="mb-2 rounded-[11px] border border-border bg-surface p-3">
            <OrgTreePicker v-model="nodePicked" :disabled="shareBusy" />
          </div>
          <p class="mb-2 text-[11px] text-faint">
            保存为<b>整体替换</b>：未勾选的部门会被撤销。「含下级」关掉 = 仅直挂本部门的人可见。
          </p>
          <p v-if="nodeErr" class="mb-2 text-[11px] text-danger">{{ nodeErr }}</p>
          <button
            type="button"
            class="mb-5 inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-[13px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
            :disabled="shareBusy || !docMeta"
            @click="saveNodes"
          ><Loader2 v-if="shareBusy" :size="13" :stroke-width="2" class="animate-spin" />{{
            shareBusy ? '保存中…' : (nodePicked.length ? `保存可见范围（${nodePicked.length} 个节点）` : '保存可见范围（清空）')
          }}</button>
        </template>

        <!-- 跨部门共享：仅 dept_internal 文档可共享（public 已全员可读，restricted 不外露） -->
        <template v-if="isDeptInternal">
        <!-- 现行已共享（可撤销） -->
        <template v-if="rows.length">
          <p class="mb-2 text-[11px] font-bold uppercase tracking-[0.04em] text-faint">已共享给</p>
          <div class="mb-4 overflow-hidden rounded-[11px] border border-border">
            <div
              v-for="g in rows" :key="g.id"
              class="flex items-center gap-2.5 border-t border-border bg-surface px-3 py-2 first:border-t-0"
            >
              <span class="min-w-0 flex-1 truncate text-[13px] text-foreground">
                {{ rowLabel(g.requester_dept) }}
                <span class="ml-1.5 text-[11px] text-faint">{{ (g.decided_at || '').slice(0, 10) }}<span v-if="g.reason"> · {{ g.reason }}</span></span>
              </span>
              <button
                type="button"
                class="shrink-0 rounded-md px-2 py-1 text-[11.5px] font-medium text-muted-foreground transition hover:bg-panel hover:text-foreground disabled:opacity-50"
                :disabled="isBusy(`grant:${g.id}`)"
                @click="onRevoke(g)"
              >{{ isBusy(`grant:${g.id}`) ? '撤销中…' : '撤销' }}</button>
            </div>
          </div>
        </template>

        <!-- 新增共享目标 -->
        <p class="mb-2 text-[11px] font-bold uppercase tracking-[0.04em] text-faint">共享给更多部门</p>
        <div v-if="options.length" class="flex flex-wrap gap-1.5">
          <button
            v-for="t in options" :key="t" type="button"
            class="rounded-full border px-2.5 py-1 text-[12.5px] transition"
            :class="picked.includes(t) ? 'border-accent-strong bg-accent-soft font-medium text-accent-text' : 'border-border text-muted-foreground hover:border-ring'"
            :aria-pressed="picked.includes(t)"
            @click="toggle(t)"
          >{{ deptLabel(t) }}</button>
        </div>
        <p v-else class="text-[12.5px] text-muted-foreground">全部可选部门均已共享。</p>

        <label class="mb-1.5 mt-4 block text-[11px] font-bold uppercase tracking-[0.04em] text-faint">共享说明（可空）</label>
        <input
          v-model="reason" type="text" maxlength="200" placeholder="为何开放给这些部门（记录于审批历史）"
          class="w-full rounded-[9px] border border-input bg-surface px-3 py-2 text-[13px] text-foreground placeholder:text-faint focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15"
        />

        <p class="mt-3 text-[11.5px] leading-relaxed text-muted-foreground">
          所选部门将获得该文档检索授权（检索开放随后续维护生效），可随时在此处或「已授权」清单撤销；操作会记录于审批历史。
        </p>
        </template>

        <!-- 非 dept_internal：共享区不适用，给一句说明 -->
        <p v-else class="rounded-[10px] bg-panel/60 px-3 py-2.5 text-[12px] leading-relaxed text-muted-foreground">
          {{ curLevel === 'public' ? '全公司文档已对全员可见，无需按部门共享。' : '受限文档不进检索、不对外开放；如需共享请先改为「仅本部门」。' }}
        </p>

        <p v-if="err" class="mt-2 text-[12.5px] text-st-fail">{{ err }}</p>
      </div>

      <!-- 底 -->
      <div class="flex items-center justify-end gap-2.5 border-t border-border px-[22px] py-3.5">
        <button type="button" class="rounded-lg border border-border px-4 py-2 text-[13px] font-medium text-foreground transition hover:border-border-strong" @click="close">{{ isDeptInternal ? '取消' : '关闭' }}</button>
        <button
          v-if="isDeptInternal"
          type="button"
          class="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-[13px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          :disabled="shareBusy || !picked.length"
          @click="onSubmit"
        ><Loader2 v-if="shareBusy" :size="13" :stroke-width="2" class="animate-spin" />{{ shareBusy ? '共享中…' : (picked.length ? `共享给 ${picked.length} 个部门` : '共享') }}</button>
      </div>
    </div>
  </div>
</template>
