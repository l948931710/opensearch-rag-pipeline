import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import AppShell from '@/components/shell/AppShell.vue'

/**
 * 回归门 — 路由视图的挂载【绝不能】依赖动画帧
 *
 * 曾经的 bug（2026-07-19，?preview 直链 /console/manage 整片空白）：AppShell 的视图容器包在
 * `<Transition name="fade" mode="out-in">` 里。out-in 把「挂载新视图」压在「旧元素离场完成」之后，
 * 而 Vue 判定离场完成要先过 nextFrame（双 requestAnimationFrame）。浏览器不产帧时
 * （后台/隐藏标签页、离屏预览面板、被遮挡窗口、内嵌 webview）离场永远停在
 * fade-leave-from + fade-leave-active → 懒加载视图永不挂载 = main 区空白，且 console 零报错。
 * ?preview 是唯一同步翻 ready 的登录路径，故 AppShell 早于路由首次导航挂载、先渲一个空占位再换场，
 * 把这条缝暴露出来；?token 走网络往返、导航早已完成，因此既有 e2e 全绿也照不出。
 *
 * 本门把不变量钉死：冻结 requestAnimationFrame（不产帧），路由切换后视图仍必须出现在 DOM 里。
 * 换回任何「靠过渡/动画帧才挂载或才可见」的实现都会在这里红。
 */

const Lazy = (text: string) =>
  () => Promise.resolve(defineComponent({ render: () => h('div', text) }))

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    // 与真路由表同构：两个视图都懒加载（真实现是 () => import('@/views/...')）
    routes: [
      { path: '/', name: 'qa', component: Lazy('QA-VIEW') },
      { path: '/manage', name: 'manage', component: Lazy('MANAGE-VIEW') },
    ],
  })
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ items: [] }), text: async () => '{}' })))
})

function mountShell(router: ReturnType<typeof makeRouter>) {
  return mount(AppShell, {
    global: {
      plugins: [
        createTestingPinia({
          createSpy: vi.fn,
          initialState: {
            session: {
              identity: { userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'] },
              token: 't', ready: true, error: '',
            },
          },
        }),
        router,
      ],
      // ⚠️ 必须关掉 VTU 默认的 transition 桩：默认桩把 <Transition> 直通渲染，
      // 过渡逻辑整个不跑，本门就永远绿（修复前的代码也照过）——那样的门等于没有。
      stubs: { transition: false },
    },
  })
}

describe('AppShell 视图容器 — 不产帧也必须挂载视图', () => {
  it('冻结 requestAnimationFrame 后切路由，新视图仍挂上（out-in 过渡门卡死的根因）', async () => {
    const router = makeRouter()
    await router.push('/')
    await router.isReady()
    const w = mountShell(router)
    await vi.waitFor(() => expect(w.text()).toContain('QA-VIEW'))

    // 模拟「浏览器不产帧」：rAF 登记回调但永不触发。
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 0))

    await router.push('/manage')
    await vi.waitFor(() => expect(w.text()).toContain('MANAGE-VIEW'))
    expect(w.text()).not.toContain('QA-VIEW')   // 旧视图确实换掉了，不是两个叠着
  })

  it('导航未定（无匹配组件）时不渲空占位容器——省掉那次多余换场', () => {
    // 未 push 任何路由：currentRoute 仍是 START_LOCATION、matched 为空。
    // 这正是 ?preview 同步翻 ready 时 AppShell 遇到的状态：此刻若渲个空 div 占位，
    // 随后导航落定就要多换一次场——而那次换场就是卡死的地方。
    const w = mountShell(makeRouter())
    expect(w.find('main').element.children.length).toBe(0)
  })
})
