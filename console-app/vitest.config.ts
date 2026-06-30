import { defineConfig, configDefaults } from 'vitest/config'
import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  test: {
    environment: 'happy-dom',
    globals: true,
    // Playwright 的 E2E 用例放在 tests/(由 playwright.config.ts 的 testDir 接管)。
    // 这里排除,免得 vitest 误抓 tests/*.spec.ts 而在 import @playwright/test 时报错。
    exclude: [...configDefaults.exclude, 'tests/**'],
  },
})
