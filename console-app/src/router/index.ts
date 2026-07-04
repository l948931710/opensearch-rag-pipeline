import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

// 视图按需加载：首屏只拉问答，管理页懒载（多数员工用不到）。
const QaView = () => import('@/views/QaView.vue')
const ManageView = () => import('@/views/ManageView.vue')
const ContributeView = () => import('@/views/ContributeView.vue')

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'qa', component: QaView, meta: { title: '问答' } },
  // requiresManage 仅作语义标注 + 视图内自检；不在此处做会跳转的守卫，
  // 免登/权限解析全部收口在 useAuth（修正#6），路由守卫绝不触发免登。
  { path: '/manage', name: 'manage', component: ManageView, meta: { title: '知识库管理', requiresManage: true } },
  // 知识贡献：员工可访问（不加 requiresManage）；审核区在视图内按 role 门控。
  { path: '/contribute', name: 'contribute', component: ContributeView, meta: { title: '知识贡献' } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

// history base 取构建期注入的 BASE_URL（= vite.config 的 base），单一来源；
// 并行 /console-next/ 与正式 /console/ 同一份代码、零硬编码。
export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

// 懒载视图的 idle 预取（Perf-4）：首屏（多为问答页）交互就绪后，趁空闲把另两个视图 chunk
// 拉进 HTTP 缓存——管理员点「知识库」不再现场下 100KB+。动态 import 幂等（模块缓存），
// 失败静默（预取本就是尽力而为，真导航时会正常再拉）。requestIdleCallback 缺席（Safari）退化 2s 定时。
router.isReady().then(() => {
  const prefetch = () => { QaView(); ManageView(); ContributeView() }
  const idle = (window as any).requestIdleCallback as ((cb: () => void, o?: any) => void) | undefined
  try {
    if (idle) idle(prefetch, { timeout: 5000 })
    else setTimeout(prefetch, 2000)
  } catch { /* 预取失败无害 */ }
})
