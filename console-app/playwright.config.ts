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
    // 专用端口 5177（下方 webServer 自动拉起本树 dev server）。⚠️ 曾踩坑：此前默认 5173
    // 且 webServer 注释掉 —— e2e 蹭的是环境里碰巧在跑的【另一份 checkout】的 dev server，
    // 硬门测的根本不是被测代码。BASE_URL 仍可显式覆盖（联调真后端等场景）。
    baseURL: process.env.BASE_URL ?? 'http://127.0.0.1:5177',
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
  // e2e 永远测【本树】代码：专用端口 + 绝不复用外部 server（reuse 会蹭到别的 checkout）。
  // 显式传 BASE_URL 时跳过自起（联调真后端/远端环境）。硬门 spec 全走 page.route mock，
  // 不需要 FastAPI 后端。
  webServer: process.env.BASE_URL ? undefined : {
    command: 'npm run dev -- --port 5177 --host 127.0.0.1 --strictPort',
    url: 'http://127.0.0.1:5177/console/',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
