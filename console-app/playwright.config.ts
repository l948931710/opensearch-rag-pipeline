import { defineConfig, devices } from '@playwright/test';

// UX 硬门配置。三个视口对应 dispatcher 里要求的 1440×900 / 1280×800 / 1024×768。
// 跑之前先起 dev server,或用下面的 webServer 自动拉起。
export default defineConfig({
  testDir: './tests',
  // 任一断言失败即视为本轮未通过,所以不容忍 flake:失败不重试,逼出真实问题。
  retries: 0,
  fullyParallel: true,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    // 应用基址在 :5173;具体路由(/console/...)写在各 spec 的 ROUTE 常量里。
    baseURL: process.env.BASE_URL ?? 'http://localhost:5173',
    screenshot: 'only-on-failure',
    trace: 'on-first-retry',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'desktop-1440',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    {
      name: 'mid-1280',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'small-1024',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1024, height: 768 } },
    },
  ],
  // 可选:让 Playwright 自己拉起 dev server。已在外部起好就删掉这段。
  // 注意:本项目前端走 /api 代理到 FastAPI:8000,非 mock 的用例还需后端在跑(make api)。
  // webServer: {
  //   command: 'npm run dev',
  //   url: process.env.BASE_URL ?? 'http://localhost:5173',
  //   reuseExistingServer: !process.env.CI,
  //   timeout: 120_000,
  // },
});
