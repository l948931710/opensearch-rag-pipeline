import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// base 环境化注入（绝不硬编码）：并行验收用 /console-next/，正式切换用 /console/。
//   并行：CONSOLE_BASE=/console-next/ npm run build   （默认值）
//   正式：CONSOLE_BASE=/console/      npm run build
// Router 的 history base 在运行时取 import.meta.env.BASE_URL（= 此处 base），单一来源。
const BASE = process.env.CONSOLE_BASE || '/console-next/'

export default defineConfig({
  base: BASE,
  plugins: [vue(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    // dev：/api 透传到 FastAPI:8000，HMR 对真后端联调
    proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } },
  },
  build: {
    // 产物落进 python 包内，便于 SAE 打包 + FastAPI StaticFiles 托管（P5 接入）
    outDir: fileURLToPath(new URL('../opensearch_pipeline/webconsole/next-dist', import.meta.url)),
    emptyOutDir: true,
  },
})
