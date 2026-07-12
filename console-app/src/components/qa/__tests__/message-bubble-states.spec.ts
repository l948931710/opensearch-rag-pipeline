import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import MessageBubble from '@/components/qa/MessageBubble.vue'

// 回归守卫：AI 消息状态机的「兜底空态」只许对【非流式】消息出现。
// 修复的缺陷：深度思考独占期（reasoning 帧把 loading 置 false、答案 html 尚 null）落进
// 兜底分支 →「这条回答没有内容（可能上次未生成完）+ 重试」渲染在思考流下方，
// 且重试此刻可点（会打断在途流）。首个答案帧到 html 首渲之间的窗口同理（无思考也闪一帧）。
// 持久化白名单剥掉 streaming 字段 → 回灌的半截会话恒无该字段，兜底原用途必须保留。

const retry = vi.fn()
vi.mock('@/composables/useAsk', () => ({
  useAsk: () => ({
    retry, handoff: vi.fn(), fillInput: vi.fn(),
    vote: vi.fn(), copyAns: vi.fn(),
    resignImage: vi.fn(), imgFailed: vi.fn(), preview: vi.fn(),
  }),
}))

const FALLBACK = '这条回答没有内容'

import type { ChatMessage } from '@/composables/useAsk'

function aiMsg(extra: Record<string, unknown>): ChatMessage {
  return { id: 'a1', role: 'ai', sourcesOpen: false, voted: '', viewBlocks: null,
           ...extra } as ChatMessage
}

describe('MessageBubble — 兜底空态不得泄漏进在途流', () => {
  it('深度思考独占期（streaming + reasoning、html 未至）→ 只显披露条，无兜底/重试', () => {
    const w = mount(MessageBubble, {
      props: {
        message: aiMsg({
          streaming: true, loading: false, question: '判定标准是什么？',
          reasoning: '先读文档…', reasoningHtml: '<p>先读文档…</p>', reasoningOpen: true,
          html: null,
        }),
      },
    })
    expect(w.text()).toContain('正在思考')          // 披露条在场
    expect(w.text()).not.toContain(FALLBACK)        // 兜底不得同屏
    expect(w.text()).not.toContain('重试')          // 在途流不给可打断入口
  })

  it('无思考的首帧窗口（streaming、loading 已收、html 未渲）→ 留白，不闪兜底', () => {
    const w = mount(MessageBubble, {
      props: { message: aiMsg({ streaming: true, loading: false, question: 'q', html: null }) },
    })
    expect(w.text()).not.toContain(FALLBACK)
  })

  it('回灌的半截会话（持久化已剥 streaming 字段）→ 兜底 + 重试照常', async () => {
    const w = mount(MessageBubble, {
      props: { message: aiMsg({ loading: false, html: null, question: '原问句' }) },
    })
    expect(w.text()).toContain(FALLBACK)
    const btn = w.findAll('button').find((b) => b.text().includes('重试'))
    expect(btn).toBeTruthy()
    await btn!.trigger('click')
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it('流已终止且有部分答案（stop 定稿 html）→ 走答案分支而非兜底', () => {
    const w = mount(MessageBubble, {
      props: {
        message: aiMsg({ streaming: false, loading: false, html: '<p>部分答案</p>', question: 'q' }),
      },
    })
    expect(w.text()).toContain('部分答案')
    expect(w.text()).not.toContain(FALLBACK)
  })
})
