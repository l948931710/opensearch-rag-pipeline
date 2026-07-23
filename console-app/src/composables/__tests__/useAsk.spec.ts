import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAsk, __resetAsk } from '@/composables/useAsk'
import { useSession } from '@/stores/session'

const enc = new TextEncoder()
const frame = (o: unknown) => enc.encode('data: ' + JSON.stringify(o) + '\n\n')
const DONE = enc.encode('data: [DONE]\n\n')

/** 鸭子类型的流式 Response：apiFetch 只看 ok/status，ask 只用 body.getReader()/text。 */
function streamResp(chunks: Uint8Array[], { ok = true, status = 200 } = {}) {
  let i = 0
  const reader = {
    read: async () => (i < chunks.length ? { value: chunks[i++], done: false } : { value: undefined, done: true }),
    cancel() {},
  }
  return { ok, status, body: { getReader: () => reader }, text: async () => '' }
}
function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}
async function waitFor(cond: () => boolean, ms = 1000) {
  const t0 = Date.now()
  while (!cond() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 5))
}

beforeEach(() => {
  setActivePinia(createPinia())
  __resetAsk()
  vi.restoreAllMocks()
  useSession().setToken('TKN')
})

describe('useAsk.ask — 正常流式（session→sources→chunk*→done→[DONE]）', () => {
  it('累积打字、来源、guard、收尾态', async () => {
    const chunks = [
      frame({ type: 'session', session_id: 's1', message_id: 'm1' }),
      frame({ type: 'sources', sources: [{ doc_id: 'd1', title: '年假制度.pdf', section: '第3条', level: 'high', score: 9 }] }),
      frame({ type: 'chunk', content: '每年' }),
      frame({ type: 'chunk', content: '5 天' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: true }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp(chunks)))
    const { ask, messages, asking } = useAsk()

    await ask('年假几天')

    expect(messages.value.map((m) => m.role)).toEqual(['user', 'ai'])
    const ai = messages.value[1]
    expect(ai.messageId).toBe('m1')
    expect(ai.sources?.[0]).toMatchObject({ idx: 1, title: '年假制度.pdf', section: '第3条', level: 'high', levelLabel: '高' })
    expect(ai.html).toContain('每年')
    expect(ai.html).toContain('5 天')
    expect(ai.guard).toBe(true)
    expect(ai.loading).toBe(false)
    expect(asking.value).toBe(false)
  })
})

describe('useAsk.ask — content_blocks 帧定稿图文（覆盖纯文本 html）', () => {
  it('viewBlocks 接管，copyText 不含图', async () => {
    const chunks = [
      frame({ type: 'session', session_id: 's', message_id: 'm2' }),
      frame({ type: 'chunk', content: '见下图 <<IMG:1>>' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: false }),
      frame({ type: 'content_blocks', content_blocks: [
        { type: 'markdown', content: '操作步骤如下' },
        { type: 'image', url: 'https://oss/x.png?sig=1', oss_key: 'processing/assets/hr/d/v1/x.png', caption: '步骤截图' },
      ] }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp(chunks)))
    const { ask, messages } = useAsk()

    await ask('怎么操作')
    const ai = messages.value[1]
    expect(ai.viewBlocks).toHaveLength(2)
    expect(ai.viewBlocks?.[0]).toMatchObject({ type: 'text' })
    expect(ai.viewBlocks?.[1]).toMatchObject({ type: 'image', oss_key: 'processing/assets/hr/d/v1/x.png', caption: '步骤截图', failed: false })
    expect(ai.copyText).toBe('操作步骤如下')      // 不含图片块
    // 打字途中 <<IMG:1>> 被 stripImg 擦掉，不进 html
    expect(ai.html).not.toContain('IMG')
  })
})

describe('useAsk.ask — 无结果分支（done 带 no_result+rephrase，无 sources）', () => {
  it('落 noResult 卡 + 改写建议', async () => {
    const chunks = [
      frame({ type: 'session', session_id: 's', message_id: 'm3' }),
      frame({ type: 'chunk', content: '抱歉，未找到相关信息。' }),
      frame({ type: 'done', model: 'N/A', usage: {}, guard: true, no_result: true, rephrase: ['换个说法A', '换个说法B'] }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp(chunks)))
    const { ask, messages } = useAsk()

    await ask('火星基地密码')
    const ai = messages.value[1]
    expect(ai.noResult).toBe(true)
    expect(ai.answer).toContain('未找到')
    expect(ai.rephrase).toEqual(['换个说法A', '换个说法B'])
    // 旧协议（flag 关）done 无 suggest_titles/source → 空数组/未定义（回归护栏）
    expect(ai.suggestTitles).toEqual([])
    expect(ai.source).toBeUndefined()
  })

  it('引导式拒答：done 带 suggest_titles（与 rephrase 二选一），流出话术保留', async () => {
    const guided = '抱歉，知识库中暂时没有找到能直接回答这个问题的资料。\n您是不是想问：\n· 《考勤管理制度》'
    const chunks = [
      frame({ type: 'session', session_id: 's', message_id: 'm3t' }),
      frame({ type: 'chunk', content: guided }),
      frame({ type: 'done', model: 'N/A', usage: {}, guard: true, no_result: true,
              suggest_titles: ['考勤管理制度', '请假管理规定'], source: 'guard' }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp(chunks)))
    const { ask, messages } = useAsk()

    await ask('外派驻场怎么打卡')
    const ai = messages.value[1]
    expect(ai.noResult).toBe(true)
    expect(ai.answer).toContain('《考勤管理制度》')   // 流出的引导话术进 answer，不被吞
    expect(ai.suggestTitles).toEqual(['考勤管理制度', '请假管理规定'])
    expect(ai.rephrase).toEqual([])
    expect(ai.source).toBe('guard')
  })
})

describe('useAsk.ask — 通用回答（done 带 source，正常答案路径）', () => {
  it('source 落消息供徽标渲染，答案照常收尾', async () => {
    const chunks = [
      frame({ type: 'session', session_id: 's', message_id: 'm3g' }),
      frame({ type: 'chunk', content: 'Meeting tomorrow.\n\n> 以上为通用办公辅助内容，不代表公司口径。' }),
      frame({ type: 'done', model: 'qwen-turbo', usage: {}, no_result: false, source: 'general' }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp(chunks)))
    const { ask, messages } = useAsk()

    await ask('帮我翻译：明天开会')
    const ai = messages.value[1]
    expect(ai.source).toBe('general')
    expect(ai.noResult).toBeFalsy()
    expect(ai.html).toContain('Meeting tomorrow')
  })
})

describe('useAsk.ask — 流内 error 帧（替代 done）', () => {
  it('落错误态', async () => {
    const chunks = [
      frame({ type: 'session', session_id: 's', message_id: 'm4' }),
      frame({ type: 'chunk', content: '部分…' }),
      frame({ type: 'error', message: '回答生成失败，请联系管理员 (trace: abcd1234)' }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp(chunks)))
    const { ask, messages } = useAsk()

    await ask('触发错误')
    const ai = messages.value[1]
    expect(ai.error).toBe(true)
    expect(ai.errorText).toContain('回答生成失败')
  })
})

describe('useAsk.ask — HTTP 非 2xx（限流/检索失败，SSE 未开始）', () => {
  it('落错误卡可重试', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 429, text: async () => '请求过于频繁' }))
    const { ask, messages, asking } = useAsk()
    await ask('狂按')
    const ai = messages.value[1]
    expect(ai.error).toBe(true)
    expect(asking.value).toBe(false)
  })
})

describe('useAsk.vote — 乐观置态 + 失败回滚', () => {
  it('点赞成功保持；失败回滚', async () => {
    // 先跑一条正常回答拿到 messageId
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp([
      frame({ type: 'session', session_id: 's', message_id: 'mV' }),
      frame({ type: 'chunk', content: 'ok' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: false }), DONE,
    ])))
    const { ask, vote, messages } = useAsk()
    await ask('问')
    const ai = messages.value[1]

    // 点赞失败 → 回滚
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResp({ detail: 'x' }, { ok: false, status: 500 })))
    await vote(ai, 'upvote')
    expect(ai.voted).toBe('')

    // 点赞成功 → 保持，且二次点击不再请求
    const ok = vi.fn().mockResolvedValue(jsonResp({ status: 'ok', message_id: 'mV' }))
    vi.stubGlobal('fetch', ok)
    await vote(ai, 'upvote')
    expect(ai.voted).toBe('up')
    await vote(ai, 'downvote')   // 已投票 → 忽略
    expect(ai.voted).toBe('up')
    expect(ok).toHaveBeenCalledTimes(1)
  })
})

describe('useAsk.vote — 点踩原因载荷（downvote-only extra + 规范化防线）', () => {
  // 跑一条正常回答拿到带 messageId 的 AI 消息（多次调用取最新一条）
  async function askOnce() {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp([
      frame({ type: 'session', session_id: 's', message_id: 'mR' }),
      frame({ type: 'chunk', content: 'ok' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: false }), DONE,
    ])))
    const ctx = useAsk()
    await ctx.ask('问')
    return { vote: ctx.vote, ai: ctx.messages.value[ctx.messages.value.length - 1] }
  }
  const okFetch = () => vi.fn().mockResolvedValue(jsonResp({ status: 'ok' }))
  const sentBody = (fn: ReturnType<typeof vi.fn>) => JSON.parse(fn.mock.calls[0][1].body)

  it('reason+comment 都有：trim 后随载荷，成功返回 true', async () => {
    const { vote, ai } = await askOnce()
    const f = okFetch(); vi.stubGlobal('fetch', f)
    const ret = await vote(ai, 'downvote', { reason: 'inaccurate,not_found', comment: '  答案是旧版制度  ' })
    expect(ret).toBe(true)
    expect(ai.voted).toBe('down')
    expect(sentBody(f)).toEqual({
      message_id: 'mR', feedback_type: 'downvote',
      feedback_reason: 'inaccurate,not_found', feedback_comment: '答案是旧版制度',
    })
  })

  it('只勾原因 / 只填说明：空的一侧不发键', async () => {
    let got = await askOnce()
    let f = okFetch(); vi.stubGlobal('fetch', f)
    await got.vote(got.ai, 'downvote', { reason: 'outdated', comment: '   ' })
    expect(sentBody(f)).toEqual({ message_id: 'mR', feedback_type: 'downvote', feedback_reason: 'outdated' })

    got = await askOnce()
    f = okFetch(); vi.stubGlobal('fetch', f)
    await got.vote(got.ai, 'downvote', { reason: '', comment: '图挂了' })
    expect(sentBody(f)).toEqual({ message_id: 'mR', feedback_type: 'downvote', feedback_comment: '图挂了' })
  })

  it('comment 程序化超长 → 载荷截到 200 字（maxlength 之外的最终防线）', async () => {
    const { vote, ai } = await askOnce()
    const f = okFetch(); vi.stubGlobal('fetch', f)
    await vote(ai, 'downvote', { comment: '长'.repeat(300) })
    expect(sentBody(f).feedback_comment).toHaveLength(200)
  })

  it('upvote 防御性忽略 extra：原因永不进点赞载荷', async () => {
    const { vote, ai } = await askOnce()
    const f = okFetch(); vi.stubGlobal('fetch', f)
    const ret = await vote(ai, 'upvote', { reason: 'inaccurate', comment: '误传' })
    expect(ret).toBe(true)
    expect(sentBody(f)).toEqual({ message_id: 'mR', feedback_type: 'upvote' })
  })

  it('守卫路径显式返回 false：已锁票 / 无 messageId，均不发请求', async () => {
    const { vote, ai } = await askOnce()
    const f = okFetch(); vi.stubGlobal('fetch', f)
    expect(await vote(ai, 'downvote', { reason: 'outdated' })).toBe(true)
    expect(await vote(ai, 'downvote', { reason: 'outdated' })).toBe(false)   // 已锁票
    expect(f).toHaveBeenCalledTimes(1)

    const got = await askOnce()
    got.ai.messageId = undefined
    const f2 = okFetch(); vi.stubGlobal('fetch', f2)
    expect(await got.vote(got.ai, 'downvote', { reason: 'outdated' })).toBe(false)
    expect(f2).not.toHaveBeenCalled()
  })

  it('downvote 失败：回滚 + 返回 false（面板据此恢复）', async () => {
    const { vote, ai } = await askOnce()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResp({ detail: 'x' }, { ok: false, status: 500 })))
    expect(await vote(ai, 'downvote', { reason: 'inaccurate' })).toBe(false)
    expect(ai.voted).toBe('')
  })
})

describe('useAsk — 深度思考（parity-5）', () => {
  it('开启时请求体带 thinking:true；关闭时不带', async () => {
    const mk = () => streamResp([
      frame({ type: 'session', session_id: 's', message_id: 'm' }),
      frame({ type: 'chunk', content: 'x' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: false }), DONE,
    ])
    const fetchMock = vi.fn().mockResolvedValue(mk())
    vi.stubGlobal('fetch', fetchMock)
    const { ask, thinking } = useAsk()

    await ask('普通问')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).not.toHaveProperty('thinking')

    thinking.value = true
    fetchMock.mockResolvedValue(mk())
    await ask('深度问')
    const body = JSON.parse(fetchMock.mock.calls[1][1].body)
    expect(body).toMatchObject({ question: '深度问', thinking: true })
  })
})

describe('useAsk — 多会话（新建/切换/删除/搜索/标题）', () => {
  it('提问建会话并取首问为标题；新建切换；删除回退；搜索过滤', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp([
      frame({ type: 'session', session_id: 's', message_id: 'm' }),
      frame({ type: 'chunk', content: 'ok' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: false }), DONE,
    ])))
    const { ask, conversations, activeId, newConversation, switchTo, removeConversation, searchConversations, messages } = useAsk()

    await ask('年假怎么休')
    expect(conversations.value).toHaveLength(1)
    expect(conversations.value[0].title).toBe('年假怎么休')   // 标题取首问
    const firstId = activeId.value
    expect(messages.value.length).toBe(2)                    // 当前会话有 user+ai

    // 新建 → 空会话、切为激活
    newConversation()
    expect(conversations.value.length).toBe(2)
    expect(activeId.value).not.toBe(firstId)
    expect(messages.value).toEqual([])                       // 新会话空

    // 搜索按标题过滤
    expect(searchConversations('年假').map((c) => c.id)).toEqual([firstId])
    expect(searchConversations('不存在')).toEqual([])

    // 切回第一条
    switchTo(firstId)
    expect(activeId.value).toBe(firstId)
    expect(messages.value.length).toBe(2)

    // 删除当前 → 回退到剩余一条
    removeConversation(firstId)
    expect(conversations.value.some((c) => c.id === firstId)).toBe(false)
    expect(activeId.value).not.toBe(firstId)
  })
})

describe('useAsk.resetThread — 新会话（parity-6）', () => {
  it('清空线程 + 草稿（下次提问重建会话）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp([
      frame({ type: 'session', session_id: 's', message_id: 'm' }),
      frame({ type: 'chunk', content: 'hi' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: false }), DONE,
    ])))
    const { ask, resetThread, messages, draft } = useAsk()
    await ask('问一下')
    expect(messages.value.length).toBeGreaterThan(0)
    draft.value = '半句草稿'
    resetThread()
    expect(messages.value).toEqual([])
    expect(draft.value).toBe('')
  })
})

describe('useAsk.retry — 移除错误卡并用原问句重发', () => {
  it('错误后 retry 复用 question', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, text: async () => 'boom' }))
    const { ask, retry, messages } = useAsk()
    await ask('重试我')
    const bad = messages.value[1]
    expect(bad.error).toBe(true)

    // retry：移除错误卡，用 question 重发（这次成功）
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp([
      frame({ type: 'session', session_id: 's', message_id: 'mR' }),
      frame({ type: 'chunk', content: '好了' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: false }), DONE,
    ])))
    retry(bad)   // 内部 void ask(question, true)，不可直接 await
    const { asking } = useAsk()
    await waitFor(() => !asking.value && messages.value[messages.value.length - 1]?.html === '好了')
    // 用户气泡仍只有 1 条（skipUser），AI 卡重建为成功
    expect(messages.value.filter((m) => m.role === 'user')).toHaveLength(1)
    expect(messages.value[messages.value.length - 1].html).toContain('好了')
  })
})

describe('useAsk.ask — 深度思考过程帧（reasoning，披露条）', () => {
  it('reasoning 累积入 ai.reasoning + 渲染；答案开始即定稿收起；不污染答案 html', async () => {
    const chunks = [
      frame({ type: 'session', session_id: 's', message_id: 'mT' }),
      frame({ type: 'sources', sources: [{ doc_id: 'd1', title: '年假制度.pdf', section: '第3条', level: 'high', score: 9 }] }),
      frame({ type: 'reasoning', content: '先确认范围，' }),
      frame({ type: 'reasoning', content: '应查年假制度。' }),
      frame({ type: 'chunk', content: '每年' }),
      frame({ type: 'chunk', content: '5 天。' }),
      frame({ type: 'done', model: 'q', usage: {}, guard: false }),
      DONE,
    ]
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(streamResp(chunks)))
    const { ask, messages } = useAsk()

    await ask('年假几天')
    const ai = messages.value[1]
    expect(ai.reasoning).toBe('先确认范围，应查年假制度。')        // 思考全文累积
    expect(ai.reasoningHtml).toContain('应查年假制度')             // 已渲染
    expect(ai.reasoningOpen).toBe(false)                          // 答案开始 → 自动收起
    expect(ai.html).toContain('每年')                             // 答案正常
    expect(ai.html).toContain('5 天')
    expect(ai.html).not.toContain('应查年假制度')                  // 思考不混入答案
  })
})
