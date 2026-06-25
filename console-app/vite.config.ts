import { defineConfig } from 'vite'
import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// base 环境化注入（绝不硬编码）。P7 已切换：默认 = /console/（正式入口）。
//   正式（默认）：npm run build                       → base /console/
//   并行/回归：  CONSOLE_BASE=/console-next/ npm run build
// Router 的 history base 在运行时取 import.meta.env.BASE_URL（= 此处 base），单一来源。
const BASE = process.env.CONSOLE_BASE || '/console/'

// dev 便利：base 是 /console/，故根路径 / 不是应用入口（直接开 / 会白屏）。
// 这个 dev-only 插件把 / 重定向到 base，省得忘了带路径。生产构建不含 dev server。
const devRootRedirect = {
  name: 'dev-root-redirect',
  configureServer(server: any) {
    server.middlewares.use((req: any, res: any, next: any) => {
      const url = (req.url || '').split('?')[0]
      if (url === '/' || url === '/index.html') {
        res.writeHead(302, { Location: BASE })
        res.end()
        return
      }
      next()
    })
  },
}

export default defineConfig({
  base: BASE,
  plugins: [vue(), tailwindcss(), devRootRedirect],
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
