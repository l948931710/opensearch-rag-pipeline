import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import DocTable from '@/components/manage/DocTable.vue'
import { useKb, __resetKb, type DocItem } from '@/composables/useKb'

/**
 * 台账 node 口径（2026-08-05 对账）。这一族缺陷不会报错、只会静默显示错：
 * node 文档的 `owner_dept` 按后端契约恒为空串，共享关系写在 kb_doc_node_grant（节点 id）
 * 而**不在**组码授权表里。生产实测 270 篇「指定部门」上传全部显示成「仅本部门」。
 */
beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return {
    userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'],
    canManage: true, managedOwnerDepts: ['marketing'], ...over,
  }
}
function doc(over: Partial<DocItem> = {}): DocItem {
  return {
    doc_id: 'DOC1', title: '设备SOP', original_filename: 'sop.pdf', owner_dept: 'marketing',
    permission_level: 'dept_internal', current_version_no: 1, status: 'active',
    status_badge: '已上线', updated_at: '2026-08-01', can_manage: true, ...over,
  } as DocItem
}
function mountWith(docs: DocItem[], grants: any[] = []) {
  const p = createTestingPinia({
    createSpy: vi.fn,
    initialState: { session: { identity: identity(), token: 't', ready: true } },
  })
  setActivePinia(p)
  const kb = useKb()
  ;(kb as any).docs.value = docs
  ;(kb as any).accessGrants.value = grants
  return mount(DocTable, { global: { plugins: [p] } })
}
// ⚠️ 断言必须**锚到行内**，不能用 w.text() 扫整棵树：归属下拉的选项在 stats 缺席时
// 由已加载文档派生（ledgerOwnerOptions → ownerOptions），于是 owner_label 会在下拉里
// 也出现一份 —— 整树断言即便把归属列改回 deptLabel(owner_dept) 也照样绿（本文件首版
// 实测踩中：变异验证时 M2 未变红，该断言完全空转）。
const row = (w: any) => w.get('.led-row')
const ownerCell = (w: any) => w.get('.led-row [data-label="归属"]')

describe('DocTable — node 归属与共享', () => {
  it('node 文档：owner_dept 空串，归属列出后端直给的节点现名（不是空格子）', () => {
    const w = mountWith([doc({ owner_dept: '', acl_mode: 'node', owner_key: 'node:2', owner_label: '生产中心' })])
    expect(ownerCell(w).text()).toContain('生产中心')
  })

  it('node 共享：副行列出 shared_labels —— 组码授权表里没有它的行，只读 accessGrants 会恒空', () => {
    const w = mountWith([doc({
      owner_dept: '', acl_mode: 'node', owner_key: 'node:2', owner_label: '生产中心',
      shared_labels: ['质量中心', '人力资源部'],
    })])
    expect(row(w).text()).toContain('共享 质量中心、人力资源部')
  })

  it('>2 个共享折成 +N（副行不因共享面变宽而挤掉利用度/文件名）', () => {
    const w = mountWith([doc({
      owner_dept: '', acl_mode: 'node', owner_key: 'node:2', owner_label: '生产中心',
      shared_labels: ['质量中心', '人力资源部', '财务部', '采购部'],
    })])
    expect(row(w).text()).toContain('共享 质量中心、人力资源部')
    expect(row(w).text()).toContain('+2')
  })

  it('legacy 文档回退组码聚合 —— node 化不得把既有 legacy 台账的共享显示弄丢', () => {
    const w = mountWith(
      [doc({ doc_id: 'DOC1', owner_dept: 'marketing', acl_mode: 'legacy', owner_key: 'legacy:marketing' })],
      [{ doc_id: 'DOC1', requester_dept: 'hr', requester_name: '', status: 'approved' }],
    )
    expect(row(w).text()).toContain('共享')
  })

  it('未共享的 node 文档不显示共享段（空数组 ≠ 显示一个空的「共享 」）', () => {
    const w = mountWith([doc({
      owner_dept: '', acl_mode: 'node', owner_key: 'node:2', owner_label: '生产中心',
      shared_labels: [],
    })])
    expect(row(w).text()).not.toContain('共享')
  })
})
