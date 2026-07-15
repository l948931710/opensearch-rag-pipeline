import { test, expect, Page, Route } from '@playwright/test';
import { attachConsoleGuard } from './ux-gate.helpers';

// ─────────────────────────────────────────────────────────────────────────────
// 「忽略此缺口」硬门（2026-07-15 拍板交 dept_admin；断言↔auditor 验收条件逐条对应）：
//   AC1 管理员每行可见「忽略」（gap-dismiss）
//   AC2 员工 DOM 零变化：无 gap-dismiss/gap-restore 节点，「回答」/徽章原样
//   AC3 忽略成功=行内翻转（已忽略+撤销），行不消失；POST body 带 hash/question/reason
//   AC4 撤销出路：点撤销调 /restore 同 hash，行翻回原状=「误点等价于无事发生」
//   AC5 403 诚实降级：notice 失败提示，行保持原状（绝不悄悄移除/死等）
//   AC6 500 失败不乐观翻转：行保持「回答」态
//   AC7 忙态防重入：慢请求期间连点撤销只发一次 /restore
//   AC8 刷新语义：忽略/撤销全程零 loadGaps 重拉（GET /api/kb/gaps 仅初始 1 次）
//   AC9 计数一致：忽略后卡头徽标（=summary.unanswered）不变、无 NaN
//   AC10 mock 注册晚于 **/api/** 兜底；入口 ?token= + mock whoami（绝不 ?preview）
// ─────────────────────────────────────────────────────────────────────────────
const ROUTE = '/console/contribute?token=e2e-fake-token';

const GAP = (over: Record<string, unknown> = {}) => ({
  question: '模具保养周期是多久', asks: 5, last_days: 3, dept: 'production',
  kind: 'no_result', question_hash: 'a'.repeat(64), source_message_id: 'm1',
  has_pending_contribution: false, phrasings: 1, ...over,
});
const GAPS_BODY = {
  items: [GAP(), GAP({ question: '发票抬头怎么填', question_hash: 'b'.repeat(64), asks: 2 })],
  summary: { unanswered: 7, answered: 3, this_month: 1, contributors: 2 },
  window_days: 365, has_more: false,
};

interface MockOpts {
  canManage?: boolean;
  dismiss?: (r: Route) => Promise<void> | void;   // 缺省 200 {ok:true,affected:1}
  restore?: (r: Route) => Promise<void> | void;
  dismissed?: object[];                            // GET /gaps/dismissed 的 items
}
async function mockGaps(page: Page, o: MockOpts = {}) {
  const seen = { gapsGet: 0, dismiss: 0, restore: 0, dismissBodies: [] as unknown[], restoreBodies: [] as unknown[] };
  // AC10：兜底先注册，具体路由后注册（Playwright 后注册者优先）
  await page.route('**/api/**', (r) => r.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], questions: [], docs: [], total: 0, has_more: false }),
  }));
  await page.route('**/api/kb/whoami', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      user_id: 'u1', display_name: o.canManage ? '生产部管理员' : '一线员工',
      role: o.canManage ? 'dept_admin' : 'employee',
      can_manage_kb: !!o.canManage, acl_groups: ['production'],
      managed_owner_depts: o.canManage ? ['production'] : [],
    }),
  }));
  // 注意 glob 边界：'**/api/kb/gaps*' 的 * 不跨 '/'，不会误伤 /gaps/dismiss（审计风险4，已实证）
  await page.route('**/api/kb/gaps*', (r) => {
    seen.gapsGet += 1;
    return r.fulfill({ contentType: 'application/json', body: JSON.stringify(GAPS_BODY) });
  });
  await page.route('**/api/kb/gaps/dismiss', async (r) => {
    seen.dismiss += 1; seen.dismissBodies.push(r.request().postDataJSON());
    if (o.dismiss) return o.dismiss(r);
    return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, affected: 1 }) });
  });
  await page.route('**/api/kb/gaps/restore', async (r) => {
    seen.restore += 1; seen.restoreBodies.push(r.request().postDataJSON());
    if (o.restore) return o.restore(r);
    return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, affected: 1 }) });
  });
  await page.route('**/api/kb/gaps/dismissed', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({ items: o.dismissed ?? [] }),
  }));
  return seen;
}

function rows(page: Page) {
  return page.locator('[data-testid="gap-rank"]');
}
async function dismissViaDialog(page: Page, reason = '') {
  await page.getByTestId('gap-dismiss').first().click();
  const dlg = page.getByRole('dialog');
  await expect(dlg).toBeVisible();
  if (reason) await dlg.locator('textarea').fill(reason);
  await dlg.getByRole('button', { name: '忽略', exact: true }).click();
}

test.describe('待回答 — 忽略此缺口', () => {
  test.beforeEach(async ({ page }) => { attachConsoleGuard(page); });

  test('AC1+AC3+AC4+AC8+AC9：管理员忽略→行内翻转可撤销，撤销后等价于无事发生', async ({ page }) => {
    const seen = await mockGaps(page, { canManage: true });
    await page.goto(ROUTE);
    await expect(rows(page)).toHaveCount(2);
    await expect(page.getByTestId('gap-dismiss')).toHaveCount(2);            // AC1 每行可见
    const badgeBefore = await page.getByTestId('gap-total-badge').innerText();

    await dismissViaDialog(page, '闲聊噪音');
    // AC3：行不消失，末尾切「已忽略 + 撤销」
    await expect(rows(page)).toHaveCount(2);
    await expect(page.getByTestId('gap-dismissed-hint')).toHaveCount(1);
    await expect(page.getByTestId('gap-restore')).toHaveCount(1);
    expect(seen.dismiss).toBe(1);
    expect(seen.dismissBodies[0]).toMatchObject({
      question_hash: 'a'.repeat(64), question: '模具保养周期是多久', reason: '闲聊噪音',
    });
    // AC9：卡头徽标（全集数=summary.unanswered）不因本地翻转变化、无 NaN
    await expect(page.getByTestId('gap-total-badge')).toHaveText(badgeBefore);

    // AC4：撤销 → /restore 同 hash → 行翻回「回答」原状
    await page.getByTestId('gap-restore').click();
    await expect(page.getByTestId('gap-dismissed-hint')).toHaveCount(0);
    await expect(page.getByRole('button', { name: '回答' })).toHaveCount(2);
    expect(seen.restore).toBe(1);
    expect(seen.restoreBodies[0]).toMatchObject({ question_hash: 'a'.repeat(64) });
    // AC8：全程零重拉（初始 1 次 GET）
    expect(seen.gapsGet).toBe(1);
  });

  test('AC2：员工 DOM 零变化——无忽略/撤销节点，行内元素原样', async ({ page }) => {
    await mockGaps(page, { canManage: false });
    await page.goto(ROUTE);
    await expect(rows(page)).toHaveCount(2);
    await expect(page.getByTestId('gap-dismiss')).toHaveCount(0);
    await expect(page.getByTestId('gap-restore')).toHaveCount(0);
    await expect(page.getByTestId('gap-dismissed-hint')).toHaveCount(0);
    await expect(page.getByTestId('gap-dismissed-toggle')).toHaveCount(0);   // 折叠区零节点
    await expect(page.getByRole('button', { name: '回答' })).toHaveCount(2); // 既有动作原样
  });

  test('AC11：「已忽略」折叠区——展开拉现值，恢复=restore+移出+重拉缺口列表', async ({ page }) => {
    const seen = await mockGaps(page, {
      canManage: true,
      dismissed: [{ question_hash: 'c'.repeat(64), question_preview: '今天食堂有什么菜',
                    reason: '闲聊噪音', dismissed_by_name: '生产部管理员',
                    dismissed_at: '2026-07-15T10:00:00' }],
    });
    await page.goto(ROUTE);
    const toggle = page.getByTestId('gap-dismissed-toggle');
    await expect(toggle).toBeVisible();                       // 管理员可见折叠头
    await expect(page.getByTestId('gap-dismissed-row')).toHaveCount(0);  // 默认收起
    await toggle.click();
    await expect(page.getByTestId('gap-dismissed-row')).toHaveCount(1);  // 展开拉现值
    await expect(page.getByTestId('gap-dismissed-row')).toContainText('今天食堂有什么菜');
    await expect(page.getByTestId('gap-dismissed-row')).toContainText('闲聊噪音');
    await page.getByTestId('gap-dismissed-restore').click();
    await expect(page.getByTestId('gap-dismissed-row')).toHaveCount(0);  // 恢复后移出本区
    expect(seen.restore).toBe(1);
    expect(seen.restoreBodies[0]).toMatchObject({ question_hash: 'c'.repeat(64) });
    expect(seen.gapsGet).toBe(2);   // 恢复触发一次重拉（缺口回到「待回答」的诚实闭环）
  });

  test('AC5：后端 403 → 失败提示 + 行保持原状（不悄悄移除/不死等）', async ({ page }) => {
    await mockGaps(page, {
      canManage: true,
      dismiss: (r) => r.fulfill({ status: 403, contentType: 'application/json', body: JSON.stringify({ detail: '无知识库管理权限' }) }),
    });
    await page.goto(ROUTE);
    await dismissViaDialog(page);
    await expect(page.getByRole('dialog').getByText('忽略失败')).toBeVisible();
    await page.getByRole('dialog').getByRole('button', { name: '知道了' }).click();
    await expect(page.getByTestId('gap-dismissed-hint')).toHaveCount(0);     // 未翻转
    await expect(page.getByRole('button', { name: '回答' })).toHaveCount(2);
  });

  test('AC6：后端 500 → 不乐观翻转，行保持「回答」态', async ({ page }) => {
    await mockGaps(page, {
      canManage: true,
      dismiss: (r) => r.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'boom' }) }),
    });
    await page.goto(ROUTE);
    await dismissViaDialog(page);
    await expect(page.getByRole('dialog').getByText('忽略失败')).toBeVisible();
    await page.getByRole('dialog').getByRole('button', { name: '知道了' }).click();
    await expect(page.getByTestId('gap-dismissed-hint')).toHaveCount(0);
  });

  test('AC7：慢请求期间连点撤销只发一次 /restore（isBusy 防重入）', async ({ page }) => {
    let release: () => void = () => {};
    const gate = new Promise<void>((res) => { release = res; });
    const seen = await mockGaps(page, {
      canManage: true,
      restore: async (r) => {
        await gate;
        return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true, affected: 1 }) });
      },
    });
    await page.goto(ROUTE);
    await dismissViaDialog(page);
    await expect(page.getByTestId('gap-restore')).toHaveCount(1);
    const btn = page.getByTestId('gap-restore');
    await btn.click();
    await btn.click({ force: true }).catch(() => {});   // 第二次点击（disabled 时 force 也不应产生请求）
    release();
    await expect(page.getByTestId('gap-dismissed-hint')).toHaveCount(0);
    expect(seen.restore).toBe(1);
  });
});
