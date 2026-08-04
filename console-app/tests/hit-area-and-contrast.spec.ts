import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, MANAGE_ROUTE } from './node-mode.helpers';

/**
 * 硬门 —— 轮 4：命中区扩张（视觉尺寸零变化）+ WCAG 1.4.11 对比度。
 *
 * 审计推翻了方案的一条前提：「除 .doc-actions 外几乎处处达标」**不成立**。实测至少 7 处
 * 不达标，其中两处（台账表头/行 checkbox，16×16，且表头那个到排序按钮只有 8px）就在本轮
 * 编辑的同一文件里，而方案原本要扩的三个操作按钮实测 28×28 **本征达标**。
 * 所以本轮的定性是：
 *   · .doc-actions 对比度 = **合规修复**（1.4.11 明确不达标）
 *   · checkbox / chip × / 展开箭头的命中区 = **合规修复**（2.5.8 尺寸+间距双重不达标）
 *   · 三个操作按钮的 gap 与命中区 = **吞吐优化**，不拿合规话术贴金
 */

const KB_ADMIN = { role: 'kb_admin' as const, managedRoots: [] };
const J = (body: unknown) => ({ contentType: 'application/json', body: JSON.stringify(body) });
const tab = (p: Page, name: RegExp) => p.locator('[aria-label="管理台分区"]').getByRole('tab', { name });

const DOCS = (n: number) => ({
  items: Array.from({ length: n }, (_, i) => ({
    doc_id: `d${i}`, title: `文档${i}`, owner_dept: 'marketing', permission_level: 'dept_internal',
    status_badge: '已上线', version_no: 1, updated_at: '2026-08-01 10:00:00', is_active: 1,
    chunks: 3, can_manage: true,
  })),
  total: n, has_more: false,
});

/** ::before 是 abspos + translate(-50%,-50%)，boundingBox() 测不到，须读伪元素计算值再手算。 */
async function hitBox(page: Page, sel: string) {
  return page.locator(sel).first().evaluate((el) => {
    const cs = getComputedStyle(el, '::before');
    const r = el.getBoundingClientRect();
    return {
      w: Math.round(parseFloat(cs.width)), h: Math.round(parseFloat(cs.height)),
      pos: cs.position, cx: r.x + r.width / 2, cy: r.y + r.height / 2,
      selfW: Math.round(r.width), selfH: Math.round(r.height),
    };
  });
}

async function gotoDocs(page: Page, n = 3) {
  await mockNodeMode(page, KB_ADMIN);
  await page.route('**/api/kb/my-docs**', (r) => r.fulfill(J(DOCS(n))));
  await page.goto(MANAGE_ROUTE);
  await tab(page, /文档管理/).click();
  await expect(page.locator('.led-row').first()).toBeVisible();
}

test.describe('硬门 — 轮 4 命中区与对比度', () => {
  test('H1 视觉尺寸零变化：三个操作按钮恒 28×28、勾选框恒 16×16', async ({ page }) => {
    await gotoDocs(page);
    for (const sel of ['button[aria-label="版本历史"]', '[data-testid="doc-preview"]', '[data-testid="doc-more"]']) {
      const b = await hitBox(page, sel);
      expect(b.selfW, `${sel} 视觉宽度不得变`).toBe(28);
      expect(b.selfH, `${sel} 视觉高度不得变`).toBe(28);
    }
    for (const sel of ['.led-head [role="checkbox"]', '.led-row .led-cell-main [role="checkbox"]']) {
      const b = await hitBox(page, sel);
      expect(b.selfW, `${sel} 勾选框视觉尺寸不得变`).toBe(16);
      expect(b.selfH).toBe(16);
    }
    // 图标也不得偷偷放大（防「按钮不变但内部图标变大」的障眼法）
    const svg = await page.locator('.doc-actions svg').first().evaluate((el) => el.getAttribute('width'));
    expect(svg).toBe('14');
  });

  test('H2 命中区真的扩了：勾选框 16×16 → ≥32×44 / ≥32×32', async ({ page }) => {
    await gotoDocs(page);
    const row = await hitBox(page, '.led-row .led-cell-main [role="checkbox"]');
    expect(row.pos, '必须是 abspos——脱离文档流才不参与 grid track').toBe('absolute');
    expect(row.w).toBeGreaterThanOrEqual(32);
    expect(row.h, '行内取 44（2.5.5）').toBeGreaterThanOrEqual(44);

    const head = await hitBox(page, '.led-head [role="checkbox"]');
    expect(head.w).toBeGreaterThanOrEqual(32);
    expect(head.h, '表头封顶 32——44 会与首行命中区叠上').toBe(32);

    // 守卫（评审 P3）：表头命中区右缘与首个排序按钮左缘实测恰好 0px——无重叠但零余量。
    // 不缩尺寸（会让掉 2px 合规余量），只钉「不重叠」：将来谁动表头 gap/内边距把两者
    // 压叠，这里红，而不是静默抢点。
    const sort = await page.locator('.led-head .led-sort').first().boundingBox();
    expect(head.cx + head.w / 2, '表头勾选框命中区不得压上排序按钮（现距 0px，零余量）')
      .toBeLessThanOrEqual(sort!.x);
  });

  test('H3 行操作：中心距 30→38，命中区不重叠且留死区', async ({ page }) => {
    await gotoDocs(page);
    const a = await hitBox(page, 'button[aria-label="版本历史"]');
    const b = await hitBox(page, '[data-testid="doc-preview"]');
    const c = await hitBox(page, '[data-testid="doc-more"]');

    expect(Math.round(b.cx - a.cx), '相邻中心距 30→38').toBe(38);
    expect(Math.round(c.cx - b.cx)).toBe(38);
    for (const [l, r] of [[a, b], [b, c]] as const) {
      const gapPx = (r.cx - r.w / 2) - (l.cx + l.w / 2);
      expect(gapPx, '命中区之间必须留死区：误点落空 ≠ 误触发').toBeGreaterThanOrEqual(4);
    }
  });

  test('H4 操作列没被撑破：仍 110px 且三视口无横向滚动', async ({ page }) => {
    await gotoDocs(page);
    const cols = await page.locator('.led-row').first().evaluate((el) => getComputedStyle(el).gridTemplateColumns);
    expect(cols, 'grid-template-columns 一字不改').toContain('110px');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });

  test('H5 WCAG 1.4.11：.doc-actions 静止态 ≥3:1（亮/暗各一次）', async ({ page }) => {
    for (const scheme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: scheme });
      await gotoDocs(page);
      const ratio = await page.locator('.doc-actions').first().evaluate((el) => {
        const toLin = (c: number) => { const x = c / 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4; };
        const lum = (v: number[]) => 0.2126 * toLin(v[0]) + 0.7152 * toLin(v[1]) + 0.0722 * toLin(v[2]);
        const rgb = (s: string) => s.match(/\d+/g)!.map(Number).slice(0, 3);
        const fg = rgb(getComputedStyle(el.querySelector('button')!).color);
        let bgEl: Element | null = el, bg = [255, 255, 255];
        while (bgEl) {
          const c = getComputedStyle(bgEl).backgroundColor;
          if (c && !/rgba?\(0, 0, 0, 0\)|transparent/.test(c)) { bg = rgb(c); break; }
          bgEl = bgEl.parentElement;
        }
        const a = parseFloat(getComputedStyle(el).opacity);
        const blend = fg.map((c, i) => c * a + bg[i] * (1 - a));
        const [hi, lo] = [lum(blend), lum(bg)].sort((x, y) => y - x);
        return (hi + 0.05) / (lo + 0.05);
      });
      expect(ratio, `${scheme} 静止态对比度（基线：亮 1.895 / 暗 2.244，均 < 3）`).toBeGreaterThanOrEqual(3.0);
    }
    await page.emulateMedia({ colorScheme: null });
  });

  test('H6 两级设计保留：hover 升满', async ({ page }) => {
    await gotoDocs(page);
    const actions = page.locator('.doc-actions').first();
    expect(Number(await actions.evaluate((el) => getComputedStyle(el).opacity))).toBeLessThan(1);
    await page.locator('.led-row').first().hover();
    await expect.poll(() => actions.evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
  });

  // hover:none 半边不走 CDP：page.emulateMedia 没有 hover 字段（核对过类型签名），而
  // 运行时 CDP `Emulation.setEmulatedMedia` 在全量并行下**不可靠**——一次性 send 与
  // poll 内幂等重发两种写法各复现 2+ 轮「单跑必绿、并行必红」，trace 显示 poll 首读距
  // send 仅 9ms 且全程零导航，opacity 却恒 .75：覆写不是被 reload 顶掉，是并行负载下
  // 多 CDP 会话的 emulation 状态竞态让它压根没落到渲染器（机理未定案，现象可复现）。
  // 改用上下文级 hasTouch：**建上下文时**即翻转 (hover:none)+(pointer:coarse)（实测
  // about:blank matchMedia 均 true），没有运行时下发环节 ⇒ 没有竞态面。
  test.describe('H6b 触屏上下文', () => {
    test.use({ hasTouch: true });
    test('hover:none 下操作恒亮', async ({ page }) => {
      await gotoDocs(page);
      // 先证仿真在位，把「仿真失效」和「CSS 规则被删」两种红区分开
      expect(await page.evaluate(() => matchMedia('(hover: none)').matches),
        'hasTouch 上下文应使 (hover: none) 命中').toBe(true);
      // poll 而非裸读：.13s 过渡中途读数会拿到误导值（评审探针实测栽过），等稳态
      await expect.poll(() => page.locator('.doc-actions').first().evaluate((el) => getComputedStyle(el).opacity),
        { message: '触屏无悬停：操作必须恒可见（原注释的盲区论证仍成立）' }).toBe('1');
    });
  });

  test('H7 回归守卫：本征达标的控件未被一刀切放大', async ({ page }) => {
    await gotoDocs(page);
    await page.locator('.led-row .led-cell-main [role="checkbox"]').first().click();
    const cancel = page.getByRole('button', { name: /取消选择/ });
    const box = await cancel.boundingBox();
    expect(Math.round(box!.height), '24×24 恰好达标 ⇒ 本轮明确不改').toBe(24);
    const hasBefore = await cancel.evaluate((el) => getComputedStyle(el, '::before').content);
    expect(hasBefore, '不该给它挂 .hit').toBe('none');
  });
});
