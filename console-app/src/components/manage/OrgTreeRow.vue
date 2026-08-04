<script setup lang="ts">
import { inject } from 'vue'
import { ChevronRight, Users } from '@lucide/vue'
import type { OrgNode } from '@/composables/useOrgSnapshot'

/**
 * 组织树的一行（递归）。此前 OrgTreePicker 模板硬编码 root→child→grandchild 三层，
 * 而生产树深 5——归属恰恰要选到车间/班组（深层），三层根本够不到（codex 阶段 B 评审）。
 * 状态/回调经 provide('orgPickerApi') 注入（OrgTreePicker 是唯一 provider），
 * 自引用递归渲染任意深度。
 */
defineProps<{ node: OrgNode }>()

// OrgTreePicker 注入的操作面；类型上按需声明（provider 与本文件同仓同改，不做跨包契约）
const api = inject('orgPickerApi') as {
  multiple: boolean
  disabled: boolean
  children: (id: number) => OrgNode[]
  picked: (id: number) => { dept_id: number; subtree: boolean } | undefined
  isPicked: (id: number) => boolean
  shown: (id: number) => boolean
  isOpen: (id: number) => boolean
  hasKids: (id: number) => boolean
  toggle: (n: OrgNode) => void
  toggleSubtree: (id: number) => void
  toggleExpand: (id: number) => void
  nodeLabel: (n: OrgNode) => string
  caretLabel: (n: OrgNode) => string
  subtreeLabel: (n: OrgNode) => string
}
</script>

<template>
  <li v-if="api.shown(node.dept_id)">
    <div class="op-row" :class="{ picked: api.isPicked(node.dept_id) }">
      <button class="op-caret" :class="{ open: api.isOpen(node.dept_id) }" type="button"
              :disabled="!api.hasKids(node.dept_id)" :aria-label="api.caretLabel(node)"
              :aria-expanded="api.isOpen(node.dept_id)" @click="api.toggleExpand(node.dept_id)">
        <ChevronRight v-if="api.hasKids(node.dept_id)" :size="13" />
      </button>
      <label class="op-label">
        <input :type="api.multiple ? 'checkbox' : 'radio'" :checked="api.isPicked(node.dept_id)"
               :aria-label="api.nodeLabel(node)"
               :disabled="api.disabled" @change="api.toggle(node)" />
        <span class="op-name">{{ node.name }}</span>
        <span class="op-count" :class="{ zero: node.staff_count === 0 }">
          <Users :size="11" />{{ node.staff_count }}
        </span>
      </label>
      <button v-if="api.multiple && api.isPicked(node.dept_id)" type="button" class="op-sub"
              :class="{ on: api.picked(node.dept_id)?.subtree }" :disabled="api.disabled"
              :aria-label="api.subtreeLabel(node)"
              @click="api.toggleSubtree(node.dept_id)">
        {{ api.picked(node.dept_id)?.subtree ? '含下级' : '仅本级' }}
      </button>
    </div>
    <ul v-if="api.isOpen(node.dept_id) && api.hasKids(node.dept_id)" class="op-children">
      <OrgTreeRow v-for="c in api.children(node.dept_id)" :key="c.dept_id" :node="c" />
    </ul>
  </li>
</template>

<style scoped>
/* 行级样式住在行组件里 —— OrgTreePicker 的 scoped 样式不会穿透到子组件 DOM。
   2026-08-03:硬编码十六进制 → tokens.css 语义变量(暗色主题跳色,基线审计 ④) */
.op-children { list-style: none; margin: 0; padding: 0 0 0 16px; }
.op-row { display: flex; align-items: center; gap: 4px; padding: 2px 4px; border-radius: 5px; font-size: 12px; }
.op-row.picked { background: var(--accent-soft); }
.op-caret {
  width: 16px; height: 16px; display: inline-flex; align-items: center; justify-content: center;
  border: 0; background: transparent; cursor: pointer; color: var(--muted);
  transition: transform .15s;
}
.op-caret.open { transform: rotate(90deg); }
.op-caret:disabled { cursor: default; opacity: 0; }
.op-label { display: flex; align-items: center; gap: 6px; flex: 1; cursor: pointer; min-width: 0; }
.op-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.op-count {
  display: inline-flex; align-items: center; gap: 2px; padding: 0 5px; border-radius: 9px;
  background: var(--panel); color: var(--muted); font-size: 11px; flex: none;
}
/* 0 人节点 = 授权给它没人能看到 —— 必须显眼 */
.op-count.zero {
  background: color-mix(in srgb, var(--destructive) 12%, transparent);
  color: var(--destructive); font-weight: 600;
}
.op-sub {
  flex: none; padding: 1px 7px; border-radius: 9px; font-size: 11px; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--muted);
}
.op-sub.on { border-color: var(--accent); background: var(--accent-soft); color: var(--accent-text); }
</style>
