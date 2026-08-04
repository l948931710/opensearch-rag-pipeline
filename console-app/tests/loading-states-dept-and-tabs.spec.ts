import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, MANAGE_ROUTE } from './node-mode.helpers';

/**
 * 硬门 —— 轮 2b：把轮 2a 的四态机制补到剩余面 + tab 级感知。
 *
 * 基线（轮 2 审计与其评审的实测值，本轮据此立断言）：
 *   · dept_admin + stats 404：与 kb_admin 侧**一模一样**——4 骨架 5 秒仍在转、0 alert、0 重试、
 *     「暂无文档数据。」在显、占位仍写「稍后自动呈现」。同一缺陷此前只在兄弟文件里修了一半。
 *   · 切到「审批」tab：「暂无审批历史」从 97ms 显示到 1242ms（**1145ms**），再被真实内容悄悄换掉。
 *   · 被点的 tab 按钮**自身零状态变化**（无 aria-busy、无 spinner，只有 active/inactive 类）。
 */

const DEPT = { role: 'dept_admin' as const, managedRoots: [3] };
const KB_ADMIN = { role: 'kb_admin' as const, managedRoots: [] };
const J = (body: unknown) => ({ contentType: 'application/json', body: JSON.stringify(body) });
const tab = (p: Page, name: RegExp) => p.locator('[aria-label="管理台分区"]').getByRole('tab', { name });

test.describe('硬门 — 轮 2b 加载态补齐', () => {
  test('G1 dept_admin + stats 404：骨架必须停并给出重试（与 kb_admin 侧同款）', async ({ page }) => {
    await mockNodeMode(page, DEPT);
    await page.route('**/api/kb/stats**', (r) => r.fulfill({ status: 404, ...J({}) }));
    await page.goto(MANAGE_ROUTE);

    const box = page.getByTestId('stats-unavailable');
    await expect(box).toBeVisible({ timeout: 5000 });
    await expect(page.locator('.animate-pulse')).toHaveCount(0);
    await expect(box.getByRole('button', { name: '重试' })).toBeEnabled();
    await expect(page.getByText('暂无文档数据。'), '未接入 ≠ 没有文档').toHaveCount(0);
  });

  for (const role of [KB_ADMIN, DEPT]) {
    test(`G1b ${role.role} + stats 5xx：同样不得断言「暂无文档数据」（5xx 也是不知道分布）`, async ({ page }) => {
      await mockNodeMode(page, role);
      await page.route('**/api/kb/stats**', (r) => r.fulfill({ status: 500, ...J({}) }));
      await page.goto(MANAGE_ROUTE);

      await expect(page.getByRole('alert').filter({ hasText: '加载失败' }).first()).toBeVisible();
      // 「不知道 ≠ 是零」这条原则起初只挡了 404，5xx 分支漏执行——评审实测同屏出现
      // 「加载失败」alert 与「暂无文档数据。」两句话。
      await expect(page.getByText('暂无文档数据。'), '加载失败同样是不知道分布').toHaveCount(0);
    });
  }

  test('G1c 真无数据不得被误伤：200 + 空 by_badge 仍要说「暂无文档数据」', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/stats**', (r) => r.fulfill(J({ total: 0, active: 0, retired: 0, chunks: 0, by_badge: {} })));
    await page.goto(MANAGE_ROUTE);
    // 收紧谓词后最容易过头的方向：把「确认为空」也一并藏掉。这条守住反向。
    await expect(page.getByText('暂无文档数据。')).toBeVisible();
  });

  test('G2 dept_admin + insights 404：说「未接入」而不是「稍后自动呈现」', async ({ page }) => {
    await mockNodeMode(page, DEPT);
    await page.route('**/api/kb/insights**', (r) => r.fulfill({ status: 404, ...J({}) }));
    await page.goto(MANAGE_ROUTE);

    await expect(page.getByTestId('insights-not-deployed')).toBeVisible();
    await expect(page.getByText('稍后自动呈现'), '404 不会自动变好').toHaveCount(0);
  });

  test('G3 审批历史在途：显骨架，不得先断言「暂无审批历史」', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/approval-history**', async (r) => {
      await new Promise((s) => setTimeout(s, 2000));
      await r.fulfill(J({ items: [] }));
    });
    await page.goto(MANAGE_ROUTE);
    await tab(page, /^审批/).click();

    // 基线：此刻起「暂无审批历史」已显示，并一直挂到 ~1.2s 后被悄悄换掉
    await expect(page.getByTestId('skel-approval-history')).toBeVisible({ timeout: 2000 });
    await expect(page.getByText('暂无审批历史'), '在途期间不得断言为空').toHaveCount(0);

    // 真到达且确实为空 → 这时才该说「暂无」
    await expect(page.getByTestId('skel-approval-history')).toHaveCount(0, { timeout: 6000 });
    await expect(page.getByText('暂无审批历史')).toBeVisible();
  });

  test('G4 切 tab：被点的 tab 按钮自身出现在途指示（基线零状态变化）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/approval-history**', async (r) => {
      await new Promise((s) => setTimeout(s, 1500));
      await r.fulfill(J({ items: [] }));
    });
    await page.goto(MANAGE_ROUTE);
    await tab(page, /^审批/).click();

    const busy = page.getByTestId('tab-loading');
    await expect(busy, 'tab chrome 层必须有统一信号').toBeVisible({ timeout: 1000 });
    await expect(busy).toHaveAttribute('aria-busy', 'true');
    await expect(busy).toHaveAttribute('role', 'tab');
    await expect(busy).toHaveCount(0, { timeout: 6000 });   // settle 后收掉
  });

  test('G5 hover 预取：悬停即发请求，但不切 tab、不显在途指示', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    let hits = 0;
    page.on('request', (r) => { if (r.url().includes('/api/kb/my-docs')) hits++; });
    await page.goto(MANAGE_ROUTE);
    await expect(page.locator('.kb-card').first()).toBeVisible();
    expect(hits, '进 dash 不该拉台账').toBe(0);

    await tab(page, /文档管理/).hover();
    await expect.poll(() => hits, { timeout: 3000 }).toBeGreaterThan(0);
    // 预取是后台行为：不切 tab，也不该让用户看到自己没点过的 tab 在转
    await expect(page).not.toHaveURL(/tab=docs/);
    await expect(page.getByTestId('tab-loading')).toHaveCount(0);
  });

  test('G6 预取 + 点击不重复发请求（_loadedTabs 去重）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    let hits = 0;
    page.on('request', (r) => { if (r.url().includes('/api/kb/my-docs')) hits++; });
    await page.goto(MANAGE_ROUTE);
    await expect(page.locator('.kb-card').first()).toBeVisible();

    await tab(page, /文档管理/).hover();
    await expect.poll(() => hits, { timeout: 3000 }).toBe(1);
    await tab(page, /文档管理/).click();
    await page.waitForTimeout(800);
    expect(hits, '预取过的 tab 再点击不重复拉').toBe(1);
  });
});
