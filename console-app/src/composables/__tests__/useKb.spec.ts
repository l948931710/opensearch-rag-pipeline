import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useKb, __resetKb, __setSelectedFiles, type DocItem } from '@/composables/useKb'
import { useSession, type Role } from '@/stores/session'

function jsonResp(body: unknown, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => body, text: async () => JSON.stringify(body) }
}
async function waitFor(cond: () => boolean, ms = 1000) {
  const t0 = Date.now()
  while (!cond() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 5))
}

// 路由式 fetch mock：按 path 给响应（apiJson 走 apiFetch→fetch）。
function routeFetch(map: Record<string, any>) {
  return vi.fn(async (path: string) => {
    if (path.startsWith('/api/kb/my-docs')) return map.myDocs ?? jsonResp({ items: [], has_more: false })
    if (path.startsWith('/api/kb/upload-url')) return map.uploadUrl
    if (path.startsWith('/api/kb/register')) return map.register
    if (path.startsWith('/api/kb/doc-status')) return map.docStatus ?? jsonResp({ status_badge: '处理中', chunk_active: 0, error_message: '' })
    if (path.startsWith('/api/kb/pending-approvals')) return map.pending ?? jsonResp({ items: [] })
    if (path.startsWith('/api/kb/approve')) return map.approve ?? jsonResp({ status: 'ok', approved: 1 })
    if (path.startsWith('/api/kb/reject')) return map.reject ?? jsonResp({ status: 'ok', rejected: 1 })
    if (path.startsWith('/api/kb/retire')) return map.retire
    return jsonResp({}, { ok: false, status: 404 })
  })
}

// 直传 OSS 的 XHR 立即成功。
class FakeXHR {
  upload: any = {}
  status = 200
  timeout = 0
  onload: any = null; onerror: any = null; ontimeout: any = null
  open() {}
  send() { if (this.onload) this.onload() }
}

function setIdentity(role: Role, managed: string[]) {
  useSession().setIdentity({ userId: 'u', name: '张三', role, aclGroups: managed, canManage: role !== 'employee', managedOwnerDepts: managed })
}

beforeEach(() => {
  setActivePinia(createPinia())
  __resetKb()
  vi.restoreAllMocks()
  vi.stubGlobal('XMLHttpRequest', FakeXHR as any)
  useSession().setToken('TKN')
})

describe('useKb.loadDocs + 过滤/排序/计数', () => {
  it('载入后 filtered/countOf/sortBy 正确', async () => {
    const items: DocItem[] = [
      { doc_id: 'd1', title: 'B文档', original_filename: '', owner_dept: 'hr', permission_level: 'dept_internal', current_version_no: 2, status: 'active', status_badge: '已上线', updated_at: '2026-06-20 10:00' },
      { doc_id: 'd2', title: 'A文档', original_filename: '', owner_dept: 'hr', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '处理中', updated_at: '2026-06-22 09:00' },
      { doc_id: 'd3', title: 'C文档', original_filename: '', owner_dept: 'finance', permission_level: 'public', current_version_no: 5, status: 'active', status_badge: '已上线', updated_at: '2026-06-19 08:00' },
    ]
    vi.stubGlobal('fetch', routeFetch({ myDocs: jsonResp({ items, has_more: false }) }))
    const kb = useKb()
    await kb.loadDocs()
    expect(kb.docs.value).toHaveLength(3)

    // 计数
    expect(kb.countOf('')).toBe(3)
    expect(kb.countOf('已上线')).toBe(2)

    // 过滤
    kb.filter.value = '已上线'
    expect(kb.filtered.value.map((d) => d.doc_id).sort()).toEqual(['d1', 'd3'])
    kb.filter.value = ''

    // 排序：标题升序
    kb.sortBy('title')   // 非 updated_at → 升序
    expect(kb.filtered.value.map((d) => d.title)).toEqual(['A文档', 'B文档', 'C文档'])
    kb.sortBy('title')   // 再点 → 降序
    expect(kb.filtered.value.map((d) => d.title)).toEqual(['C文档', 'B文档', 'A文档'])

    // 版本号按数值排序（非字符串）
    kb.sortBy('current_version_no')
    expect(kb.filtered.value.map((d) => d.current_version_no)).toEqual([1, 2, 5])
  })
})

describe('useKb 上传（两段式：upload-url → PUT → register）', () => {
  it('单文件新建成功：进度→已提交，含内容查重提示', async () => {
    vi.stubGlobal('fetch', routeFetch({
      uploadUrl: jsonResp({ upload_token: 'UT', put_url: 'https://oss/x', raw_key: 'raw/hr/d/u/a.pdf', doc_id: 'DOC_X', expires_in: 1800, requires_kb_admin_approval: false }),
      register: jsonResp({ doc_id: 'DOC_X', version_no: 1, content_process_status: 'NOT_STARTED', requires_kb_admin_approval: false, status_badge: '排队中', idempotent: false, title: '年假制度', content_dups: [{ doc_id: 'd9', title: '旧年假', owner_dept: 'hr' }], content_dups_other: 1 }),
      myDocs: jsonResp({ items: [], has_more: false }),
    }))
    const kb = useKb()
    setIdentity('dept_admin', ['hr'])
    kb.newOwner.value = 'hr'
    __setSelectedFiles([new File([new Uint8Array(20)], 'a.pdf', { type: 'application/pdf' })])

    kb.doUpload()
    await waitFor(() => kb.uploadOk.value === true)
    expect(kb.uploadMsg.value).toContain('已提交')
    expect(kb.uploadMsg.value).toContain('年假制度 v1')
    expect(kb.contentDupMsg.value).toContain('《旧年假》')
    expect(kb.contentDupMsg.value).toContain('另有 1 篇')
  })

  it('空文件被客户端预检拦下（不发任何请求）', async () => {
    const fetchMock = routeFetch({})
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('dept_admin', ['hr'])
    kb.newOwner.value = 'hr'
    __setSelectedFiles([new File([], 'empty.pdf')])

    kb.doUpload()
    await waitFor(() => !!kb.uploadErr.value)
    expect(kb.uploadErr.value).toContain('为空')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('幂等命中：register idempotent=true 也照常展示徽章（无报错）', async () => {
    vi.stubGlobal('fetch', routeFetch({
      uploadUrl: jsonResp({ upload_token: 'UT', put_url: 'https://oss/x', raw_key: 'r', doc_id: 'DOC_Y', expires_in: 1800, requires_kb_admin_approval: false }),
      register: jsonResp({ doc_id: 'DOC_Y', version_no: 3, content_process_status: 'SKIPPED_DUPLICATE', requires_kb_admin_approval: false, status_badge: '内容未变', idempotent: true, title: 'x', content_dups: [], content_dups_other: 0 }),
    }))
    const kb = useKb()
    setIdentity('dept_admin', ['hr'])
    kb.newOwner.value = 'hr'
    __setSelectedFiles([new File([new Uint8Array(5)], 'b.pdf')])
    kb.doUpload()
    await waitFor(() => kb.uploadOk.value === true)
    expect(kb.uploadMsg.value).toContain('内容未变')
    expect(kb.uploadErr.value).toBe('')
  })
})

describe('useKb.applyPendingVersion — 升版深链落地（parity-1/3）', () => {
  it('命中已加载文档 → 进升版态（继承该行）', async () => {
    const d: DocItem = { doc_id: 'd1', title: '年假制度', original_filename: '', owner_dept: 'hr', permission_level: 'dept_internal', current_version_no: 2, status: 'active', status_badge: '已上线', updated_at: '' }
    vi.stubGlobal('fetch', routeFetch({ myDocs: jsonResp({ items: [d], has_more: false }) }))
    const kb = useKb()
    await kb.loadDocs()
    kb.applyPendingVersion({ docId: 'd1', owner: 'hr', title: '年假制度' })
    expect(kb.verCtx.value).toMatchObject({ doc_id: 'd1', owner_dept: 'hr', permission_level: 'dept_internal', current_version_no: 2 })
  })

  it('列表外文档（>50/旧）→ 合成 verCtx，perm 留空交后端继承', () => {
    const kb = useKb()
    kb.applyPendingVersion({ docId: 'DOC_OLD', owner: 'finance', title: '历史制度' })
    expect(kb.verCtx.value).toMatchObject({ doc_id: 'DOC_OLD', owner_dept: 'finance', title: '历史制度', permission_level: '', current_version_no: 0 })
  })
})

describe('useKb.retire', () => {
  it('成功 → 行徽章变已退役', async () => {
    const d: DocItem = { doc_id: 'd1', title: 'x', original_filename: '', owner_dept: 'hr', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '' }
    vi.stubGlobal('fetch', routeFetch({ retire: jsonResp({ status: 'ok', retired: true, already: false, status_badge: '已退役', note: 'ok' }), myDocs: jsonResp({ items: [d], has_more: false }) }))
    const kb = useKb()
    const r = await kb.retire(d)
    expect(r.ok).toBe(true)
    expect(d.status_badge).toBe('已退役')
  })

  it('403（公开文档需 kb_admin）→ 返回失败 + detail', async () => {
    const d: DocItem = { doc_id: 'd2', title: 'pub', original_filename: '', owner_dept: 'hr', permission_level: 'public', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '' }
    vi.stubGlobal('fetch', routeFetch({ retire: jsonResp({ detail: '公开文档需知识库管理员退役' }, { ok: false, status: 403 }) }))
    const kb = useKb()
    const r = await kb.retire(d)
    expect(r.ok).toBe(false)
    expect(r.msg).toContain('公开文档')
    expect(d.status_badge).toBe('已上线')   // 未误改
  })
})

describe('useKb.loadApprovals — 仅 kb_admin', () => {
  it('employee/dept_admin 不拉审批队列', async () => {
    const fetchMock = routeFetch({ pending: jsonResp({ items: [{ doc_id: 'p1', version_no: 1 }] }) })
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('dept_admin', ['hr'])
    await kb.loadApprovals()
    expect(kb.approvals.value).toEqual([])
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('kb_admin 拉到队列', async () => {
    vi.stubGlobal('fetch', routeFetch({ pending: jsonResp({ items: [{ doc_id: 'p1', version_no: 1, title: '公开件', owner_dept: 'hr', permission_level: 'public', owner_name: '李四', created_at: '', original_filename: '' }] }) }))
    const kb = useKb()
    setIdentity('kb_admin', ['hr', 'finance'])
    await kb.loadApprovals()
    expect(kb.approvals.value).toHaveLength(1)
    expect(kb.approvals.value[0].title).toBe('公开件')
  })
})
