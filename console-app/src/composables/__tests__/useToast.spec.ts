import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { useToast } from '@/composables/useToast'
import { useDialog } from '@/composables/useDialog'

/**
 * toast 通道的单测。最重要的一条是「与 useDialog 互不干扰」——见 describe 内注释。
 */
describe('useToast', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    useToast().clearToasts()
  })
  afterEach(() => { vi.useRealTimers() })

  it('成功/失败按 tone 分流，并在各自 ttl 后自动消失（失败停留更久）', () => {
    const { toasts, toast } = useToast()
    toast('已保存')
    toast('保存失败', { tone: 'error' })
    expect(toasts.value.map((t) => t.tone)).toEqual(['success', 'error'])

    vi.advanceTimersByTime(3200)
    expect(toasts.value.map((t) => t.tone), 'success 3.2s 后消失，error 仍在').toEqual(['error'])

    vi.advanceTimersByTime(6000 - 3200)
    expect(toasts.value).toHaveLength(0)
  })

  it('同文案连发不去重——两次操作都成功了，合并会瞒报其中一次', () => {
    const { toasts, toast } = useToast()
    toast('已标记为已处理')
    toast('已标记为已处理')
    expect(toasts.value).toHaveLength(2)
  })

  it('并发上限 3：超出丢最旧的，且被丢的定时器一并清掉（不会回头再动新队列）', () => {
    const { toasts, toast } = useToast()
    const first = toast('1')
    toast('2'); toast('3'); toast('4')
    expect(toasts.value.map((t) => t.text)).toEqual(['2', '3', '4'])

    // 若被挤掉的第 1 条定时器没清，它会在 3.2s 时触发一次 filter，
    // 把此刻队列里 id 相同的项误删——这里断言推进时间后剩余三条不受影响。
    expect(toasts.value.find((t) => t.id === first)).toBeUndefined()
    vi.advanceTimersByTime(3199)
    expect(toasts.value).toHaveLength(3)
  })

  it('clearToasts 清空队列与全部定时器（换身份时的泄漏防线）', () => {
    const { toasts, toast, clearToasts } = useToast()
    toast('A 的回执'); toast('A 的另一条')
    clearToasts()
    expect(toasts.value).toHaveLength(0)
    // 定时器若没清，3.2s 后旧回调会在新身份的队列上再跑一次 filter
    const { toast: t2 } = useToast()
    t2('B 的回执')
    vi.advanceTimersByTime(3200)
    expect(toasts.value, 'B 的回执按自己的 ttl 走，不被 A 的残留定时器提前清掉').toHaveLength(0)
  })

  it('【核心】toast 不经 useDialog 单槽：弹出回执不会顶掉正在等待的破坏性确认框', async () => {
    // useDialog 的 _resolve 是模块级单槽，_supersede() 会把旧 Promise 按「取消」强制了结。
    // 若成功回执图省事走 notice()，一个后台请求返回就能让用户正盯着的「确认退役」
    // 静默 resolve 成 false —— 用户以为自己点了取消。故 toast 必须是完全独立的通道。
    const { confirm, dialog } = useDialog()
    let settled: boolean | null = null
    void confirm({ title: '退役文档', message: '确认退役？', danger: true }).then((v) => { settled = v })

    expect(dialog.value.open).toBe(true)
    expect(dialog.value.title).toBe('退役文档')

    const { toast, toasts } = useToast()
    toast('已标记为已处理')
    await Promise.resolve()

    expect(toasts.value, 'toast 已入队').toHaveLength(1)
    expect(dialog.value.open, '确认框必须仍然打开').toBe(true)
    expect(dialog.value.title, '确认框内容不得被替换').toBe('退役文档')
    expect(settled, '确认框的 Promise 不得被提前 resolve').toBeNull()
  })
})
