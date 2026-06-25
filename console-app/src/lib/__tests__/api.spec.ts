import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { apiFetch, apiJson, ApiError, setReauthHandler } from '@/lib/api'
import { useSession } from '@/stores/session'

function jsonRes(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

beforeEach(() => {
  setActivePinia(createPinia())
  setReauthHandler(null)
  vi.restoreAllMocks()
})

describe('apiFetch — Bearer + auth 开关', () => {
  it('有 token 时附带 Authorization: Bearer', async () => {
    useSession().setToken('TKN')
    const fetchMock = vi.fn().mockResolvedValue(jsonRes({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    await apiFetch('/api/x')
    const headers = fetchMock.mock.calls[0][1].headers as Headers
    expect(headers.get('Authorization')).toBe('Bearer TKN')
  })

  it('auth:false 时不带 Authorization（匿名端点）', async () => {
    useSession().setToken('TKN')
    const fetchMock = vi.fn().mockResolvedValue(jsonRes({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    await apiFetch('/api/hot-questions', { auth: false })
    const headers = fetchMock.mock.calls[0][1].headers as Headers
    expect(headers.has('Authorization')).toBe(false)
  })

  it('有 body 自动补 Content-Type: application/json', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonRes({}))
    vi.stubGlobal('fetch', fetchMock)
    await apiFetch('/api/x', { method: 'POST', body: JSON.stringify({ a: 1 }) })
    expect((fetchMock.mock.calls[0][1].headers as Headers).get('Content-Type')).toBe('application/json')
  })
})

describe('apiFetch — 401 重登一次', () => {
  it('401 → reauth 成功 → 用新 token 重试一次并返回 200', async () => {
    const session = useSession()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonRes({ detail: 'expired' }, 401))
      .mockResolvedValueOnce(jsonRes({ ok: true }, 200))
    vi.stubGlobal('fetch', fetchMock)
    setReauthHandler(async () => { session.setToken('NEW'); return true })

    const res = await apiFetch('/api/x')
    expect(res.status).toBe(200)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    // 重试用的是新 token
    expect((fetchMock.mock.calls[1][1].headers as Headers).get('Authorization')).toBe('Bearer NEW')
  })

  it('401 → reauth 失败 → 不重试，直接返回 401', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonRes({ detail: 'expired' }, 401))
    vi.stubGlobal('fetch', fetchMock)
    setReauthHandler(async () => false)
    const res = await apiFetch('/api/x')
    expect(res.status).toBe(401)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('重试后仍 401 → 不再重试（防无限循环）', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonRes({ detail: 'expired' }, 401))
    vi.stubGlobal('fetch', fetchMock)
    const reauth = vi.fn().mockResolvedValue(true)
    setReauthHandler(reauth)
    const res = await apiFetch('/api/x')
    expect(res.status).toBe(401)
    expect(fetchMock).toHaveBeenCalledTimes(2) // 原始 + 一次重试，到此为止
    expect(reauth).toHaveBeenCalledTimes(1)
  })
})

describe('apiJson — 错误抛 ApiError', () => {
  it('非 2xx 抛 ApiError，带 status 与后端 detail', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonRes({ detail: '无权操作' }, 403)))
    setReauthHandler(null)
    await expect(apiJson('/api/kb/x')).rejects.toMatchObject({ status: 403, detail: '无权操作' })
    await expect(apiJson('/api/kb/x')).rejects.toBeInstanceOf(ApiError)
  })

  it('2xx 返回解析后的 JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonRes({ questions: ['a'] })))
    const data = await apiJson<{ questions: string[] }>('/api/hot-questions', { auth: false })
    expect(data.questions).toEqual(['a'])
  })
})
