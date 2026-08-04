import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { useKb, __resetKb, __setSelectedFiles } from '@/composables/useKb'
import { useSession, type Role } from '@/stores/session'
import type { OrgSnapshot } from '@/composables/useOrgSnapshot'

/**
 * 归属自动预填（Sam 拍板：dept_admin 单管辖根 ⇒ 预填；多根/stale/unknown 不动）
 * + 身份切换防线（codex v4 共识：草稿清理、上传链 epoch guard、查重结果不跨身份回写）。
 */

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}
async function waitFor(cond: () => boolean, ms = 1000) {
  const t0 = Date.now()
  while (!cond() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 5))
}

function login(userId: string, role: Role = 'dept_admin', uploadTargets: string[] = ['production']) {
  useSession().setIdentity({
    userId, name: userId, role, aclGroups: [], canManage: role !== 'employee',
    managedOwnerDepts: ['production'], uploadTargetDepts: uploadTargets,
  })
}

function snapOf(over: Partial<OrgSnapshot> = {}): OrgSnapshot {
  return {
    // 生产形状（中心=depth1、无公司根）;颗粒度守卫要求根 depth<=2
    nodes: [
      { dept_id: 2, parent_id: 1, name: '生产中心', depth: 1, staff_count: 80, direct_staff_count: 4 },
      { dept_id: 3, parent_id: 2, name: '注塑事业部', depth: 2, staff_count: 30, direct_staff_count: 8 },
    ],
    stale: false, synced_at: 'x', status: 'fresh', my_managed_node_roots: [3],
    ...over,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  __resetKb()
  vi.restoreAllMocks()
  useSession().setToken('TKN')
})

describe('maybeAutoSeedOwner — node 口径', () => {
  it('dept_admin 单管辖根 + fresh ⇒ 预填（含下级），且级联既有 owner→可见集 watch 的入参形状', () => {
    login('mgr1')
    const kb = useKb()
    kb.nodeAclGrant.value = true
    kb.maybeAutoSeedOwner(snapOf())
    expect(kb.newOwnerNode.value).toEqual([{ dept_id: 3, subtree: true }])
  })

  it('多根 / roots=null(旧后端) / stale / unavailable / 升版态 ⇒ 一律不播种', () => {
    login('mgr1')
    const kb = useKb()
    kb.nodeAclGrant.value = true
    kb.maybeAutoSeedOwner(snapOf({ my_managed_node_roots: [3, 5] }))
    expect(kb.newOwnerNode.value).toEqual([])
    kb.maybeAutoSeedOwner(snapOf({ my_managed_node_roots: null }))
    expect(kb.newOwnerNode.value).toEqual([])
    kb.maybeAutoSeedOwner(snapOf({ status: 'stale', stale: true }))
    expect(kb.newOwnerNode.value).toEqual([])
    kb.maybeAutoSeedOwner(snapOf({ status: 'unavailable', nodes: [] }))
    expect(kb.newOwnerNode.value).toEqual([])
    kb.verCtx.value = { doc_id: 'd', title: 't', owner_dept: 'production', permission_level: 'dept_internal', current_version_no: 1 }
    kb.maybeAutoSeedOwner(snapOf())
    expect(kb.newOwnerNode.value).toEqual([])
  })

  it('根不在快照 / 已有选择 ⇒ 不播种、不覆盖', () => {
    login('mgr1')
    const kb = useKb()
    kb.nodeAclGrant.value = true
    kb.maybeAutoSeedOwner(snapOf({ my_managed_node_roots: [99] }))
    expect(kb.newOwnerNode.value).toEqual([])
    kb.newOwnerNode.value = [{ dept_id: 2, subtree: true }]
    kb.maybeAutoSeedOwner(snapOf())
    expect(kb.newOwnerNode.value).toEqual([{ dept_id: 2, subtree: true }])
  })

  it('每身份一次：清空后不回填；__resetKb 后可再播；kb_admin/employee 不播', () => {
    login('mgr1')
    const kb = useKb()
    kb.nodeAclGrant.value = true
    kb.maybeAutoSeedOwner(snapOf())
    expect(kb.newOwnerNode.value).toHaveLength(1)
    kb.newOwnerNode.value = []                       // 用户主动清空
    kb.maybeAutoSeedOwner(snapOf())
    expect(kb.newOwnerNode.value).toEqual([])        // 尊重清空，不回填
    __resetKb()
    login('mgr1')
    const kb2 = useKb()
    kb2.nodeAclGrant.value = true
    kb2.maybeAutoSeedOwner(snapOf())
    expect(kb2.newOwnerNode.value).toHaveLength(1)   // reset 后重新生效

    __resetKb()
    login('boss', 'kb_admin')
    const kb3 = useKb()
    kb3.nodeAclGrant.value = true
    kb3.maybeAutoSeedOwner(snapOf())
    expect(kb3.newOwnerNode.value).toEqual([])
  })
})

describe('maybeAutoSeedOwner — legacy 口径', () => {
  it('唯一上传目标 ⇒ 预选；多目标/已有值 ⇒ 不动', () => {
    login('mgr1', 'dept_admin', ['production_straw'])
    const kb = useKb()
    kb.nodeAclGrant.value = false
    kb.maybeAutoSeedOwner()
    expect(kb.newOwner.value).toBe('production_straw')

    __resetKb()
    login('mgr2', 'dept_admin', ['production', 'quality'])
    const kb2 = useKb()
    kb2.maybeAutoSeedOwner()
    expect(kb2.newOwner.value).toBe('')
  })
})

describe('身份切换防线', () => {
  it('A 的草稿（标题/归属/文件）在切到 B 后被清空；B 可重新自动播种', async () => {
    login('a')
    const kb = useKb()                              // 注册 principal watcher
    kb.nodeAclGrant.value = true
    kb.maybeAutoSeedOwner(snapOf())
    kb.newTitle.value = 'A 的草稿'
    __setSelectedFiles([new File(['x'], 'a.pdf')])
    kb.selectedNames.value = ['a.pdf']

    login('b')                                      // A→B
    await nextTick()
    expect(kb.newTitle.value).toBe('')
    expect(kb.newOwnerNode.value).toEqual([])
    expect(kb.selectedNames.value).toEqual([])
    kb.doUpload()
    expect(kb.uploadErr.value).toBe('请先选择文件。')   // File 真身也已清空

    kb.maybeAutoSeedOwner(snapOf())                  // B 的每身份额度未被 A 烧掉
    expect(kb.newOwnerNode.value).toEqual([{ dept_id: 3, subtree: true }])
  })

  it('同用户重登（userId 不变）草稿保留', async () => {
    login('a')
    const kb = useKb()
    kb.newTitle.value = '草稿'
    login('a')
    await nextTick()
    expect(kb.newTitle.value).toBe('草稿')
  })

  it('watcher 按 store 实例注册：重建 Pinia 后新 store 的身份切换仍触发清理', async () => {
    login('a')
    useKb().newTitle.value = 'S1'
    // 模拟测试隔离：重建 Pinia（新 session store 实例）
    setActivePinia(createPinia())
    useSession().setToken('TKN')
    login('x')
    const kb2 = useKb()                             // 新 store 注册新 watcher
    kb2.newTitle.value = 'S2'
    login('y')
    await nextTick()
    expect(kb2.newTitle.value).toBe('')             // 新实例的切换同样生效
  })

  it('上传链：upload-url 返回前换人 ⇒ 不再 register、不回写 UI', async () => {
    login('a')
    const kb = useKb()
    kb.newOwner.value = 'production'
    __setSelectedFiles([new File(['x'], 'a.pdf')])
    kb.selectedNames.value = ['a.pdf']

    let resolveUrl!: (v: unknown) => void
    const paths: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      paths.push(path)
      if (path.startsWith('/api/kb/upload-url')) return new Promise((r) => { resolveUrl = r })
      return jsonResp({})
    }))
    kb.doUpload()
    await waitFor(() => paths.some((p) => p.startsWith('/api/kb/upload-url')))
    login('b')                                      // 响应回来前换人（watcher 顺带清草稿）
    await nextTick()
    resolveUrl(jsonResp({ upload_token: 'T', put_url: 'https://oss/x', raw_key: 'k', doc_id: 'd', expires_in: 600 }))
    await new Promise((r) => setTimeout(r, 30))
    expect(paths.some((p) => p.startsWith('/api/kb/register'))).toBe(false)
    expect(kb.uploadMsg.value).toBe('')             // 旧身份的进度/结果没写进 B 的 UI
    expect(kb.uploadOk.value).toBe(false)
    expect(kb.uploadBusy.value).toBe(false)
  })

  it('文件名查重：await 期间换人 ⇒ dupWarn 不写入新身份 UI', async () => {
    login('a')
    const kb = useKb()
    let resolveDocs!: (v: unknown) => void
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (path.startsWith('/api/kb/my-docs')) return new Promise((r) => { resolveDocs = r })
      return jsonResp({})
    }))
    const p = kb.onFileSelected({ length: 1, 0: new File(['x'], '货代发票审批.pdf'), item: () => null } as unknown as FileList)
    await waitFor(() => !!resolveDocs)
    login('b')
    await nextTick()
    resolveDocs(jsonResp({ items: [{ doc_id: 'd', title: '货代发票审批', original_filename: '', owner_dept: 'production', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '' }], has_more: false }))
    await p
    expect(kb.dupWarn.value).toBe('')
  })
})
