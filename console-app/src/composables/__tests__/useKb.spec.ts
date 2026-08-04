import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useKb, __resetKb, __setSelectedFiles, type DocItem } from '@/composables/useKb'
import { useDialog } from '@/composables/useDialog'
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
  return vi.fn(async (path: string, init?: any) => {
    if (path.startsWith('/api/kb/my-docs')) return map.myDocs ?? jsonResp({ items: [], has_more: false })
    if (path.startsWith('/api/kb/upload-url')) return map.uploadUrl
    if (path.startsWith('/api/kb/register')) return map.register
    if (path.startsWith('/api/kb/doc-status')) return map.docStatus ?? jsonResp({ status_badge: '处理中', chunk_active: 0, error_message: '' })
    if (path.startsWith('/api/kb/pending-approvals')) return map.pending ?? jsonResp({ items: [] })
    if (path.startsWith('/api/kb/approve')) return map.approve ?? jsonResp({ status: 'ok', approved: 1 })
    if (path.startsWith('/api/kb/reject')) return map.reject ?? jsonResp({ status: 'ok', rejected: 1 })
    if (path.startsWith('/api/kb/retire')) return map.retire
    if (path.startsWith('/api/kb/access-grants')) {
      // 同路径双动词：POST=主动共享（直插 approved）；GET=已授权清单
      if (init?.method === 'POST') return map.grantCreate ?? jsonResp({ doc_id: 'D', granted: [], skipped: [], ok: true })
      return map.grants ?? jsonResp({ items: [] })
    }
    if (path.startsWith('/api/kb/set-visibility')) return map.setVis ?? jsonResp({ doc_id: 'D', permission_level: 'restricted', changed: true })
    if (path.startsWith('/api/kb/visibility-explain')) return map.visExplain ?? jsonResp({}, { ok: false, status: 404 })
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

describe('useKb 分页（has_more / loadMoreDocs）', () => {
  function pageItem(id: string): DocItem {
    return { doc_id: id, title: id, original_filename: '', owner_dept: 'hr', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-06-20 10:00' }
  }

  it('首屏消费 has_more；loadMoreDocs 追加下一页且 offset 累进；末页隐藏', async () => {
    const calls: string[] = []
    const page1 = Array.from({ length: 3 }, (_, i) => pageItem(`a${i}`))
    const page2 = Array.from({ length: 2 }, (_, i) => pageItem(`b${i}`))
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      calls.push(path)
      if (path.startsWith('/api/kb/my-docs')) {
        if (path.includes('offset=0')) return jsonResp({ items: page1, has_more: true })
        if (path.includes('offset=50')) return jsonResp({ items: page2, has_more: false })
      }
      return jsonResp({}, { ok: false, status: 404 })
    }))
    const kb = useKb()
    await kb.loadDocs()
    expect(kb.docs.value).toHaveLength(3)
    expect(kb.hasMoreDocs.value).toBe(true)

    await kb.loadMoreDocs()
    expect(kb.docs.value.map((d) => d.doc_id)).toEqual(['a0', 'a1', 'a2', 'b0', 'b1'])   // 追加不覆盖
    expect(kb.hasMoreDocs.value).toBe(false)                                              // 末页
    expect(calls.some((c) => c.includes('offset=50'))).toBe(true)                          // offset 累进

    await kb.loadMoreDocs()   // hasMore=false → no-op，不再请求第三页
    expect(kb.docs.value).toHaveLength(5)
  })

  it('loadDocs 重置 offset：翻页后重载从第一页起、hasMore 重判', async () => {
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (path.startsWith('/api/kb/my-docs')) {
        if (path.includes('offset=0')) return jsonResp({ items: [pageItem('x0')], has_more: false })
        if (path.includes('offset=50')) return jsonResp({ items: [pageItem('x1')], has_more: false })
      }
      return jsonResp({}, { ok: false, status: 404 })
    }))
    const kb = useKb()
    await kb.loadDocs()
    kb.hasMoreDocs.value = true       // 模拟仍有下一页
    await kb.loadMoreDocs()
    expect(kb.docs.value).toHaveLength(2)
    await kb.loadDocs()               // 重载 → offset 归 0、列表重置
    expect(kb.docs.value.map((d) => d.doc_id)).toEqual(['x0'])
    expect(kb.hasMoreDocs.value).toBe(false)
  })
})

describe('useKb 加载错误态（404 静默 / 5xx 显错 + 重试清除）', () => {
  it('my-docs 5xx → loadErrors.docs 置；404 → 静默空', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ detail: 'boom' }, { ok: false, status: 500 })))
    const kb = useKb()
    await kb.loadDocs()
    expect(kb.loadErrors.value.docs).toBeTruthy()

    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ detail: 'not found' }, { ok: false, status: 404 })))
    await kb.loadDocs()
    expect(kb.loadErrors.value.docs).toBeUndefined()   // 404（端点未上线）静默，不当作错误
  })

  it('governance 5xx → loadErrors.governance；重试成功后清除', async () => {
    setIdentity('kb_admin', ['hr'])
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({}, { ok: false, status: 503 })))
    const kb = useKb()
    await kb.loadGovernance()
    expect(kb.loadErrors.value.governance).toBeTruthy()

    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ window_days: 30, dept_coverage: [] }, { ok: true, status: 200 })))
    await kb.loadGovernance()
    expect(kb.loadErrors.value.governance).toBeUndefined()   // 成功 → 清错误条
  })

  it('access-requests 404（Phase C 未上线）→ 静默空、不置错误', async () => {
    setIdentity('dept_admin', ['hr'])
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({}, { ok: false, status: 404 })))
    const kb = useKb()
    await kb.loadAccessRequests()
    expect(kb.accessRequests.value).toEqual([])
    expect(kb.loadErrors.value.accessRequests).toBeUndefined()
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

describe('useKb 台账筛选 + 多选 + 批量', () => {
  const mk = (id: string, owner: string, perm: string, badge = '已上线', can = true): DocItem => ({
    doc_id: id, title: id, original_filename: '', owner_dept: owner, permission_level: perm,
    current_version_no: 1, status: 'active', status_badge: badge, updated_at: '2026-07-04 10:00', can_manage: can,
  })

  it('permFilter / ownerFilter 叠加过滤；ownerOptions 去重含子线', async () => {
    const items = [
      mk('a', 'production', 'dept_internal'), mk('b', 'production_mold', 'public'),
      mk('c', 'production', 'restricted'), mk('d', 'finance', 'dept_internal'),
    ]
    vi.stubGlobal('fetch', routeFetch({ myDocs: jsonResp({ items, has_more: false }) }))
    const kb = useKb()
    await kb.loadDocs()
    expect(kb.ownerOptions.value).toEqual(['finance', 'production', 'production_mold'])
    kb.ownerFilter.value = 'production'
    expect(kb.filtered.value.map((d) => d.doc_id).sort()).toEqual(['a', 'c'])
    kb.permFilter.value = 'restricted'
    expect(kb.filtered.value.map((d) => d.doc_id)).toEqual(['c'])   // 叠加
  })

  it('多选：toggle / 全选可见 / 隐藏行不参与；筛选后 selectedDocs 收敛', async () => {
    const items = [mk('a', 'production', 'dept_internal'), mk('b', 'production', 'public'), mk('x', 'hr', 'dept_internal', '已上线', false)]
    vi.stubGlobal('fetch', routeFetch({ myDocs: jsonResp({ items, has_more: false }) }))
    const kb = useKb()
    await kb.loadDocs()
    kb.toggleSelectAllVisible()
    expect(kb.selectedCount.value).toBe(2)                 // 外部门只读行 x 不计入
    expect(kb.allVisibleSelected.value).toBe(true)
    kb.permFilter.value = 'public'                         // 只剩 b 可见 → 选中收敛到 1
    expect(kb.selectedDocs.value.map((d) => d.doc_id)).toEqual(['b'])
    kb.permFilter.value = ''
    kb.toggleSelect('a')                                    // 取消 a
    expect(kb.selectedDocs.value.map((d) => d.doc_id)).toEqual(['b'])
  })

  it('bulkRetire：逐篇 POST retire + 清空选中 + 重拉', async () => {
    const items = [mk('a', 'production', 'dept_internal'), mk('b', 'production', 'dept_internal')]
    const calls: string[] = []
    const fetchMock = vi.fn(async (path: string) => {
      calls.push(path)
      if (path.startsWith('/api/kb/my-docs')) return jsonResp({ items, has_more: false })
      if (path.startsWith('/api/kb/retire')) return jsonResp({ retired: true })
      if (path.startsWith('/api/kb/pending-approvals')) return jsonResp({ items: [] })
      return jsonResp({}, { ok: false, status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('kb_admin', ['production'])
    await kb.loadDocs()
    kb.toggleSelectAllVisible()
    await kb.bulkRetire()
    expect(calls.filter((p) => p.startsWith('/api/kb/retire')).length).toBe(2)
    expect(kb.selectedCount.value).toBe(0)                 // 成功后清空
    expect(kb.bulkMsg.value).toContain('成功 2')
  })

  it('citedFilter：never=cited_count===0，used=有引用，数据不可用(null)两档都不入', async () => {
    const items = [
      { ...mk('a', 'production', 'dept_internal'), cited_count: 0 },
      { ...mk('b', 'production', 'dept_internal'), cited_count: 7 },
      mk('c', 'production', 'dept_internal'),                          // cited_count 缺省=不可用
    ]
    vi.stubGlobal('fetch', routeFetch({ myDocs: jsonResp({ items, has_more: false }) }))
    const kb = useKb()
    await kb.loadDocs()
    kb.citedFilter.value = 'never'
    expect(kb.filtered.value.map((d) => d.doc_id)).toEqual(['a'])
    kb.citedFilter.value = 'used'
    expect(kb.filtered.value.map((d) => d.doc_id)).toEqual(['b'])
    kb.citedFilter.value = ''
    expect(kb.filtered.value.length).toBe(3)
  })

  it('bulkSetVisibility：只对级别不同的行发 set-visibility', async () => {
    const items = [mk('a', 'production', 'dept_internal'), mk('b', 'production', 'restricted')]
    const posted: any[] = []
    const fetchMock = vi.fn(async (path: string, init?: any) => {
      if (path.startsWith('/api/kb/my-docs')) return jsonResp({ items, has_more: false })
      if (path.startsWith('/api/kb/set-visibility')) { posted.push(JSON.parse(init.body)); return jsonResp({ changed: true }) }
      return jsonResp({}, { ok: false, status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('kb_admin', ['production'])
    await kb.loadDocs()
    kb.toggleSelectAllVisible()
    await kb.bulkSetVisibility('restricted')
    expect(posted).toHaveLength(1)                          // b 已是 restricted → 跳过；只发 a
    expect(posted[0]).toMatchObject({ doc_id: 'a', permission_level: 'restricted' })
  })

  it('bulkSetVisibility：批中不逐篇重拉列表，批末仅一次权威 loadDocs（N+1 防御）', async () => {
    const items = [mk('a', 'production', 'dept_internal'), mk('b', 'production', 'dept_internal')]
    const calls: string[] = []
    const fetchMock = vi.fn(async (path: string) => {
      calls.push(String(path))
      if (String(path).startsWith('/api/kb/my-docs')) return jsonResp({ items, has_more: false })
      if (String(path).startsWith('/api/kb/set-visibility')) return jsonResp({ changed: true })
      return jsonResp({}, { ok: false, status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('kb_admin', ['production'])
    await kb.loadDocs()
    kb.toggleSelectAllVisible()
    await kb.bulkSetVisibility('restricted')
    expect(calls.filter((p) => p.startsWith('/api/kb/set-visibility')).length).toBe(2)
    await waitFor(() => calls.filter((p) => p.startsWith('/api/kb/my-docs')).length === 2)
    // 初始 loadDocs 1 次 + 批末权威 1 次；逐篇 setVisibility 不再各自触发（旧行为=4 次）
    expect(calls.filter((p) => p.startsWith('/api/kb/my-docs')).length).toBe(2)
  })
})

describe('useKb 主动共享（多部门可见度）', () => {
  const doc: DocItem = { doc_id: 'D1', title: '营销规范', original_filename: 'g.pdf', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-07-03 10:00', can_manage: true }

  it('submitShare 成功：POST target_depts → 关闭弹窗并刷新已授权清单', async () => {
    const fetchMock = routeFetch({
      grantCreate: jsonResp({ doc_id: 'D1', granted: ['hr', 'rd'], skipped: [], ok: true }),
      grants: jsonResp({ items: [{ id: 'g1', doc_id: 'D1', doc_title: '营销规范', owner_dept: 'marketing', requester_dept: 'hr', requester_name: '张三', permission_level: 'dept_internal', reason: '管理员主动共享', decided_at: '2026-07-03' }] }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('dept_admin', ['marketing'])
    kb.openShare(doc)
    expect(kb.shareCtx.value?.doc_id).toBe('D1')
    const err = await kb.submitShare(['hr', 'rd'], '巡检需要')
    expect(err).toBeNull()
    expect(kb.shareCtx.value).toBeNull()                       // 成功即关闭
    const post = fetchMock.mock.calls.find(([p, i]) => String(p).startsWith('/api/kb/access-grants') && i?.method === 'POST')!
    expect(JSON.parse(post[1].body)).toEqual({ doc_id: 'D1', target_depts: ['hr', 'rd'], reason: '巡检需要' })
    await waitFor(() => kb.accessGrants.value.length === 1)    // 成功后刷新清单
  })

  it('submitShare 403 → 返回错误文案、弹窗保持打开', async () => {
    vi.stubGlobal('fetch', routeFetch({
      grantCreate: jsonResp({ detail: '无权共享该文档（非文档所属部门管理员）' }, { ok: false, status: 403 }),
    }))
    const kb = useKb()
    setIdentity('dept_admin', ['marketing'])
    kb.openShare(doc)
    const err = await kb.submitShare(['hr'], '')
    expect(err).toContain('无权共享')
    expect(kb.shareCtx.value?.doc_id).toBe('D1')               // 不关闭，可改重试
  })

  it('grantedDeptsOf：按 doc 聚合 approved 行（含被动流 CSV）去重排序', async () => {
    vi.stubGlobal('fetch', routeFetch({
      grants: jsonResp({ items: [
        { id: 'g1', doc_id: 'D1', doc_title: 'x', owner_dept: 'marketing', requester_dept: 'production,quality', requester_name: 'a', permission_level: 'dept_internal', reason: '', decided_at: '' },
        { id: 'g2', doc_id: 'D1', doc_title: 'x', owner_dept: 'marketing', requester_dept: 'hr', requester_name: 'b', permission_level: 'dept_internal', reason: '', decided_at: '' },
        { id: 'g3', doc_id: 'D2', doc_title: 'y', owner_dept: 'marketing', requester_dept: 'rd', requester_name: 'c', permission_level: 'dept_internal', reason: '', decided_at: '' },
      ] }),
    }))
    const kb = useKb()
    setIdentity('dept_admin', ['marketing'])
    await kb.loadAccessGrants()
    expect(kb.grantedDeptsOf('D1')).toEqual(['hr', 'production', 'quality'])
    expect(kb.docGrantRows('D1').map((g) => g.id)).toEqual(['g1', 'g2'])
    expect(kb.grantedDeptsOf('D9')).toEqual([])
  })

  it('setVisibility 成功：POST 目标级别 + 乐观改行 + 重拉列表', async () => {
    const fetchMock = routeFetch({
      setVis: jsonResp({ doc_id: 'D1', permission_level: 'public', changed: true }),
      myDocs: jsonResp({ items: [], has_more: false }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('kb_admin', ['marketing'])
    const err = await kb.setVisibility(doc, 'public', '开放给全公司')
    expect(err).toBeNull()
    expect(doc.permission_level).toBe('public')                 // 乐观即时反映
    const post = fetchMock.mock.calls.find(([p]) => String(p).startsWith('/api/kb/set-visibility'))!
    expect(JSON.parse(post[1].body)).toEqual({ doc_id: 'D1', permission_level: 'public', reason: '开放给全公司' })
  })

  it('setVisibility 403 → 返回文案、不改本地级别', async () => {
    const d2: DocItem = { ...doc, doc_id: 'D9', permission_level: 'dept_internal' }
    vi.stubGlobal('fetch', routeFetch({
      setVis: jsonResp({ detail: '涉及全公司公开的可见范围变更需知识库管理员操作' }, { ok: false, status: 403 }),
    }))
    const kb = useKb()
    setIdentity('dept_admin', ['marketing'])
    const err = await kb.setVisibility(d2, 'public')
    expect(err).toContain('知识库管理员')
    expect(d2.permission_level).toBe('dept_internal')           // 失败不动
  })

  it('上传「指定部门」模式：登记走 dept_internal，随后 POST 共享所选部门', async () => {
    const fetchMock = routeFetch({
      uploadUrl: jsonResp({ upload_token: 'UT', put_url: 'https://oss/x', raw_key: 'r', doc_id: 'DOC_S', expires_in: 1800, requires_kb_admin_approval: false }),
      register: jsonResp({ doc_id: 'DOC_S', version_no: 1, content_process_status: 'NOT_STARTED', requires_kb_admin_approval: false, status_badge: '排队中', idempotent: false, title: '工艺卡', content_dups: [], content_dups_other: 0 }),
      grantCreate: jsonResp({ doc_id: 'DOC_S', granted: ['rd', 'quality'], skipped: [], ok: true }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('dept_admin', ['production'])
    kb.newOwner.value = 'production'
    kb.newPerm.value = 'shared'
    kb.newShareDepts.value = ['rd', 'quality']
    __setSelectedFiles([new File([new Uint8Array(8)], 'p.pdf')])
    kb.doUpload()
    await waitFor(() => kb.uploadOk.value === true)
    const up = fetchMock.mock.calls.find(([p]) => String(p).startsWith('/api/kb/upload-url'))!
    expect(JSON.parse(up[1].body).permission_level).toBe('dept_internal')   // 登记基线仍是 dept_internal
    const post = fetchMock.mock.calls.find(([p, i]) => String(p).startsWith('/api/kb/access-grants') && i?.method === 'POST')!
    expect(JSON.parse(post[1].body)).toMatchObject({ doc_id: 'DOC_S', target_depts: ['rd', 'quality'] })
    expect(kb.uploadMsg.value).toContain('已共享 2 部门')
  })

  it('openVisibility：拉取解释并落 visExplain；403 → visErr 文案', async () => {
    const explain = {
      doc_id: 'D1', owner_dept: 'production_mold', permission_level: 'dept_internal',
      everyone: false, nobody: false, quarantined: false, active: true,
      readers: [{ dept: 'production', via: 'umbrella' }, { dept: 'marketing', via: 'shared_policy' }],
    }
    vi.stubGlobal('fetch', routeFetch({ visExplain: jsonResp(explain) }))
    const kb = useKb()
    setIdentity('kb_admin', ['production'])
    await kb.openVisibility({ ...doc, doc_id: 'D1', owner_dept: 'production_mold' })
    expect(kb.visCtx.value?.doc_id).toBe('D1')
    expect(kb.visExplain.value?.readers.map((r) => r.via)).toEqual(['umbrella', 'shared_policy'])
    kb.closeVisibility()
    expect(kb.visCtx.value).toBeNull()
    expect(kb.visExplain.value).toBeNull()
    // 403：错误进 visErr（弹窗内联显示，可重试）
    vi.stubGlobal('fetch', routeFetch({ visExplain: jsonResp({ detail: '无权查看该文档的可见范围明细' }, { ok: false, status: 403 }) }))
    await kb.openVisibility(doc)
    expect(kb.visErr.value).toContain('无权查看')
    expect(kb.visExplain.value).toBeNull()
    kb.closeVisibility()
  })

  it('submitShare 全部目标被跳过（伞组/共享面覆盖）→ 不关弹窗、如实提示无新增授权', async () => {
    vi.stubGlobal('fetch', routeFetch({
      grantCreate: jsonResp({ doc_id: 'D1', granted: [], skipped: ['production'], ok: true }),
    }))
    const kb = useKb()
    setIdentity('kb_admin', ['production'])
    kb.openShare(doc)
    const msg = await kb.submitShare(['production'], '')
    expect(msg).toContain('无需新增授权')
    expect(kb.shareCtx.value?.doc_id).toBe('D1')     // 不关闭——用户须知道没写任何行
    kb.closeShare()
  })

  it('共享目标全被后端跳过（已覆盖/伞下冗余）→ 文案如实，不谎报「已共享」', async () => {
    const fetchMock = routeFetch({
      uploadUrl: jsonResp({ upload_token: 'UT', put_url: 'https://oss/x', raw_key: 'r', doc_id: 'DOC_S', expires_in: 1800, requires_kb_admin_approval: false }),
      register: jsonResp({ doc_id: 'DOC_S', version_no: 1, content_process_status: 'NOT_STARTED', requires_kb_admin_approval: false, status_badge: '排队中', idempotent: false, title: '工艺卡', content_dups: [], content_dups_other: 0 }),
      grantCreate: jsonResp({ doc_id: 'DOC_S', granted: [], skipped: ['rd', 'quality'], ok: true }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('dept_admin', ['production'])
    kb.newOwner.value = 'production'
    kb.newPerm.value = 'shared'
    kb.newShareDepts.value = ['rd', 'quality']
    __setSelectedFiles([new File([new Uint8Array(8)], 'p.pdf')])
    kb.doUpload()
    await waitFor(() => kb.uploadOk.value === true)
    expect(kb.uploadMsg.value).toContain('已覆盖')            // granted=0：以响应为准
    expect(kb.uploadMsg.value).not.toContain('已共享')
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

describe('useKb staleness 门（#82）— 预载后 30s 内不重拉', () => {
  const P1 = { doc_id: 'p1', version_no: 1, title: '公开件', owner_dept: 'hr', permission_level: 'public', owner_name: '李四', created_at: '', original_filename: '' }

  it('二次 loadApprovals 跳过（App ready 预载 → ManageView 挂载不重拉）；force 逃生', async () => {
    const fetchMock = routeFetch({ pending: jsonResp({ items: [P1] }) })
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    setIdentity('kb_admin', ['hr'])
    await kb.loadApprovals()            // App ready 预载
    await kb.loadApprovals()            // ManageView onMounted（非 force）→ 门内跳过
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(kb.approvals.value).toHaveLength(1)
    await kb.loadApprovals(true)        // force 逃生 → 真重拉
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('加载失败重开门：LoadError 重试（非 force）能真重拉', async () => {
    setIdentity('kb_admin', ['hr'])
    vi.stubGlobal('fetch', vi.fn(async () => jsonResp({ detail: 'boom' }, { ok: false, status: 500 })))
    const kb = useKb()
    await kb.loadApprovals()
    expect(kb.loadErrors.value.approvals).toBeTruthy()
    vi.stubGlobal('fetch', routeFetch({ pending: jsonResp({ items: [P1] }) }))
    await kb.loadApprovals()            // 失败已清时间戳 → 非 force 也重拉
    expect(kb.approvals.value).toHaveLength(1)
  })

  it('loadAccessRequests 同门：二次跳过、force 重拉', async () => {
    setIdentity('dept_admin', ['hr'])
    const fetchMock = vi.fn(async () => jsonResp({ items: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const kb = useKb()
    await kb.loadAccessRequests()
    await kb.loadAccessRequests()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await kb.loadAccessRequests(true)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

describe('useKb.approve/reject — 审批后定向更新（#82）', () => {
  const P = (id: string) => ({ doc_id: id, version_no: 2, title: id, original_filename: '', owner_dept: 'hr', permission_level: 'public', owner_name: '李四', created_at: '' })

  it('approve 成功：本地移除该单（不重拉审批队列）+ 只刷一次文档列表；reviewCount 同步', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      calls.push(path)
      if (path.startsWith('/api/kb/approve')) return jsonResp({ status: 'ok', approved: 1 })
      if (path.startsWith('/api/kb/my-docs')) return jsonResp({ items: [], has_more: false })
      return jsonResp({}, { ok: false, status: 404 })
    }))
    const kb = useKb()
    setIdentity('kb_admin', ['hr'])
    ;(kb as any).approvals.value = [P('p1'), P('p2')]
    await kb.approve(P('p1') as any)
    expect(kb.approvals.value.map((x) => x.doc_id)).toEqual(['p2'])                          // 本地移除
    expect(kb.reviewCount.value).toBe(1)                                                     // 红点/角标同步
    expect(calls.filter((c) => c.startsWith('/api/kb/pending-approvals'))).toHaveLength(0)   // 不再全量重拉队列
    expect(calls.filter((c) => c.startsWith('/api/kb/my-docs'))).toHaveLength(1)             // 保留一次权威刷新
  })

  it('reject 失败：notice 告知框（danger）、该单保留可重试（现状回退）', async () => {
    const { dialog, onConfirm } = useDialog()
    dialog.value.open = false                      // 防其他用例遗留态假阳性
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (path.startsWith('/api/kb/reject')) return jsonResp({ detail: 'boom' }, { ok: false, status: 500 })
      return jsonResp({}, { ok: false, status: 404 })
    }))
    const kb = useKb()
    setIdentity('kb_admin', ['hr'])
    ;(kb as any).approvals.value = [P('p1')]
    await kb.reject(P('p1') as any, '理由')
    expect(kb.approvals.value).toHaveLength(1)     // 失败不动队列
    expect(dialog.value.open).toBe(true)           // 应用内告知框（不再用原生 alert）
    expect(dialog.value.kind).toBe('notice')
    expect(dialog.value.title).toBe('驳回失败')
    expect(dialog.value.danger).toBe(true)
    onConfirm()                                    // 收尾关闭，不向后续用例泄漏打开态
  })

  it('approveAccess 成功：本地移除该单 + 只刷「已授权清单」（不重拉申请队列）', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      calls.push(path)
      if (path.startsWith('/api/kb/access-requests/approve')) return jsonResp({ status: 'ok' })
      if (path.startsWith('/api/kb/access-grants')) return jsonResp({ items: [{ id: 'g1' }] })
      return jsonResp({}, { ok: false, status: 404 })
    }))
    const kb = useKb()
    setIdentity('dept_admin', ['hr'])
    const req = { id: 'ar1', doc_id: 'D1', doc_title: 't', owner_dept: 'hr', requester_dept: 'it', requester_name: 'w', permission_level: 'dept_internal', reason: '', created_at: '' }
    ;(kb as any).accessRequests.value = [req, { ...req, id: 'ar2' }]
    await kb.approveAccess(req as any)
    await waitFor(() => kb.accessGrants.value.length === 1)   // void loadAccessGrants 异步落地（受影响列表）
    expect(kb.accessRequests.value.map((x) => x.id)).toEqual(['ar2'])
    expect(calls.filter((c) => c === '/api/kb/access-requests')).toHaveLength(0)   // 队列 GET 未发生
  })
})

// ── P2-11：review-tasks 此前只回 items，limit=20 静默截断（安全网承诺被截掉不留痕）──
describe('useKb.loadReviewTasks 分页', () => {
  it('消费 has_more；offset>0 追加；offset=0 替换；include_closed 与 offset 共存', async () => {
    const s = useSession(); s.setToken('T'); s.setIdentity({ userId: 'k', role: 'kb_admin', canManage: true } as never)
    const urls: string[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      urls.push(String(url))
      const off = /offset=(\d+)/.exec(String(url))?.[1] ?? '0'
      return { ok: true, status: 200, json: async () => (
        off === '0'
          ? { items: [{ task_id: 't1' }, { task_id: 't2' }], has_more: true }
          : { items: [{ task_id: 't3' }], has_more: false }) }
    }))
    const kb = useKb()
    await kb.loadReviewTasks()
    expect((kb.reviewTasks.value || []).map((x) => x.task_id)).toEqual(['t1', 't2'])
    expect(kb.reviewTasksHasMore.value).toBe(true)

    await kb.loadReviewTasks((kb.reviewTasks.value || []).length)
    expect((kb.reviewTasks.value || []).map((x) => x.task_id)).toEqual(['t1', 't2', 't3'])
    expect(kb.reviewTasksHasMore.value).toBe(false)
    expect(new URL(urls[1], 'http://x').searchParams.get('offset')).toBe('2')

    await kb.loadReviewTasks()
    expect((kb.reviewTasks.value || []).map((x) => x.task_id)).toEqual(['t1', 't2'])
    // include_closed 开关不得把 offset 挤掉。⚠️ 必须**解析** query 而非 toContain——
    // 误用 '?' 拼接会得到 `?offset=2?include_closed=true`，两个子串都还"含"在里面，
    // toContain 断言会全绿放行一个坏掉的 URL（这条反证曾真的没打红）。
    kb.toggleShowClosedReviewTasks()
    await kb.loadReviewTasks(2)
    const q = new URL(urls[urls.length - 1], 'http://x').searchParams
    expect(q.get('offset')).toBe('2')
    expect(q.get('include_closed')).toBe('true')
  })
})

// ── B8：useKb 必须消费后端的两个截断标志（否则组件永远收不到 true）────────────────
describe('useKb.loadFeedbackReview 截断标志', () => {
  it.each([
    ['truncated_messages', { truncated_messages: true }],
    ['truncated_scan', { truncated_scan: true }],
  ])('后端报 %s → feedbackReviewTruncated=true', async (_name, extra) => {
    const s = useSession(); s.setToken('T')
    s.setIdentity({ userId: 'k', role: 'kb_admin', canManage: true } as never)
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => ({ items: [], ...extra }),
    })))
    const kb = useKb()
    await kb.loadFeedbackReview()
    expect(kb.feedbackReviewTruncated.value).toBe(true)
  })

  it('两个标志都为假 → false（不制造无谓告警）', async () => {
    const s = useSession(); s.setToken('T')
    s.setIdentity({ userId: 'k', role: 'kb_admin', canManage: true } as never)
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ items: [], truncated_messages: false, truncated_scan: false }),
    })))
    const kb = useKb()
    await kb.loadFeedbackReview()
    expect(kb.feedbackReviewTruncated.value).toBe(false)
  })
})
