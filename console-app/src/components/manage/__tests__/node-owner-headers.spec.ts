import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import VisibilityModal from '@/components/manage/VisibilityModal.vue'
import ShareDocModal from '@/components/manage/ShareDocModal.vue'
import UploadCard from '@/components/manage/UploadCard.vue'
import ApprovalHistory from '@/components/manage/ApprovalHistory.vue'
import { useKb, __resetKb, type DocItem, type ApprovalHistoryItem } from '@/composables/useKb'

/**
 * 弹窗/升版态/审批历史的 **node 归属** 守卫（2026-08-05 口径对账）。
 *
 * 同一个坑的四个复制品：`deptLabel(x.owner_dept)`。node 文档的 owner_dept 按后端契约恒为
 * 空串 ⇒ 标题行渲染成「《xx》 · 归属 」这样的半截文案，审批历史那处更是 `if (owner_dept)`
 * 判空 ⇒ 整段「归属」直接消失。四处一律改走 lib/orgTree.docOwnerText。
 *
 * ⚠️ 断言一律锚到标题行/条目本身，不用整树 text() —— 归属名很容易在别处（下拉、树选择器）
 * 也出现一份，整树断言会空转（同批 DocTable 测试首版实测踩中）。
 */
beforeEach(() => { __resetKb(); vi.restoreAllMocks() })

function identity(over: Partial<Identity> = {}): Identity {
  return {
    userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'],
    canManage: true, managedOwnerDepts: ['marketing'], ...over,
  }
}
function activate() {
  const p = createTestingPinia({
    createSpy: vi.fn,
    initialState: { session: { identity: identity(), token: 't', ready: true } },
  })
  setActivePinia(p)
  return p
}
/** node 文档：归属只在 owner_key/owner_label 上，owner_dept 是空串。 */
function nodeDoc(over: Partial<DocItem> = {}): DocItem {
  return {
    doc_id: 'DOC1', title: '设备SOP', original_filename: 'sop.pdf', owner_dept: '',
    acl_mode: 'node', owner_key: 'node:2', owner_label: '生产中心',
    permission_level: 'dept_internal', current_version_no: 2, status: 'active',
    status_badge: '已上线', updated_at: '2026-08-01', can_manage: true, ...over,
  } as DocItem
}

describe('node 文档的归属文案 — 弹窗 / 升版态 / 审批历史', () => {
  it('VisibilityModal 标题行出节点名，而不是「· 归属 」半截', () => {
    const p = activate()
    const kb = useKb()
    ;(kb as any).visCtx.value = nodeDoc()
    const w = mount(VisibilityModal, { global: { plugins: [p] } })
    const sub = w.get('.truncate')
    expect(sub.text()).toContain('生产中心')
    expect(sub.text()).not.toMatch(/归属\s*$/)
  })

  it('ShareDocModal 标题行出节点名（正文的 node 分支早就对了，漏的是标题）', () => {
    const p = activate()
    const kb = useKb()
    ;(kb as any).shareCtx.value = nodeDoc()
    const w = mount(ShareDocModal, { global: { plugins: [p] } })
    const sub = w.get('.truncate')
    expect(sub.text()).toContain('生产中心')
    expect(sub.text()).not.toMatch(/归属\s*$/)
  })

  it('UploadCard 升版态出节点名 —— verCtx 必须把 owner DTO 一起带过来', () => {
    const p = activate()
    const kb = useKb()
    ;(kb as any).enterVersionMode(nodeDoc())
    const w = mount(UploadCard, { global: { plugins: [p] } })
    expect(w.text()).toContain('升版目标')
    expect(w.text()).toContain('生产中心')
  })

  it('ApprovalHistory：node 文档仍出「归属 …」整段，不因 owner_dept 空串而消失', () => {
    const p = activate()
    const kb = useKb()
    const item: ApprovalHistoryItem = {
      kind: 'upload', action: 'approved', title: '设备SOP', owner_dept: '',
      owner_key: 'node:2', owner_label: '生产中心', subject: '', detail: '',
      extra: '', decided_by: 'a1', decided_by_name: '李娜', decided_at: '2026-08-05 10:00:00',
    }
    ;(kb as any).approvalHistory.value = [item]
    const w = mount(ApprovalHistory, { global: { plugins: [p] } })
    expect(w.text()).toContain('归属 生产中心')
  })

  it('ApprovalHistory：admin_grant 无文档作用域 ⇒ 不该凭空冒出「归属 未归属」', () => {
    const p = activate()
    const kb = useKb()
    const item: ApprovalHistoryItem = {
      kind: 'admin_grant', action: 'granted', title: '王工', owner_dept: '',
      subject: '王工', detail: '', extra: '', decided_by: 'a1', decided_by_name: '李娜',
      decided_at: '2026-08-05 10:00:00',
    }
    ;(kb as any).approvalHistory.value = [item]
    const w = mount(ApprovalHistory, { global: { plugins: [p] } })
    expect(w.text()).not.toContain('归属')
  })
})
