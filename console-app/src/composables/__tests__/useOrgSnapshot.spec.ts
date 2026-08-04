import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { fetchOrgSnapshot, invalidateOrgSnapshot } from '@/composables/useOrgSnapshot'
import { useSession } from '@/stores/session'

/**
 * 组织快照缓存 v2（2026-08-03 折叠选择器改版）：
 *  · status 三态（fresh/stale/unavailable）+ my_managed_node_roots 三态（null/[]/非空）
 *  · 身份分区缓存（principalEpoch+userId）：A 的管辖根绝不能喂给 B
 *  · 请求期间换人 ⇒ 丢弃响应重取（apiFetch 401 透明重登场景）
 *  · unavailable / 失败不落缓存
 */

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}

const NODES = [
  { dept_id: 1, parent_id: 0, name: '富岭科技', depth: 1, staff_count: 100, direct_staff_count: 2 },
  { dept_id: 2, parent_id: 1, name: '生产中心', depth: 2, staff_count: 80, direct_staff_count: 4 },
]

function orgResp(over: Record<string, unknown> = {}) {
  return {
    org_tree: { nodes: NODES, stale: false, synced_at: '2026-08-03 00:15:00' },
    my_managed_node_roots: [2],
    ...over,
  }
}

function login(userId: string) {
  useSession().setIdentity({
    userId, name: userId, role: 'dept_admin', aclGroups: [], canManage: true, managedOwnerDepts: ['production'],
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  invalidateOrgSnapshot()
  vi.restoreAllMocks()
  useSession().setToken('TKN')
})

describe('useOrgSnapshot — 三态解析', () => {
  it('fresh + 管辖根列表', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp(orgResp())))
    login('a')
    const s = await fetchOrgSnapshot()
    expect(s.status).toBe('fresh')
    expect(s.my_managed_node_roots).toEqual([2])
    expect(s.nodes).toHaveLength(2)
  })

  it('字段缺失 → roots=null（unknown≠空授权）；显式 [] → 真零授权', async () => {
    login('a')
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp(orgResp({ my_managed_node_roots: undefined }))))
    expect((await fetchOrgSnapshot()).my_managed_node_roots).toBeNull()
    invalidateOrgSnapshot()
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp(orgResp({ my_managed_node_roots: [] }))))
    expect((await fetchOrgSnapshot()).my_managed_node_roots).toEqual([])
  })

  it('org_tree=null / nodes 空+stale → unavailable；stale+有树 → stale', async () => {
    login('a')
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp(orgResp({ org_tree: null }))))
    expect((await fetchOrgSnapshot()).status).toBe('unavailable')
    invalidateOrgSnapshot()
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp(orgResp({ org_tree: { nodes: [], stale: true, synced_at: '' } }))))
    expect((await fetchOrgSnapshot()).status).toBe('unavailable')
    invalidateOrgSnapshot()
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp(orgResp({ org_tree: { nodes: NODES, stale: true, synced_at: 'x' } }))))
    const s = await fetchOrgSnapshot()
    expect(s.status).toBe('stale')
    expect(s.stale).toBe(true)
  })
})

describe('useOrgSnapshot — 缓存语义', () => {
  it('fresh 落缓存（同身份第二次不打请求）；unavailable 不落缓存（可真重试）', async () => {
    login('a')
    const f1 = vi.fn(async () => jsonResp(orgResp()))
    vi.stubGlobal('fetch', f1)
    await fetchOrgSnapshot(); await fetchOrgSnapshot()
    expect(f1).toHaveBeenCalledTimes(1)

    invalidateOrgSnapshot()
    const f2 = vi.fn(async () => jsonResp(orgResp({ org_tree: null })))
    vi.stubGlobal('fetch', f2)
    await fetchOrgSnapshot(); await fetchOrgSnapshot()
    expect(f2).toHaveBeenCalledTimes(2)
  })

  it('请求失败不落缓存', async () => {
    login('a')
    const f = vi.fn(async () => jsonResp({ detail: 'boom' }, { ok: false, status: 500 }))
    vi.stubGlobal('fetch', f)
    await expect(fetchOrgSnapshot()).rejects.toThrow()
    await expect(fetchOrgSnapshot()).rejects.toThrow()
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('身份切换（A→B）后缓存不可达，重新请求', async () => {
    login('a')
    const f = vi.fn(async () => jsonResp(orgResp()))
    vi.stubGlobal('fetch', f)
    await fetchOrgSnapshot()
    expect(f).toHaveBeenCalledTimes(1)
    login('b')                                  // userId 变化 ⇒ principalEpoch++
    await fetchOrgSnapshot()
    expect(f).toHaveBeenCalledTimes(2)
  })

  it('请求期间换人：丢弃 A 期响应、为 B 重取（deferred）', async () => {
    login('a')
    let resolveA!: (v: unknown) => void
    const deferred = new Promise((r) => { resolveA = r })
    const f = vi.fn()
      .mockImplementationOnce(() => deferred)                                  // A 期请求挂起
      .mockImplementationOnce(async () => jsonResp(orgResp({ my_managed_node_roots: [9] })))  // B 期重取
    vi.stubGlobal('fetch', f)
    const p = fetchOrgSnapshot()
    login('b')                                  // 响应回来前换人
    resolveA(jsonResp(orgResp({ my_managed_node_roots: [2] })))
    const s = await p
    expect(f).toHaveBeenCalledTimes(2)
    expect(s.my_managed_node_roots).toEqual([9])   // 拿到的是 B 身份的重取结果
    // A 期响应没被缓存：B 再取命中的是 B 的缓存（不再多打请求）
    const s2 = await fetchOrgSnapshot()
    expect(f).toHaveBeenCalledTimes(2)
    expect(s2.my_managed_node_roots).toEqual([9])
  })
})
