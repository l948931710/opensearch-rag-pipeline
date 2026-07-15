import { test, expect, Page } from '@playwright/test';
import { attachConsoleGuard } from './ux-gate.helpers';

// ─────────────────────────────────────────────────────────────────────────────
// 批次ε-1「贡献审核体验」硬门（断言逐条落实 auditor 验收条件）：
//   B1 全文展开：溢出才显控件；展开=去 clamp、逐字等于原文；可收起；键盘可达；零网络请求
//   B2 采纳前修订：预填、只下发变更字段、未修订=只发 permission_level、取消弃改
//   B3 缺口溯源徽标：有 gap_query 显、无则隐
//   B4 has_more：显「加载更多」→ 按 offset 追加 → 尽头收起
// 进入方式与其它硬门一致：?token= 透传 + mock /api/kb/whoami（绝不用 ?preview——
// 其 authed 请求被合成 503，page.route 截不到）。
// ─────────────────────────────────────────────────────────────────────────────
const ROUTE = '/console/contribute?token=e2e-fake-token';

const LONG_CONTENT = Array.from({ length: 8 }, (_, i) =>
  `第 ${i + 1} 步：清洁流道并检查顶针磨损情况，确认无划伤后涂抹专用防锈油。`).join('\n');

const ITEM = (over: Record<string, unknown> = {}) => ({
  contribution_id: 'c1', question: '模具保养周期是多久？', content: LONG_CONTENT,
  category_dept: 'production', author_id: 'u9', author_name: '王伟',
  review_status: 'pending', ingestion_status: 'none', state: 'pending',
  doc_id: null, review_note: '', created_at: '2026-06-29', reviewed_at: null,
  source_message_id: null, gap_query: null, ...over,
});

interface MockOpts {
  role?: 'dept_admin' | 'kb_admin';
  managed?: string[];
  pending?: object[];
  pendingPages?: Record<string, { items: object[]; has_more: boolean }>;  // key=offset
}
async function mockContribute(page: Page, o: MockOpts = {}) {
  await page.route('**/api/**', (r) => r.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], questions: [], docs: [], total: 0, has_more: false }),
  }));
  await page.route('**/api/kb/whoami', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      user_id: 'admin1', display_name: '生产部管理员', role: o.role ?? 'dept_admin',
      can_manage_kb: true, acl_groups: ['production'],
      managed_owner_depts: o.managed ?? ['production'],
    }),
  }));
  await page.route('**/api/kb/gaps*', (r) => r.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], summary: { unanswered: 0, answered: 0, this_month: 0, contributors: 0 }, has_more: false }),
  }));
  await page.route('**/api/kb/contributions/pending*', (r) => {
    if (o.pendingPages) {
      const offset = new URL(r.request().url()).searchParams.get('offset') || '0';
      const pg = o.pendingPages[offset] ?? { items: [], has_more: false };
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify(pg) });
    }
    return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ items: o.pending ?? [], has_more: false }) });
  });
}

const contentOf = (page: Page) => page.getByTestId('contrib-content');

test.describe('贡献审核 — B1 全文展开', () => {
  test('溢出内容：初始截断 → 展开逐字全文 → 可收起；键盘可达；纯前端零请求', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockContribute(page, { pending: [ITEM()] });
    await page.goto(ROUTE);
    const content = contentOf(page);
    await expect(content).toBeVisible();
    // 初始：确实溢出（验收条件=运行时几何，不拍字符数）
    expect(await content.evaluate((el) => el.scrollHeight > el.clientHeight + 1)).toBe(true);
    const expand = page.getByTestId('contrib-expand');
    await expect(expand).toHaveText('展开全文');
    // 展开期间不得有任何贡献接口请求（纯前端态）
    const reqs: string[] = [];
    page.on('request', (q) => { if (q.url().includes('/api/kb/contributions')) reqs.push(q.url()); });
    await expand.click();
    expect(await content.evaluate((el) => el.scrollHeight <= el.clientHeight + 1)).toBe(true);
    await expect(content).toHaveText(LONG_CONTENT);   // 逐字等于原文（pre-wrap 换行保留）
    await expect(expand).toHaveText('收起');
    // 键盘可达：聚焦 + Enter 触发收起
    await expand.focus();
    await page.keyboard.press('Enter');
    expect(await content.evaluate((el) => el.scrollHeight > el.clientHeight + 1)).toBe(true);
    expect(reqs, '展开/收起不得触发网络请求').toHaveLength(0);
    guard.assertClean();
  });

  test('短内容不溢出 → 无展开控件（零噪声）', async ({ page }) => {
    await mockContribute(page, { pending: [ITEM({ content: '短答案。' })] });
    await page.goto(ROUTE);
    await expect(contentOf(page)).toBeVisible();
    await expect(page.getByTestId('contrib-expand')).toHaveCount(0);
  });
});

test.describe('贡献审核 — B2 采纳前修订', () => {
  test('修订：预填原值 → 改内容+改归属 → 请求体只含变更字段（question 不下发）', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockContribute(page, { pending: [ITEM()], managed: ['production', 'quality'] });
    let acceptBody: Record<string, unknown> | null = null;
    await page.route('**/api/kb/contributions/*/accept', (r) => {
      acceptBody = r.request().postDataJSON();
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ contribution_id: 'c1', review_status: 'accepted', ingestion_status: 'searchable', state: 'searchable', ok: true }) });
    });
    await page.goto(ROUTE);
    await page.getByTestId('contrib-revise').click();
    // 预填断言
    await expect(page.getByTestId('contrib-revise-q')).toHaveValue('模具保养周期是多久？');
    await expect(page.getByTestId('contrib-revise-c')).toHaveValue(LONG_CONTENT);
    await page.getByTestId('contrib-revise-c').fill('修订后的标准答案正文。');
    await page.getByTestId('contrib-revise-dept').selectOption('quality');
    // 改了归属 → 按钮文案显式带目标部门（不可逆入库前的零点击确认线索）
    const acceptBtn = page.getByTestId('contrib-accept-revised');
    await expect(acceptBtn).toContainText('采纳到');
    await acceptBtn.click();
    await expect.poll(() => acceptBody).not.toBeNull();
    expect(acceptBody).toMatchObject({ permission_level: 'dept_internal', content: '修订后的标准答案正文。', category_dept: 'quality' });
    expect(acceptBody).not.toHaveProperty('question');
    guard.assertClean();
  });

  test('未修订直接采纳 → 请求体只有 permission_level（后端「缺省=保留原值」契约）', async ({ page }) => {
    await mockContribute(page, { pending: [ITEM()] });
    let acceptBody: Record<string, unknown> | null = null;
    await page.route('**/api/kb/contributions/*/accept', (r) => {
      acceptBody = r.request().postDataJSON();
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify({ ok: true }) });
    });
    await page.goto(ROUTE);
    await expect(contentOf(page)).toBeVisible();
    await page.getByRole('button', { name: '采纳', exact: true }).click();
    await expect.poll(() => acceptBody).not.toBeNull();
    expect(Object.keys(acceptBody!)).toEqual(['permission_level']);
  });

  test('取消弃改：改坏字段 → 取消 → 重开修订字段还原原值', async ({ page }) => {
    await mockContribute(page, { pending: [ITEM()] });
    await page.goto(ROUTE);
    await page.getByTestId('contrib-revise').click();
    await page.getByTestId('contrib-revise-q').fill('被改坏的问题');
    await page.getByTestId('contrib-revise-cancel').click();
    await expect(page.getByTestId('contrib-revise-q')).toHaveCount(0);
    await page.getByTestId('contrib-revise').click();
    await expect(page.getByTestId('contrib-revise-q')).toHaveValue('模具保养周期是多久？');
  });
});

test.describe('贡献审核 — B3 缺口溯源徽标', () => {
  test('带 gap_query 的行显「来自缺口」；无溯源的行不显', async ({ page }) => {
    await mockContribute(page, {
      pending: [
        ITEM({ contribution_id: 'g1', question: '如何申请密钥？', content: '短', gap_query: '密钥在哪申请', source_message_id: 'm1' }),
        ITEM({ contribution_id: 'g2', content: '短' }),
      ],
    });
    await page.goto(ROUTE);
    await expect(page.getByTestId('contrib-from-gap')).toHaveCount(1);
    await expect(page.getByTestId('contrib-from-gap')).toHaveAttribute('title', '密钥在哪申请');
  });
});

test.describe('贡献审核 — B4 队列分页诚实', () => {
  test('has_more → 计数带 + 且显「加载更多」；点击按 offset 追加；尽头收起', async ({ page }) => {
    const guard = attachConsoleGuard(page);
    await mockContribute(page, {
      pendingPages: {
        '0': { items: [ITEM({ contribution_id: 'a', question: '问题甲？', content: '短' }), ITEM({ contribution_id: 'b', question: '问题乙？', content: '短' })], has_more: true },
        '2': { items: [ITEM({ contribution_id: 'c', question: '问题丙？', content: '短' })], has_more: false },
      },
    });
    await page.goto(ROUTE);
    await expect(page.getByText('2+')).toBeVisible();
    const more = page.getByTestId('contrib-load-more');
    await expect(more).toBeVisible();
    await more.click();
    await expect(page.getByText('问题丙？')).toBeVisible();
    await expect(page.getByText('问题甲？')).toBeVisible();   // 追加而非替换
    await expect(more).toHaveCount(0);                        // 尽头收起
    guard.assertClean();
  });
});

test.describe('贡献审核 — δ-3 回归', () => {
  test('kb_admin 卡头兜底标注仍在（新增展开/修订 UI 不得挤掉）', async ({ page }) => {
    await mockContribute(page, { role: 'kb_admin', pending: [ITEM({ content: '短' })] });
    await page.goto(ROUTE);
    await expect(page.getByTestId('contrib-orphan-hint')).toBeVisible();
  });
});
