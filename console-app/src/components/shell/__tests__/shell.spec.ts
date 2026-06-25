import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import type { Identity } from '@/stores/session'
import Sidebar from '@/components/shell/Sidebar.vue'
import ManageView from '@/views/ManageView.vue'
import { router } from '@/router'

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

describe('Sidebar — 导航按权限过滤', () => {
  it('管理员：问答 + 知识库管理 两项', () => {
    const w = mountWith(Sidebar, identity({ canManage: true }))
    const links = w.findAll('a.rl')
    expect(links.map((l) => l.attributes('data-to'))).toEqual(['/', '/manage'])
    expect(w.text()).toContain('知识库管理')
  })

  it('普通员工：仅问答，无管理入口', () => {
    const w = mountWith(Sidebar, identity({ canManage: false, role: 'employee' }))
    const links = w.findAll('a.rl')
    expect(links.map((l) => l.attributes('data-to'))).toEqual(['/'])
    expect(w.text()).not.toContain('知识库管理')
  })

  it('展示姓名首字与角色', () => {
    const w = mountWith(Sidebar, identity({ name: '李四', role: 'dept_admin' }))
    expect(w.text()).toContain('李') // 头像首字
    expect(w.text()).toContain('部门管理员')
  })
})

describe('ManageView — 视图内权限自检（深链兜底）', () => {
  it('非管理员深链 /manage → 落「无管理权限」，不暴露管理 UI', () => {
    const w = mountWith(ManageView, identity({ canManage: false }))
    expect(w.text()).toContain('无管理权限')
    expect(w.text()).not.toContain('P4')
  })

  it('管理员 → 渲染管理区（占位）', () => {
    const w = mountWith(ManageView, identity({ canManage: true, managedOwnerDepts: ['hr'] }))
    expect(w.text()).not.toContain('无管理权限')
    expect(w.text()).toContain('知识库管理')
    expect(w.text()).toContain('hr')
  })
})

describe('router — 路由表 + base 单一来源', () => {
  it('注册 qa / manage；manage 标记 requiresManage', () => {
    expect(router.hasRoute('qa')).toBe(true)
    expect(router.hasRoute('manage')).toBe(true)
    expect(router.resolve('/manage').meta.requiresManage).toBe(true)
  })

  it('未知路径回落到 /（catch-all 重定向已配置）', () => {
    const catchAll = router.getRoutes().find((r) => r.path === '/:pathMatch(.*)*')
    expect(catchAll?.redirect).toBe('/')
    // resolve 命中 catch-all（导航时才展开到 /）
    expect(router.resolve('/no-such-page').matched.at(-1)?.path).toBe('/:pathMatch(.*)*')
  })
})
