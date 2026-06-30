import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { syncHistoryForUser, __resetAsk } from '@/composables/useAsk'

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
