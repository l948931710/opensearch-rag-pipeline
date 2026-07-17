import { test, expect } from '@playwright/test';

/**
 * 回归门 — 管理台切 tab / 搜索不再整页销毁重建（AppShell 视图容器 key）
 *
 * 曾经的 bug（commit d712305 引入，本分支已于 4632992 修好）：AppShell 的路由视图容器
 * `<div :key="route.fullPath">` 用了含 query 的 fullPath。管理台的切 tab 与文档搜索都只改
 * URL 的 query（router.replace({ query })），于是每次操作 key 都变 → Vue 销毁并重建整个视图子树：
 *   • 切 tab → 整个 ManageView 连同看板/台账重挂、onMounted 全量重拉 + Transition 淡入淡出
 *     中间空一帧 = 黑屏闪
 *   • 搜索框每输入一字就重建 → 输入框被销毁、焦点丢失 → 根本无法连续输入
 * 修复：key 用 route.path（不含 query）。本门锁死该行为，防再次回归（main 曾因 cherry-pick 落后重现）。
 *
 * 进入方式与其它硬门一致：?token= 透传免登 + mock /api/kb/whoami（绝不用 ?preview）。
 */

const MANAGE_ROUTE = '/console/manage?token=e2e-fake-token';

// ManageView 挂载会并发拉一串管理接口——先 catch-all 回空，再用 whoami 专属 mock 覆盖身份。
// （Playwright 后注册者优先。）空形状足以让 ManageView graceful 渲染出 tab 栏与文档管理页。
async function mockKbAdmin(page: import('@playwright/test').Page) {
  await page.route('**/api/**', (r) => r.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], questions: [], docs: [], total: 0, has_more: false }),
  }));
  await page.route('**/api/kb/whoami', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      user_id: 'admin1', display_name: '知识库管理员', role: 'kb_admin',
      can_manage_kb: true, acl_groups: ['marketing'], managed_owner_depts: ['marketing'],
    }),
  }));
}

test.describe('回归门 — 管理台切 tab / 搜索不整页重建', () => {
  test.beforeEach(async ({ page }) => { await mockKbAdmin(page); });

  test('切 tab / 搜索抖动不重挂视图（黑屏 + 全量重载根因）', async ({ page }) => {
    // /api/kb/stats 作「视图是否被重挂」的探针。注意 9ba0f90 起 ManageView 按 tab 惰性加载：
    // 【首次】激活某 tab 会合法补拉一次（_loadedTabs 记账）——门只断言首次激活之后的
    // query 抖动（搜索输入）与 tab 二次往返不再触发任何 stats 拉取：
    // key=path（修复）→ 0；key=fullPath（bug）→ 每次 query 变化都重挂 onMounted 重拉。
    let statsHits = 0;
    page.on('request', (r) => { if (r.url().includes('/api/kb/stats')) statsHits++; });

    await page.goto(MANAGE_ROUTE);
    const zone = page.locator('[aria-label="管理台分区"]');
    await expect(zone.getByRole('tab', { name: /概览看板/ })).toBeVisible();
    // 初次挂载已拉过一次概览统计 → 建立基线。
    await expect.poll(() => statsHits, { message: '初始应拉取一次 /api/kb/stats' }).toBeGreaterThan(0);

    // 首次进「文档管理」：允许惰性加载的一次性补拉。
    await zone.getByRole('tab', { name: /文档管理/ }).click();
    await expect(page).toHaveURL(/tab=docs/);
    await expect(page.getByPlaceholder('搜索文档名…')).toBeVisible();   // 文档管理页确已渲染
    await page.waitForTimeout(400);
    const afterFirstEntry = statsHits;

    // 探针①：搜索输入只改 URL query——fullPath key（bug）会每字销毁重建整个视图。
    await page.getByPlaceholder('搜索文档名…').fill('sop');
    await page.waitForTimeout(400);
    // 探针②：tab 二次往返——docs 已在 _loadedTabs，重挂（onMounted 重跑）才会再拉。
    await zone.getByRole('tab', { name: /概览看板/ }).click();
    await zone.getByRole('tab', { name: /文档管理/ }).click();
    await expect(page.getByPlaceholder('搜索文档名…')).toBeVisible();
    await page.waitForTimeout(400);

    expect(
      statsHits - afterFirstEntry,
      '首次激活后的搜索输入 / tab 往返只改 query，不应重挂 ManageView 重拉数据（fullPath key → 重建 → 黑屏闪 + 全量重载）',
    ).toBe(0);
  });

  test('搜索框可连续输入：不重挂、焦点不丢、值累积', async ({ page }) => {
    await page.goto(MANAGE_ROUTE);
    const zone = page.locator('[aria-label="管理台分区"]');
    await zone.getByRole('tab', { name: /文档管理/ }).click();

    const search = page.getByPlaceholder('搜索文档名…');
    await expect(search).toBeVisible();
    await search.click();
    // 逐字输入：每个字符触发 setQuery → router.replace({ query:{ q } })。
    // fullPath key 下这会每字重建输入框、焦点丢失 → 后续字符落空、值不完整（实测停在首字「包」）。
    await search.pressSequentially('包装作业', { delay: 60 });

    await expect(search, '连续输入的字符应完整累积（重建会丢字）').toHaveValue('包装作业');
    await expect(search, '输入框不应在输入过程中被重建而失焦').toBeFocused();
    await expect(page).toHaveURL(/q=/);
  });
});
