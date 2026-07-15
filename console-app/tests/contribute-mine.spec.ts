import { test, expect, Page } from '@playwright/test';
import { attachConsoleGuard } from './ux-gate.helpers';

// ─────────────────────────────────────────────────────────────────────────────
// 批次ε-2 Round1「告知→行动半环」作者侧硬门（与 contribute-review.spec.ts 审核侧对称）：
//   重交：rejected 行「修改重交」→ 弹窗带旧稿+原归属 → 提交体继承缺口溯源 →
//         原驳回记录保留 + 新 pending 行（新 contribution_id，非复活旧行）
//   原稿可见：点行标题展开 content 全文
//   失败原因：failed 行透出 ingestion_error；缺字段（老后端）→ 兜底句
//   空驳回理由 → 「未填写驳回理由」兜底
// 通知（钉钉工作通知）浏览器不可见，不设 UI 断言——由 pytest 侧硬门覆盖。
// ─────────────────────────────────────────────────────────────────────────────
const ROUTE = '/console/contribute?token=e2e-fake-token';

const MINE = (over: Record<string, unknown> = {}) => ({
  contribution_id: 'c1', question: '差旅报销凭证要保存几年？', content: '至少 5 年，依据财务档案管理制度第 3 章。',
  category_dept: 'finance', author_id: 'emp1', author_name: '张三',
  review_status: 'rejected', ingestion_status: 'none', state: 'rejected',
  doc_id: null, review_note: '', created_at: '2026-07-01', reviewed_at: '2026-07-02',
  source_message_id: 'm9', gap_query: '报销凭证保存年限', ingestion_error: null, ...over,
});

async function mockMine(page: Page, o: { mine?: object[]; mineAfterSubmit?: object[] } = {}) {
  let submitted = false;
  await page.route('**/api/**', (r) => r.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], questions: [], docs: [], total: 0, has_more: false }),
  }));
  await page.route('**/api/kb/whoami', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      user_id: 'emp1', display_name: '张三', role: 'employee',
      can_manage_kb: false, acl_groups: ['finance'], managed_owner_depts: [],
    }),
  }));
  await page.route('**/api/kb/gaps*', (r) => r.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], summary: { unanswered: 0, answered: 0, this_month: 0, contributors: 0 }, has_more: false }),
  }));
  await page.route('**/api/kb/contributions/mine*', (r) => r.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: (submitted && o.mineAfterSubmit) ? o.mineAfterSubmit : (o.mine ?? []), has_more: false }),
  }));
  await page.route('**/api/kb/contributions', (r) => {
    submitted = true;
    return r.fulfill({ contentType: 'application/json', body: JSON.stringify(MINE({ contribution_id: 'c2', state: 'pending', review_status: 'pending' })) });
  });
}

test.describe('我的贡献 — 修改重交', () => {
  test('rejected 行重交：弹窗带旧稿+原归属 → 提交体继承溯源 → 原驳回记录保留+新 pending 行', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockMine(page, {
      mine: [MINE()],
      mineAfterSubmit: [MINE({ contribution_id: 'c2', state: 'pending', review_status: 'pending' }), MINE()],
    });
    let submitBody: Record<string, unknown> | null = null;
    await page.route('**/api/kb/contributions', (r) => {
      submitBody = r.request().postDataJSON();
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify(MINE({ contribution_id: 'c2', state: 'pending' })) });
    });
    await page.goto(ROUTE);
    // 空驳回理由 → 兜底句（不留「已驳回」孤零零三个字）
    await expect(page.getByText('未填写驳回理由')).toBeVisible();
    await page.getByTestId('mycontrib-reopen').click();
    // 弹窗预填=旧稿逐字（不是空白起草）
    const modal = page.getByText('贡献知识').locator('..').locator('..').locator('..');
    await expect(modal.locator('input[type="text"]')).toHaveValue('差旅报销凭证要保存几年？');
    await expect(modal.locator('textarea')).toHaveValue('至少 5 年，依据财务档案管理制度第 3 章。');
    await expect(modal.locator('select')).toHaveValue('finance');
    // 改稿后提交
    await modal.locator('textarea').fill('至少 5 年；电子凭证同样适用，见财务档案管理制度 3.2。');
    await page.getByRole('button', { name: '提交贡献' }).click();
    await expect.poll(() => submitBody).not.toBeNull();
    expect(submitBody).toMatchObject({
      category_dept: 'finance',
      source_message_id: 'm9',                 // 溯源继承：接上原缺口关闭链路
      gap_query: '报销凭证保存年限',
    });
    // 原驳回记录保留（审计）+ 新 pending 行同屏
    await expect(page.getByTestId('mycontrib-reopen')).toBeVisible();
    await expect(page.getByText('待审核')).toBeVisible();
    guard.assertClean();
  });

  test('非 rejected 行不显重交入口（回归）', async ({ page }) => {
    await mockMine(page, { mine: [MINE({ state: 'pending', review_status: 'pending' })] });
    await page.goto(ROUTE);
    await expect(page.getByTestId('mycontrib-toggle')).toBeVisible();
    await expect(page.getByTestId('mycontrib-reopen')).toHaveCount(0);
  });
});

test.describe('我的贡献 — 原稿可见 / 失败原因', () => {
  test('点行标题展开原稿全文，再点收起', async ({ page }) => {
    await mockMine(page, { mine: [MINE()] });
    await page.goto(ROUTE);
    await expect(page.getByTestId('mycontrib-content')).toHaveCount(0);
    await page.getByTestId('mycontrib-toggle').click();
    await expect(page.getByTestId('mycontrib-content')).toHaveText('至少 5 年，依据财务档案管理制度第 3 章。');
    await page.getByTestId('mycontrib-toggle').click();
    await expect(page.getByTestId('mycontrib-content')).toHaveCount(0);
  });

  test('failed 行透出失败原因；老后端缺字段 → 兜底句不留空白', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockMine(page, {
      mine: [
        MINE({ contribution_id: 'f1', state: 'failed', review_status: 'accepted', ingestion_status: 'failed', ingestion_error: 'OSS 写入超时（trace: T1）' }),
        MINE({ contribution_id: 'f2', state: 'failed', review_status: 'accepted', ingestion_status: 'failed', ingestion_error: null }),
      ],
    });
    await page.goto(ROUTE);
    const reasons = page.getByTestId('mycontrib-fail-reason');
    await expect(reasons).toHaveCount(2);
    await expect(reasons.nth(0)).toHaveText('OSS 写入超时（trace: T1）');
    await expect(reasons.nth(1)).toContainText('入库失败');
    guard.assertClean();
  });
});

test.describe('价值反馈 — 被引用数（批次ε-2 R2）', () => {
  test('我的贡献：searchable 行显「被引用 N 次」（含 0）；hits 缺失/非入库行自隐', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockMine(page, {
      mine: [
        MINE({ contribution_id: 'h1', state: 'searchable', review_status: 'accepted', ingestion_status: 'searchable', doc_id: 'D1', hits: 6 }),
        MINE({ contribution_id: 'h2', state: 'searchable', review_status: 'accepted', ingestion_status: 'searchable', doc_id: 'D2', hits: null }),
        MINE({ contribution_id: 'h3', state: 'pending', review_status: 'pending' }),
      ],
    });
    await page.goto(ROUTE);
    const chips = page.getByTestId('mycontrib-hits');
    await expect(chips).toHaveCount(1);
    await expect(chips).toHaveText('被引用 6 次');
    guard.assertClean();
  });

  test('英雄榜：hits 非空显「引用 N」（0=真零照显）；null=算不出自隐；榜序仍按入库篇数', async ({ page }) => {
    await mockMine(page, { mine: [] });
    await page.route('**/api/kb/contributions/heroes*', (r) => r.fulfill({
      contentType: 'application/json', body: JSON.stringify({ items: [
        { rank: 1, author_id: 'u1', author_name: '李娜', count: 8, hits: 41 },
        { rank: 2, author_id: 'u2', author_name: '王伟', count: 5, hits: 0 },
        { rank: 3, author_id: 'u3', author_name: '陈强', count: 3, hits: null },
      ] }),
    }));
    await page.goto(ROUTE);
    const chips = page.getByTestId('hero-hits');
    await expect(chips).toHaveCount(2);
    await expect(chips.nth(0)).toHaveText('引用 41');
    await expect(chips.nth(1)).toHaveText('引用 0');
    // 榜序=入库篇数（hits 不改序）：李娜(8) 在 王伟(5) 前
    const names = await page.locator('section:has-text("英雄榜") .truncate').allInnerTexts();
    expect(names.findIndex((n) => n.includes('李娜'))).toBeLessThan(names.findIndex((n) => n.includes('王伟')));
  });
});
