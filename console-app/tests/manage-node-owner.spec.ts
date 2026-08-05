import { test, expect, type Page } from '@playwright/test';
import { assertNoHorizontalScroll } from './ux-gate.helpers';
import { mockNodeMode, gotoManageDocs, MANAGE_ROUTE } from './node-mode.helpers';

/**
 * 硬门 — node 模式文档管理改版（2026-08-03，codex 四轮共识 + ui-iterate）
 *
 * ① 归属/可见范围双组织树 → 折叠式选择器：默认态**无整树**，上传卡与编辑弹窗不再被
 *    两棵树撑爆首屏（基线：双树占上传卡 52%、弹窗 56%，保存按钮贴视口下沿）。
 * ② dept_admin 归属自动化：单管辖根 ⇒ 锁定徽章+「更改」；多根 ⇒ 不自动、选择器限管辖
 *    子树；stale ⇒ 不锁定不播种、收起态给 ⚠️（Sam 拍板 + B2/B3 共识）。
 * ③ legacy 唯一上传目标 ⇒ 下拉预选（同一每身份一次机制）。
 *
 * 覆盖缺口说明：既有 ux-gate mock 不开 node_acl_grant，走不到双树/选择器界面——
 * 本 spec 是 node 模式的唯一硬门（codex onboarding major）。
 */

async function gotoNodeUpload(page: Page, opts: Parameters<typeof mockNodeMode>[1] = {}) {
  await mockNodeMode(page, { managedRoots: [3], ...opts });
  await gotoManageDocs(page);
  await expect(page.locator('#kb-sec-upload').getByText('归属部门（组织架构，单选）')).toBeVisible();
}

test.describe('硬门 — 折叠式组织树选择器 + 归属自动化', () => {
  test('单管辖根：锁定徽章 + 默认态全页无整树 + 归属已自动预勾进可见范围', async ({ page }) => {
    await gotoNodeUpload(page);
    const upload = page.locator('#kb-sec-upload');
    // 锁定徽章（自动预填）：显示管辖部门名，不渲染归属树
    await expect(upload.getByText('（你的管辖部门）')).toBeVisible();
    await expect(upload.getByText('注塑事业部').first()).toBeVisible();
    // 默认态无整树（折叠改版的核心断言）
    await expect(upload.locator('.op-tree')).toHaveCount(0);
    // 「仅本部门」档（2026-08-03 拍板）：无节点选择器，只读摘要 = 归属子树
    await expect(upload.getByTestId('share-select')).toHaveCount(0);
    await expect(upload.getByText(/仅本部门可见：/)).toBeVisible();
    await expect(upload.getByText('约 300 人可见')).toBeVisible();
    await assertNoHorizontalScroll(page);
  });

  test('「更改」解锁 → 归属选择器展开后限管辖子树（看不到全公司树根）', async ({ page }) => {
    await gotoNodeUpload(page);
    const upload = page.locator('#kb-sec-upload');
    await upload.getByRole('button', { name: '更改' }).click();
    const owner = upload.getByTestId('owner-select');
    await owner.locator('button[aria-expanded]').click();
    await expect(owner.locator('.op-tree')).toBeVisible();
    await expect(owner.locator('.op-tree').getByText('注塑事业部')).toBeVisible();
    await expect(owner.locator('.op-tree').getByText('富岭科技')).toHaveCount(0);
  });

  test('可见范围：切「指定部门」→ 展开选择 → chips 更新 → 「完成」收起', async ({ page }) => {
    await gotoNodeUpload(page);
    // 节点选择器仅「指定部门」档显示（仅本部门=只读归属子树，2026-08-03 拍板）。
    // ⚠️ 用 combobox role 定位：OrgTreeSelect 触发器的 aria-label 也以「可见范围」开头（button role）
    await page.locator('#kb-sec-upload').getByRole('combobox', { name: /^可见范围/ }).selectOption('shared');
    const share = page.locator('#kb-sec-upload').getByTestId('share-select');
    await expect(share.getByText('注塑事业部')).toBeVisible();   // 归属预勾进 chips
    await share.locator('button[aria-expanded]').click();
    await expect(share.locator('.op-tree')).toBeVisible();
    // 选择颗粒度止于二级（2026-08-03 拍板）：车间级不进选择树
    await expect(share.locator('.op-tree').getByText('注塑一车间')).toHaveCount(0);
    await share.locator('input[aria-label="质量中心，子树 60 人"]').check();
    await share.getByRole('button', { name: '完成' }).click();
    await expect(share.locator('.op-tree')).toBeHidden();
    await expect(share.getByText('质量中心').first()).toBeVisible();       // chip 留在收起态
    await expect(share.getByText(/约 \d+ 人可见/).first()).toBeVisible();  // 人数概览收起态可见
  });

  test('切档语义：指定部门加的节点在切回仅本部门时丢弃；payload 恒=归属子树', async ({ page }) => {
    await gotoNodeUpload(page);
    const upload = page.locator('#kb-sec-upload');
    const perm = upload.getByRole('combobox', { name: /^可见范围/ });
    // 指定部门：加质量中心（chips = 归属 + 质量中心）
    await perm.selectOption('shared');
    const share = upload.getByTestId('share-select');
    await share.locator('button[aria-expanded]').click();
    await share.locator('input[aria-label="质量中心，子树 60 人"]').check();
    await share.getByRole('button', { name: '完成' }).click();
    await expect(share.getByText('质量中心').first()).toBeVisible();
    // 切回仅本部门：选择器消失、只读摘要回归
    await perm.selectOption('dept_internal');
    await expect(upload.getByTestId('share-select')).toHaveCount(0);
    await expect(upload.getByText(/仅本部门可见：/)).toBeVisible();
    // 再进指定部门：从归属-only 起步，质量中心已被有意丢弃
    await perm.selectOption('shared');
    await expect(upload.getByTestId('share-select').getByText('注塑事业部')).toBeVisible();
    await expect(upload.getByTestId('share-select').getByText('质量中心')).toHaveCount(0);
    // payload 断言（codex minor）：仅本部门直接上传 ⇒ visible_nodes 严格 = [归属含下级]
    await perm.selectOption('dept_internal');
    let uploadBody: any = null;
    await page.route('**/api/kb/upload-url', (r) => {
      uploadBody = r.request().postDataJSON();
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify({
        upload_token: 'T', put_url: '/api/mock-oss-put', raw_key: 'k', doc_id: 'd_new',
        expires_in: 600, requires_kb_admin_approval: false, content_type: 'application/pdf',
      }) });
    });
    await page.route('**/api/kb/register', (r) => r.fulfill({ contentType: 'application/json',
      body: JSON.stringify({ doc_id: 'd_new', version_no: 1, status_badge: '处理中',
        title: 't', requires_kb_admin_approval: false, content_dups: [], content_dups_other: 0 }) }));
    await page.route('**/api/mock-oss-put', (r) => r.fulfill({ status: 200, body: '' }));
    await upload.locator('input[type="file"]').setInputFiles({
      name: 'demo.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 demo'),
    });
    await upload.getByRole('button', { name: '上传', exact: true }).click();
    await expect.poll(() => uploadBody).not.toBeNull();
    expect(uploadBody.permission_level).toBe('dept_internal');
    expect(uploadBody.owner_dept_id).toBe(3);
    expect(uploadBody.visible_nodes).toEqual([{ dept_id: 3, subtree: true }]);
  });

  test('多管辖根：不自动预填，归属为折叠选择器', async ({ page }) => {
    await gotoNodeUpload(page, { managedRoots: [3, 6] });
    const upload = page.locator('#kb-sec-upload');
    await expect(upload.getByText('（你的管辖部门）')).toHaveCount(0);
    await expect(upload.getByTestId('owner-select').getByText('选择部门…')).toBeVisible();
  });

  test('快照 stale：不锁定不播种，收起态给 ⚠️ 过期提示', async ({ page }) => {
    await gotoNodeUpload(page, { stale: true });
    const upload = page.locator('#kb-sec-upload');
    await expect(upload.getByText('（你的管辖部门）')).toHaveCount(0);
    await expect(upload.getByText('组织数据可能已过期').first()).toBeVisible();
  });

  // 2026-08-05 回归门：首篇 node 文档上线后，台账「归属」列是**空格子**——DocTable 还在渲染
  // 旧字段 owner_dept（node 文档按后端契约恒为空串），而归属只在 owner_key/owner_label 上。
  // 这条把「node 文档在台账里能看见归属」钉死；名字来自后端 JOIN dept_dim ⇒ 部门改名免疫。
  test('台账归属列：node 文档显示节点名而非空格子', async ({ page }) => {
    await gotoNodeUpload(page);
    const ownerCell = page.locator('[data-label="归属"]').first();
    await expect(ownerCell).toHaveText(/注塑事业部/);
    await expect(ownerCell).not.toHaveText(/^\s*$/);
    await expect(ownerCell).not.toHaveText(/未归属|node:|legacy:/);
  });

  test('编辑信息弹窗：双折叠选择器预填、无整树、保存按钮免滚动可达', async ({ page }) => {
    await gotoNodeUpload(page);
    await page.getByTestId('doc-more').first().click();
    await page.getByRole('menuitem', { name: '编辑信息' }).click();
    const modal = page.locator('.fixed.inset-0');
    await expect(modal.getByTestId('meta-owner-select').getByText('注塑事业部')).toBeVisible();
    await expect(modal.getByTestId('meta-visible-select').getByText('含下级').first()).toBeVisible();
    await expect(modal.locator('.op-tree')).toHaveCount(0);
    // 基线痛点回归：双树时代保存按钮贴视口下沿，现在必须免滚动在视口内
    await expect(modal.getByRole('button', { name: '保存' })).toBeInViewport();
  });
});

test.describe('硬门 — legacy 唯一上传目标预选', () => {
  test('单一 upload_target ⇒ 归属下拉自动预选', async ({ page }) => {
    await page.route('**/api/**', (r) => r.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ items: [], questions: [], docs: [], total: 0, has_more: false }),
    }));
    await page.route('**/api/kb/whoami', (r) => r.fulfill({
      contentType: 'application/json', body: JSON.stringify({
        user_id: 'straw1', display_name: '吸管管理员', role: 'dept_admin',
        can_manage_kb: true, acl_groups: ['production'],
        managed_owner_depts: ['production_straw'], upload_target_depts: ['production_straw'],
      }),
    }));
    await page.route('**/api/kb/config', (r) => r.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({ max_upload_bytes: 209715200, accepted_exts: ['pdf'], node_acl_grant: false }),
    }));
    await gotoManageDocs(page);
    const sel = page.locator('#kb-sec-upload').getByLabel('归属部门');
    await expect(sel).toBeVisible();
    await expect(sel).toHaveValue('production_straw');
  });
});

test.describe('硬门 — 看板组织覆盖（2026-08-03 重设计）', () => {
  test('kb_admin 概览：无裸 node: 键；中心卷积可展开；使用量中心行诚实「—」', async ({ page }) => {
    await mockNodeMode(page, { role: 'kb_admin', managedRoots: [] });
    await page.goto(MANAGE_ROUTE);
    await expect(page.getByText('组织覆盖')).toBeVisible();
    // 裸稳定键绝不上屏（重设计核心断言）
    await expect(page.getByText(/node:\d+/)).toHaveCount(0);
    // 中心行：注塑(d2)+车间(d3) 卷到生产中心（docs 23+9=32），可展开
    const table = page.getByTestId('dept-usage-block').getByRole('table');
    await expect(table.getByText('生产中心')).toBeVisible();
    await expect(table.getByText('32')).toBeVisible();
    await table.getByRole('button', { name: '展开 生产中心 的下级部门' }).click();
    await expect(table.getByText('注塑事业部')).toBeVisible();
    await expect(table.getByText('注塑一车间')).toBeVisible();
    // 中心自身持桶 → 「（本级）」子行
    await table.getByRole('button', { name: '展开 质量中心 的下级部门' }).click();
    await expect(table.getByText('质量中心（本级）')).toBeVisible();
    // 中心行非可加指标诚实「—」（跨桶提问去重不可加）
    await expect(table.locator('[title^="跨部门提问去重"]').first()).toBeVisible();
    // legacy/unknown 顶层行（不猜中心）
    await expect(table.getByText('未归属')).toBeVisible();
    await assertNoHorizontalScroll(page);
  });
});

test.describe('硬门 — 反馈按部门筛选（2026-08-03）', () => {
  test('选部门 ⇒ 卡片切筛选口径；清除回全库；颗粒度默认止二级', async ({ page }) => {
    await mockNodeMode(page, { role: 'kb_admin', managedRoots: [] });
    await page.goto(MANAGE_ROUTE);
    const fb = page.getByTestId('fb-filter');
    await expect(fb).toBeVisible();
    await expect(page.getByText('96').first()).toBeVisible();          // 全库点赞（governance）
    await fb.locator('button[aria-expanded]').click();
    await expect(fb.locator('.op-tree').getByText('注塑一车间')).toHaveCount(0);  // 止二级
    await fb.locator('input[aria-label="注塑事业部，子树 300 人"]').check();
    await expect(fb.getByText('筛选：注塑事业部')).toBeVisible();
    await expect(page.getByText('33').first()).toBeVisible();          // 筛选口径点赞（feedback-stats）
    // D2 回归：部门筛选空列表给专属文案（不误导去切时间范围），且不渲染全库旧行
    await expect(page.getByText('该部门近期无「被引用且点踩」的回答')).toBeVisible();
    await page.getByRole('button', { name: '清除' }).click();
    await expect(fb.getByText('筛选：注塑事业部')).toHaveCount(0);
    await expect(page.getByText('96').first()).toBeVisible();
  });

  test('快照过期 409 ⇒ 显式降级提示，不显示成「该部门零反馈」', async ({ page }) => {
    await mockNodeMode(page, { role: 'kb_admin', managedRoots: [], fbStats409: true });
    await page.goto(MANAGE_ROUTE);
    const fb = page.getByTestId('fb-filter');
    await fb.locator('button[aria-expanded]').click();
    await fb.locator('input[aria-label="注塑事业部，子树 300 人"]').check();
    await expect(page.getByTestId('fb-filter-error')).toBeVisible();
    await expect(page.getByTestId('fb-filter-error')).toContainText('组织快照过期');
    await expect(page.getByText('96').first()).toBeVisible();          // 回落全库口径而非伪 0
  });
});
