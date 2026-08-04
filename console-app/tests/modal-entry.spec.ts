import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, gotoManageDocs, NODE_DOC } from './node-mode.helpers';

/**
 * 硬门 —— 6 个模态与 toast 的入场动效（2026-08-04）。
 *
 * 基线（独立审计实测）：6 个模态的遮罩与面板**全部** animationName:none / duration:0s，
 * 从「不存在」到「完整遮罩+面板」是单帧跳变，与全局已建立的换场有动效的预期不一致。
 *
 * 写法约束（不可协商，源码级断言在 src/styles/__tests__/modal-entry-anim.spec.ts）：
 *   ① 绝不带 animation-fill-mode；② 起始帧只动 transform/background-color，绝不碰 opacity。
 *
 * 本门与那道源码镜像门**有重叠但都不冗余**。曾以为分工是「计算值看不出作者写没写 both、
 * 源码看不出类挂没挂上」——**前半句经实测证伪**：getComputedStyle().animationFillMode 确实
 * 能区分（注入 both 后读到 'both'），约束① 在本门已被 `expect(fill).toBe('none')` 覆盖；
 * 运行时也能从 CSSOM dump 出关键帧全文，约束② 原则上同样能测。
 * 本门真正独有的是两条：**只有它能证明类真的挂到了 6/6 模态 + toast 上**，
 * 以及**只有它覆盖 reduced-motion**。镜像门独有的三条见那个文件的头注释。
 *
 * 覆盖 6/6 模态（轮 1a 的 Esc 矩阵只覆盖了 4 个：AccessRequestModal 需要外部门只读行，
 * 本门在 mock 里补了一条 can_manage:false 的文档，把最后一个也纳进来）。
 */

const KB_ADMIN = { role: 'kb_admin' as const, managedRoots: [] };

/** 外部门只读行——DocTable 对它渲染「申请授权」，是 AccessRequestModal 的唯一入口。 */
const FOREIGN_DOC = {
  ...NODE_DOC, doc_id: 'fd1', title: '营销费用报销流程', original_filename: 'reimburse.docx',
  owner_dept: 'marketing', can_manage: false,
};

async function seed(page: Page) {
  await mockNodeMode(page, { ...KB_ADMIN, docs: [NODE_DOC, FOREIGN_DOC] });
  await page.route('**/api/kb/visibility-explain**', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({
      doc_id: 'nd1', owner_dept: '', permission_level: 'dept_internal',
      everyone: false, nobody: false, quarantined: false, active: true,
      readers: [{ dept: '注塑事业部', via: 'owner' }],
    }),
  }));
  await page.route('**/api/kb/version-history**', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({ versions: [
      { version_no: 1, status: 'SUCCESS', created_at: '2026-08-01 10:00', chunks: 12, error_message: '' },
    ] }),
  }));
}

/** 读遮罩与面板的动效计算值。 */
async function animOf(page: Page) {
  return page.evaluate(() => {
    const scrim = document.querySelector('.kb-scrim');
    const pop = document.querySelector('.kb-pop');
    const g = (el: Element | null) => {
      if (!el) return null;
      const c = getComputedStyle(el);
      return { name: c.animationName, dur: c.animationDuration, fill: c.animationFillMode };
    };
    return { scrim: g(scrim), pop: g(pop) };
  });
}

/** 打开一个模态（返回打开动作），逐个断言其入场动效。 */
const OPENERS: { label: string; open: (p: Page) => Promise<void> }[] = [
  { label: '版本历史', open: async (p) => { await p.getByRole('button', { name: '版本历史' }).first().click(); } },
  { label: '可见范围', open: async (p) => { await p.getByTestId('doc-more').first().click(); await p.getByTestId('doc-visibility').click(); } },
  { label: '跨部门共享·权限', open: async (p) => { await p.getByTestId('doc-more').first().click(); await p.getByTestId('doc-share').click(); } },
  { label: '编辑信息', open: async (p) => { await p.getByTestId('doc-more').first().click(); await p.getByTestId('doc-meta').click(); } },
  { label: '退役确认（ConfirmDialog）', open: async (p) => { await p.getByTestId('doc-more').first().click(); await p.getByRole('menuitem', { name: '退役下线' }).click(); } },
  { label: '申请授权（AccessRequestModal）', open: async (p) => { await p.getByRole('button', { name: '申请授权' }).first().click(); } },
];

test.describe('硬门 — 模态入场动效', () => {
  for (const { label, open } of OPENERS) {
    test(`${label}：遮罩与面板都有入场动效，且 fill-mode 为 none`, async ({ page }) => {
      await seed(page);
      await gotoManageDocs(page);
      await expect(page.locator('.kb-scrim')).toHaveCount(0);

      await open(page);
      await expect(page.locator('.kb-scrim')).toHaveCount(1);

      const a = await animOf(page);
      expect(a.scrim!.name, '遮罩必须挂 kb-scrim-in（基线为 none）').toBe('kb-scrim-in');
      expect(a.scrim!.dur).toBe('0.14s');
      expect(a.scrim!.fill, 'fill-mode 一旦非 none，帧冻结时遮罩会停在起始帧却仍拦点击').toBe('none');

      expect(a.pop!.name, '面板必须挂 kb-pop-in').toBe('kb-pop-in');
      expect(a.pop!.dur).toBe('0.16s');
      expect(a.pop!.fill).toBe('none');
    });
  }

  test('toast 也有入场动效，且同样无 fill-mode', async ({ page }) => {
    await seed(page);
    await page.route('**/api/kb/feedback-review**', (r) => r.fulfill({
      contentType: 'application/json', body: JSON.stringify({ items: [{
        message_id: 'fb1', question: '收缩率标准？', created_at: new Date(Date.now() - 86400000).toISOString().slice(0, 19).replace('T', ' '),
        reasons: ['不准确'], comment: '', handled: false, handled_status: 'PENDING', docs: [],
      }] }),
    }));
    await page.route('**/api/kb/feedback-review/resolve', (r) => r.fulfill({ contentType: 'application/json', body: '{}' }));
    await page.goto('/console/manage?token=e2e-fake-token');

    await page.getByRole('button', { name: '标记已处理' }).first().click();
    const li = page.locator('[data-testid="toast-polite"] li').first();
    await expect(li).toBeVisible();
    const a = await li.evaluate((el) => {
      const c = getComputedStyle(el);
      return { name: c.animationName, dur: c.animationDuration, fill: c.animationFillMode };
    });
    expect(a.name).toBe('kb-toast-in');
    expect(a.dur).toBe('0.16s');
    expect(a.fill).toBe('none');
  });

  test('reduced-motion 下入场被全局兜底压成瞬时（token 化未绕过它）', async ({ browser }) => {
    const ctx = await browser.newContext({ reducedMotion: 'reduce' });
    const page = await ctx.newPage();
    await seed(page);
    await gotoManageDocs(page);
    await page.getByRole('button', { name: '版本历史' }).first().click();
    await expect(page.locator('.kb-scrim')).toHaveCount(1);

    const a = await animOf(page);
    expect(a.scrim!.dur).toBe('1e-05s');
    expect(a.pop!.dur).toBe('1e-05s');
    // 名字仍在（全局兜底压的是 duration 不是 animation-name）——这正是它与 .vb-fade 那种
    // 局部 `animation: none` 覆盖的区别，别把两套机制混为一谈。
    expect(a.scrim!.name).toBe('kb-scrim-in');
    await ctx.close();
  });
});
