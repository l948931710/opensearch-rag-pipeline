<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import Sidebar from './Sidebar.vue'
import ErrorBoundary from './ErrorBoundary.vue'
import ConfirmDialog from '@/components/manage/ConfirmDialog.vue'
import { useAsk } from '@/composables/useAsk'

// 应用外壳：左图标轨 + 右内容区。router-view 只在外壳内（= ready 之后）渲染，
// 故视图挂载时身份已就绪，无需在路由守卫里等待 / 触发免登。
// 内容区包一层 ErrorBoundary：某视图/消息渲染崩了只兜底该区、不整页白屏；
// 侧栏在边界外，永远可用（切路由/切会话即 resetSignal 变化 → 自愈重渲）。
const route = useRoute()
const { activeId } = useAsk()
// route.path(非 fullPath)：切路由 / 切会话才重置错误边界;同页 query 抖动(tab/搜索/筛选)不应触发。
const resetSignal = computed(() => route.path + '::' + activeId.value)
</script>

<template>
  <div class="relative flex h-[100dvh] overflow-hidden bg-background text-foreground">
    <Sidebar />
    <!-- overflow-y-scroll 常驻 11px 滚动条槽位（track 透明不可见）：短内容页（审批历史/成员管理）
         无滚动条时 mx-auto 居中内容相对长页（概览/文档管理）平移 ~半个滚动条宽。
         此前只靠 [scrollbar-gutter:stable] 兜底，真机（Safari<17.4 等）不认该属性时平移仍现——
         scroll 常驻是跨浏览器兜底，gutter 声明保留无害。 -->
    <main class="min-w-0 flex-1 overflow-y-scroll [scrollbar-gutter:stable]">
      <ErrorBoundary :reset-signal="resetSignal">
        <!-- 视图容器：单根 div + key，切换靠「入场动画」而非 <Transition>。
             ⚠️ 曾用 <Transition name="fade" mode="out-in">，是 ?preview 直链整片空白的根因：
               ① out-in 把「挂载新视图」压在「旧元素离场完成」之后，而离场完成要等 Vue 的
                  nextFrame（双 requestAnimationFrame）+ transitionend——一旦浏览器不产出动画帧
                  （后台/隐藏标签页、离屏预览面板、被遮挡窗口、内嵌 webview），离场永远停在
                  fade-leave-from + fade-leave-active，懒加载视图永不挂载 = main 区空白且无任何报错。
               ② 即便不用 out-in，Vue Transition 的入场必经 opacity:0 的 *-enter-from：帧不来就
                  永远停在 opacity:0，照样看不见。实测（帧冻结环境）：CSS transition 停在 opacity 0，
                  CSS 关键帧动画则回落到基础样式 opacity 1 —— 故改用关键帧「入场」动画。
             ?preview 独有的触发条件：它是唯一同步就把 session.ready 翻 true 的登录路径，AppShell 因此
             早于路由首次导航（要现拉懒加载 chunk）挂载，先渲一个空占位 div，随后 key 由 / 变 /manage
             触发离场——?token 走 whoami 网络往返，导航早已完成、不产生这次离场，故 e2e 全绿照不出。
             v-if="Component"：导航未定前不渲占位 div，从源头消灭这次多余的换场。 -->
        <RouterView v-slot="{ Component, route: viewRoute }">
          <!-- 单根包装（d712305）：视图可能是多根(如 ManageView 有 员工/管理员 两套根 div)，
               包成单根后任何视图形态都能整体换场，并统一挂 h-full 撑满内容区。 -->
          <!-- key 必须用 route.path 而非 fullPath：区分 /·/manage·/contribute 三视图即可；用 fullPath
               (含 query) 会让同页内 tab 切换 / 搜索 / 筛选等 query 变动都改 key、整个视图被销毁重建 →
               切 tab 黑屏闪、搜索框每输入一字就重挂失焦、数据全量重拉(d712305 回归，勿改回)。
               取 slot 的 route 而非 useRoute()：与同一次渲染的 Component 严格同源，key 不会先于视图变。 -->
          <div v-if="Component" :key="viewRoute.path" class="view-swap h-full">
            <component :is="Component" />
          </div>
        </RouterView>
      </ErrorBoundary>
    </main>
    <!-- 全局 确认/输入/告知 框（useDialog 单例）：挂外壳层、ErrorBoundary 外——
         所有路由视图（管理台/贡献/问答）共用一份，贡献页审核失败等 notice 也有处渲染。 -->
    <ConfirmDialog />
  </div>
</template>

<style scoped>
/* 换视图的入场动效：【只动 transform，不碰 opacity】——刻意的硬约束，别加 opacity 回来。
   帧冻结时（后台/隐藏标签页、离屏预览面板、被遮挡窗口）动画会停在首帧：带 opacity:0 的关键帧
   就永久停在全透明 = 内容挂上了却看不见，与本次修的白屏无异（已实测复现）。只位移则最坏情况
   也只是内容低 4px，始终可读可点。
   代价：动画运行的 0.16s 内本元素是 fixed 定位后代的包含块——视图内的模态框（可见性/共享等）
   都由用户操作触发，不可能落在换路由后的这 0.16s 内，故无实际影响。 */
.view-swap { animation: view-swap-in 0.16s ease-out; }
@keyframes view-swap-in { from { transform: translateY(4px); } }
@media (prefers-reduced-motion: reduce) { .view-swap { animation: none; } }
</style>
