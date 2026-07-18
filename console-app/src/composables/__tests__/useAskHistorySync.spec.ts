import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { syncHistoryForUser, __loadPersistedForTest, __resetAsk } from '@/composables/useAsk'

const LS_KEY = 'fl-conversations'

beforeEach(() => {
  setActivePinia(createPinia())
  __resetAsk()
  localStorage.clear()
})

describe('syncHistoryForUser — 共享设备防跨用户残留', () => {
  it('本地缓存属于他人 → 清空（不把上一个人的部门内部答案留给下一个人）', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({
      uid: 'userA', activeId: 'c1',
      conversations: [{ id: 'c1', title: 'A 的对话', messages: [{ role: 'ai', answer: 'A 的部门内部答案' }] }],
    }))
    syncHistoryForUser('userB')
    expect(localStorage.getItem(LS_KEY)).toBeNull()
  })

  it('同一用户 → 保留本地历史', () => {
    const blob = JSON.stringify({
      uid: 'userA', activeId: 'c1',
      conversations: [{ id: 'c1', title: 'x', messages: [] }],
    })
    localStorage.setItem(LS_KEY, blob)
    syncHistoryForUser('userA')
    expect(localStorage.getItem(LS_KEY)).toBe(blob)
  })

  it('旧版无 uid 戳 → 无法证明归属，按他人清空', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({
      activeId: 'c1', conversations: [{ id: 'c1', messages: [] }],
    }))
    syncHistoryForUser('userB')
    expect(localStorage.getItem(LS_KEY)).toBeNull()
  })

  it('无缓存 → 安全 no-op', () => {
    syncHistoryForUser('userB')
    expect(localStorage.getItem(LS_KEY)).toBeNull()
  })
})

describe('loadPersisted TTL — B5/P2-05 本地会话过期淘汰', () => {
  it('超过 14 天的会话不回灌且回写收缩，新会话保留', () => {
    const now = Date.now()
    localStorage.setItem(LS_KEY, JSON.stringify({
      uid: 'userA', activeId: '',
      conversations: [
        { id: 'old', title: '过期对话', updatedAt: now - 15 * 24 * 3600 * 1000,
          messages: [{ role: 'ai', answer: '部门内部旧答案' }] },
        { id: 'fresh', title: '近期对话', updatedAt: now - 3600 * 1000,
          messages: [{ role: 'ai', answer: '近期答案' }] },
      ],
    }))
    __loadPersistedForTest()
    const stored = JSON.parse(localStorage.getItem(LS_KEY) || '{}')
    expect(stored.conversations.map((c: any) => c.id)).toEqual(['fresh'])   // 存量已收缩
    expect(stored.conversations.some((c: any) => c.id === 'old')).toBe(false)
  })

  it('缺 updatedAt 的旧格式会话按过期处理（宁清不留）', () => {
    localStorage.setItem(LS_KEY, JSON.stringify({
      uid: 'userA', activeId: '',
      conversations: [{ id: 'legacy', title: '无戳会话', messages: [] }],
    }))
    __loadPersistedForTest()
    const stored = JSON.parse(localStorage.getItem(LS_KEY) || '{}')
    expect(stored.conversations).toEqual([])
  })

  it('全部在 TTL 内 → 不动存量', () => {
    const blob = JSON.stringify({
      uid: 'userA', activeId: '',
      conversations: [{ id: 'c1', title: 'x', updatedAt: Date.now(), messages: [] }],
    })
    localStorage.setItem(LS_KEY, blob)
    __loadPersistedForTest()
    expect(localStorage.getItem(LS_KEY)).toBe(blob)   // 无淘汰不回写
  })
})
