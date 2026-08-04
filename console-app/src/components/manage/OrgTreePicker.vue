<script setup lang="ts">
import { computed, onMounted, provide, ref } from 'vue'
import { Loader2, Search, TriangleAlert } from '@lucide/vue'
import OrgTreeRow from './OrgTreeRow.vue'
import { fetchOrgSnapshot, type OrgNode } from '@/composables/useOrgSnapshot'
import { coverCount, normalizeAllowedRoots, uncoveredDirectParents } from '@/lib/orgTree'

/**
 * 组织树选择器 —— 归属(单选)与可见范围(多选)共用同一棵树。
 *
 * 数据源 = `/api/kb/org-tree` 的 `org_tree.nodes`(RDS `dept_dim` 快照,扁平列表)。
 * ⚠️ 不再读 scratch 文件:那份被 .dockerignore 排除、生产镜像里根本不存在。
 *
 * 两个刻意的设计:
 *  · **每个节点显示子树人数** —— 授权是子树语义,管理员要看到的是"勾这个会给多少人看"。
 *    人数为 0 的节点显眼标红:授权给比员工挂载点更深的空节点 = 没人能看到,这是最容易
 *    踩的坑(现网实测确有 3 个 0 人节点)。
 *  · **每个选中项带「含下级」开关**(默认开)。关掉 = 仅直挂本节点的人。现网 26 个节点
 *    有子部门却仍有直挂人员、合计 172 人,纯子树语义表达不了"要部分子部门 + 直挂本级"。
 *
 * 2026-08-03(折叠式选择器改版):
 *  · `allowedRoots`:给定时树只从这些节点起渲染(dept_admin 归属限管辖子树,与后端
 *    _kb_can_manage_doc 的后代集判定对齐)。先归一化最小根集——授权模型允许祖先+后代
 *    同时为管辖根,直接过滤会让后代根在祖先子树里重复出现。仅归属用;可见范围永不过滤。
 *  · 单选(归属)模式不再显示「约 N 人可见/直挂遗漏」——那是可见范围语义,归属 payload
 *    只发 owner_dept_id,人数概览在归属侧纯属噪音(基线审计确认)。
 *  · coverHint/uncoveredDirect 计算抽到 `@/lib/orgTree`,与 OrgTreeSelect 收起态共用。
 */
export interface PickedNode { dept_id: number; subtree: boolean }

const props = withDefaults(defineProps<{
  modelValue: PickedNode[]
  multiple?: boolean          // false = 归属单选
  disabled?: boolean
  /** 限定可选子树根(null/缺省=全树)。传值时按最小根集归一化后作顶层渲染。 */
  allowedRoots?: number[] | null
  /** 选择颗粒度上限（2026-08-03 Sam 拍板:管理员授权与奖励口径最多二级 ⇒ 默认 2,
   *  车间/班组不再出现在选择树;「含下级」语义不变——二级 staff_count 本就含子树。
   *  ⚠️ 只收敛**可选集**;人数/存量名称/根存在性仍按全量快照算（codex blocker）。 */
  maxDepth?: number
}>(), { multiple: true, disabled: false, allowedRoots: null, maxDepth: 2 })
const emit = defineEmits<{ (e: 'update:modelValue', v: PickedNode[]): void }>()

const nodes = ref<OrgNode[]>([])
const loading = ref(true)
const err = ref('')
const stale = ref(false)
const syncedAt = ref('')
const q = ref('')
// 默认展开到二级 —— 119 个节点一次全铺会淹没管理员
const expanded = ref<Set<number>>(new Set())

// 组织快照的缓存/去重在 `@/composables/useOrgSnapshot` —— **不能**写在这里:
// `<script setup>` 顶层代码全部编译进 setup()，模块级 `let` 会变成每个实例的局部变量
// （2026-08-01 实测连开三次弹窗打三次请求）。同理这里也不允许 `export` 任何值。

onMounted(async () => {
  try {
    const snap = await fetchOrgSnapshot()
    nodes.value = snap.nodes
    stale.value = snap.stale
    syncedAt.value = snap.synced_at
    for (const n of nodes.value) if (n.depth <= 1) expanded.value.add(n.dept_id)
    // 限定子树时把允许根也展开，否则深层根默认折叠、一眼看不到自己的管辖范围
    if (props.allowedRoots) for (const id of props.allowedRoots) expanded.value.add(id)
  } catch (e: any) {
    err.value = e?.message || '组织数据加载失败'
  } finally {
    loading.value = false
  }
})

const selectable = computed(() => nodes.value.filter((n) => n.depth <= props.maxDepth))
const childrenOf = computed(() => {
  const m = new Map<number, OrgNode[]>()
  for (const n of selectable.value) {
    if (!m.has(n.parent_id)) m.set(n.parent_id, [])
    m.get(n.parent_id)!.push(n)
  }
  for (const arr of m.values()) arr.sort((a, b) => b.staff_count - a.staff_count)
  return m
})
const byId = computed(() => new Map(nodes.value.map(n => [n.dept_id, n])))

// 管辖根过滤：null=全树；归一化后为空但入参非空 ⇒ 根不在快照，显式报出而非渲染空白
const allowedSet = computed(() => normalizeAllowedRoots(props.allowedRoots, byId.value))
const roots = computed(() => allowedSet.value
  ? selectable.value.filter(n => allowedSet.value!.has(n.dept_id))
  : selectable.value.filter(n => n.depth === 1))
const rootsMissing = computed(() =>
  !loading.value && !err.value && nodes.value.length > 0
  && props.allowedRoots != null && roots.value.length === 0)
// 管辖根都在快照但深于颗粒度上限：与「不在快照」区分,给诚实定向提示而非放开全树
const rootsTooDeep = computed(() =>
  rootsMissing.value && normalizeAllowedRoots(props.allowedRoots, byId.value)!.size > 0)
// 存量深层选中项（政策收紧前留下的车间级归属/授权）:可见、可移除、不可新增
const retainedDeep = computed(() => props.modelValue.filter((pk) => {
  const n = byId.value.get(pk.dept_id)
  return n ? n.depth > props.maxDepth : false
}))

// 搜索:命中节点及其全部祖先都要显示(否则命中项在折叠的父下看不见)
const visibleIds = computed<Set<number> | null>(() => {
  const kw = q.value.trim()
  if (!kw) return null
  const keep = new Set<number>()
  for (const n of selectable.value) {
    if (!n.name.includes(kw)) continue
    let cur: OrgNode | undefined = n
    let hops = 0
    while (cur && hops++ < 20) { keep.add(cur.dept_id); cur = byId.value.get(cur.parent_id) }
  }
  return keep
})

const pickedMap = computed(() => new Map(props.modelValue.map(p => [p.dept_id, p])))
const isPicked = (id: number) => pickedMap.value.has(id)

function toggle(n: OrgNode) {
  if (props.disabled) return
  const cur = [...props.modelValue]
  const i = cur.findIndex(p => p.dept_id === n.dept_id)
  if (i >= 0) { cur.splice(i, 1) } else if (props.multiple) {
    cur.push({ dept_id: n.dept_id, subtree: true })
  } else {
    emit('update:modelValue', [{ dept_id: n.dept_id, subtree: true }]); return
  }
  emit('update:modelValue', cur)
}
function toggleSubtree(id: number) {
  if (props.disabled) return
  emit('update:modelValue', props.modelValue.map(
    p => (p.dept_id === id ? { ...p, subtree: !p.subtree } : p)))
}
function toggleExpand(id: number) {
  const s = new Set(expanded.value)
  s.has(id) ? s.delete(id) : s.add(id)
  expanded.value = s
}
const hasKids = (id: number) => (childrenOf.value.get(id)?.length || 0) > 0
const shown = (id: number) => !visibleIds.value || visibleIds.value.has(id)
// 搜索时强制展开,否则命中的深层节点仍被折叠隐藏
const isOpen = (id: number) => !!visibleIds.value || expanded.value.has(id)

// ── 无障碍名（2026-07-31）───────────────────────────────────────────────────
// 勾选框与展开箭头此前只有图标/裸 input，读屏软件念出来是 "复选框 选中" —— 听不出勾的是
// 哪个部门、有多少人。人数是这个选择器的**核心判断依据**（授权是子树语义），必须进无障碍名；
// 0 人节点还要把"没人能看到"这条防呆一并念出来，否则视觉标红对读屏用户等于不存在。
const nodeLabel = (n: OrgNode) =>
  n.staff_count === 0
    ? `${n.name}，子树 0 人（授权后可能没有人能看到）`
    : `${n.name}，子树 ${n.staff_count} 人`
const caretLabel = (n: OrgNode) =>
  `${isOpen(n.dept_id) ? '收起' : '展开'} ${n.name} 的下级部门`
const subtreeLabel = (n: OrgNode) =>
  pickedMap.value.get(n.dept_id)?.subtree
    ? `${n.name}：当前含下级，点击改为仅本级`
    : `${n.name}：当前仅本级，点击改为含下级`

// 人数概览与直挂遗漏警示（共享公式,仅多选/可见范围显示——单选归属侧是噪音）
const coverHint = computed(() => {
  if (!props.multiple || !props.modelValue.length) return ''
  const n = coverCount(props.modelValue, byId.value)
  return n ? `约 ${n} 人可见` : '⚠️ 所选节点人数为 0 —— 可能没有人能看到'
})
const uncoveredDirect = computed(() =>
  props.multiple ? uncoveredDirectParents(props.modelValue, byId.value) : [])

// 递归行组件的操作面（OrgTreeRow 注入；函数闭包引用响应式状态，天然保持响应性）。
// 深树修复（codex 阶段 B）：此前模板硬编码三层，生产树深 5——归属要选到车间/班组根本够不到。
provide('orgPickerApi', {
  get multiple() { return props.multiple },
  get disabled() { return props.disabled },
  children: (id: number) => childrenOf.value.get(id) || [],
  picked: (id: number) => pickedMap.value.get(id),
  isPicked,
  shown,
  isOpen,
  hasKids,
  toggle,
  toggleSubtree,
  toggleExpand,
  nodeLabel,
  caretLabel,
  subtreeLabel,
})
</script>

<template>
  <div class="org-picker">
    <div v-if="loading" class="op-state"><Loader2 class="spin" :size="14" /> 加载组织架构…</div>
    <div v-else-if="err" class="op-state op-err">{{ err }}</div>
    <template v-else>
      <div v-if="stale" class="op-stale">
        <TriangleAlert :size="13" /> 组织数据可能已过期（最后同步 {{ syncedAt || '未知' }}），
        请先运行组织同步后再设置可见范围。
      </div>
      <div v-if="rootsTooDeep" class="op-state op-err">
        当前管辖授权均深于二级部门（选择颗粒度止于二级）—— 请联系 kb_admin 调整授权层级。
      </div>
      <div v-else-if="rootsMissing" class="op-state op-err">
        管辖节点不在组织快照中 —— 请先运行组织同步，或联系 kb_admin 核对授权。
      </div>
      <template v-else>
        <label class="op-search">
          <Search :size="13" />
          <input v-model="q" type="text" placeholder="搜索部门名…" aria-label="搜索部门名"
                 :disabled="disabled" />
        </label>

        <ul class="op-tree">
          <OrgTreeRow v-for="r in roots" :key="r.dept_id" :node="r" />
        </ul>

        <div v-if="retainedDeep.length" class="op-cover">
          已保留的深层选择（现颗粒度止于二级，可移除、不可新增）：
          <button v-for="pk in retainedDeep" :key="pk.dept_id" type="button" class="op-deep"
                  :disabled="disabled" :aria-label="`移除 ${byId.get(pk.dept_id)?.name ?? pk.dept_id}`"
                  @click="toggle(byId.get(pk.dept_id)!)">{{ byId.get(pk.dept_id)?.name }} ×</button>
        </div>
        <div v-if="coverHint" class="op-cover" :class="{ warn: coverHint.startsWith('⚠️') }">
          {{ coverHint }}
        </div>
        <!-- 勾了子部门却漏掉父节点直挂的人：人数估算看不出来，必须显式点名 -->
        <div v-for="u in uncoveredDirect" :key="u.name" class="op-cover warn">
          ⚠️ 「{{ u.name }}」直挂的 {{ u.n }} 人不在可见范围内 —— 需要的话请勾上它并选「仅本级」
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
/* 语义色变量（tokens.css 亮/暗双主题）——2026-08-03 基线审计:硬编码十六进制在暗色下跳色 */
.org-picker { font-size: 12px; }
.op-state { display: flex; align-items: center; gap: 6px; padding: 10px 2px; color: var(--muted); }
.op-err { color: var(--destructive); }
.spin { animation: op-spin 1s linear infinite; }
@keyframes op-spin { to { transform: rotate(360deg); } }
.op-stale {
  display: flex; align-items: flex-start; gap: 6px; margin-bottom: 8px; padding: 7px 9px;
  border-radius: 6px; line-height: 1.5;
  background: color-mix(in srgb, var(--st-busy) 12%, transparent); color: var(--st-busy);
}
.op-search {
  display: flex; align-items: center; gap: 6px; padding: 5px 8px; margin-bottom: 6px;
  border: 1px solid var(--border); border-radius: 6px; color: var(--muted);
}
.op-search input { flex: 1; border: 0; outline: 0; background: transparent; font-size: 12px; color: var(--text); }
/* 行级样式（op-row/op-caret/op-label/op-count/op-sub/op-children）住在 OrgTreeRow.vue ——
   scoped 样式不会穿透到子组件 DOM，随行组件走 */
.op-tree { list-style: none; margin: 0; padding: 0; }
.op-cover { margin-top: 8px; color: var(--muted); }
.op-cover.warn { color: var(--destructive); }
.op-deep {
  margin: 2px 4px 0 0; padding: 1px 7px; border-radius: 9px; font-size: 11px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--muted);
}
</style>
