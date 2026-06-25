// 必须是 main.ts 的【第一个】import：在 `@/router`（createWebHistory 读 window.location）被加载
// 之前，先把 URL 透传 token 抹除（修正#4）。ES 模块按出现顺序执行，故此副作用早于 router 模块加载，
// router 初始导航就看不到 token，也不会在 finalize 时把它写回地址栏。
import { captureUrlCredential } from '@/composables/useAuth'

captureUrlCredential()
