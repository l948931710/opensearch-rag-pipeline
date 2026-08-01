<script setup lang="ts">
import { computed, onMounted, provide, ref } from 'vue'
import { Loader2, Search, TriangleAlert } from '@lucide/vue'
import OrgTreeRow from './OrgTreeRow.vue'
import { fetchOrgSnapshot, type OrgNode } from '@/composables/useOrgSnapshot'

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
 */
export interface PickedNode { dept_id: number; subtree: boolean }

const props = withDefaults(defineProps<{
  modelValue: PickedNode[]
  multiple?: boolean          // false = 归属单选
  disabled?: boolean
}>(), { multiple: true, disabled: false })
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
  } catch (e: any) {
    err.value = e?.message || '组织数据加载失败'
  } finally {
    loading.value = false
  }
})

const childrenOf = computed(() => {
  const m = new Map<number, OrgNode[]>()
  for (const n of nodes.value) {
    if (!m.has(n.parent_id)) m.set(n.parent_id, [])
    m.get(n.parent_id)!.push(n)
  }
  for (const arr of m.values()) arr.sort((a, b) => b.staff_count - a.staff_count)
  return m
})
const roots = computed(() => nodes.value.filter(n => n.depth === 1))

// 搜索:命中节点及其全部祖先都要显示(否则命中项在折叠的父下看不见)
const byId = computed(() => new Map(nodes.value.map(n => [n.dept_id, n])))
const visibleIds = computed<Set<number> | null>(() => {
  const kw = q.value.trim()
  if (!kw) return null
  const keep = new Set<number>()
  for (const n of nodes.value) {
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

const directOf = (n: OrgNode) => n.direct_staff_count ?? 0

/** 已选节点覆盖的总人数(仅供概览,不去重跨节点重叠)。
 *
 * ⚠️ 两种勾法取**不同的数**（2026-08-01 修）：
 *   含下级 = d:<id>  → 子树数 staff_count
 *   仅本级 = dx:<id> → 直挂数 direct_staff_count
 * 旧实现对「仅本级」一律按 0 计 ⇒ 勾了中心仅本级人数纹丝不动，看着像白勾，
 * 而实际是真放行了那些直挂的人（生产中心直挂 4 人、注塑事业部 8 人）。
 */
const coverHint = computed(() => {
  if (!props.modelValue.length) return ''
  const n = props.modelValue.reduce((s, p) => {
    const node = byId.value.get(p.dept_id)
    if (!node) return s
    return s + (p.subtree ? node.staff_count : directOf(node))
  }, 0)
  return n ? `约 ${n} 人可见` : '⚠️ 所选节点人数为 0 —— 可能没有人能看到'
})

/** 「勾了子部门、却漏掉父节点直挂的人」——最容易踩且完全无声的坑。
 *
 * 现网 26 个节点有子部门却仍有直挂人员、合计 171 人次；一级中心自己也直挂人
 * （获胜包装 14 人、生产中心 4 人）。管理员勾齐了几个子部门却没勾父节点时，
 * 那些直挂的人一个都看不到，而人数估算是"对的"（它只是没算他们），
 * 光看数字发现不了 —— 必须显式点名。
 */
const uncoveredDirect = computed(() => {
  if (!props.modelValue.length) return [] as { name: string; n: number }[]
  // 已被覆盖 = 该节点本身被勾（任一模式），或它的某个祖先被勾且是「含下级」
  const covered = (id: number): boolean => {
    let cur: number | undefined = id
    let hops = 0
    while (cur && hops++ < 20) {
      const p = pickedMap.value.get(cur)
      if (p && (cur === id || p.subtree)) return true
      cur = byId.value.get(cur)?.parent_id
    }
    return false
  }
  const out: { name: string; n: number }[] = []
  for (const p of props.modelValue) {
    const node = byId.value.get(p.dept_id)
    if (!node) continue
    const par = byId.value.get(node.parent_id)
    if (!par || directOf(par) === 0 || covered(par.dept_id)) continue
    if (!out.some(o => o.name === par.name)) out.push({ name: par.name, n: directOf(par) })
  }
  return out
})

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
      <label class="op-search">
        <Search :size="13" />
        <input v-model="q" type="text" placeholder="搜索部门名…" :disabled="disabled" />
      </label>

      <ul class="op-tree">
        <OrgTreeRow v-for="r in roots" :key="r.dept_id" :node="r" />
      </ul>

      <div v-if="coverHint" class="op-cover" :class="{ warn: coverHint.startsWith('⚠️') }">
        {{ coverHint }}
      </div>
      <!-- 勾了子部门却漏掉父节点直挂的人：人数估算看不出来，必须显式点名 -->
      <div v-for="u in uncoveredDirect" :key="u.name" class="op-cover warn">
        ⚠️ 「{{ u.name }}」直挂的 {{ u.n }} 人不在可见范围内 —— 需要的话请勾上它并选「仅本级」
      </div>
    </template>
  </div>
</template>

<style scoped>
.org-picker { font-size: 12px; }
.op-state { display: flex; align-items: center; gap: 6px; padding: 10px 2px; color: var(--muted, #6b7280); }
.op-err { color: #b91c1c; }
.spin { animation: op-spin 1s linear infinite; }
@keyframes op-spin { to { transform: rotate(360deg); } }
.op-stale {
  display: flex; align-items: flex-start; gap: 6px; margin-bottom: 8px; padding: 7px 9px;
  border-radius: 6px; background: #fffbeb; color: #92400e; line-height: 1.5;
}
.op-search {
  display: flex; align-items: center; gap: 6px; padding: 5px 8px; margin-bottom: 6px;
  border: 1px solid var(--border, #e5e7eb); border-radius: 6px; color: var(--muted, #6b7280);
}
.op-search input { flex: 1; border: 0; outline: 0; background: transparent; font-size: 12px; }
/* 行级样式（op-row/op-caret/op-label/op-count/op-sub/op-children）住在 OrgTreeRow.vue ——
   scoped 样式不会穿透到子组件 DOM，随行组件走 */
.op-tree { list-style: none; margin: 0; padding: 0; }
.op-cover { margin-top: 8px; color: var(--muted, #6b7280); }
.op-cover.warn { color: #b91c1c; }
</style>
