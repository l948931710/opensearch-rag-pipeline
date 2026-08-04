import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, MANAGE_ROUTE } from './node-mode.helpers';

/**
 * 硬门 —— 轮 3a：真实进度 + 台账菜单缺陷。断言逐条对应审计的 C1–C5。
 *
 * 贯穿全轮的一条纪律（redline③「不做假阶段状态条」）：**没有真实百分比的阶段一律不渲染进度条**，
 * 不画 valuenow=0，也不画三段式假条。审计核实上传四个阶段里只有 OSS PUT 一段有真值
 * （XHR upload.onprogress），「申请上传地址」「登记」两段没有任何可测进度。
 */

const KB_ADMIN = { role: 'kb_admin' as const, managedRoots: [] };
const J = (body: unknown) => ({ contentType: 'application/json', body: JSON.stringify(body) });
const tab = (p: Page, name: RegExp) => p.locator('[aria-label="管理台分区"]').getByRole('tab', { name });

const DOCS = (n: number) => ({
  items: Array.from({ length: n }, (_, i) => ({
    doc_id: `d${i}`, title: `文档${i}`, owner_dept: 'marketing', permission_level: 'dept_internal',
    status_badge: '已上线', version_no: 1, updated_at: '2026-08-01 10:00:00', is_active: 1, chunks: 3,
  })),
  total: n,
});

test.describe('硬门 — 轮 3a 真实进度与菜单', () => {
  // ── C1：bulkVisMenu 点外部 / Esc 均不关闭（审计活体复现两次）──────────────────
  test('P1 批量下拉：点外部关闭', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/my-docs**', (r) => r.fulfill(J(DOCS(3))));
    await page.goto(MANAGE_ROUTE);
    await tab(page, /文档管理/).click();

    await page.locator('[role="checkbox"]').nth(1).click();      // 选一篇 → 批量条出现
    await page.getByRole('button', { name: '改可见范围' }).click();
    const menu = page.locator('[data-act-menu="bulk"] [role="menu"]');
    await expect(menu).toBeVisible();

    await page.locator('h2, p').filter({ hasText: '文档台账' }).first().click();
    await expect(menu, '基线：点任何中性区域都关不掉').toBeHidden();
  });

  test('P2 批量下拉：Esc 关闭', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/my-docs**', (r) => r.fulfill(J(DOCS(3))));
    await page.goto(MANAGE_ROUTE);
    await tab(page, /文档管理/).click();

    await page.locator('[role="checkbox"]').nth(1).click();
    await page.getByRole('button', { name: '改可见范围' }).click();
    const menu = page.locator('[data-act-menu="bulk"] [role="menu"]');
    await expect(menu).toBeVisible();

    await page.keyboard.press('Escape');
    await expect(menu, '基线：Esc 同样失效').toBeHidden();
  });

  test('P3 两类菜单互不误伤（同一属性值会让它们互相关不掉）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/my-docs**', (r) => r.fulfill(J(DOCS(3))));
    await page.goto(MANAGE_ROUTE);
    await tab(page, /文档管理/).click();

    await page.locator('[role="checkbox"]').nth(1).click();
    await page.getByRole('button', { name: '改可见范围' }).click();
    await expect(page.locator('[data-act-menu="bulk"] [role="menu"]')).toBeVisible();

    // 点行内「更多操作」：批量下拉必须关掉，行菜单必须打开
    await page.getByTestId('doc-more').first().click();
    await expect(page.locator('[data-act-menu="bulk"] [role="menu"]'), '点行菜单锚点应关掉批量下拉').toBeHidden();
    await expect(page.locator('[data-act-menu="row"] [role="menu"]').first()).toBeVisible();
  });

  // ── C3：上传进度条只在有真值的阶段出现 ───────────────────────────────────────
  // legacy 模式（非 node 模式）：归属是普通 <select>，可脚本化。node 模式下归属是组织树
  // 折叠选择器，驱动它属于另一条测试路径，与本断言无关。
  async function setupLegacy(page: Page, uploadUrl: Parameters<Page['route']>[1]) {
    await page.route('**/api/**', (r) => r.fulfill(J({ items: [], questions: [], docs: [], total: 0 })));
    await page.route('**/api/kb/whoami', (r) => r.fulfill(J({
      user_id: 'admin1', display_name: '生产部管理员', role: 'dept_admin',
      can_manage_kb: true, acl_groups: ['production'], managed_owner_depts: ['production'],
    })));
    await page.route('**/api/kb/my-docs*', (r) => r.fulfill(J({ items: [], has_more: false })));
    await page.route('**/api/kb/upload-url', uploadUrl);
    await page.goto('/console/manage?token=e2e-fake-token&tab=docs');
  }
  const FILES = (n: number) => Array.from({ length: n }, (_, i) => ({
    name: `f${i}.pdf`, mimeType: 'application/pdf', buffer: Buffer.from(`%PDF-1.4 p${i}`),
  }));
  async function startUpload(page: Page, n: number) {
    await page.locator('input[type=file]').setInputFiles(n === 1 ? FILES(1)[0] : FILES(n));
    await page.locator('#kb-sec-upload').getByRole('combobox').first().selectOption('production');
    await page.getByRole('button', { name: /^上传$/ }).click();
  }

  test('P4 单文件「申请上传地址」阶段不得渲染任何进度条（redline③）', async ({ page }) => {
    await setupLegacy(page, async (r) => {
      await new Promise((s) => setTimeout(s, 2500));            // 卡在第一阶段
      await r.fulfill({ status: 500, ...J({}) });
    });
    await startUpload(page, 1);

    await expect(page.getByRole('button', { name: '上传中…' })).toBeVisible({ timeout: 3000 });
    await expect(page.getByTestId('upload-progress'), '该阶段没有可测进度 ⇒ 一根条都不许画').toHaveCount(0);
    await expect(page.locator('[role="progressbar"]'), 'redline③：不许出现 valuenow=0 的假条').toHaveCount(0);
  });

  // P7/P8 是评审复现的缺口：P4 只走单文件路径，队列路径当时没人守。
  // 根因是 QueueRow.pct 声明成非空 number 且播种 0 ⇒ UploadCard 的守卫静态恒真，成了空守卫。
  test('P7 批量「排队 / 申请上传地址」阶段不得有进度条（基线：3 根 valuenow=0）', async ({ page }) => {
    await setupLegacy(page, async (r) => {
      await new Promise((s) => setTimeout(s, 4000));
      await r.fulfill({ status: 500, ...J({}) });
    });
    await startUpload(page, 3);

    await expect(page.getByRole('button', { name: '上传中…' })).toBeVisible({ timeout: 3000 });
    await expect(page.getByTestId('queue-progress'), '并发池 2 ⇒ 两行在申请地址、一行还在排队，都没有可测进度').toHaveCount(0);
    await expect(page.locator('[role="progressbar"]')).toHaveCount(0);
  });

  // P9 是 P7/P8 的**非空证明**。没有它，那两条 toHaveCount(0) 分不清「正确隐藏」与
  // 「压根渲染不出来」——评审实测过后者：uploadOne 改的是建行时的裸数组元素而非 ref 里的
  // proxy，onprogress 确实在跑、pct 确实写进去了，但整个上传期间条与「N%」文本都不出现。
  // 这里用 addInitScript 打桩 XHR 的 upload.onprogress 注入可控中途值：真实 PUT 被本地
  // 拦截后进度通常一跃到 100%，测不到「中途」。
  test('P9 队列行在 PUT 进行中**必须**渲染进度条，且 valuenow 等于真实回调值', async ({ page }) => {
    await page.addInitScript(() => {
      const origOpen = XMLHttpRequest.prototype.open;
      const origSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function (this: any, m: string, u: string, ...rest: any[]) {
        this.__method = m; return origOpen.call(this, m, u, ...(rest as [boolean?]));
      } as typeof XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.send = function (this: any, b?: any) {
        if (this.__method === 'PUT' && this.upload) {
          setTimeout(() => this.upload.onprogress?.({ lengthComputable: true, loaded: 20, total: 100 }), 120);
        }
        return origSend.call(this, b);
      } as typeof XMLHttpRequest.prototype.send;
    });
    await setupLegacy(page, (r) => r.fulfill(J({
      put_url: 'https://oss.example.test/put', upload_token: 'tok', content_type: 'application/pdf',
    })));
    await page.route('**oss.example.test/**', async (r) => {
      await new Promise((s) => setTimeout(s, 4000));   // 把 PUT 按住，让中途态可观测
      await r.fulfill({ status: 200, body: '' });
    });
    await startUpload(page, 2);

    const bars = page.getByTestId('queue-progress');
    await expect(bars.first(), '有真实回调就必须出条——否则 P7/P8 是空断言').toBeVisible({ timeout: 4000 });
    await expect(bars.first()).toHaveAttribute('aria-valuenow', '20');
    await expect(page.getByText('20%').first(), '既有的 N% 文本同样依赖这次重渲染').toBeVisible();
  });

  // P10 守的是**修 P9 时差点引入的回归**：为了让队列行反应式生效而改从 ref 取行，
  // 若写成每次 `uploadQueue.value[i]`，则批量在途时点「清空已选文件」（该按钮与 dropzone、
  // file input 三个入口都没有 :disabled="uploadBusy"）会把 uploadQueue.value 重指向 []
  // ⇒ 取到 undefined ⇒ row.status 抛 TypeError ⇒ catch 里的 row.pct = null 二次抛出无人接
  // ⇒ Promise.all reject ⇒ uploadBusy 永不复位 = 上传卡永久锁死，且本批已 register 成功的
  // 文档因 loadDocs() 不执行而不进台账，界面还不报错。正解是把 proxy 数组捕获一次。
  test('P10 批量在途清空队列：不得卡死上传卡，也不得抛未捕获异常', async ({ page }) => {
    const pageErrors: string[] = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));
    await setupLegacy(page, async (r) => {
      await new Promise((s) => setTimeout(s, 3000));            // 前两个 worker 卡在申请地址
      await r.fulfill({ status: 500, ...J({}) });
    });
    await startUpload(page, 3);
    await expect(page.getByRole('button', { name: '上传中…' })).toBeVisible({ timeout: 3000 });

    await page.getByRole('button', { name: /清空已选/ }).click();

    await expect(page.getByRole('button', { name: /^上传$/ }), '基线：uploadBusy 永久 true，按钮再也回不来').toBeVisible({ timeout: 15000 });
    expect(pageErrors, '不得有未捕获 TypeError').toEqual([]);
  });

  test('P8 批量终态行（失败/已提交）不得残留进度条（基线：2 根冻在 0）', async ({ page }) => {
    await setupLegacy(page, (r) => r.fulfill(J({
      put_url: 'https://oss.invalid.example/put', upload_token: 'tok', content_type: 'application/pdf',
    })));
    await startUpload(page, 2);

    await expect(page.getByRole('button', { name: /^上传$/ })).toBeVisible({ timeout: 20000 });
    await expect(page.getByText('失败').first()).toBeVisible();
    await expect(page.getByTestId('queue-progress'), '终态没有进行中的进度可报 ⇒ 条必须撤掉').toHaveCount(0);
  });

  // ── C4：reduce-motion 下必须传 behavior:'auto' ───────────────────────────────
  test('P5 reduce-motion：scrollIntoView 收到 auto（CSS 兜底管不到显式 behavior）', async ({ browser }) => {
    const ctx = await browser.newContext({ reducedMotion: 'reduce' });
    const page = await ctx.newPage();
    await page.addInitScript(() => {
      (window as any).__scrollCalls = [];
      const orig = Element.prototype.scrollIntoView;
      Element.prototype.scrollIntoView = function (o?: any) { (window as any).__scrollCalls.push(o); return orig.call(this, o); };
    });
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/my-docs**', (r) => r.fulfill(J(DOCS(2))));
    await page.goto(MANAGE_ROUTE);
    await tab(page, /文档管理/).click();

    await page.getByTestId('doc-more').first().click();
    await page.getByRole('menuitem', { name: /升版|上传新版本/ }).first().click();

    const calls = await page.evaluate(() => (window as any).__scrollCalls);
    expect(calls.length, '升版应触发 scrollToUploadCard').toBeGreaterThan(0);
    expect(calls[calls.length - 1]?.behavior, 'reduce 下必须 auto —— 显式 smooth 会绕过 CSS 兜底').toBe('auto');
    await ctx.close();
  });

  test('P6 默认（无 reduce-motion）仍是 smooth', async ({ page }) => {
    await page.addInitScript(() => {
      (window as any).__scrollCalls = [];
      const orig = Element.prototype.scrollIntoView;
      Element.prototype.scrollIntoView = function (o?: any) { (window as any).__scrollCalls.push(o); return orig.call(this, o); };
    });
    await mockNodeMode(page, KB_ADMIN);
    await page.route('**/api/kb/my-docs**', (r) => r.fulfill(J(DOCS(2))));
    await page.goto(MANAGE_ROUTE);
    await tab(page, /文档管理/).click();

    await page.getByTestId('doc-more').first().click();
    await page.getByRole('menuitem', { name: /升版|上传新版本/ }).first().click();

    const calls = await page.evaluate(() => (window as any).__scrollCalls);
    expect(calls[calls.length - 1]?.behavior).toBe('smooth');
  });
});
