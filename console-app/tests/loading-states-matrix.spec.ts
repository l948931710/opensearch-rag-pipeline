import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, MANAGE_ROUTE } from './node-mode.helpers';

/**
 * 硬门 —— 轮 2c：把「加载四态」收口到评审矩阵里剩下的格子。
 *
 * 基线（评审的五 tab × 四态实测矩阵，11 个 loader 只有 4 个进了 withLoader）：
 *   · 运营指标 404 → **永久显示「数据加载中…」**（4s 后仍在、alert=0）——轮 2a 消灭的
 *     「骨架永远转」缺陷的文本版，只是没人给它写过门。
 *   · 成员管理在途 → 即断言「暂无部门管理员」——本轮为 approvalHistory 修掉的同一形状。
 *   · 审批·accessGrants 5xx → **完全静默**（alert=0、重试=0）。根因与轮 2a 的 B2 同构：
 *     ManageView 的 section 门是 `v-if="accessGrants.length"`，5xx 时数组被清空 → 整个
 *     section 不渲染 → 组件里那条 LoadError 根本没机会挂载。
 *   · 文档管理 my-docs 5xx → alert 与「暂无文档，先上传一篇吧」并存。
 */

const KB_ADMIN = { role: 'kb_admin' as const, managedRoots: [] };
const J = (body: unknown) => ({ contentType: 'application/json', body: JSON.stringify(body) });
const tab = (p: Page, name: RegExp) => p.locator('[aria-label="管理台分区"]').getByRole('tab', { name });

test.describe('硬门 — 轮 2c 四态收口', () => {
  test('M1 运营指标 404：说「未接入」，不得永久假「数据加载中…」', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/ops-metrics**', (r) => r.fulfill({ status: 404, ...J({}) }));
    await page.goto(MANAGE_ROUTE);
    await tab(page, /运营指标/).click();

    // 三个分区各一条，testid 各带后缀 —— 不写 .first() 正是为了让共用 testid 的写法必红
    for (const s of ['llm', 'slo', 'admission']) {
      await expect(page.getByTestId(`ops-not-deployed-${s}`)).toBeVisible({ timeout: 5000 });
    }
    await expect(page.getByText('数据加载中…'), '404 不会自动变好——基线 4s 后这句仍在').toHaveCount(0);
  });

  test('M1b 运营指标 5xx：不得印成「返回 404」（该路径后端 raise 的是 500）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/ops-metrics**', (r) => r.fulfill({ status: 503, ...J({}) }));
    await page.goto(MANAGE_ROUTE);
    await tab(page, /运营指标/).click();

    await expect(page.getByRole('alert').filter({ hasText: '加载失败' }).first()).toBeVisible({ timeout: 5000 });
    // hasSettled 在 finally 里无条件加 key ⇒ 5xx 也「已定态」。少判 !loadErrors 就会说 404。
    await expect(page.getByText('返回 404'), '把加载失败伪装成端点未上线 ⇒ 管理员停止排障').toHaveCount(0);
    await expect(page.getByTestId('ops-load-failed-llm')).toBeVisible();
    await expect(page.getByText('数据加载中…')).toHaveCount(0);
  });

  test('M2 运营指标在途：显骨架而不是「数据加载中…」文本', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/ops-metrics**', async (r) => {
      await new Promise((s) => setTimeout(s, 2500));
      await r.fulfill({ status: 404, ...J({}) });
    });
    await page.goto(MANAGE_ROUTE);
    await tab(page, /运营指标/).click();
    await expect(page.getByTestId('skel-ops-llm')).toBeVisible({ timeout: 2000 });
  });

  test('M3 成员管理在途：不得先断言「暂无部门管理员」', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/admin-grants**', async (r) => {
      await new Promise((s) => setTimeout(s, 2000));
      await r.fulfill(J({ items: [], grantable_owner_depts: [] }));
    });
    await page.goto(MANAGE_ROUTE);
    await tab(page, /成员管理/).click();

    await expect(page.getByTestId('skel-admin-grants')).toBeVisible({ timeout: 2000 });
    await expect(page.getByText('暂无部门管理员，点「授予」添加')).toHaveCount(0);
    // 真到达且确实为空 → 这时才该说「暂无」
    await expect(page.getByTestId('skel-admin-grants')).toHaveCount(0, { timeout: 6000 });
    await expect(page.getByText('暂无部门管理员，点「授予」添加')).toBeVisible();
  });

  test('M4 已授权 5xx：不得完全静默（基线 alert=0、重试=0）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/access-grants**', (r) => r.fulfill({ status: 500, ...J({}) }));
    await page.goto(MANAGE_ROUTE);
    await tab(page, /^审批/).click();

    const alert = page.getByRole('alert').filter({ hasText: '加载失败' });
    await expect(alert.first(), '错误出口不得被门在「数据存在」上').toBeVisible({ timeout: 5000 });
    await expect(alert.first().getByRole('button', { name: '重试' })).toBeEnabled();
  });

  test('M5 台账 5xx：不得同时断言「暂无文档，先上传一篇吧」', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/my-docs**', (r) => r.fulfill({ status: 500, ...J({}) }));
    await page.goto(MANAGE_ROUTE);
    await tab(page, /文档管理/).click();

    await expect(page.getByRole('alert').filter({ hasText: '加载失败' }).first()).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('暂无文档，先上传一篇吧'), '加载失败 ≠ 一篇都没有').toHaveCount(0);
    await expect(page.getByText('文档列表暂不可用——上方错误条可重试。')).toBeVisible();
  });

  test('M5b 成员/审批 5xx：不得与「暂无」并存（M5 同款形状，另两处组件）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/admin-grants**', (r) => r.fulfill({ status: 500, ...J({}) }));
    await page.route('**/api/kb/approval-history**', (r) => r.fulfill({ status: 500, ...J({}) }));
    await page.goto(MANAGE_ROUTE);

    await tab(page, /成员管理/).click();
    await expect(page.getByRole('alert').filter({ hasText: '加载失败' }).first()).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('暂无部门管理员，点「授予」添加')).toHaveCount(0);
    await expect(page.getByText('部门管理员名单暂不可用——上方错误条可重试。')).toBeVisible();

    await tab(page, /^审批/).click();
    await expect(page.getByText('暂无审批历史 —— 通过 / 驳回 / 撤销 / 采纳后，记录会在这里留痕。')).toHaveCount(0);
    await expect(page.getByText('审批历史暂不可用——上方错误条可重试。')).toBeVisible();
  });

  test('M6 预取后点击仍有 tab 指示（判据改为「数据是否真在途」）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/ops-metrics**', async (r) => {
      await new Promise((s) => setTimeout(s, 2500));
      await r.fulfill(J({ window_days: 30, llm_available: false, slo_available: false, admission_available: false }));
    });
    await page.goto(MANAGE_ROUTE);
    await expect(page.locator('.kb-card').first()).toBeVisible();

    // 先悬停触发预取（>150ms 闸），再点击——基线判据下此时 tab-loading = 0
    await tab(page, /运营指标/).hover();
    await page.waitForTimeout(400);
    await tab(page, /运营指标/).click();
    await expect(page.getByTestId('tab-loading'), '预取尚未回来时点击，指示必须仍在').toBeVisible();
    await expect(page.getByTestId('tab-loading')).toHaveCount(0, { timeout: 6000 });
  });

  test('M7 在途判据覆盖 ensureTabLoaded 派出的**全部** job，不只是有 loader key 的那些', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    // dash 派 5 个 job。这两个走 null 哨兵、不在 withLoader 里 —— 名单式判据表达不了它们，
    // 评审实测此场景下指示恒为 0。只延迟它们，其余照常快返。
    for (const p of ['**/api/kb/feedback-review**', '**/api/kb/review-tasks**']) {
      await page.route(p, async (r) => {
        await new Promise((s) => setTimeout(s, 3000));
        await r.fulfill(J({ items: [] }));
      });
    }
    await page.goto(MANAGE_ROUTE);

    const busy = page.getByTestId('tab-loading');
    await expect(busy, 'dash 仍有 job 在途，指示必须点亮').toBeVisible({ timeout: 2500 });
    await expect(busy).toHaveAttribute('role', 'tab');
    await expect(busy).toHaveCount(0, { timeout: 8000 });
  });
});
