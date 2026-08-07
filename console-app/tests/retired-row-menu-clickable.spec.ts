import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, gotoManageDocs } from './node-mode.helpers';

/**
 * 硬门 —— 退役行「更多操作」菜单必须可点（2026-08-06 修复的回归门）。
 *
 * 缺陷形态：`.led-row[data-retired="1"] { opacity: .5 }` 让整行成为 stacking context，
 * 行内 `absolute z-20` 的菜单弹层被困在本行内，压不过后面的兄弟行；而后续每一行的
 * `.doc-actions` 自带 opacity .75 同样建 stacking context、DOM 顺序在后 ⇒ 绘制在弹层
 * 之上。表现：退役文档只要不是台账最后一行，整个菜单（含**恢复上线**）鼠标点不动，
 * 图标还因 .5×.75=.375 的复合不透明度看着像禁用。修复=行级 data-menu-open 恒亮。
 *
 * 🔴 本 spec 自带**反证锚**（用例 3）：强制把行 opacity 压回 .5 后必须重新变得不可点。
 * 没有它，前两条会在"布局恰好没遮挡"时假绿——它们断言的是命中测试结果，不是修复本身。
 */

const DOC = (i: number, retired: boolean) => ({
  doc_id: `d${i}`, title: `文档${i}`, owner_dept: '', acl_mode: 'node',
  owner_key: 'node:3', owner_label: '注塑事业部', permission_level: 'dept_internal',
  current_version_no: 1, status: retired ? 'retired' : 'active',
  status_badge: retired ? '已退役' : '已上线', updated_at: '2026-08-01 10:00',
});

/** 目标行**后面必须还有行**——这是遮挡成立的前提，最后一行天然无人可盖。 */
const LAYOUT = [DOC(0, false), DOC(1, true), DOC(2, true), DOC(3, true), DOC(4, false)];
const TARGET = 1;

async function openRowMenu(page: Page, row: number) {
  const gear = page.getByTestId('doc-more').nth(row);
  await gear.scrollIntoViewIfNeeded();
  // 把齿轮钉在视口 y≈220：向下弹出的菜单完整落在视口内，命中测试才有意义
  await page.evaluate((n) => {
    const el = document.querySelectorAll('[data-testid="doc-more"]')[n] as HTMLElement;
    window.scrollBy(0, el.getBoundingClientRect().top - 220);
  }, row);
  await gear.click();
  await expect(page.locator('[data-act-menu="row"] [role="menu"]')).toHaveCount(1);
  // 等操作列的 opacity 过渡收尾再做命中测试。`.doc-actions` 有 `transition: opacity .13s`，
  // 从 .75 升到 1 的这 130ms 里它仍 <1 ⇒ 仍是 stacking context ⇒ 菜单**瞬时**被困。
  // 那是修复前后都存在的既有行为，且短于任何一次鼠标移动；不等它收尾会抓到中间帧假红。
  // 缺陷态（R3）不受影响：那里压的是**行**的 opacity，静态值，等多久都不会变。
  await expect.poll(() => page.evaluate(() => {
    const el = document.querySelector('.doc-actions[data-open="1"]');
    return el ? getComputedStyle(el).opacity : '';
  }), { timeout: 2000 }).toBe('1');
}

/** 每个菜单项中心点上，真正接收指针事件的是不是它自己。 */
function hitTest(page: Page) {
  return page.evaluate(() => {
    const items = [...document.querySelectorAll('[data-act-menu="row"] [role="menu"] [role="menuitem"]')];
    return items.map((el) => {
      const r = el.getBoundingClientRect();
      const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return {
        label: (el.textContent || '').trim(),
        clickable: !!top && (el === top || el.contains(top)),
        // 失败时直接看到是谁盖的（视口外 ⇒ elementFromPoint 返回 null）
        blockedBy: !top ? 'OUT_OF_VIEWPORT'
          : `${top.tagName}.${(top.className || '').toString().split(' ').slice(0, 2).join('.')}`,
      };
    });
  });
}

test.describe('硬门 — 退役行菜单可点性', () => {
  test('R1 退役行的每一个菜单项都接收指针事件', async ({ page }) => {
    await mockNodeMode(page, { role: 'kb_admin', docs: LAYOUT });
    await gotoManageDocs(page);
    await openRowMenu(page, TARGET);

    const hits = await hitTest(page);
    expect(hits.length, '菜单项应被渲染（否则下面的 every 空过）').toBeGreaterThan(2);
    expect(hits.filter((h) => !h.clickable), '退役行菜单项被上层元素盖住 ⇒ 鼠标点不动').toEqual([]);
  });

  test('R2 退役行能点开「编辑信息」（改归属的唯一入口）', async ({ page }) => {
    await mockNodeMode(page, { role: 'kb_admin', docs: LAYOUT });
    await gotoManageDocs(page);
    await openRowMenu(page, TARGET);

    await page.getByRole('menuitem', { name: '编辑信息' }).click({ timeout: 3000 });
    await expect(page.locator('h3', { hasText: '编辑信息' })).toBeVisible();
  });

  test('R3 反证锚：把行 opacity 压回 .5，菜单必须重新变得不可点', async ({ page }) => {
    await mockNodeMode(page, { role: 'kb_admin', docs: LAYOUT });
    await gotoManageDocs(page);
    // 精确复原缺陷态：只推翻 data-menu-open 那条恒亮规则，其余一律不动
    await page.addStyleTag({ content:
      '.led-row[data-retired="1"][data-menu-open="1"],' +
      '.led-row[data-retired="1"][data-menu-open="1"]:hover{opacity:.5 !important}' });
    await openRowMenu(page, TARGET);

    const hits = await hitTest(page);
    expect(hits.some((h) => !h.clickable),
      '缺陷态下应至少有一项被遮挡——若这里也全可点，说明 R1/R2 测的根本不是遮挡').toBe(true);
  });
});
