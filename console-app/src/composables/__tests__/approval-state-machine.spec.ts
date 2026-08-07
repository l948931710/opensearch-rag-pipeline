import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useKb, __resetKb } from '@/composables/useKb'
import { useSession } from '@/stores/session'

/**
 * 批 B（2026-08-06 补评审）：审批状态机的前端半边。
 *
 * B3 —— `reject` 此前拿到任何 2xx 就本地移除，后端的 `rejected: 0` 无人消费。
 *       与 approve 侧 2026-08-06 修掉的「现网 20 个僵尸条目」**完全同型**，只是那边修了。
 *       后端现在 0 行回 409（走 catch），但两条路径都要收敛到「不移除 + 刷队列」。
 * B4 —— 退役会把该文档【全部】待审批版本撤销成 WITHDRAWN（kb_console.py:3427）
 *       ⇒ 队列里那些单已经不该在了；此前 retire/restore/批量退役都只 loadDocs()。
 */

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}

const ITEM = { doc_id: 'D1', version_no: 2, title: 't', owner_dept: 'hr',
               permission_level: 'public', uploaded_by_name: 'x', received_at: '2026-08-06' }

beforeEach(() => {
  vi.restoreAllMocks(); vi.unstubAllGlobals()
  setActivePinia(createPinia()); __resetKb()
  useSession().setIdentity({ userId: 'k', name: '管', role: 'kb_admin', aclGroups: [],
                             canManage: true, managedOwnerDepts: [] })
  useSession().setToken('TKN')
})

/** 记录每个端点被打了几次；approvals/docs 的响应可定制。 */
function stub(rejectResp: () => any) {
  const hits: Record<string, number> = {}
  const bump = (k: string) => { hits[k] = (hits[k] || 0) + 1 }
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    const p = String(path)
    if (p.includes('/api/kb/reject')) { bump('reject'); return rejectResp() }
    if (p.includes('/api/kb/retire')) { bump('retire'); return jsonResp({ note: 'ok' }) }
    if (p.includes('/api/kb/restore')) { bump('restore'); return jsonResp({ note: 'ok' }) }
    // ⚠️ 服务端仍返回该单：这样才能区分「本地乐观移除了」与「拉了服务端真值」——
    // 若队列桩回空集，两种行为的最终条数都是 0，断言等于空转（第一版就这么写的，抓不到）。
    if (p.includes('/api/kb/pending-approvals')) { bump('approvals'); return jsonResp({ items: [{ ...ITEM }] }) }
    bump('other')
    return jsonResp({ items: [], has_more: false })
  }))
  return hits
}

function seedApprovals(kb: ReturnType<typeof useKb>) {
  ;(kb as unknown as { approvals: { value: unknown[] } }).approvals.value = [{ ...ITEM }]
}
const approvalCount = (kb: ReturnType<typeof useKb>) =>
  ((kb as unknown as { approvals: { value: unknown[] } }).approvals.value || []).length

describe('B3 reject 必须看计数 / 认 409', () => {
  it('409（竞态）⇒ 不本地移除 + 强制刷一次审批队列', async () => {
    const kb = useKb(); seedApprovals(kb)
    const hits = stub(() => jsonResp({ detail: '未驳回：该版本当前状态不可驳回' }, { ok: false, status: 409 }))
    await kb.reject({ ...ITEM } as never, '理由')
    // 收敛到服务端真值（服务端仍有该单）⇒ 证明没做本地乐观移除、且确实拉了权威
    expect(approvalCount(kb)).toBe(1)      // ★ 反证锚：修复前 409 只弹窗、根本不刷新
    expect(hits.approvals).toBe(1)
  })

  it('200 但 rejected:0 ⇒ 同样不移除 + 刷队列（409 只覆盖"全 0"，计数判据不可省）', async () => {
    const kb = useKb(); seedApprovals(kb)
    const hits = stub(() => jsonResp({ status: 'ok', rejected: 0 }))
    await kb.reject({ ...ITEM } as never, '理由')
    expect(approvalCount(kb)).toBe(1)      // ★ 反证锚：修复前 2xx 即 removeApproval + 不刷新
    expect(hits.approvals).toBe(1)
  })

  it('rejected:1 ⇒ 才移除，且刷文档列表而不是审批队列', async () => {
    const kb = useKb(); seedApprovals(kb)
    const hits = stub(() => jsonResp({ status: 'ok', rejected: 1 }))
    await kb.reject({ ...ITEM } as never, '理由')
    expect(approvalCount(kb)).toBe(0)
    expect(hits.approvals).toBeUndefined()
  })
})

describe('B4 退役/恢复必须刷审批队列', () => {
  it('单篇退役 ⇒ 恰好刷一次', async () => {
    const kb = useKb()
    const hits = stub(() => jsonResp({}))
    await kb.retire({ doc_id: 'D1' } as never)
    await new Promise((r) => setTimeout(r, 0))
    expect(hits.retire).toBe(1)
    expect(hits.approvals).toBe(1)         // ★ 反证锚：修复前恒为 undefined
  })

  it('单篇恢复 ⇒ 恰好刷一次（restore 把 WITHDRAWN 还原成 PENDING，单会回来）', async () => {
    const kb = useKb()
    const hits = stub(() => jsonResp({}))
    await kb.restore({ doc_id: 'D1' } as never)
    await new Promise((r) => setTimeout(r, 0))
    expect(hits.restore).toBe(1)
    expect(hits.approvals).toBe(1)         // ★ 反证锚
  })

  it('批量退役 2 篇 ⇒ 队列只刷一次（不得按文档 N 次）', async () => {
    const kb = useKb()
    const hits = stub(() => jsonResp({}))
    ;(kb as unknown as { docs: { value: unknown[] } }).docs.value = [
      { doc_id: 'D1', can_manage: true, status_badge: '已上线', permission_level: 'dept_internal' },
      { doc_id: 'D2', can_manage: true, status_badge: '已上线', permission_level: 'dept_internal' },
    ]
    kb.toggleSelect('D1'); kb.toggleSelect('D2')
    await kb.bulkRetire()
    await new Promise((r) => setTimeout(r, 0))
    expect(hits.retire).toBe(2)
    expect(hits.approvals).toBe(1)         // ★ 反证锚（也钉住"别 N 次"）
  })
})

describe('B4 收窄：只有会改变审批可见性的批量操作才刷队列', () => {
  it('批量改可见范围 ⇒ **不**刷审批队列（_bulkRun 与 bulkRetire 共用，别顺带多打一次）', async () => {
    const kb = useKb()
    const hits = stub(() => jsonResp({}))
    ;(kb as unknown as { docs: { value: unknown[] } }).docs.value = [
      { doc_id: 'D1', can_manage: true, status_badge: '已上线', permission_level: 'dept_internal' },
    ]
    kb.toggleSelect('D1')
    await kb.bulkSetVisibility('restricted')
    await new Promise((r) => setTimeout(r, 0))
    expect(hits.approvals).toBeUndefined()   // ★ 反证锚：无条件刷时这里是 1
  })
})
