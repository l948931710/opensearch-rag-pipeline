import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import { apiJson } from '@/lib/api'
import { useKb, __resetKb } from '@/composables/useKb'

// 只替换 apiJson，保留 @/lib/api 其余导出（ApiError 等）不被破坏。
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, apiJson: vi.fn() }
})

beforeEach(() => { __resetKb(); (apiJson as any).mockReset() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'dept_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}
function activate(id: Identity) {
  setActivePinia(createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } }))
}

describe('loadMyAccessRequests — 每 doc 保留最新行（后端 DESC；修 last-write-wins）', () => {
  it('同 doc 多行（拒后重申）→ 取最新 pending，不被最旧 rejected 覆盖', async () => {
    activate(identity())
    const kb = useKb()
    // 后端 ORDER BY created_at DESC：最新在前。旧实现 last-write-wins 会让最旧 rejected 覆盖 → 误得 'none'。
    ;(apiJson as any).mockResolvedValue({ items: [
      { doc_id: 'D1', status: 'pending', sync_state: 'n/a', created_at: '2026-06-27' },    // 最新（重申）
      { doc_id: 'D1', status: 'rejected', sync_state: 'n/a', created_at: '2026-06-20' },   // 最旧（被拒）
      { doc_id: 'D2', status: 'approved', sync_state: 'projected', created_at: '2026-06-25' },
    ] })
    await kb.loadMyAccessRequests()
    expect(kb.accessStateOf('D1')).toBe('pending')      // 最新 pending 胜出（旧实现得 'none'）
    expect(kb.accessStateOf('D2')).toBe('projected')
  })

  it('撤销后重申同理：最新 pending 不被最旧 revoked 覆盖', async () => {
    activate(identity())
    const kb = useKb()
    ;(apiJson as any).mockResolvedValue({ items: [
      { doc_id: 'D3', status: 'pending', sync_state: 'n/a', created_at: '2026-06-27' },
      { doc_id: 'D3', status: 'revoked', sync_state: 'n/a', created_at: '2026-06-10' },
    ] })
    await kb.loadMyAccessRequests()
    expect(kb.accessStateOf('D3')).toBe('pending')
  })
})
