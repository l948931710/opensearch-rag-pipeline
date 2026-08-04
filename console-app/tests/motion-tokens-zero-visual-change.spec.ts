import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, gotoManageDocs, MANAGE_ROUTE } from './node-mode.helpers';

/**
 * 硬门 —— 动效 token 化 / 分区常量抽取的「零视觉变化」证明（2026-08-04）。
 *
 * 本轮把 tokens.css 里散落的时长与缓动收成具名 token、把三个看板各写一遍的分区常量
 * 抽到 @/lib/section，并清掉两处镜像死代码（.kb-seg 有规则无 DOM / .kb-cards 有 DOM 无规则）。
 * 契约是**不改任何观感**，所以断言全部是「计算值与基线严格相等」，不是「差 ≤Nms」——
 * 数值合并会把这道门弱化成人为设定的容差，那就证明不了任何事。
 *
 * 断言 ↔ 审计验收条件对应关系：
 *   G1  ← B1（SUBHEAD 收敛不得产生新错位）
 *   G2  ← B3（ease / CSS ease-out / Tailwind ease-out 三条同名不同值曲线不得互换）
 *   G3  ← A1（动效计算值逐条不变，含 reduced-motion 兜底仍生效）
 *   G4  ← A5 + B2（两处死代码确实消失，且未误伤活规则）
 *   G5  ← C1（StatCard 骨架维持 animate-pulse，statcard.spec.ts:11,27,33 不得被打红）
 *   G6  ← A6（三视口无整页横向滚动）
 */

const KB_ADMIN = { role: 'kb_admin' as const, managedRoots: [] };
const DEPT_ADMIN = { role: 'dept_admin' as const, managedRoots: [3] };

/** 读一个元素的 x（用于跨角色比对 SUBHEAD 基准位）。 */
async function xOf(page: Page, text: string): Promise<number> {
  const box = await page.getByText(text, { exact: true }).first().boundingBox();
  if (!box) throw new Error(`定位失败：${text}`);
  return box.x;
}

/** 读某 class 首个实例的动效计算值。 */
async function motionOf(page: Page, sel: string) {
  return page.locator(sel).first().evaluate((el) => {
    const c = getComputedStyle(el);
    return {
      transitionDuration: c.transitionDuration,
      transitionProperty: c.transitionProperty,
      transitionTimingFunction: c.transitionTimingFunction,
      animationName: c.animationName,
      animationDuration: c.animationDuration,
      animationTimingFunction: c.animationTimingFunction,
      animationFillMode: c.animationFillMode,
    };
  });
}

test.describe('硬门 — 动效 token 化零视觉变化', () => {
  test('G1 SUBHEAD 收敛后 20 个实例落在同一基准，未产生新错位', async ({ page }) => {
    // 收敛前：3 个实例在 x=277.5（KbAdmin「状态分布」经调用点 override、Dept 两处经常量），
    // 17 个在 x=275.5。两条互不知情的代码路径造成同一个 2px —— 只改常量会让两个看板
    // 本来恰好对齐的「状态分布」反而错开。故常量取无偏移版 + 同步移除调用点 override。
    await mockNodeMode(page, KB_ADMIN);
    await page.goto(MANAGE_ROUTE);
    await expect(page.getByText('运行健康', { exact: true })).toBeVisible();
    const adminBaseline = await xOf(page, '运行健康');       // 裸 SUBHEAD 基准位
    const adminDist = await xOf(page, '状态分布');            // 曾带调用点 ml-0.5

    expect(adminDist, 'KbAdmin「状态分布」应已回到裸 SUBHEAD 基准位').toBe(adminBaseline);

    await mockNodeMode(page, DEPT_ADMIN);
    await page.goto(MANAGE_ROUTE);
    await expect(page.getByText('状态分布', { exact: true })).toBeVisible();
    const deptDist = await xOf(page, '状态分布');
    const deptTop = await xOf(page, '最常被检索的文档');

    // 验收条件 1：两个看板的「状态分布」必须仍然相等（不产生新错位）
    expect(deptDist, '两个看板的「状态分布」必须对齐在同一 x').toBe(adminDist);
    // 验收条件 2：Dept「最常被检索的文档」必须落到与 KbAdmin 裸 SUBHEAD 同一基准
    expect(deptTop, 'Dept「最常被检索的文档」应与裸 SUBHEAD 同基准').toBe(adminBaseline);
  });

  test('G2 三条同名不同值的缓动曲线未被互换', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.goto(MANAGE_ROUTE);

    // 【本断言抓到过一个真回归，勿删勿放宽】Sidebar.vue:58 用 Tailwind 的 `ease-out` 工具类，
    // 它编译成对 --ease-out 变量的引用，Tailwind v4 默认 @theme 把该变量定义为
    // cubic-bezier(0,0,.2,1)。若有人在 tokens.css 的 :root 里声明同名的 --ease-out
    // （哪怕值写成 CSS 关键字 `ease-out` 求「统一」），就会覆盖 Tailwind 主题变量，
    // 侧栏展开曲线被静默改成另一条 —— 静态截图完全看不出来，只有本断言能拦住。
    const sidebar = await page.locator('.group\\/sb').first().evaluate(
      (el) => getComputedStyle(el).transitionTimingFunction);
    expect(sidebar, 'Tailwind 的 --ease-out 主题变量不得被 tokens.css 覆盖').toBe('cubic-bezier(0, 0, 0.2, 1)');

    // CSS 原生 ease-out（.kb-in / .vb-fade 用它）：Chrome 保留关键字原形，与上面的展开形
    // 恰好可区分 —— 两者计算值不同本身就是「没被互换」的证据。
    await expect(page.locator('.kb-in').first()).toBeAttached();
    const kbIn = await motionOf(page, '.kb-in');
    expect(kbIn.animationTimingFunction, '.kb-in 必须仍是 CSS 原生 ease-out，不是 Tailwind 那条').toBe('ease-out');
    expect(kbIn.animationName).toBe('kb-in');
    expect(kbIn.animationDuration).toBe('0.5s');
    expect(kbIn.animationFillMode).toBe('both');
  });

  test('G3 动效计算值逐条不变（含未收编的 straggler）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.goto(MANAGE_ROUTE);
    await expect(page.locator('.kb-card').first()).toBeVisible();

    const card = await motionOf(page, '.kb-card');
    expect(card.transitionDuration, '.kb-card 已换 --dur-fast，值必须仍是 .14s').toBe('0.14s, 0.14s');
    expect(card.transitionProperty).toBe('border-color, transform');
    expect(card.transitionTimingFunction, '缓动刻意不 token 化，规则里写字面量 ease').toBe('ease, ease');

    await expect(page.locator('.kb-trend-bar').first()).toBeAttached();
    const trend = await motionOf(page, '.kb-trend-bar');
    expect(trend.transitionDuration).toBe('0.14s, 0.14s');
    expect(trend.transitionTimingFunction).toBe('ease, ease');
    expect(trend.animationName, '同元素同时挂 .kb-rise').toBe('kb-rise');
    expect(trend.animationDuration, '.kb-rise 已换 --dur-reveal，值必须仍是 .5s').toBe('0.5s');
    expect(trend.animationTimingFunction).toBe('cubic-bezier(0.22, 1, 0.36, 1)');
    expect(trend.animationFillMode).toBe('both');

    // .kb-grow 吃 --curve-emphasis（时长 .55s 是有意保留的 straggler）。渲染于 BarList / StatusDistBar。
    await expect(page.locator('.kb-grow').first()).toBeAttached();
    const grow = await motionOf(page, '.kb-grow');
    expect(grow.animationName).toBe('kb-grow');
    expect(grow.animationDuration, '.55s 是有意保留的 straggler，未并入 --dur-reveal').toBe('0.55s');
    expect(grow.animationTimingFunction, '--curve-emphasis 必须解析回原曲线').toBe('cubic-bezier(0.22, 1, 0.36, 1)');
    expect(grow.animationFillMode).toBe('both');

    // 「文档管理」tab：.dropzone（吃 --dur-fast）与 .doc-actions（straggler，.13s 未收编）
    await gotoManageDocs(page);
    await expect(page.locator('.dropzone').first()).toBeAttached();
    const dz = await motionOf(page, '.dropzone');
    expect(dz.transitionDuration, '.dropzone 已换 --dur-fast').toBe('0.14s, 0.14s');
    expect(dz.transitionProperty).toBe('border-color, background');
    expect(dz.transitionTimingFunction).toBe('ease, ease');

    await expect(page.locator('.doc-actions').first()).toBeAttached();
    const actions = await motionOf(page, '.doc-actions');
    expect(actions.transitionDuration, '.13s 是有意保留的 straggler').toBe('0.13s');
    expect(actions.transitionProperty).toBe('opacity');

    // 注：.kb-rise-top 同样吃 --dur-reveal + --curve-emphasis，但它与 .kb-seg 一样是**孤儿规则**
    // （全 src grep 零消费方），没有 DOM 实例可测，故本门无法覆盖 —— 如实标注，不假装已验证。
  });

  test('G3b reduced-motion 全局兜底仍把新 token 压成瞬时', async ({ browser }) => {
    const ctx = await browser.newContext({ reducedMotion: 'reduce' });
    const page = await ctx.newPage();
    await mockNodeMode(page, KB_ADMIN);
    await page.goto(MANAGE_ROUTE);
    await expect(page.locator('.kb-card').first()).toBeVisible();

    // token 化不得绕过 tokens.css 的全局 @media 兜底（它压的是 duration，不是 token 本身）。
    // 注意兜底用的是单值 `transition-duration: 0.01ms !important`，会把多属性的时长列表
    // 塌成一个值 —— 故这里是 '1e-05s' 而非常态下的 '1e-05s, 1e-05s'。
    const card = await motionOf(page, '.kb-card');
    expect(card.transitionDuration).toBe('1e-05s');
    const trend = await motionOf(page, '.kb-trend-bar');
    expect(trend.animationDuration).toBe('1e-05s');
    await ctx.close();
  });

  test('G4 两处镜像死代码已清除，且未误伤活规则', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.goto(MANAGE_ROUTE);
    await expect(page.locator('.kb-card').first()).toBeVisible();

    // .kb-cards：有 DOM 消费方、零 CSS 规则 → 从三个 GRID 常量里删掉
    expect(await page.locator('.kb-cards').count(), '.kb-cards 应已从模板消失').toBe(0);

    // .kb-seg：有 CSS 规则、零 DOM 消费方 → 规则删除
    const rules = await page.evaluate(() => {
      const hit: string[] = [];
      for (const sheet of Array.from(document.styleSheets)) {
        let list: CSSRuleList;
        try { list = sheet.cssRules; } catch { continue; }   // 跨源样式表跳过
        for (const rule of Array.from(list)) {
          const sel = (rule as CSSStyleRule).selectorText;
          if (sel && (sel.includes('.kb-seg') || sel.includes('.kb-cards'))) hit.push(sel);
        }
      }
      return hit;
    });
    expect(rules, '.kb-seg / .kb-cards 都不应再有 CSS 规则').toEqual([]);

    // 反向守卫：同一区块的活规则未被误删
    await expect(page.locator('.kb-card').first()).toBeVisible();
    await expect(page.locator('.kb-trend-bar').first()).toBeAttached();
  });

  test('G5 StatCard 骨架维持 animate-pulse（statcard.spec.ts 不得被打红）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    // stats 慢响应 → StatCard 停在 loading 态
    await page.route('**/api/kb/stats**', async (r) => {
      await new Promise((s) => setTimeout(s, 3000));
      await r.fulfill({ contentType: 'application/json', body: JSON.stringify({ total: 1, active: 1, retired: 0, chunks: 1, by_badge: {} }) });
    });
    await page.goto(MANAGE_ROUTE);

    const skel = page.locator('.animate-pulse').first();
    await expect(skel, '骨架必须仍靠 .animate-pulse 类名存在').toBeVisible();
    const s = await skel.evaluate((el) => {
      const c = getComputedStyle(el);
      return { name: c.animationName, dur: c.animationDuration, ease: c.animationTimingFunction };
    });
    expect(s.name).toBe('pulse');
    expect(s.dur).toBe('2s');
    expect(s.ease).toBe('cubic-bezier(0.4, 0, 0.6, 1)');
  });

  test('G6 三视口无整页横向滚动', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    for (const vp of [{ width: 1440, height: 900 }, { width: 1280, height: 800 }, { width: 1024, height: 768 }]) {
      await page.setViewportSize(vp);
      await page.goto(MANAGE_ROUTE);
      await expect(page.locator('.kb-card').first()).toBeVisible();
      const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(over, `${vp.width}px 视口不得出现整页横向滚动`).toBeLessThanOrEqual(1);
    }
  });
});
