import { beforeEach, describe, expect, it } from 'vitest'
import { diag, diagLines, debugEnabled, __resetDiag } from '@/lib/diag'

beforeEach(() => __resetDiag())

describe('diag（parity-4 诊断环形日志）', () => {
  it('累积打点 + 环形上限 100（丢最旧）', () => {
    for (let i = 0; i < 105; i++) diag('m' + i)
    expect(diagLines().value).toHaveLength(100)
    expect(diagLines().value[0].msg).toBe('m5')      // 前 5 条被挤出
    expect(diagLines().value.at(-1)?.msg).toBe('m104')
  })

  it('debugEnabled 读 ?debug（且不受 scrub 影响）', () => {
    window.history.replaceState(null, '', '/console/?debug=1')
    expect(debugEnabled()).toBe(true)
    window.history.replaceState(null, '', '/console/')
    expect(debugEnabled()).toBe(false)
  })
})
