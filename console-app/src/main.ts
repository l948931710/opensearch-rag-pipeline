import { createApp } from 'vue'
import '@/styles/tokens.css'
import App from './App.vue'

// P1 起：createApp(App).use(pinia).use(router) —— 此处先最小挂载验证主题。
createApp(App).mount('#app')
