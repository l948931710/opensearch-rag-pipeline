import { test, expect } from '@playwright/test';

/**
 * 硬门 — 上传「归属部门」可选生产子线（2026-07-17 拍板：写目标细化，不扩 writer 受众）
 *
 * 管 production 的 dept_admin：whoami 带 upload_target_depts（伞值+5 子线）→
 * UploadCard 归属下拉出现中文子线选项（生产·吸管 等）。
 * 兼容门：旧后端缺 upload_target_depts 字段 → 回退 managed_owner_depts（仅伞值），不炸不空。
 *
 * 进入方式与其它硬门一致：?token= 透传免登 + mock /api/kb/whoami（绝不用 ?preview）。
 */

const MANAGE_ROUTE = '/console/manage?token=e2e-fake-token';

const SUBLINES = [
  'production_injection', 'production_mold', 'production_paper_cup',
  'production_straw', 'production_thermoforming',
];

async function mockProdDeptAdmin(page: import('@playwright/test').Page, withUploadTargets: boolean) {
  await page.route('**/api/**', (r) => r.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({ items: [], questions: [], docs: [], total: 0, has_more: false }),
  }));
  await page.route('**/api/kb/whoami', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      user_id: 'prod1', display_name: '生产管理员', role: 'dept_admin',
      can_manage_kb: true, acl_groups: ['production'], managed_owner_depts: ['production'],
      ...(withUploadTargets ? { upload_target_depts: ['production', ...SUBLINES] } : {}),
    }),
  }));
}

async function gotoUploadCard(page: import('@playwright/test').Page) {
  await page.goto(MANAGE_ROUTE);
  const zone = page.locator('[aria-label="管理台分区"]');
  await zone.getByRole('tab', { name: /文档管理/ }).click();
  await expect(page.locator('#kb-sec-upload').getByLabel('归属部门')).toBeVisible();
}

test.describe('硬门 — 上传归属可选生产子线', () => {
  test('whoami 带 upload_target_depts → 下拉含伞值 + 5 个中文子线选项', async ({ page }) => {
    await mockProdDeptAdmin(page, true);
    await gotoUploadCard(page);
    const texts = await page.locator('#kb-sec-upload').getByLabel('归属部门').locator('option').allTextContents();
    expect(texts).toContain('生产');
    for (const zh of ['生产·注塑', '生产·模具', '生产·纸杯', '生产·吸管', '生产·吸塑']) {
      expect(texts, `子线中文选项缺失: ${zh}`).toContain(zh);
    }
  });

  test('旧后端缺 upload_target_depts → 回退 managed（仅伞值），不炸', async ({ page }) => {
    await mockProdDeptAdmin(page, false);
    await gotoUploadCard(page);
    const texts = await page.locator('#kb-sec-upload').getByLabel('归属部门').locator('option').allTextContents();
    expect(texts).toContain('生产');
    expect(texts).not.toContain('生产·吸管');
  });

  test('台账点「升版」→ 上传卡滚进视口且显「升版模式」（点了没反应根因）', async ({ page }) => {
    await mockProdDeptAdmin(page, true);
    // 台账给一行可升版文档（my-docs 行 can_manage 恒可操作）
    await page.route('**/api/kb/my-docs**', (r) => r.fulfill({
      contentType: 'application/json', body: JSON.stringify({
        items: [{
          doc_id: 'd1', title: '吸管车间作业指导书', original_filename: 'sop.docx',
          owner_dept: 'production_straw', permission_level: 'dept_internal',
          current_version_no: 2, status: 'active', status_badge: '已上线',
          updated_at: '2026-07-17 10:00',
        }],
        total: 1, has_more: false,
      }),
    }));
    await gotoUploadCard(page);
    await page.getByRole('button', { name: '上传新版本' }).first().click();
    // 修复点：enterVersionMode 现在滚动 #kb-sec-upload 进视口——否则模式切换发生在屏幕外
    await expect(page.locator('#kb-sec-upload')).toBeInViewport();
    await expect(page.getByText('升版模式')).toBeVisible();
  });
});
