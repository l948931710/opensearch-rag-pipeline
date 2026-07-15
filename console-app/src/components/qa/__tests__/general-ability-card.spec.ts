import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import MessageBubble from '@/components/qa/MessageBubble.vue'

// 通用能力分级开放的前端同步（docs/general_ability_opening_design.md）：
// ① no_result 卡渲染引导式拒答多行话术（whitespace-pre-wrap）+「您是不是想问」
//    《标题》chips（done 帧 suggest_titles，点击回填纯标题）；
// ② 正常答案区 source=general/smalltalk 时显示「通用回答 · 非公司口径」弱徽标，
//    kb（缺省）绝不显示 —— flag 关时视觉与改动前一致。

const fillInput = vi.fn()
vi.mock('@/composables/useAsk', () => ({
  useAsk: () => ({
    retry: vi.fn(), handoff: vi.fn(), fillInput,
    vote: vi.fn(), copyAns: vi.fn(),
    resignImage: vi.fn(), imgFailed: vi.fn(), preview: vi.fn(),
  }),
}))

import type { ChatMessage } from '@/composables/useAsk'

function aiMsg(extra: Record<string, unknown>): ChatMessage {
  return { id: 'a1', role: 'ai', sourcesOpen: false, voted: '', viewBlocks: null,
           ...extra } as ChatMessage
}

const GUIDED = '抱歉，知识库中暂时没有找到能直接回答这个问题的资料。\n您是不是想问：\n· 《考勤管理制度》'
const BADGE = '通用回答 · 非公司口径'

describe('MessageBubble — 引导式拒答卡（no_result + suggest_titles）', () => {
  it('多行话术保留换行样式，《标题》chip 渲染且点击回填纯标题', async () => {
    const w = mount(MessageBubble, {
      props: {
        message: aiMsg({
          noResult: true, answer: GUIDED,
          suggestTitles: ['考勤管理制度'], rephrase: [],
          messageId: 'm1', question: '外派驻场怎么打卡',
        }),
      },
    })
    expect(w.text()).toContain('未找到相关内容')
    expect(w.text()).toContain('知识库中暂时没有找到')
    // 多行样式护栏：塌行会把「· 《标题》」列表揉成一团
    const body = w.find('.whitespace-pre-wrap')
    expect(body.exists()).toBe(true)
    expect(body.text()).toContain('知识库中暂时没有找到')
    // 《标题》chip 在场且点击回填【纯标题】（不含书名号）
    expect(w.text()).toContain('您是不是想问：')
    const chip = w.findAll('button').find((b) => b.text() === '《考勤管理制度》')
    expect(chip).toBeTruthy()
    await chip!.trigger('click')
    expect(fillInput).toHaveBeenCalledWith('考勤管理制度')
  })

  it('旧协议（无 suggestTitles，仅 rephrase）→ chips 块不渲染，换说法照旧', () => {
    const w = mount(MessageBubble, {
      props: {
        message: aiMsg({
          noResult: true, answer: '抱歉，当前知识库中未找到相关信息。',
          rephrase: ['换个说法A'], messageId: 'm2', question: 'q',
        }),
      },
    })
    expect(w.text()).not.toContain('您是不是想问')
    expect(w.text()).toContain('换个说法A')
  })
})

describe('MessageBubble — 通用回答来源徽标', () => {
  it('source=general → 徽标显示；smalltalk 同', () => {
    for (const source of ['general', 'smalltalk']) {
      const w = mount(MessageBubble, {
        props: { message: aiMsg({ html: '<p>Meeting tomorrow.</p>', source, question: 'q' }) },
      })
      expect(w.text()).toContain(BADGE)
    }
  })

  it('source 缺省/kb/guard → 绝不显示徽标（flag 关视觉不变）', () => {
    for (const source of [undefined, 'kb', 'guard']) {
      const w = mount(MessageBubble, {
        props: { message: aiMsg({ html: '<p>按手册执行。</p>', source, question: 'q' }) },
      })
      expect(w.text()).not.toContain(BADGE)
    }
  })
})
