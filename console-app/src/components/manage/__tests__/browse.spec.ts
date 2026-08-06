import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import DocTable from '@/components/manage/DocTable.vue'
import { useKb, __resetKb, type DocItem } from '@/composables/useKb'

beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'dept_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}
function activate(id: Identity, token = 't') {
  const p = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token, ready: true } } })
  setActivePinia(p)
  return p
}
function doc(over: Partial<DocItem>): DocItem {
  return { doc_id: 'd', title: 't', original_filename: 'f', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 1, status: 'active', status_badge: '已上线', updated_at: '2026-06-26', can_manage: true, ...over }
}

describe('DocTable — 本部门/全部门 切换', () => {
  it('dept_admin 显示切换；kb_admin 不显示（本就全见）', () => {
    const w1 = mount(DocTable, { global: { plugins: [activate(identity({ role: 'dept_admin' }))] } })
    expect(w1.text()).toContain('本部门')
    expect(w1.text()).toContain('全部门')

    __resetKb()
    const w2 = mount(DocTable, { global: { plugins: [activate(identity({ role: 'kb_admin', managedOwnerDepts: ['marketing', 'hr'] }))] } })
    expect(w2.text()).not.toContain('全部门')
  })

  it('setScope 切换作用域并清状态筛选', () => {
    activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).filter.value = '已上线'
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ items: [], has_more: false }), text: async () => '{}' })))
    kb.setScope('all')
    expect(kb.docScope.value).toBe('all')
    expect(kb.filter.value).toBe('')
  })
})

describe('DocTable — 全部门只读浏览', () => {
  it('横幅 + 外部门行「申请授权」、本部门行「退役」', async () => {
    const p = activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).docScope.value = 'all'
    ;(kb as any).docs.value = [
      doc({ doc_id: 'mine', owner_dept: 'marketing', can_manage: true }),
      doc({ doc_id: 'foreign', owner_dept: 'hr', can_manage: false }),
    ]
    const w = mount(DocTable, { global: { plugins: [p] } })
    expect(w.text()).toContain('全部门为只读视图')
    expect(w.text()).toContain('其他部门')
    const btns = w.findAll('button').map((b) => b.text())
    expect(btns.filter((t) => t.includes('申请授权')).length).toBe(1)   // 仅外部门 1 行
    // 操作收敛后退役入口在「更多操作」菜单里（gear 仅本部门可管理行有）
    const gears = w.findAll('button[aria-label="更多操作"]')
    expect(gears.length).toBe(1)                                        // 仅本部门 1 行
    await gears[0].trigger('click')
    const items = w.findAll('[role="menuitem"]').map((b) => b.text())
    expect(items.some((t) => t.includes('退役下线'))).toBe(true)
  })

  it('外部门行不暴露 升版/退役（只读）', () => {
    const p = activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).docScope.value = 'all'
    ;(kb as any).docs.value = [doc({ doc_id: 'foreign', owner_dept: 'hr', can_manage: false })]
    const w = mount(DocTable, { global: { plugins: [p] } })
    const btns = w.findAll('button').map((b) => b.text())
    expect(btns.some((t) => t.includes('升版'))).toBe(false)
    expect(btns.some((t) => t.includes('退役'))).toBe(false)
    expect(btns.some((t) => t.includes('申请授权'))).toBe(true)
  })
})

describe('授权申请（申请人侧）', () => {
  it('submitAccessRequest（DEV preview）→ requestedDocIds 标记审批中', async () => {
    activate(identity({ role: 'dept_admin' }), 'dev-preview')
    const kb = useKb()
    kb.openAccessRequest(doc({ doc_id: 'foreign', owner_dept: 'hr', can_manage: false }))
    await kb.submitAccessRequest('需引用')
    expect(kb.accessStateOf('foreign')).toBe('pending')
    expect(kb.accessReqDoc.value).toBeNull()
  })
})

describe('DocTable — 退役行「恢复上线」', () => {
  it('已退役本部门行 → 菜单里显示恢复上线、隐藏退役（且无共享/权限入口）', async () => {
    const p = activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).docs.value = [doc({ doc_id: 'mine', owner_dept: 'marketing', can_manage: true, status_badge: '已退役' })]
    const w = mount(DocTable, { global: { plugins: [p] } })
    await w.find('button[aria-label="更多操作"]').trigger('click')
    const items = w.findAll('[role="menuitem"]').map((b) => b.text())
    expect(items.some((t) => t.includes('恢复上线'))).toBe(true)
    expect(items.some((t) => t.includes('退役下线'))).toBe(false)   // 退役↔恢复 v-if/else 互斥
    expect(items.some((t) => t.includes('跨部门共享'))).toBe(false) // canManagePerm：已退役不给权限入口
  })

  it('restore（DEV preview）→ status_badge 即时变排队中', async () => {
    activate(identity({ role: 'kb_admin' }), 'dev-preview')
    const d = doc({ doc_id: 'mine', status_badge: '已退役' })
    const r = await useKb().restore(d)
    expect(r.ok).toBe(true)
    expect(d.status_badge).toBe('排队中')
  })
})

describe('DocTable — 行勾选（2026-07-19 重设计）', () => {
  it('行 checkbox 勾选 → data-selected 高亮 + 计数；已退役行也可选；外部门行无选框', async () => {
    const p = activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).docs.value = [
      doc({ doc_id: 'a', can_manage: true }),
      doc({ doc_id: 'b', can_manage: true, status_badge: '已退役' }),
      doc({ doc_id: 'f', owner_dept: 'hr', can_manage: false }),
    ]
    const w = mount(DocTable, { global: { plugins: [p] } })
    const rowCbs = w.findAll('.led-row button[role="checkbox"]')
    expect(rowCbs.length).toBe(2)                       // 外部门只读行不给选框
    await rowCbs[0].trigger('click')
    expect(kb.selectedCount.value).toBe(1)
    expect(w.find('.led-row[data-selected="1"]').exists()).toBe(true)
    await rowCbs[1].trigger('click')                    // 已退役行同样可选
    expect(kb.selectedCount.value).toBe(2)
  })

  it('表头全选 → 全选当前可见可管理行，再点清空', async () => {
    const p = activate(identity({ role: 'dept_admin' }))
    const kb = useKb()
    ;(kb as any).docs.value = [doc({ doc_id: 'a' }), doc({ doc_id: 'b' }), doc({ doc_id: 'f', owner_dept: 'hr', can_manage: false })]
    const w = mount(DocTable, { global: { plugins: [p] } })
    const headCb = w.find('.led-head button[role="checkbox"]')
    await headCb.trigger('click')
    expect(kb.selectedCount.value).toBe(2)              // 只圈可管理行
    expect(headCb.attributes('aria-checked')).toBe('true')
    await headCb.trigger('click')
    expect(kb.selectedCount.value).toBe(0)
  })
})

describe('DocTable — 更多操作菜单（2026-07-19 重设计）', () => {
  function mountOne(over: Partial<DocItem> = {}) {
    const p = activate(identity({ role: 'dept_admin' }))
    ;(useKb() as any).docs.value = [doc({ doc_id: 'a', ...over })]
    return mount(DocTable, { global: { plugins: [p] }, attachTo: document.body })
  }

  it('gear 展开：可见范围/共享权限/上传新版本/退役下线 四项 + aria-expanded', async () => {
    const w = mountOne()
    const gear = w.find('button[aria-label="更多操作"]')
    expect(gear.attributes('aria-haspopup')).toBe('menu')
    expect(gear.attributes('aria-expanded')).toBe('false')
    await gear.trigger('click')
    expect(gear.attributes('aria-expanded')).toBe('true')
    const items = w.findAll('[role="menuitem"]').map((b) => b.text())
    expect(items.some((t) => t.includes('可见范围'))).toBe(true)
    expect(items.some((t) => t.includes('跨部门共享'))).toBe(true)
    expect(items.some((t) => t.includes('上传新版本'))).toBe(true)
    expect(items.some((t) => t.includes('退役下线'))).toBe(true)
    w.unmount()
  })

  it('Esc / 点击外部 关闭菜单', async () => {
    const w = mountOne()
    await w.find('button[aria-label="更多操作"]').trigger('click')
    expect(w.find('[role="menu"]').exists()).toBe(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await w.vm.$nextTick()
    expect(w.find('[role="menu"]').exists()).toBe(false)
    await w.find('button[aria-label="更多操作"]').trigger('click')
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await w.vm.$nextTick()
    expect(w.find('[role="menu"]').exists()).toBe(false)
    w.unmount()
  })

  it('已隔离行菜单不给「跨部门共享 / 权限」（canManagePerm 语义保持）', async () => {
    const w = mountOne({ status_badge: '已隔离' })
    await w.find('button[aria-label="更多操作"]').trigger('click')
    const items = w.findAll('[role="menuitem"]').map((b) => b.text())
    expect(items.some((t) => t.includes('跨部门共享'))).toBe(false)
    expect(items.some((t) => t.includes('可见范围'))).toBe(true)
    w.unmount()
  })

  it('下载原始文件：data-testid=doc-preview 保留、title/aria 换新文案', () => {
    const w = mountOne()
    const dl = w.find('[data-testid="doc-preview"]')
    expect(dl.exists()).toBe(true)
    expect(dl.attributes('title')).toBe('下载原始文件')
    expect(dl.attributes('aria-label')).toBe('下载原始文件')
    w.unmount()
  })
})

// 这两条各挂载 50~120 行 DocTable 并用 5ms 步进轮询，本身就要 6~12s——vitest 默认 5000ms
// 对它们从来不够。隔离跑一直 19/19 绿，只在全量并行争抢 CPU 时红，于是长期被当"抖动"
// 忽略；2026-08-05 加了两个新 spec、transform 负载上去一点，第二条也跟着翻红才逼出真相。
// **不是缩短测试**：断言的就是整页替换与筛选回第 1 页，行数少了就测不到。给足时限。
const PAGER_TIMEOUT_MS = 30_000

describe('DocTable — 页码翻页器（2026-07-19 设计稿 doc-table.html pager）', () => {
  async function waitFor(cond: () => boolean, ms = 1000) {
    const t0 = Date.now()
    while (!cond() && Date.now() - t0 < ms) await new Promise((r) => setTimeout(r, 5))
  }
  function pageDocs(prefix: string, n: number): DocItem[] {
    return Array.from({ length: n }, (_, i) => doc({ doc_id: `${prefix}${i}`, title: `${prefix}${i}` }))
  }
  function jsonResp(body: unknown) {
    return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }
  }
  // 分页路由 mock：120 条（faceted badge_counts）分 3 页；calls 记录请求 URL 供断言。
  function pagedFetch(calls: string[]) {
    return vi.fn(async (path: string) => {
      calls.push(path)
      if (path.includes('offset=100')) return jsonResp({ items: pageDocs('p3-', 20), has_more: false, badge_counts: { 已上线: 120 } })
      if (path.includes('offset=50')) return jsonResp({ items: pageDocs('p2-', 50), has_more: true, badge_counts: { 已上线: 120 } })
      return jsonResp({ items: pageDocs('p1-', 50), has_more: true, badge_counts: { 已上线: 120 } })
    })
  }

  it('QueuePager 取代「加载更多」：范围/总数直出，点页码 → loadDocsPage 整页【替换】', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', pagedFetch(calls))
    const p = activate(identity())
    const w = mount(DocTable, { global: { plugins: [p] } })
    const kb = useKb()
    await kb.loadDocs()
    await w.vm.$nextTick()
    expect(w.text()).not.toContain('加载更多')
    expect(w.find('[data-testid="queue-pager"]').exists()).toBe(true)
    expect(w.find('[data-testid="pager-info"]').text()).toContain('第 1–50 条 · 共 120 条')

    const p2 = w.findAll('[data-testid="pager-page"]').find((b) => b.text() === '2')!
    await p2.trigger('click')
    await waitFor(() => kb.docsPage.value === 2)
    await w.vm.$nextTick()
    expect(calls.some((c) => c.includes('offset=50'))).toBe(true)          // offset=(page-1)*50
    expect(kb.docs.value).toHaveLength(50)                                  // 替换而非追加（追加会是 100）
    expect(kb.docs.value[0].doc_id).toBe('p2-0')
    expect(w.find('[data-testid="pager-info"]').text()).toContain('第 51–100 条')
    expect(w.find('[data-testid="pager-page"][data-current="1"]').text()).toBe('2')
  }, PAGER_TIMEOUT_MS)

  it('筛选变化回第 1 页（接现有 loadDocs 重置路径，不造第二套）', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', pagedFetch(calls))
    activate(identity())
    const kb = useKb()
    await kb.loadDocs()
    await kb.loadDocsPage(2)
    expect(kb.docsPage.value).toBe(2)
    calls.length = 0
    kb.setBadgeFilter('已上线')                     // 150ms 防抖 → loadDocs → 回第 1 页
    await waitFor(() => calls.length > 0 && kb.docsPage.value === 1)
    expect(kb.docsPage.value).toBe(1)
    expect(calls[0]).toContain('offset=0')
    expect(calls[0]).toContain('badge=')            // 服务端筛选参数随行
  }, PAGER_TIMEOUT_MS)

  it('竞态守卫：翻页在途时列表被重载 → 旧页响应作废不落地', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => { release = r })
    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      if (path.includes('offset=50')) {             // 第 2 页响应被挂起（慢请求）
        await gate
        return jsonResp({ items: [doc({ doc_id: 'stale' })], has_more: true })
      }
      return jsonResp({ items: [doc({ doc_id: 'fresh' })], has_more: false })
    }))
    activate(identity())
    const kb = useKb()
    const flip = kb.loadDocsPage(2)                 // 在途
    await kb.loadDocs()                             // 期间搜索/筛选触发重载 → docsSeq 递增
    release()
    await flip
    expect(kb.docs.value.map((d) => d.doc_id)).toEqual(['fresh'])   // 旧第 2 页被丢弃
    expect(kb.docsPage.value).toBe(1)
    expect(kb.loadingDocs.value).toBe(false)
  })

  it('深页守卫：超出服务端 _KB_MAX_OFFSET 上界 → 不发请求、页号不变', async () => {
    const f = vi.fn(async () => jsonResp({ items: [], has_more: false }))
    vi.stubGlobal('fetch', f)
    activate(identity())
    const kb = useKb()
    await kb.loadDocsPage(202)                      // (202-1)*50 = 10050 > 10000
    expect(f).not.toHaveBeenCalled()
    expect(kb.docsPage.value).toBe(1)
  })

  it('加载中禁点：pointer-events-none 视觉降级 + onPage 守卫不发请求', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', pagedFetch(calls))
    const p = activate(identity())
    const w = mount(DocTable, { global: { plugins: [p] } })
    const kb = useKb()
    await kb.loadDocs()
    await w.vm.$nextTick()
    ;(kb as any).loadingDocs.value = true
    await w.vm.$nextTick()
    expect(w.find('[data-testid="queue-pager"]').classes()).toContain('pointer-events-none')
    calls.length = 0
    const p2 = w.findAll('[data-testid="pager-page"]').find((b) => b.text() === '2')!
    await p2.trigger('click')                        // trigger 绕过 CSS → 靠 onPage 守卫兜底
    await new Promise((r) => setTimeout(r, 10))
    expect(calls.length).toBe(0)
    expect(kb.docsPage.value).toBe(1)
  })
})

describe('DocTable — 筛选归位（2026-07-16 生产实测：跳到页面底部）', () => {
  it('筛选变更 → 台账区 scrollIntoView 归位（结果骤缩时不再钳在底部）', async () => {
    const p = activate(identity({ role: 'kb_admin', managedOwnerDepts: ['marketing', 'hr'] }))
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ items: [], has_more: false }), text: async () => '{}' })))
    const spy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
    mount(DocTable, { global: { plugins: [p] } })
    const kb = useKb()
    spy.mockClear()                       // 掐掉挂载期 URL 恢复可能触发的调用，只看筛选动作
    kb.setOwnerFilter('hr')
    await new Promise((r) => setTimeout(r, 0))   // watch flush（默认 pre → 微任务后已跑）
    expect(spy).toHaveBeenCalled()
    expect(spy.mock.calls[0][0]).toMatchObject({ block: 'start' })
  })
})
