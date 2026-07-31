<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ChevronRight, Loader2, Search, TriangleAlert, Users } from '@lucide/vue'
import { apiJson } from '@/lib/api'

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
export interface OrgNode {
  dept_id: number
  parent_id: number
  name: string
  depth: number
  staff_count: number
}
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

onMounted(async () => {
  try {
    const r = await apiJson<any>('/api/kb/org-tree')
    const t = r?.org_tree || {}
    nodes.value = (t.nodes || []) as OrgNode[]
    stale.value = !!t.stale
    syncedAt.value = t.synced_at || ''
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

/** 已选节点覆盖的总人数(按子树/仅本节点分别计;仅供概览,不去重跨节点重叠) */
const coverHint = computed(() => {
  if (!props.modelValue.length) return ''
  const n = props.modelValue.reduce((s, p) => {
    const node = byId.value.get(p.dept_id)
    return s + (node ? (p.subtree ? node.staff_count : 0) : 0)
  }, 0)
  return n ? `约 ${n} 人可见` : '⚠️ 所选节点子树人数为 0 —— 可能没有人能看到'
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
        <template v-for="r in roots" :key="r.dept_id">
          <li v-if="shown(r.dept_id)">
            <div class="op-row" :class="{ picked: isPicked(r.dept_id) }">
              <button class="op-caret" :class="{ open: isOpen(r.dept_id) }" type="button"
                      :disabled="!hasKids(r.dept_id)" @click="toggleExpand(r.dept_id)">
                <ChevronRight v-if="hasKids(r.dept_id)" :size="13" />
              </button>
              <label class="op-label">
                <input :type="multiple ? 'checkbox' : 'radio'" :checked="isPicked(r.dept_id)"
                       :disabled="disabled" @change="toggle(r)" />
                <span class="op-name">{{ r.name }}</span>
                <span class="op-count" :class="{ zero: r.staff_count === 0 }">
                  <Users :size="11" />{{ r.staff_count }}
                </span>
              </label>
              <button v-if="multiple && isPicked(r.dept_id)" type="button" class="op-sub"
                      :class="{ on: pickedMap.get(r.dept_id)?.subtree }" :disabled="disabled"
                      @click="toggleSubtree(r.dept_id)">
                {{ pickedMap.get(r.dept_id)?.subtree ? '含下级' : '仅本级' }}
              </button>
            </div>
            <ul v-if="isOpen(r.dept_id)" class="op-children">
              <template v-for="c in (childrenOf.get(r.dept_id) || [])" :key="c.dept_id">
                <li v-if="shown(c.dept_id)">
                  <div class="op-row" :class="{ picked: isPicked(c.dept_id) }">
                    <button class="op-caret" :class="{ open: isOpen(c.dept_id) }" type="button"
                            :disabled="!hasKids(c.dept_id)" @click="toggleExpand(c.dept_id)">
                      <ChevronRight v-if="hasKids(c.dept_id)" :size="13" />
                    </button>
                    <label class="op-label">
                      <input :type="multiple ? 'checkbox' : 'radio'" :checked="isPicked(c.dept_id)"
                             :disabled="disabled" @change="toggle(c)" />
                      <span class="op-name">{{ c.name }}</span>
                      <span class="op-count" :class="{ zero: c.staff_count === 0 }">
                        <Users :size="11" />{{ c.staff_count }}
                      </span>
                    </label>
                    <button v-if="multiple && isPicked(c.dept_id)" type="button" class="op-sub"
                            :class="{ on: pickedMap.get(c.dept_id)?.subtree }" :disabled="disabled"
                            @click="toggleSubtree(c.dept_id)">
                      {{ pickedMap.get(c.dept_id)?.subtree ? '含下级' : '仅本级' }}
                    </button>
                  </div>
                  <ul v-if="isOpen(c.dept_id)" class="op-children">
                    <template v-for="g in (childrenOf.get(c.dept_id) || [])" :key="g.dept_id">
                      <li v-if="shown(g.dept_id)" class="op-row" :class="{ picked: isPicked(g.dept_id) }">
                        <span class="op-caret" />
                        <label class="op-label">
                          <input :type="multiple ? 'checkbox' : 'radio'" :checked="isPicked(g.dept_id)"
                                 :disabled="disabled" @change="toggle(g)" />
                          <span class="op-name">{{ g.name }}</span>
                          <span class="op-count" :class="{ zero: g.staff_count === 0 }">
                            <Users :size="11" />{{ g.staff_count }}
                          </span>
                        </label>
                        <button v-if="multiple && isPicked(g.dept_id)" type="button" class="op-sub"
                                :class="{ on: pickedMap.get(g.dept_id)?.subtree }" :disabled="disabled"
                                @click="toggleSubtree(g.dept_id)">
                          {{ pickedMap.get(g.dept_id)?.subtree ? '含下级' : '仅本级' }}
                        </button>
                      </li>
                    </template>
                  </ul>
                </li>
              </template>
            </ul>
          </li>
        </template>
      </ul>

      <div v-if="coverHint" class="op-cover" :class="{ warn: coverHint.startsWith('⚠️') }">
        {{ coverHint }}
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
.op-tree, .op-children { list-style: none; margin: 0; padding: 0; }
.op-children { padding-left: 16px; }
.op-row { display: flex; align-items: center; gap: 4px; padding: 2px 4px; border-radius: 5px; }
.op-row.picked { background: #eef2ff; }
.op-caret {
  width: 16px; height: 16px; display: inline-flex; align-items: center; justify-content: center;
  border: 0; background: transparent; cursor: pointer; color: var(--muted, #9ca3af);
  transition: transform .15s;
}
.op-caret.open { transform: rotate(90deg); }
.op-caret:disabled { cursor: default; opacity: 0; }
.op-label { display: flex; align-items: center; gap: 6px; flex: 1; cursor: pointer; min-width: 0; }
.op-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.op-count {
  display: inline-flex; align-items: center; gap: 2px; padding: 0 5px; border-radius: 9px;
  background: var(--chip, #f3f4f6); color: var(--muted, #6b7280); font-size: 11px; flex: none;
}
/* 0 人节点 = 授权给它没人能看到 —— 必须显眼 */
.op-count.zero { background: #fef2f2; color: #b91c1c; font-weight: 600; }
.op-sub {
  flex: none; padding: 1px 7px; border-radius: 9px; font-size: 11px; cursor: pointer;
  border: 1px solid var(--border, #e5e7eb); background: #fff; color: var(--muted, #6b7280);
}
.op-sub.on { border-color: #6366f1; background: #eef2ff; color: #4338ca; }
.op-cover { margin-top: 8px; color: var(--muted, #6b7280); }
.op-cover.warn { color: #b91c1c; }
</style>
