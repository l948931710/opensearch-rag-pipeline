import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import type { Identity } from '@/stores/session'
import Sidebar from '@/components/shell/Sidebar.vue'
import ManageView from '@/views/ManageView.vue'
import { __resetKb } from '@/composables/useKb'
import { router } from '@/router'

// ManageView 的 onMounted 会拉 my-docs/pending-approvals/access-requests（canManage 时）——桩掉 fetch，
// 避免真 fetch 泄漏到 teardown（AbortError 噪声）；重置 useKb 单例避免跨例污染 reviewCount。
beforeEach(() => {
  vi.restoreAllMocks()
  __resetKb()
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ items: [], has_more: false }), text: async () => '{}' })))
})

function identity(over: Partial<Identity> = {}): Identity {
  return {
    userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'],
    canManage: true, managedOwnerDepts: ['marketing'], ...over,
  }
}

function mountWith(comp: any, id: Identity) {
  return mount(comp, {
    global: {
      plugins: [createTestingPinia({
        createSpy: vi.fn,
        initialState: { session: { identity: id, token: 't', ready: true, error: '' } },
      })],
      // 不挂真路由：把 RouterLink 桩成 <a>，只验导航项的可见性与文案
      stubs: { RouterLink: { props: ['to'], template: '<a class="rl" :data-to="to"><slot /></a>' } },
    },
  })
}

describe('Sidebar — 新会话/知识库入口/角色标签', () => {
  it('管理员：新会话 + 搜索对话 + 知识库管理入口', () => {
    const w = mountWith(Sidebar, identity({ canManage: true }))
    expect(w.text()).toContain('新会话')
    expect(w.find('input[type="search"]').exists()).toBe(true)   // 搜索对话
    const links = w.findAll('a.rl')                              // 聊天走会话列表（非 RouterLink）；导航链接 = /contribute + /manage
    expect(links.map((l) => l.attributes('data-to'))).toEqual(['/contribute', '/manage'])
    expect(w.text()).toContain('知识贡献')
    expect(w.text()).toContain('知识库管理')
  })

  it('普通员工：知识贡献 + 知识库入口存在（库标签精简为「知识库」，非管理）', () => {
    const w = mountWith(Sidebar, identity({ canManage: false, role: 'employee' }))
    const links = w.findAll('a.rl')
    expect(links.map((l) => l.attributes('data-to'))).toEqual(['/contribute', '/manage'])  // 知识贡献员工也可见
    expect(w.text()).toContain('知识贡献')
    expect(w.text()).toContain('知识库')
    expect(w.text()).not.toContain('知识库管理')
  })

  it('展示姓名首字与角色', () => {
    const w = mountWith(Sidebar, identity({ name: '李四', role: 'dept_admin' }))
    expect(w.text()).toContain('李') // 头像首字
    expect(w.text()).toContain('部门管理员')
  })
})

describe('ManageView — 按角色分流', () => {
  it('普通员工 → 只读概览（不暴露上传/台账/审批管理 UI）', () => {
    const w = mountWith(ManageView, identity({ canManage: false, role: 'employee' }))
    expect(w.text()).toContain('知识库概览')
    expect(w.text()).toContain('员工身份')
    expect(w.text()).toContain('去问答')
    expect(w.text()).not.toContain('上传文档')   // 无管理控件
    expect(w.text()).not.toContain('待审批')
  })

  it('知识库管理员 → 分 tab 管理台（概览看板 / 文档管理），默认全库看板', () => {
    const w = mountWith(ManageView, identity({ canManage: true, role: 'kb_admin', managedOwnerDepts: ['hr'] }))
    expect(w.text()).toContain('知识库管理')
    expect(w.text()).toContain('hr')
    expect(w.text()).toContain('概览看板')        // 子 tab
    expect(w.text()).toContain('文档管理')        // 子 tab
    expect(w.text()).toContain('全库资产概览')     // kb_admin 看板（默认 tab）
    expect(w.text()).not.toContain('知识库概览')   // 员工专属文案
  })

  it('部门管理员 → 默认看板为本部门视图（非全库）', () => {
    const w = mountWith(ManageView, identity({ canManage: true, role: 'dept_admin', managedOwnerDepts: ['marketing'] }))
    expect(w.text()).toContain('概览看板')
    expect(w.text()).toContain('文档总数')        // 本部门概览卡
    expect(w.text()).not.toContain('全库资产概览') // 不是 kb_admin 看板
  })
})

describe('router — 路由表 + base 单一来源', () => {
  it('注册 qa / manage / contribute；manage 标记 requiresManage，contribute 不标记', () => {
    expect(router.hasRoute('qa')).toBe(true)
    expect(router.hasRoute('manage')).toBe(true)
    expect(router.hasRoute('contribute')).toBe(true)
    expect(router.resolve('/manage').meta.requiresManage).toBe(true)
    expect(router.resolve('/contribute').meta.requiresManage).toBeFalsy()   // 员工可访问
  })

  it('未知路径回落到 /（catch-all 重定向已配置）', () => {
    const catchAll = router.getRoutes().find((r) => r.path === '/:pathMatch(.*)*')
    expect(catchAll?.redirect).toBe('/')
    // resolve 命中 catch-all（导航时才展开到 /）
    expect(router.resolve('/no-such-page').matched.at(-1)?.path).toBe('/:pathMatch(.*)*')
  })
})
