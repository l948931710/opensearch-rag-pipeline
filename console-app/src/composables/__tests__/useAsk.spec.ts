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
