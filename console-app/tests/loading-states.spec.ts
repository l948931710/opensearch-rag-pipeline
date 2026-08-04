import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, MANAGE_ROUTE, GOVERNANCE_MOCK, INSIGHTS_MOCK } from './node-mode.helpers';

/**
 * 硬门 —— 加载四态与请求合流（2026-08-04）。
 *
 * 断言 ↔ 审计验收条件：G1 ← B1；G2/G3 ← B2；G4 ← B3；G5 ← B5。
 *
 * 全部断言都落在**基线确定性会红**的点上（审计已逐条实测过基线行为）：
 *   B1 基线：stats 404 → 4 张骨架转 5 秒仍在转、0 alert、0 重试；
 *   B2 基线：governance 5xx 而 insights 正常 → [role=alert] 计数 0、0 重试、相关分区直接消失；
 *   B3 基线：stats 在途期间 StatusDistBar 已显示「暂无文档数据。」；
 *   B5 基线：dash→docs 对 /api/kb/stats 发出 2 个并发请求。
 */

const KB_ADMIN = { role: 'kb_admin' as const, managedRoots: [] };
const J = (body: unknown) => ({ contentType: 'application/json', body: JSON.stringify(body) });

test.describe('硬门 — 加载四态', () => {
  test('G1 stats 404：骨架必须停，且给出「端点未上线」与重试（基线转 5 秒仍在转）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/stats**', (r) => r.fulfill({ status: 404, ...J({ detail: 'not found' }) }));
    await page.goto(MANAGE_ROUTE);

    const placeholder = page.getByTestId('stats-unavailable');
    await expect(placeholder, '404 必须落到诚实占位，而不是继续转骨架').toBeVisible({ timeout: 5000 });
    // 基线在此刻是 4；`?? 0` 的兜底零也不得当成真实数字显出来
    await expect(page.locator('.animate-pulse')).toHaveCount(0);
    await expect(placeholder).toContainText('404');
    await expect(placeholder.getByRole('button', { name: '重试' })).toBeEnabled();
    // 这条是轮 2a 漏掉、被轮 2b 的 dept_admin 同款门抓出来的：资产卡换成占位后，下方的
    // 状态分布条仍会独立断言「暂无文档数据。」——那是**不知道**分布，不是分布为空。
    await expect(page.getByText('暂无文档数据。'), '未接入 ≠ 没有文档').toHaveCount(0);
  });

  test('G2 governance 5xx 而 insights 正常：必须逐端点上报（基线 alert=0）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/governance**', (r) => r.fulfill({ status: 500, ...J({}) }));
    await page.goto(MANAGE_ROUTE);

    const alert = page.getByRole('alert').filter({ hasText: '全库治理数据' });
    await expect(alert, '单边失败不得静默——基线此处 alert 计数为 0').toBeVisible();
    await expect(alert.getByRole('button', { name: '重试' })).toBeEnabled();
    // insights 正常 → 它自己的错误条不得出现（两条文案必须能分辨是谁坏了）
    await expect(page.getByRole('alert').filter({ hasText: '知识效果数据' })).toHaveCount(0);
    // 且 insights 侧内容照常渲染，没被 governance 的失败连坐
    await expect(page.getByText('全库知识效果')).toBeVisible();
  });

  test('G3 重试只重打失败的那个端点，不连坐正常端点', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    let govHits = 0, insHits = 0;
    await page.route('**/api/kb/governance**', (r) => { govHits++; return r.fulfill({ status: 500, ...J({}) }); });
    await page.route('**/api/kb/insights**', (r) => { insHits++; return r.fulfill(J(INSIGHTS_MOCK)); });
    await page.goto(MANAGE_ROUTE);

    const alert = page.getByRole('alert').filter({ hasText: '全库治理数据' });
    await expect(alert).toBeVisible();
    const govBefore = govHits, insBefore = insHits;
    await alert.getByRole('button', { name: '重试' }).click();
    await expect.poll(() => govHits).toBe(govBefore + 1);
    expect(insHits, '重试 governance 不该连带重打 insights').toBe(insBefore);
  });

  test('G6 两个端点都 404：占位说「未接入」，且文案里不得有字面 Markdown 星号', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/governance**', (r) => r.fulfill({ status: 404, ...J({}) }));
    await page.route('**/api/kb/insights**', (r) => r.fulfill({ status: 404, ...J({}) }));
    await page.goto(MANAGE_ROUTE);

    const box = page.getByTestId('gov-not-deployed');
    await expect(box, '404 不会自动变好，占位不能再说「稍后自动呈现」').toBeVisible();
    await expect(box).toContainText('未接入');
    await expect(box).toContainText('404');
    await expect(page.getByText('稍后自动呈现')).toHaveCount(0);
    // 这条挡的是一个真发生过的疏漏：文案里写了 `**未接入**`，星号被逐字渲染上屏。
    expect(await box.innerText(), '上屏文案不得含字面 Markdown 标记').not.toContain('**');
  });

  test('G7 单端点 404：insights 未接入必须单独上报（不再要求两个都缺）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/insights**', (r) => r.fulfill({ status: 404, ...J({}) }));
    await page.goto(MANAGE_ROUTE);

    const p = page.getByTestId('insights-not-deployed');
    await expect(p, '基线此处静默少掉两块内容，与「真无数据」不可区分').toBeVisible();
    await expect(p).toContainText('/api/kb/insights');
    // governance 正常 → 它自己的未接入提示不得出现；合并占位也不得出现（避免同一句说两遍）
    await expect(page.getByTestId('governance-not-deployed')).toHaveCount(0);
    await expect(page.getByTestId('gov-not-deployed')).toHaveCount(0);
    await expect(page.getByText('组织覆盖', { exact: true })).toBeVisible();
  });

  // G8 挡的是一个**相对基线的倒退**：合并占位曾用 hasSettled 冒充 notDeployed，而 hasSettled
  // 不区分「跑完没数据没错误（404）」与「跑完但错了（5xx）」。混合态下页面因此对一个返回 500
  // 的端点断言「返回 404，数字不会自动出现」，同屏还并排着它自己的「加载失败，请重试」。
  // 基线在这个态下不作任何 404 声明——是新引入的假陈述，且正落在本轮要立的不变量上。
  for (const [notDep, failed] of [['governance', 'insights'], ['insights', 'governance']] as const) {
    test(`G8 混合态：${notDep} 404 + ${failed} 5xx，绝不对 5xx 的端点声称「返回 404」`, async ({ page }) => {
      await mockNodeMode(page, KB_ADMIN);
      await page.route(`**/api/kb/${notDep}**`, (r) => r.fulfill({ status: 404, ...J({}) }));
      await page.route(`**/api/kb/${failed}**`, (r) => r.fulfill({ status: 500, ...J({}) }));
      await page.goto(MANAGE_ROUTE);

      // 404 那一侧：逐端点未接入行（互斥守卫必须用 notDeployed 而非「另一侧有没有数据」，
      // 否则混合态下另一侧恒为 null，这条会被误抑制）
      await expect(page.getByTestId(`${notDep}-not-deployed`)).toBeVisible();
      // 5xx 那一侧：带端点前缀的 LoadError + 重试
      const label = failed === 'insights' ? '知识效果数据' : '全库治理数据';
      await expect(page.getByRole('alert').filter({ hasText: label })).toBeVisible();

      // 合并块绝不出现——它会把两个端点一起说成 404
      await expect(page.getByTestId('gov-not-deployed')).toHaveCount(0);
      // 「稍后自动呈现」同样为假（两个都已 settled，不会再自动变好）
      await expect(page.getByText('稍后自动呈现')).toHaveCount(0);

      // 兜底：整页文本里不得出现「对 5xx 端点说 404」的组合
      const txt = await page.locator('main').innerText();
      const failedPath = `/api/kb/${failed}`;
      const idx = txt.indexOf(failedPath);
      if (idx >= 0) {
        expect(txt.slice(idx, idx + 60), `不得对返回 5xx 的 ${failedPath} 声称 404`).not.toContain('404');
      }
    });
  }

  test('G4 stats 在途：状态分布必须说「加载中」而不是「暂无文档数据」', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/stats**', async (r) => {
      await new Promise((s) => setTimeout(s, 2500));
      await r.fulfill(J({ total: 5, active: 5, retired: 0, chunks: 9, by_badge: { 已上线: 5 } }));
    });
    await page.goto(MANAGE_ROUTE);

    // 基线：此刻已经显示「暂无文档数据。」——一句在数据到达前就下的、可能为假的结论
    await expect(page.getByTestId('skel-status-dist')).toBeVisible({ timeout: 2000 });
    await expect(page.getByText('暂无文档数据。'), '在途期间不得断言为空').toHaveCount(0);

    // 数据到达后：骨架撤掉，且**仍然**不说「暂无文档数据」（因为确实有数据）——
    // 三态各自互斥。不断言「已上线」文本：它在 StatCard 标签与分布条图例里各有一处，
    // 裸 getByText 会 strict-mode 多命中（本断言要的是三态互斥，不是找那四个字）。
    await expect(page.getByTestId('skel-status-dist')).toHaveCount(0, { timeout: 6000 });
    await expect(page.getByText('暂无文档数据。')).toHaveCount(0);
  });

  test('G5 请求合流：dash→docs 只发一次 stats（基线 2 次）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    let statsHits = 0;
    await page.route('**/api/kb/stats**', async (r) => {
      statsHits++;
      await new Promise((s) => setTimeout(s, 1200));   // 制造「dash 的请求还没回就切 tab」的窗口
      await r.fulfill(J({ total: 3, active: 3, retired: 0, chunks: 4, by_badge: { 已上线: 3 } }));
    });
    await page.goto(MANAGE_ROUTE);
    // 不等 dash 的 stats 回来就切到文档管理——ensureTabLoaded 会从 docs 分支再调一次 loadStats
    await page.locator('[aria-label="管理台分区"]').getByRole('tab', { name: /文档管理/ }).click();
    await expect.poll(() => statsHits, { timeout: 6000 }).toBeGreaterThan(0);
    await page.waitForTimeout(2000);
    expect(statsHits, '两条 tab 路径共用同一份在途请求，不重复发').toBe(1);
  });
});
