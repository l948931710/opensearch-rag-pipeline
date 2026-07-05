import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useKb, __resetKb, type EscalationItem } from '@/composables/useKb'
import { useSession, type Role } from '@/stores/session'

// 转人工工单队列（盲区审计 P1-2）：加载 / 「显示已处理」切换 / 处置（答复→推回用户提示、
// 收件箱移出、busy 互斥）。fetch 走路由式 mock，与 useKb.spec 同一套约定。

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}

function setIdentity(role: Role, managed: string[]) {
  useSession().setIdentity({ userId: 'u', name: '张三', role, aclGroups: managed, canManage: role !== 'employee', managedOwnerDepts: managed })
}

function esc(id: string, closed = false): EscalationItem {
  return {
    ticket_id: id, message_id: `m-${id}`, question: `问题${id}`, ai_answer_excerpt: '',
    user_name: '王强', user_dept: '生产部', created_at: '2026-06-28 09:12', age_days: 3,
    status: closed ? 'RESOLVED' : 'PENDING', closed, expert_answer: '', assigned_user_name: '',
    docs: [],
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  __resetKb()
  vi.restoreAllMocks()
  useSession().setToken('TKN')
  setIdentity('dept_admin', ['production'])
})

describe('useKb 转人工工单队列', () => {
  it('loadEscalations：默认不带 include_closed；切换「显示已处理」后带上并重载', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      calls.push(path)
      return jsonResp({ items: [esc('t1')] })
    }))
    const kb = useKb()
    await kb.loadEscalations()
    expect(kb.escalations.value).toHaveLength(1)
    expect(calls[0]).toBe('/api/kb/escalations')
    kb.toggleShowClosedEscalations()
    await vi.waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[1]).toBe('/api/kb/escalations?include_closed=true')
  })

  it('resolveEscalation：答复并关闭 → POST 载荷带 expert_answer；收件箱视图处置即移出', async () => {
    let posted: any = null
    vi.stubGlobal('fetch', vi.fn(async (path: string, init?: any) => {
      if (path === '/api/kb/escalations/resolve') {
        posted = JSON.parse(init.body)
        return jsonResp({ status: 'ok', user_notified: true })
      }
      return jsonResp({ items: [esc('t1'), esc('t2')] })
    }))
    const kb = useKb()
    await kb.loadEscalations()
    const ok = await kb.resolveEscalation('t1', 'resolve', '弃审后红字冲销')
    expect(ok).toBe(true)
    expect(posted).toEqual({ ticket_id: 't1', action: 'resolve', expert_answer: '弃审后红字冲销' })
    expect((kb.escalations.value || []).map((x) => x.ticket_id)).toEqual(['t2'])   // 收件箱移出
  })

  it('resolveEscalation：失败不移出且返回 false；员工身份不加载（canManage 门）', async () => {
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (path === '/api/kb/escalations/resolve') return jsonResp({ detail: '无权' }, { ok: false, status: 403 })
      return jsonResp({ items: [esc('t1')] })
    }))
    const kb = useKb()
    await kb.loadEscalations()
    expect(await kb.resolveEscalation('t1', 'dismiss')).toBe(false)
    expect(kb.escalations.value).toHaveLength(1)

    __resetKb()
    setIdentity('employee', [])
    const fetchSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    await useKb().loadEscalations()
    expect(useKb().escalations.value).toEqual([])
    expect(fetchSpy).not.toHaveBeenCalled()
  })
})
