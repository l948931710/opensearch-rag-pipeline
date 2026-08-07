import { test, expect, type Page } from '@playwright/test';

const J = (body: unknown) => ({ contentType: 'application/json', body: JSON.stringify(body) });

/** legacy 组码模式：归属是普通 <select>，可脚本化。node 模式要先驱动组织树选择器，
 *  与本用例要测的"失败清单跨轮存活"无关，徒增噪音。 */
async function setupLegacy(page: Page) {
  await page.route('**/api/**', (r) => r.fulfill(J({ items: [], questions: [], docs: [], total: 0, has_more: false })));
  await page.route('**/api/kb/whoami', (r) => r.fulfill(J({
    user_id: 'admin1', display_name: '生产部管理员', role: 'dept_admin',
    can_manage_kb: true, acl_groups: ['production'], managed_owner_depts: ['production'],
  })));
  await page.route('**/api/kb/config', (r) => r.fulfill(J({
    max_upload_bytes: 209715200, accepted_exts: ['pdf', 'docx'], node_acl_grant: false })));
  await page.route('**/api/kb/my-docs*', (r) => r.fulfill(J({ items: [], has_more: false })));
}

/**
 * 硬门 —— 批量上传的失败清单必须跨轮存活（2026-08-07 现网事故的回归门）。
 *
 * 事故还原：用户批量传一批、失败若干，接着**去选另一批文件**准备继续传。
 * `onFileSelected` 第一行 `uploadQueue.value = []`（useKb.ts:1105）把失败清单当场清空，
 * 同时 `selectedFiles` 被新选择整体覆盖——连 2026-08-06 那版「批末收敛成失败集」的
 * 成果也一起没了。用户再也查不到失败的是哪些，只能整批重传 ⇒ 库里多出 117 份重复文档
 * （全部 ETag 相同，实测确认）。
 *
 * 🔴 反证锚（R3）：把 `failedUploads` 强制清空后，前两条断言必须全部失败。
 * 没有它，"清单还在"可能只是因为压根没触发过清空路径——那样测的就不是跨轮存活。
 */

const OK_FILE = { name: 'ok.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 ok') };
const BAD_FILE = { name: 'boom.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 bad') };
const OTHER_FILE = { name: 'other.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 other') };

/** upload-url 对 boom.pdf 返回 500 ⇒ 该行必失败；ok.pdf 正常走完。 */
async function mockUpload(page: Page) {
  await page.route('**/api/kb/upload-url', async (r) => {
    const body = JSON.parse(r.request().postData() || '{}');
    if (String(body.filename).startsWith('boom')) {
      return r.fulfill({ status: 500, contentType: 'application/json',
        body: JSON.stringify({ detail: '模拟失败：后端 500' }) });
    }
    return r.fulfill({ contentType: 'application/json', body: JSON.stringify({
      put_url: 'https://oss.example.test/put', upload_token: 'tok', content_type: 'application/pdf',
      raw_key: 'raw/x', doc_id: 'D1', expires_in: 900 }) });
  });
  await page.route('**oss.example.test/**', (r) => r.fulfill({ status: 200, body: '' }));
  await page.route('**/api/kb/register', (r) => r.fulfill({ contentType: 'application/json',
    body: JSON.stringify({ doc_id: 'D1', version_no: 1, status_badge: '排队中' }) }));
}

/** 进管理台文档页 + 选归属（legacy 组码），到"可以点上传"为止。 */
async function gotoUpload(page: Page) {
  await setupLegacy(page);
  await mockUpload(page);       // 后注册 ⇒ 覆盖上面的 `**/api/**` 通配
  await page.goto('/console/manage?token=e2e-fake-token&tab=docs');
}

async function batchUploadWithOneFailure(page: Page) {
  await page.locator('input[type=file]').setInputFiles([OK_FILE, BAD_FILE]);
  await page.locator('#kb-sec-upload').getByRole('combobox').first().selectOption('production');
  await page.getByRole('button', { name: /^上传$/ }).click();
  // 等批次跑完（上传按钮复位）
  await expect(page.getByRole('button', { name: /^上传$/ })).toBeVisible({ timeout: 15000 });
  await expect(page.getByTestId('failed-uploads')).toBeVisible();
  await expect(page.getByTestId('failed-uploads')).toContainText('boom.pdf');
}

test.describe('硬门 — 上传失败清单跨轮存活', () => {
  test('F1 选了另一批文件之后，失败清单仍在', async ({ page }) => {
    await gotoUpload(page);
    await batchUploadWithOneFailure(page);

    // ← 事故动作：去选另一批文件（这一步过去会把失败清单清空）
    await page.locator('input[type=file]').setInputFiles([OTHER_FILE]);

    await expect(page.getByTestId('failed-uploads'),
      '选了别的文件后失败清单消失 ⇒ 用户再也查不到失败的是哪些，只能整批重传').toBeVisible();
    await expect(page.getByTestId('failed-uploads')).toContainText('boom.pdf');
  });

  test('F2 「重选这些文件」把失败项恢复成当前选择', async ({ page }) => {
    await gotoUpload(page);
    await batchUploadWithOneFailure(page);
    await page.locator('input[type=file]').setInputFiles([OTHER_FILE]);

    await page.getByTestId('failed-restore').click();
    // 选择列表回到失败项：other.pdf 让位给 boom.pdf
    await expect(page.locator('#kb-sec-upload')).toContainText('boom.pdf');
    await expect(page.getByTestId('failed-uploads')).toBeVisible();
  });

  test('F3 🔴 反证锚：清空 failedUploads 后，F1 的断言必须失败', async ({ page }) => {
    await gotoUpload(page);
    await batchUploadWithOneFailure(page);

    await page.getByTestId('failed-clear').click();          // 模拟"清单没能存活"
    await page.locator('input[type=file]').setInputFiles([OTHER_FILE]);

    await expect(page.getByTestId('failed-uploads'),
      '清空后仍可见 ⇒ F1 测的不是跨轮存活，而是别的东西').toHaveCount(0);
  });
});
