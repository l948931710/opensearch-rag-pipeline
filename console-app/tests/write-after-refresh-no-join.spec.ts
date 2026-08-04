import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, MANAGE_ROUTE } from './node-mode.helpers';

/**
 * 硬门 —— 写后权威重拉禁止合流。
 *
 * 缺陷形状（评审 S1 实测，本轮 2c 自己引入）：`withLoader` 把「在途/已定态记账」与「请求合流」
 * 捆在一起，而写后重拉只需要前者。第二次撤销的重拉 join 到第一次那个**快照早于本次写**的
 * 在途 GET 上 ⇒ 服务端已撤销、界面仍显示该行；`revokeAdminGrant` 的 toast 却已播报
 * 「已撤销全部管理权限」（useKb.ts 那条注释承诺的正是「播报时列表已是最终状态」）。
 * `_loadedTabs` 短路让切走再切回也不重拉，脏行留到整页刷新为止。
 *
 * 判据锚点（评审实测）：GET 次数 缺陷版 2 / 正确版 3；界面 BBB 残留 1 / 0。
 *
 * 建模要点两条，缺一门就测不到该测的那一半：
 *  ① GET 响应体必须在**请求到达时**快照，晚于此的写不得反映进去——真实慢查询的语义，
 *     也是合流之所以有害的原因。
 *  ② **三次 GET 的时延必须不等长**。等长（本门初版全用 2200ms）会让响应恒按发出顺序到达，
 *     于是只证明了「不再合流」，证不到「界面落到最终状态」——评审用 150/3000/300ms 让
 *     GET#2 的旧快照后到，实测撤销过的 BBB 行复活、且带可点的「撤销全部」。等长版对这半
 *     是恒绿的，留着等于给出「已覆盖」的错误信号。落地侧靠 adminSeq 守卫（同 grantsSeq）。
 */

const KB_ADMIN = { role: 'kb_admin' as const, managedRoots: [] };
const J = (body: unknown) => ({ contentType: 'application/json', body: JSON.stringify(body) });

test('撤销两个 dept_admin：界面须落到服务端最终状态（不得 join 到旧快照）', async ({ page }) => {
  await mockNodeMode(page, KB_ADMIN);

  // 服务端权威状态
  let server = [
    { user_id: 'u1', user_name: 'AAA', role: 'dept_admin', managed_owner_depts: ['marketing'] },
    { user_id: 'u2', user_name: 'BBB', role: 'dept_admin', managed_owner_depts: ['quality'] },
  ];
  let gets = 0;

  await page.route('**/api/kb/admin-grants/revoke**', async (r) => {
    const { user_id } = JSON.parse(r.request().postData() || '{}');
    server = server.filter((a) => a.user_id !== user_id);   // 写立即生效
    await r.fulfill(J({ ok: true }));
  });
  // 不等长：GET#2 拖 3000ms、GET#3 只 300ms ⇒ 旧快照后到，逼出落地侧的竞态
  const DELAY = [150, 3000, 300];
  await page.route('**/api/kb/admin-grants**', async (r) => {
    if (r.request().method() !== 'GET') return r.fallback();
    const n = gets++;
    const snapshot = JSON.parse(JSON.stringify(server));    // ← 到达即快照
    await new Promise((s) => setTimeout(s, DELAY[n] ?? 300));
    await r.fulfill(J({ items: snapshot, grantable_owner_depts: ['marketing', 'quality'] }));
  });

  await page.goto(MANAGE_ROUTE);
  await page.locator('[aria-label="管理台分区"]').getByRole('tab', { name: /成员管理/ }).click();
  await expect(page.getByText('BBB')).toBeVisible({ timeout: 6000 });
  expect(gets, '首载一次').toBe(1);

  const revokeAll = (name: string) =>
    page.locator('div').filter({ hasText: new RegExp(`^${name}`) }).getByRole('button', { name: '撤销全部' }).first();

  // 撤销 AAA → 触发 GET#2（快照此刻 = 只剩 BBB），该 GET 在途 2.2s
  await revokeAll('AAA').click();
  await page.getByRole('button', { name: '撤销全部' }).last().click();   // 确认框
  await expect.poll(() => gets, { timeout: 3000 }).toBe(2);

  // GET#2 仍在途时撤销 BBB → 正确实现应发 GET#3（快照 = 空）；合流版会 join 到 GET#2
  await revokeAll('BBB').click();
  await page.getByRole('button', { name: '撤销全部' }).last().click();
  await expect.poll(() => gets, { timeout: 3000 }).toBe(3);

  // 界面必须落到服务端最终状态。等 GET#2（3000ms）也落地后再断言——正是它携带旧快照。
  await page.waitForTimeout(4000);
  await expect(page.getByText('BBB'), '旧快照后到覆盖新清单 ⇒ 撤销过的行复活').toHaveCount(0);
  await expect(page.getByText('AAA')).toHaveCount(0);
  await expect(page.getByText('部门管理员名单暂不可用——上方错误条可重试。')).toHaveCount(0);
});
