import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useKb, __resetKb } from '@/composables/useKb'
import { useSession } from '@/stores/session'
import * as api from '@/lib/api'

/**
 * 进度条的渲染契约（e2e 只能覆盖「没有真值就不渲染」那一半；有真值的那一半在真实上传里
 * 依赖 XHR upload.onprogress 的触发时机，不可稳定脚本化，故在这里定死数值语义）。
 *
 * 核心不变量（redline③）：**没有真实百分比就没有条**。null ≠ 0 —— 0 是「传了 0%」，
 * null 是「这个阶段压根没有可测进度」，UI 侧据此整条不渲染。
 */
describe('真实进度字段语义', () => {
  beforeEach(() => { setActivePinia(createPinia()); __resetKb() })

  it('uploadPct 初值为 null（不是 0）——两者在 UI 上必须是不同结果', () => {
    const kb = useKb()
    expect(kb.uploadPct.value).toBeNull()
    expect(kb.uploadPct.value).not.toBe(0)
  })

  it('bulkTotal 初值 0 ⇒ 批量进度条不渲染', () => {
    const kb = useKb()
    expect(kb.bulkTotal.value).toBe(0)
    expect(kb.bulkDone.value).toBe(0)
  })

  it('__resetKb 复位三个进度字段（换身份后不得留着上一个人的进度）', () => {
    const kb = useKb()
    kb.uploadPct.value = 42
    kb.bulkDone.value = 3
    kb.bulkTotal.value = 5
    __resetKb()
    expect(kb.uploadPct.value).toBeNull()
    expect(kb.bulkDone.value).toBe(0)
    expect(kb.bulkTotal.value).toBe(0)
  })

  it('bulkDone/bulkTotal 是确定值而非估算：done 恒 ≤ total 且 total>0 时百分比可算', () => {
    const kb = useKb()
    kb.bulkTotal.value = 4
    for (let i = 1; i <= 4; i++) {
      kb.bulkDone.value = i
      expect(kb.bulkDone.value).toBeLessThanOrEqual(kb.bulkTotal.value)
      expect(Math.round((kb.bulkDone.value / kb.bulkTotal.value) * 100)).toBe(i * 25)
    }
  })
})

/**
 * 管辖根候选决策的互斥。审计发现这是全组件唯一零防护的写按钮（无 :disabled、无 busy 追踪，
 * 连点可并发发决策请求）。评审指出候选区因 onMounted 竞态在 e2e 里从不挂载 ⇒ 新加的
 * cand-confirm/cand-reject 两个 testid 拿不到端到端证据，所以判据放在 store 层——
 * 互斥本来就发生在这里。
 */
describe('decideNodeCandidate 互斥', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    __resetKb()
    const s = useSession()
    s.setToken('t')
    s.identity = { userId: 'u', displayName: 'U', role: 'kb_admin', canManage: true,
      aclGroups: ['production'], managedOwnerDepts: ['production'] } as any
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('同一候选连点两次只发一次 decide 请求', async () => {
    const kb = useKb()
    const calls: string[] = []
    let release: (v: any) => void = () => {}
    vi.spyOn(api, 'apiJson').mockImplementation((path: string) => {
      calls.push(path)
      return path.includes('/decide') ? new Promise((res) => { release = res }) : Promise.resolve({ items: [] } as any)
    })

    const p1 = kb.decideNodeCandidate(7, 'confirm')
    const p2 = kb.decideNodeCandidate(7, 'confirm')      // 在途 ⇒ 必须被拦下
    release({})
    const [r1, r2] = await Promise.all([p1, p2])

    expect(calls.filter((c) => c.includes('/decide')).length, '基线：两次都发出去').toBe(1)
    expect(r1, '首次成功 ⇒ 无错误').toBeNull()
    expect(r2, '被互斥拦下不得显示成失败').toBeNull()
  })

  it('不同候选各自独立，不互相阻塞', async () => {
    const kb = useKb()
    const calls: string[] = []
    vi.spyOn(api, 'apiJson').mockImplementation((path: string) => {
      calls.push(path); return Promise.resolve({ items: [] } as any)
    })
    await Promise.all([kb.decideNodeCandidate(1, 'confirm'), kb.decideNodeCandidate(2, 'reject')])
    expect(calls.filter((c) => c.includes('/decide')).length).toBe(2)
  })

  it('真实失败仍返回错误文案（?? null 不得把失败吞成 null）', async () => {
    const kb = useKb()
    vi.spyOn(api, 'apiJson').mockImplementation((path: string) => (
      path.includes('/decide') ? Promise.reject({ detail: { message: '快照已翻' } }) : Promise.resolve({ items: [] } as any)
    ))
    expect(await kb.decideNodeCandidate(9, 'confirm')).toBe('快照已翻')
  })
})
