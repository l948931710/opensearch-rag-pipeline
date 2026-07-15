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

  test('非 rejected/非入库受阻 行不显重交入口（回归）', async ({ page }) => {
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

test.describe('状态可辨 — 待放行/入库受阻（批次ε-3 R1）', () => {
  test('同为已采纳未入库的四行：待放行/已隔离/未入索引/正常排队 徽章与提示互异', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    const REG = (id: string, badge: string | null) => MINE({
      contribution_id: id, state: 'registering', review_status: 'accepted',
      ingestion_status: 'registered', doc_id: `D_${id}`, doc_badge: badge,
    });
    await mockMine(page, {
      mine: [REG('a', '待审核'), REG('b', '已隔离'), REG('c', '未入索引'), REG('d', '排队中')],
    });
    await page.goto(ROUTE);
    // 待放行：徽章 + 「找谁」提示
    await expect(page.getByText('已采纳·待放行')).toBeVisible();
    await expect(page.getByTestId('mycontrib-approval-hint')).toContainText('知识库管理员放行');
    // 死链两态：入库受阻徽章 ×2 + 两种互异提示（隔离 vs 空块）
    await expect(page.getByText('入库受阻')).toHaveCount(2);
    const stalls = page.getByTestId('mycontrib-stall-reason');
    await expect(stalls).toHaveCount(2);
    await expect(stalls.nth(0)).toContainText('敏感信息隔离');
    await expect(stalls.nth(1)).toContainText('未能生成可检索内容');
    // 正常排队：回落默认徽章
    await expect(page.getByText('已采纳·待入库')).toBeVisible();
    guard.assertClean();
  });

  test('老后端缺 doc_badge 字段 → 全部回落默认徽章零提示（前向兼容回归）', async ({ page }) => {
    await mockMine(page, {
      mine: [MINE({ contribution_id: 'r9', state: 'registering', review_status: 'accepted', ingestion_status: 'registered', doc_id: 'D9', doc_badge: undefined })],
    });
    await page.goto(ROUTE);
    await expect(page.getByText('已采纳·待入库')).toBeVisible();
    await expect(page.getByTestId('mycontrib-approval-hint')).toHaveCount(0);
    await expect(page.getByTestId('mycontrib-stall-reason')).toHaveCount(0);
  });
});

test.describe('统计卡口径与窗口标注（批次ε-3 R3）', () => {
  test('统计卡：已入库(全部时间累计)/本月提交(含待审核、已驳回)/贡献者(近 90 天)；缺口卡头+空态带「近 N 天」', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockMine(page, { mine: [] });
    await page.route('**/api/kb/gaps*', (r) => r.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ items: [], summary: { unanswered: 0, answered: 12, this_month: 4, contributors: 6 }, has_more: false, window_days: 30 }),
    }));
    await page.goto(ROUTE);
    // 口径修正后的标签/hint（标签不再把 searchable 计数叫「已采纳」；隐藏口径如实披露）
    await expect(page.getByText('已入库', { exact: true })).toBeVisible();
    await expect(page.getByText('全部时间累计')).toBeVisible();
    await expect(page.getByText('本月提交', { exact: true })).toBeVisible();
    await expect(page.getByText('含待审核、已驳回')).toBeVisible();
    await expect(page.getByText('近 90 天有提交')).toBeVisible();
    await expect(page.getByText('已采纳', { exact: true })).toHaveCount(0);
    // 缺口窗口标注：卡头 + 空态（「全被回答」vs「老缺口过期出窗」此前文案完全相同）
    await expect(page.getByTestId('gap-window-note')).toContainText('近 30 天');
    // ε-5 R2 空态：标题纳窗 + 出窗免责副题（「全被回答」vs「过期出窗」两成因不再同文案）
    await expect(page.getByText('近 30 天内暂无未答出的提问')).toBeVisible();
    await expect(page.getByText('更早的提问已超出统计窗口——不在此列表，不代表已解决。')).toBeVisible();
    guard.assertClean();
  });
});

test.describe('待回答 — 高频排行 Top 30（批次ε-4）', () => {
  const GAPS = (n: number) => Array.from({ length: n }, (_, i) => ({
    question: `高频问题第${i + 1}号是什么？`, asks: 100 - i, last_days: i + 1, dept: 'finance',
    kind: 'no_result', question_hash: `h${i + 1}`, source_message_id: `m${i + 1}`, has_pending_contribution: false,
  }));

  test('截断态：30 行+名次 1..30、卡头徽标=87、尾注两口径、无「加载更多」、卡头「近一年」', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockMine(page, { mine: [] });
    await page.route('**/api/kb/gaps*', (r) => r.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ items: GAPS(30), summary: { unanswered: 87, answered: 0, this_month: 0, contributors: 0 }, has_more: true, window_days: 365 }),
    }));
    await page.goto(ROUTE);
    await expect(page.getByTestId('gap-rank')).toHaveCount(30);
    await expect(page.getByTestId('gap-rank').first()).toHaveText('1');
    await expect(page.getByTestId('gap-rank').nth(29)).toHaveText('30');
    await expect(page.getByTestId('gap-total-badge')).toHaveText('87');
    await expect(page.getByTestId('gap-top-note')).toHaveText('仅显示询问最多的前 30 条 · 共 87 条待回答');
    await expect(page.getByRole('button', { name: '加载更多' })).toHaveCount(0);
    await expect(page.getByTestId('gap-window-note')).toContainText('近一年');
    guard.assertClean();
  });

  test('未截断（12/12）：尾注自隐、徽标=12（不误导「还有更多被截掉」）', async ({ page }) => {
    await mockMine(page, { mine: [] });
    await page.route('**/api/kb/gaps*', (r) => r.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ items: GAPS(12), summary: { unanswered: 12, answered: 0, this_month: 0, contributors: 0 }, has_more: false, window_days: 365 }),
    }));
    await page.goto(ROUTE);
    await expect(page.getByTestId('gap-rank')).toHaveCount(12);
    await expect(page.getByTestId('gap-total-badge')).toHaveText('12');
    await expect(page.getByTestId('gap-top-note')).toHaveCount(0);
  });
});

test.describe('状态可辨收尾 — 隐形态补全与分流重投（批次ε-5 R1）', () => {
  const REG5 = (over: Record<string, unknown> = {}) => MINE({
    contribution_id: 'r5', state: 'registering', review_status: 'accepted',
    ingestion_status: 'registered', doc_id: 'D5', review_note: '', ...over,
  });

  test('已隔离行重投：弹窗顶警示明说「再次被隔离」+ 旧稿预填保留（底稿改）', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockMine(page, { mine: [REG5({ doc_badge: '已隔离' })] });
    await page.goto(ROUTE);
    await expect(page.getByTestId('mycontrib-stall-reason')).toContainText('敏感信息隔离');
    await page.getByTestId('mycontrib-reopen').click();
    await expect(page.getByTestId('contribute-warning')).toContainText('再次被隔离');
    await expect(page.getByPlaceholder(/写下步骤或要点/)).toHaveValue('至少 5 年，依据财务档案管理制度第 3 章。');
    guard.assertClean();
  });

  test('未入索引行重投：常规改稿警示（无「隔离」措辞）——按成因分流', async ({ page }) => {
    await mockMine(page, { mine: [REG5({ doc_badge: '未入索引' })] });
    await page.goto(ROUTE);
    await page.getByTestId('mycontrib-reopen').click();
    const warning = page.getByTestId('contribute-warning');
    await expect(warning).toContainText('修改完善');
    await expect(warning).not.toContainText('隔离');
  });

  test('处理失败（隐形态补全）→「入库受阻」+专属提示+重投；内容未变 →「同内容已在库」+去重提示、无重投', async ({ page }) => {
    await mockMine(page, {
      mine: [REG5({ contribution_id: 'pf', doc_badge: '处理失败' }),
             REG5({ contribution_id: 'dup', doc_badge: '内容未变' })],
    });
    await page.goto(ROUTE);
    await expect(page.getByText('入库受阻')).toBeVisible();
    await expect(page.getByTestId('mycontrib-stall-reason')).toContainText('入库处理失败');
    await expect(page.getByText('同内容已在库')).toBeVisible();
    await expect(page.getByTestId('mycontrib-duplicate-hint')).toContainText('完全相同');
    await expect(page.getByTestId('mycontrib-reopen')).toHaveCount(1);   // 仅处理失败行有
    await expect(page.getByText('已采纳·待入库')).toHaveCount(0);        // 两个隐形态都不再冒充排队
  });

  test('rejected 行重投弹窗无警示条（警示只属于入库受阻成因）', async ({ page }) => {
    await mockMine(page, { mine: [MINE()] });
    await page.goto(ROUTE);
    await page.getByTestId('mycontrib-reopen').click();
    // 「提交贡献」全 src 唯一（弹窗提交钮）——'贡献知识' 会 strict-mode 多命中页头按钮+弹窗标题
    await expect(page.getByRole('button', { name: '提交贡献' })).toBeVisible();
    await expect(page.getByTestId('contribute-warning')).toHaveCount(0);
  });
});
