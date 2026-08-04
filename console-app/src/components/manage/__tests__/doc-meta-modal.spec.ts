import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import DocMetaModal from '../DocMetaModal.vue'
import OrgTreeSelect from '../OrgTreeSelect.vue'
import { useKb, __resetKb, type DocItem } from '@/composables/useKb'
import { useSession, type Role } from '@/stores/session'

/**
 * 编辑信息弹窗（2026-08-03 折叠选择器改版回归）：
 *  · 归属选择器对 dept_admin 限管辖子树（restrictToManaged），kb_admin 全树
 *  · 保存 payload 与 CAS（expected_acl_revision）语义一字不动
 */

const NODES = [
  { dept_id: 1, parent_id: 0, name: '富岭科技', depth: 1, staff_count: 100, direct_staff_count: 2 },
  { dept_id: 3, parent_id: 1, name: '注塑事业部', depth: 2, staff_count: 30, direct_staff_count: 8 },
  { dept_id: 6, parent_id: 1, name: '质量中心', depth: 2, staff_count: 60, direct_staff_count: 60 },
]
vi.mock('@/composables/useOrgSnapshot', () => ({
  fetchOrgSnapshot: async () => ({
    nodes: NODES, stale: false, synced_at: 'x', status: 'fresh', my_managed_node_roots: [3],
  }),
  invalidateOrgSnapshot: () => {},
}))

const DOC: DocItem = {
  doc_id: 'nd1', title: '注塑机保养作业指导书', original_filename: 'sop.docx', owner_dept: '',
  permission_level: 'dept_internal', current_version_no: 3, status: 'active',
  status_badge: '已上线', updated_at: '2026-08-01 10:00',
}

const META = {
  doc_id: 'nd1', title: '注塑机保养作业指导书', category_l1: '生产', category_l2: '设备保养',
  permission_level: 'dept_internal', status: 'active', acl_mode: 'node',
  owner_dept: '', owner_dept_id: 3, owner_key: 'node:3', owner_label: '注塑事业部',
  acl_revision: 5,
  node_grants: [{ dept_id: 3, scope: 'subtree', name: '注塑事业部', active: true }],
  legacy_grants: [],
}

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}

let posted: any[] = []
function stubFetch() {
  posted = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
    if (path.startsWith('/api/kb/doc-meta')) {
      if (init?.method === 'POST') {
        posted.push(JSON.parse(init.body))
        return jsonResp({ doc_id: 'nd1', acl_mode: 'node', acl_revision: 6, changed: ['title'], ok: true })
      }
      return jsonResp(META)
    }
    if (path.startsWith('/api/kb/my-docs')) return jsonResp({ items: [], has_more: false })
    return jsonResp({}, { ok: false, status: 404 })
  }))
}

function login(role: Role) {
  useSession().setIdentity({
    userId: 'u1', name: 'u1', role, aclGroups: [], canManage: true, managedOwnerDepts: ['production'],
  })
}

async function openModal(role: Role) {
  login(role)
  const kb = useKb()
  kb.nodeAclGrant.value = true
  kb.closeDocMeta()                 // docMetaCtx 是模块级 ref：先归零，确保 open 触发加载 watcher
  const w = mount(DocMetaModal)
  kb.openDocMeta(DOC)
  await nextTick(); await new Promise((r) => setTimeout(r, 0)); await nextTick(); await nextTick()
  return { w, kb }
}

beforeEach(() => {
  setActivePinia(createPinia())
  __resetKb()
  vi.restoreAllMocks()
  useSession().setToken('TKN')
  stubFetch()
})

describe('DocMetaModal — 折叠选择器改版回归', () => {
  it('node 文档预填归属/可见集；dept_admin 的归属选择器限管辖子树', async () => {
    const { w } = await openModal('dept_admin')
    const selects = w.findAllComponents(OrgTreeSelect)
    expect(selects).toHaveLength(2)
    expect(selects[0].props('mode')).toBe('owner')
    expect(selects[0].props('restrictToManaged')).toBe(true)
    expect(selects[0].text()).toContain('注塑事业部')          // 预填名称（收起态）
    expect(selects[1].props('mode')).toBe('visibility')
    expect(selects[1].props('restrictToManaged')).toBe(false)  // 可见范围永不过滤
    expect(selects[1].text()).toContain('注塑事业部')          // 预填 chips
  })

  it('kb_admin 的归属选择器不过滤', async () => {
    const { w } = await openModal('kb_admin')
    expect(w.findAllComponents(OrgTreeSelect)[0].props('restrictToManaged')).toBe(false)
  })

  it('保存 payload：CAS 必带 + 归属/可见集整体替换语义不变', async () => {
    const { w } = await openModal('dept_admin')
    const title = w.get('input[type="text"]')
    await title.setValue('新标题')
    await w.findAll('button').find((b) => b.text() === '保存')!.trigger('click')
    await new Promise((r) => setTimeout(r, 0))
    expect(posted).toHaveLength(1)
    expect(posted[0]).toMatchObject({
      doc_id: 'nd1',
      expected_acl_revision: 5,
      title: '新标题',
      owner_dept_id: 3,
      visible_nodes: [{ dept_id: 3, subtree: true }],
    })
  })
})
