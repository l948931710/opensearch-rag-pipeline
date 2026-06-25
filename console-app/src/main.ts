import '@/boot/capture'   // 必须最先：在 @/router 加载前抹除 URL token（修正#4，见该文件注释）
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import '@/styles/tokens.css'
import App from './App.vue'
import { router } from '@/router'
import { setReauthHandler } from '@/lib/api'
import { useAuth } from '@/composables/useAuth'

const app = createApp(App)
app.use(createPinia())
app.use(router)
// 401 重登回调（Pinia 装好后注入；仅在 401 时回调，届时 store 已激活）。
setReauthHandler(() => useAuth().reauth())
app.mount('#app')
