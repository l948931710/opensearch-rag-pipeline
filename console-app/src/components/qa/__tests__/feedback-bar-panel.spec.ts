import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import FeedbackBar from '@/components/qa/FeedbackBar.vue'
import type { ChatMessage } from '@/composables/useAsk'

// 点踩原因面板（对齐小程序 feedback-bar 语义）的组件级回归：
// 开合不直接投票 / 多选与固定顺序序列化 / 空提交禁用 / 在途全锁且面板不因乐观置态闪没 /
// 取消草稿保留 / 失败恢复重开 / 编辑草稿不污染 message（persist 是剔除式序列化，
// 挂上去的新字段会进 localStorage——这是评审定死的数据边界）。

const vote = vi.fn()
vi.mock('@/composables/useAsk', () => ({
  useAsk: () => ({ vote, copyAns: vi.fn() }),
}))

function aiMsg(extra: Record<string, unknown> = {}): ChatMessage {
  return { id: 'a1', role: 'ai', messageId: 'm1', sourcesOpen: false, voted: '', viewBlocks: null,
           ...extra } as ChatMessage
}

const chip = (w: VueWrapper, label: string) => w.findAll('button').find((b) => b.text() === label)!
const panel = (w: VueWrapper) => w.find('[id^="fb-panel-"]')
const downBtn = (w: VueWrapper) => w.find('button[aria-label="没用"]')
const submitBtn = (w: VueWrapper) => w.findAll('button').find((b) => b.text().startsWith('提交'))!
const ta = (w: VueWrapper) => w.find('textarea')

beforeEach(() => { vote.mockReset() })

describe('FeedbackBar — 点踩原因面板', () => {
  it('踩=开合面板、不直接投票；aria-expanded/controls 指向实例唯一 id', async () => {
    const w = mount(FeedbackBar, { props: { message: aiMsg() } })
    expect(panel(w).exists()).toBe(false)
    expect(downBtn(w).attributes('aria-expanded')).toBe('false')
    await downBtn(w).trigger('click')
    expect(panel(w).exists()).toBe(true)
    expect(vote).not.toHaveBeenCalled()
    expect(downBtn(w).attributes('aria-expanded')).toBe('true')
    expect(downBtn(w).attributes('aria-controls')).toBe('fb-panel-m1')
    await downBtn(w).trigger('click')
    expect(panel(w).exists()).toBe(false)
  })

  it('逆序点击 chips → 仍按固定 REASONS 顺序序列化', async () => {
    vote.mockResolvedValue(true)
    const w = mount(FeedbackBar, { props: { message: aiMsg() } })
    await downBtn(w).trigger('click')
    await chip(w, '信息过时').trigger('click')     // 列表末位先点
    await chip(w, '内容不准确').trigger('click')   // 列表首位后点
    expect(chip(w, '信息过时').attributes('aria-pressed')).toBe('true')
    await submitBtn(w).trigger('click')
    expect(vote).toHaveBeenCalledWith(expect.anything(), 'downvote',
      { reason: 'inaccurate,outdated', comment: '' })
  })

  it('原因与说明都空 → 提交禁用；填说明即可提交；maxlength=200', async () => {
    const w = mount(FeedbackBar, { props: { message: aiMsg() } })
    await downBtn(w).trigger('click')
    expect(submitBtn(w).attributes('disabled')).toBeDefined()
    await ta(w).setValue('答非所问了')
    expect(submitBtn(w).attributes('disabled')).toBeUndefined()
    expect(ta(w).attributes('maxlength')).toBe('200')
  })

  it('取消：面板收起、草稿保留、再开还在', async () => {
    const w = mount(FeedbackBar, { props: { message: aiMsg() } })
    await downBtn(w).trigger('click')
    await chip(w, '图片不对').trigger('click')
    await ta(w).setValue('第三步截图不匹配')
    await w.findAll('button').find((b) => b.text() === '取消')!.trigger('click')
    expect(panel(w).exists()).toBe(false)
    await downBtn(w).trigger('click')
    expect(chip(w, '图片不对').attributes('aria-pressed')).toBe('true')
    expect((ta(w).element as HTMLTextAreaElement).value).toBe('第三步截图不匹配')
  })

  it('在途窗口：全控件禁用、面板不因乐观置态闪没、重复提交被阻', async () => {
    const m = aiMsg()
    let resolveVote!: (v: boolean) => void
    vote.mockImplementation((msg: ChatMessage) => {
      msg.voted = 'down'   // 模拟 vote() 的乐观置态
      return new Promise<boolean>((r) => { resolveVote = r })
    })
    const w = mount(FeedbackBar, { props: { message: m } })
    await downBtn(w).trigger('click')
    await chip(w, '内容不准确').trigger('click')
    await submitBtn(w).trigger('click')
    expect(vote).toHaveBeenCalledTimes(1)
    expect(panel(w).exists()).toBe(true)   // 面板可见性只挂 panelOpen，不随 voted 闪没
    expect(ta(w).attributes('disabled')).toBeDefined()
    expect(chip(w, '答非所问').attributes('disabled')).toBeDefined()
    expect(w.text()).not.toContain('已反馈')   // 乐观置态在途不得提前宣告（确认后才播报）
    await submitBtn(w).trigger('click')    // 在途重复点击
    expect(vote).toHaveBeenCalledTimes(1)
    resolveVote(true)
    await flushPromises()
    expect(panel(w).exists()).toBe(false)
    expect(w.text()).toContain('已反馈')
  })

  it('失败：面板保持、草稿保留、role=alert 报错；重试成功后收起清错', async () => {
    const m = aiMsg()
    vote.mockResolvedValueOnce(false)
    const w = mount(FeedbackBar, { props: { message: m } })
    await downBtn(w).trigger('click')
    await chip(w, '答案不完整').trigger('click')
    await ta(w).setValue('少了后半部分')
    await submitBtn(w).trigger('click')
    await flushPromises()
    expect(panel(w).exists()).toBe(true)
    expect(w.find('[role="alert"]').text()).toContain('提交失败')
    expect(chip(w, '答案不完整').attributes('aria-pressed')).toBe('true')
    expect((ta(w).element as HTMLTextAreaElement).value).toBe('少了后半部分')

    vote.mockImplementation((msg: ChatMessage) => { msg.voted = 'down'; return Promise.resolve(true) })
    await submitBtn(w).trigger('click')
    await flushPromises()
    expect(panel(w).exists()).toBe(false)
    expect(w.find('[role="alert"]').exists()).toBe(false)
  })

  it('编辑面板（未提交）不向 message 添加任何字段（localStorage 边界）', async () => {
    const m = aiMsg()
    const keysBefore = Object.keys(m).sort()
    const w = mount(FeedbackBar, { props: { message: m } })
    await downBtn(w).trigger('click')
    await chip(w, '信息过时').trigger('click')
    await ta(w).setValue('这是草稿')
    expect(Object.keys(m).sort()).toEqual(keysBefore)
  })

  it('已锁票（含回灌恢复态）：踩按钮禁用、面板永不出现、显示已反馈', async () => {
    const w = mount(FeedbackBar, { props: { message: aiMsg({ voted: 'down' }) } })
    expect(downBtn(w).attributes('disabled')).toBeDefined()
    await downBtn(w).trigger('click')
    expect(panel(w).exists()).toBe(false)
    expect(w.text()).toContain('已反馈')
  })
})
