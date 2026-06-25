import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

// 视图按需加载：首屏只拉问答，管理页懒载（多数员工用不到）。
const QaView = () => import('@/views/QaView.vue')
const ManageView = () => import('@/views/ManageView.vue')

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'qa', component: QaView, meta: { title: '问答' } },
  // requiresManage 仅作语义标注 + 视图内自检；不在此处做会跳转的守卫，
  // 免登/权限解析全部收口在 useAuth（修正#6），路由守卫绝不触发免登。
  { path: '/manage', name: 'manage', component: ManageView, meta: { title: '知识库管理', requiresManage: true } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

// history base 取构建期注入的 BASE_URL（= vite.config 的 base），单一来源；
// 并行 /console-next/ 与正式 /console/ 同一份代码、零硬编码。
export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})
