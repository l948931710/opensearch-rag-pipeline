<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ChevronDown, Loader2, TriangleAlert, X } from '@lucide/vue'
import OrgTreePicker, { type PickedNode } from './OrgTreePicker.vue'
import { fetchOrgSnapshot, type OrgSnapshot } from '@/composables/useOrgSnapshot'
import { coverCount, uncoveredDirectParents } from '@/lib/orgTree'

/**
 * 折叠式组织树选择器（2026-08-03 改版）——收起态是一个紧凑字段,点击才内联向下展开
 * OrgTreePicker 整树。解决「归属+可见范围两棵树纵向堆叠吃掉大半首屏」（基线审计 ①②）。
 *
 * 设计要点（codex 四轮共识 + 基线审计）:
 *  · **内联展开,不用浮层**——DocMetaModal 是 overflow-y-auto 容器,浮层会被裁剪。
 *  · 三条计算型防呆（0 人标红/约 N 人可见/直挂遗漏）与快照过期横幅**收起态可见**——
 *    审计原话:折叠若把它们藏进展开态,等于把"完全无声的坑"重新变回无声。
 *  · mode='owner':单选,无「含下级/仅本级」及人数概览文案（payload 只发 owner_dept_id,
 *    子树语义在归属侧不存在）;选中即收起。mode='visibility':多选 chips,「完成」收起。
 *  · restrictToManaged:按快照的 my_managed_node_roots 过滤（dept_admin 归属限管辖子树,
 *    与后端后代集判定对齐）。三态:null=旧后端 unknown ⇒ 不过滤;[] 且快照 fresh ⇒
 *    「暂无管辖授权」占位（仅 fresh 才断言零根——「缺键=unknown≠空集」仓规）。
 *  · 快照状态优先级:unavailable（重试）> stale（⚠️ 提示,树仍可看）> 零根占位。
 *  · disabled 全链生效:触发器/chip 删除/完成/内部树。
 */
const props = withDefaults(defineProps<{
  modelValue: PickedNode[]
  mode: 'owner' | 'visibility'
  disabled?: boolean
  /** dept_admin 归属限管辖子树（kb_admin/可见范围不传） */
  restrictToManaged?: boolean
  placeholder?: string
}>(), { disabled: false, restrictToManaged: false, placeholder: '选择部门…' })
const emit = defineEmits<{ (e: 'update:modelValue', v: PickedNode[]): void }>()

const multiple = computed(() => props.mode === 'visibility')

// ── 组织快照（缓存命中零额外请求；unavailable 不落缓存 ⇒ 重试是真请求）──
const snap = ref<OrgSnapshot | null>(null)
const loading = ref(true)
const loadErr = ref('')
async function load() {
  loading.value = true; loadErr.value = ''
  try { snap.value = await fetchOrgSnapshot() } catch (e: any) {
    loadErr.value = e?.message || '组织数据加载失败'
  } finally { loading.value = false }
}
onMounted(load)

const byId = computed(() => new Map((snap.value?.nodes ?? []).map((n) => [n.dept_id, n])))
const unavailable = computed(() => !!loadErr.value || snap.value?.status === 'unavailable')
const staleHint = computed(() => snap.value?.status === 'stale')

const managedRoots = computed(() =>
  props.restrictToManaged ? snap.value?.my_managed_node_roots ?? null : null)
/** 真·零授权占位:仅 restrict + fresh + 明确空数组时成立 */
const zeroRoots = computed(() =>
  props.restrictToManaged && snap.value?.status === 'fresh'
  && Array.isArray(snap.value?.my_managed_node_roots)
  && snap.value!.my_managed_node_roots!.length === 0)

// ── 展开/收起 + 焦点管理 ──
const open = ref(false)
// 树延迟到**首次展开**才挂载：触发器在 loading 中是禁用的，所以首开时快照必已就绪——
// 否则 OrgTreePicker 会在快照未回时预挂载、以 allowedRoots=null 的瞬时值初始化展开集
//（管辖根展不开），还白打一次请求。v-show 保持后续开关不丢树内状态。
const everOpened = ref(false)
const panelId = `org-select-panel-${Math.random().toString(36).slice(2, 8)}`
const triggerRef = ref<HTMLButtonElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)
async function toggleOpen() {
  if (props.disabled || unavailable.value || zeroRoots.value) return
  open.value = !open.value
  if (open.value) {
    everOpened.value = true
    await nextTick()
    panelRef.value?.querySelector<HTMLInputElement>('.op-search input')?.focus()
  } else {
    triggerRef.value?.focus()
  }
}
function closePanel() {
  if (!open.value) return
  open.value = false
  triggerRef.value?.focus()
}
// owner 单选:选中即收起（清空/重选留在展开态）
watch(() => props.modelValue, (cur, old) => {
  if (props.mode === 'owner' && open.value && cur.length === 1
      && cur[0]?.dept_id !== old?.[0]?.dept_id) closePanel()
})

// ── 收起态摘要 ──
const nameOf = (id: number) => byId.value.get(id)?.name ?? `#${id}`
const missingInSnap = (id: number) => !byId.value.has(id)
const zeroStaff = (p: PickedNode) => {
  const n = byId.value.get(p.dept_id)
  return n ? (p.subtree ? n.staff_count : n.direct_staff_count ?? 0) === 0 : false
}
const ownerText = computed(() =>
  props.modelValue.length ? nameOf(props.modelValue[0].dept_id) : '')
/** 触发器无障碍名：同页两个选择器仅凭「已选 N 个节点」无法区分（ux-review 残留 2）。 */
const triggerA11y = computed(() => {
  const base = props.mode === 'owner' ? '归属部门' : '可见范围'
  const state = props.mode === 'owner'
    ? (ownerText.value || '未选择')
    : (props.modelValue.length ? `已选 ${props.modelValue.length} 个节点` : '未选择')
  return `${base}：${state}`
})
const cover = computed(() =>
  multiple.value && props.modelValue.length ? coverCount(props.modelValue, byId.value) : null)
const uncovered = computed(() =>
  multiple.value ? uncoveredDirectParents(props.modelValue, byId.value) : [])

function removeChip(id: number) {
  if (props.disabled) return
  emit('update:modelValue', props.modelValue.filter((p) => p.dept_id !== id))
}
function onModel(v: PickedNode[]) { emit('update:modelValue', v) }
</script>

<template>
  <div class="w-full">
    <!-- 不可用态：优先级最高（读失败/空快照都别装成「无数据可选」） -->
    <div v-if="unavailable"
         class="flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs text-muted-foreground">
      <TriangleAlert :size="13" class="shrink-0 text-st-busy" />
      <span class="flex-1">组织数据不可用{{ loadErr ? `（${loadErr}）` : '（快照为空）' }}</span>
      <button type="button" class="shrink-0 rounded border border-border px-2 py-0.5 text-[11.5px] text-foreground transition hover:bg-panel"
              :disabled="loading" @click="load">{{ loading ? '重试中…' : '重试' }}</button>
    </div>

    <!-- 真·零管辖授权（仅 fresh 断言） -->
    <div v-else-if="zeroRoots"
         class="rounded-md border border-border bg-card px-2.5 py-1.5 text-xs text-muted-foreground">
      暂无组织树管辖授权 —— 请联系 kb_admin 在「成员与角色」为你设置管辖节点。
    </div>

    <template v-else>
      <!-- 触发字段（收起态摘要） -->
      <button
        ref="triggerRef" type="button"
        class="flex w-full items-center gap-2 rounded-md border border-input bg-card px-2.5 py-1.5 text-left text-sm transition focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15 disabled:cursor-not-allowed disabled:opacity-60"
        :aria-expanded="open" :aria-controls="panelId" :aria-label="triggerA11y"
        :disabled="disabled || loading"
        @click="toggleOpen"
      >
        <Loader2 v-if="loading" :size="13" class="shrink-0 animate-spin text-muted-foreground" />
        <span v-if="mode === 'owner'" class="min-w-0 flex-1 truncate"
              :class="ownerText ? 'text-foreground' : 'text-faint'">
          <template v-if="ownerText">{{ ownerText }}
            <span v-if="missingInSnap(modelValue[0].dept_id)" class="text-st-busy">⚠️ 不在组织快照中</span>
          </template>
          <template v-else>{{ loading ? '加载组织架构…' : placeholder }}</template>
        </span>
        <span v-else class="min-w-0 flex-1 truncate"
              :class="modelValue.length ? 'text-foreground' : 'text-faint'">
          {{ loading ? '加载组织架构…'
            : (modelValue.length ? `已选 ${modelValue.length} 个节点` : placeholder) }}
        </span>
        <ChevronDown :size="14" class="shrink-0 text-muted-foreground transition-transform"
                     :class="{ 'rotate-180': open }" />
      </button>

      <!-- 多选 chips（收起态也可见/可删——0 人 chip 标红沿用树内防呆语义） -->
      <div v-if="multiple && modelValue.length" class="mt-1.5 flex flex-wrap gap-1.5">
        <span v-for="p in modelValue" :key="p.dept_id"
              class="inline-flex max-w-full items-center gap-1 rounded-full border px-2 py-0.5 text-[11.5px]"
              :class="zeroStaff(p) || missingInSnap(p.dept_id)
                ? 'border-destructive/40 bg-destructive/10 text-destructive'
                : 'border-border bg-secondary text-foreground'">
          <span class="truncate">{{ nameOf(p.dept_id) }}</span>
          <span class="shrink-0 text-[10.5px]" :class="zeroStaff(p) || missingInSnap(p.dept_id) ? '' : 'text-faint'">
            {{ missingInSnap(p.dept_id) ? '⚠️ 不在快照' : (p.subtree ? '含下级' : '仅本级') }}
          </span>
          <button type="button" class="grid size-4 shrink-0 place-items-center rounded-full transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
                  :aria-label="`移除 ${nameOf(p.dept_id)}`" :disabled="disabled"
                  @click="removeChip(p.dept_id)"><X :size="10" :stroke-width="2.25" /></button>
        </span>
      </div>

      <!-- 收起态防呆（与树内同公式,共享 lib/orgTree）：约 N 人 / 0 人告警 / 直挂遗漏 / stale。
           展开时隐去——树内已有同一份,双份渲染且「展开后勾上」文案在展开态自相矛盾（ux-review 残留 1） -->
      <template v-if="!open">
        <p v-if="cover !== null" class="mt-1 text-[11.5px]"
           :class="cover ? 'text-muted-foreground' : 'text-destructive'">
          {{ cover ? `约 ${cover} 人可见` : '⚠️ 所选节点人数为 0 —— 可能没有人能看到' }}
        </p>
        <p v-for="u in uncovered" :key="u.name" class="mt-1 text-[11.5px] text-destructive">
          ⚠️ 「{{ u.name }}」直挂的 {{ u.n }} 人不在可见范围内 —— 展开后勾上它并选「仅本级」
        </p>
      </template>
      <p v-if="staleHint && !open" class="mt-1 flex items-center gap-1 text-[11.5px] text-st-busy">
        <TriangleAlert :size="12" class="shrink-0" /> 组织数据可能已过期，展开查看详情
      </p>

      <!-- 内联展开面板（树本体；stale 详细横幅在 OrgTreePicker 内） -->
      <div v-show="open" :id="panelId" ref="panelRef"
           class="mt-1.5 rounded-md border border-border bg-surface p-2">
        <template v-if="everOpened">
          <div class="max-h-64 overflow-y-auto">
            <OrgTreePicker :model-value="modelValue" :multiple="multiple" :disabled="disabled"
                           :allowed-roots="mode === 'owner' ? managedRoots : null"
                           @update:model-value="onModel" />
          </div>
          <div v-if="multiple" class="mt-2 flex justify-end border-t border-border pt-2">
            <button type="button"
                    class="rounded-md border border-border px-3 py-1 text-[12px] font-medium text-foreground transition hover:bg-panel disabled:opacity-50"
                    :disabled="disabled" @click="closePanel">完成</button>
          </div>
        </template>
      </div>
    </template>
  </div>
</template>
