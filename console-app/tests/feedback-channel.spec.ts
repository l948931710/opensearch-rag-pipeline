import { test, expect, type Page } from '@playwright/test';
import { mockNodeMode, gotoManageDocs, MANAGE_ROUTE } from './node-mode.helpers';

/**
 * 硬门 —— 成功侧反馈通道（toast + aria-live）与模态 Esc 一致性（2026-08-04）。
 *
 * 基线事实（独立审计实测，决定了本门断言的落点）：
 *   · 点击→按钮态变化只要 17–24ms —— **在途反馈本来就不缺**，给它写断言等于写空门。
 *   · 成功之后：管理台 5 个 tab 的 [role=status]/[aria-live] 实例数**恒为 0**；
 *     resolveFeedback/resolveReviewTask 成功即把该行 filter 掉，用户唯一反馈是「行没了」；
 *     处理完最后一条时空态文案还与「从来没有过」共用同一句「干得漂亮」。
 *   · DocMetaModal 保存成功后 savedNote 实测 sawSavedNoteEver=0（含唯一那句索引重推说明）。
 *   · 6 个模态里只有 DocMetaModal 没有 Esc。
 * 故本门全部断言都落在**成功侧**与**一致性**上，不复述已经达标的在途指标。
 *
 * 断言 ↔ 审计验收条件：G1/G2 ← B1；G3 ← B2；G5/G6 ← B3；G4/G7 ← B1 的选择器约束与 C1 风险。
 */

const KB_ADMIN = { role: 'kb_admin' as const, managedRoots: [] };
const ago = (d: number) => new Date(Date.now() - d * 86400000).toISOString().slice(0, 19).replace('T', ' ');

const POLITE = '[data-testid="toast-polite"]';
const ASSERTIVE = '[data-testid="toast-assertive"]';

/** 差评一条（形状对齐 useKb.ts 的 FeedbackReviewItem）。注册在 mockNodeMode 之后才生效。 */
async function seedFeedback(page: Page) {
  await page.route('**/api/kb/feedback-review**', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({ items: [{
      message_id: 'fb1', question: '2oz 纸杯的收缩率标准是多少？', created_at: ago(1),
      reasons: ['不准确'], comment: '参数表是旧机型的。', handled: false, handled_status: 'PENDING',
      docs: [{ doc_id: 'D1', title: '纸杯工艺参数表', owner_dept: 'production' }],
    }] }),
  }));
  await page.route('**/api/kb/feedback-review/resolve', (r) => r.fulfill({
    contentType: 'application/json', body: '{}',
  }));
}

async function seedReviewTasks(page: Page) {
  await page.route('**/api/kb/review-tasks**', (r) => r.fulfill({
    contentType: 'application/json', body: JSON.stringify({ items: [{
      task_id: 'rt1', doc_id: 'D7', title: '员工薪酬发放办法', version_no: 2,
      review_type: 'spot_check_mismatch', review_reason: '实时权限 public 比 LLM 建议 restricted 更宽松',
      owner_dept: 'hr', suggested_permission_level: 'restricted',
      created_at: ago(9), age_days: 9, status: 'PENDING', closed: false, reviewer_name: '',
    }] }),
  }));
  await page.route('**/api/kb/review-tasks/resolve', (r) => r.fulfill({
    contentType: 'application/json', body: '{}',
  }));
}

test.describe('硬门 — 成功侧反馈通道', () => {
  test('G1 差评处置成功后，polite 活区出现可播报回执（基线为 0）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await seedFeedback(page);
    await page.goto(MANAGE_ROUTE);

    const row = page.getByText('2oz 纸杯的收缩率标准是多少？');
    await expect(row).toBeVisible();
    await expect(page.locator(POLITE), '活区必须先于内容存在，否则读屏不播报').toHaveCount(1);
    await expect(page.locator(POLITE)).toBeEmpty();

    await page.getByRole('button', { name: '标记已处理' }).first().click();

    // 成功侧：行消失【且】留下回执 —— 二者缺一，「行没了」就与误点/没生效不可区分
    await expect(row).toHaveCount(0);
    await expect(page.locator(POLITE)).toContainText('已标记为已处理');
    await expect(page.locator(POLITE), '回执应指出恢复路径，把消失变成可逆').toContainText('显示已处理');
  });

  test('G2 复审任务处置成功后同样有回执', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await seedReviewTasks(page);
    await page.goto(MANAGE_ROUTE);

    const row = page.getByText('员工薪酬发放办法');
    await expect(row.first()).toBeVisible();
    await page.getByRole('button', { name: '误报忽略' }).first().click();

    await expect(page.locator(POLITE)).toContainText('已按误报忽略');
    // 与 G1 对称。这条最初缺席，让一条指向不存在控件的回执（「显示已处置」）漏进了实现——
    // 回执里的控件名必须与 ReviewTaskQueue.vue:49 的按钮标签逐字一致，否则「可逆」是空头支票。
    await expect(page.locator(POLITE), '回执应指出恢复路径，且控件名与真实按钮一致').toContainText('显示已处理');
    // 要守的是「回执里的标签在屏幕上确实对应到控件」（我犯的错正是它谁也不对应），故断言 ≥1。
    // 刻意不写 toHaveCount(1)：概览看板上「显示已处理」同屏有两个——FeedbackReviewList.vue:154
    // 与 ReviewTaskQueue.vue:49 各一，两张卡各自的切换钮共用同一标签。写死 1 是把这个既有
    // 标签撞名固化进断言。撞名本身已登记为跟进项（回执指路在本页有歧义），不在本轮解决。
    await expect(page.getByRole('button', { name: '显示已处理' }),
      '回执所指的控件必须真的存在于同屏').not.toHaveCount(0);
  });

  test('G3 DocMetaModal 保存成功：关窗与回执不再互斥，索引重推说明可读', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    // 覆盖 mockNodeMode 的 POST 兜底（它回 changed: []，会落到「没有变更」分支）
    await page.route('**/api/kb/doc-meta**', (r) => (r.request().method() === 'POST'
      ? r.fulfill({ contentType: 'application/json', body: JSON.stringify({ doc_id: 'nd1', acl_mode: 'node', acl_revision: 6, changed: ['title'], ok: true }) })
      : r.fulfill({ contentType: 'application/json', body: JSON.stringify({
          doc_id: 'nd1', title: '注塑机保养作业指导书', category_l1: '生产', category_l2: '设备保养',
          permission_level: 'dept_internal', status: 'active', acl_mode: 'node',
          owner_dept: '', owner_dept_id: 3, owner_key: 'node:3', owner_label: '注塑事业部',
          acl_revision: 5, node_grants: [{ dept_id: 3, scope: 'subtree', name: '注塑事业部', active: true }], legacy_grants: [],
        }) })));
    await gotoManageDocs(page);

    await page.getByTestId('doc-more').first().click();
    await page.getByTestId('doc-meta').click();
    await expect(page.getByRole('heading', { name: '编辑信息' })).toBeVisible();
    await page.getByRole('button', { name: '保存' }).click();

    await expect(page.getByRole('heading', { name: '编辑信息' }), '弹窗仍应关闭').toHaveCount(0);
    // 基线 sawSavedNoteEver=0：这句唯一解释检索延迟的说明从未送达用户
    await expect(page.locator(POLITE)).toContainText('已保存');
    await expect(page.locator(POLITE)).toContainText('索引重推');
    // 验收条件要的是**完整**文案：这句括注解释了为什么按旧标题仍能搜到，
    // 与 DocMetaModal.vue 头部注释承诺的「文案里如实说明」对应，省掉即是信息损失。
    await expect(page.locator(POLITE)).toContainText('正文内旧标题字样不变');
  });

  test('G4 两个活区恒存在且极性正确（含零 toast 时）', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await page.goto(MANAGE_ROUTE);
    // 两个独立活区，而不是一个节点动态切 polite↔assertive（后者在多数 AT 上不可靠）
    await expect(page.locator(`${POLITE}[aria-live="polite"][aria-atomic="false"]`)).toHaveCount(1);
    await expect(page.locator(`${ASSERTIVE}[aria-live="assertive"][aria-atomic="false"]`)).toHaveCount(1);
    await expect(page.locator(POLITE)).toBeEmpty();
    await expect(page.locator(ASSERTIVE)).toBeEmpty();

    // 活区**不得**挂 role=status/alert：qa/FeedbackBar.vue 已各占一个，而活区全站常驻，
    // 挂角色会把问答页的裸 getByRole('alert') 打成 strict-mode violation（曾确定性复现）。
    await expect(page.locator(`${POLITE}[role], ${ASSERTIVE}[role]`),
      '活区只用 aria-live 基元，不挂角色').toHaveCount(0);
  });

  test('G5 DocMetaModal 可用 Esc 关闭，且不提交任何变更', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    let posted = 0;
    await page.route('**/api/kb/doc-meta**', (r) => {
      if (r.request().method() === 'POST') { posted++; return r.fulfill({ contentType: 'application/json', body: '{}' }); }
      return r.fulfill({ contentType: 'application/json', body: JSON.stringify({
        doc_id: 'nd1', title: '注塑机保养作业指导书', permission_level: 'dept_internal',
        status: 'active', acl_mode: 'node', owner_dept_id: 3, acl_revision: 5, node_grants: [], legacy_grants: [],
      }) });
    });
    await gotoManageDocs(page);

    await page.getByTestId('doc-more').first().click();
    await page.getByTestId('doc-meta').click();
    await expect(page.getByRole('heading', { name: '编辑信息' })).toBeVisible();

    await page.keyboard.press('Escape');
    await expect(page.getByRole('heading', { name: '编辑信息' })).toHaveCount(0);
    expect(posted, 'Esc 只关窗，绝不提交').toBe(0);
  });

  test('G6 台账可达的 4 个模态 Esc 行为一致', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    // mockNodeMode 的 catch-all 对本端点回 {items:[]}，而 VisibilityModal 直接读 readers.length
    // —— 缺该字段会在渲染期抛错、被 ErrorBoundary 兜底替换整个视图（调试实测）。这是 mock 形状
    // 问题不是产品缺陷（真实端点恒返回 readers；5xx/404 走的是 visErr 分支），故就地补形状。
    await page.route('**/api/kb/visibility-explain**', (r) => r.fulfill({
      contentType: 'application/json', body: JSON.stringify({
        doc_id: 'nd1', owner_dept: '', permission_level: 'dept_internal',
        everyone: false, nobody: false, quarantined: false, active: true,
        readers: [{ dept: '注塑事业部', via: 'owner' }],
      }),
    }));
    await gotoManageDocs(page);

    // 判据用模态容器计数（0→1→Esc→0）而非标题文案：这几个模态标题不是 heading 元素，
    // 且容器计数正是「Esc 能关掉这个模态」的语义本身，不随文案措辞漂移。
    const overlays = page.locator('.fixed.inset-0');
    await expect(overlays).toHaveCount(0);

    // 版本历史（行内图标，非菜单项）
    await page.getByRole('button', { name: '版本历史' }).first().click();
    await expect(overlays).toHaveCount(1);
    await page.keyboard.press('Escape');
    await expect(overlays, '版本历史应可 Esc 关闭').toHaveCount(0);

    // 可见范围 / 跨部门共享·权限 / 编辑信息（更多操作菜单三项）
    for (const testid of ['doc-visibility', 'doc-share', 'doc-meta'] as const) {
      await page.getByTestId('doc-more').first().click();
      await page.getByTestId(testid).click();
      await expect(overlays, `${testid} 应已打开`).toHaveCount(1);
      await page.keyboard.press('Escape');
      await expect(overlays, `${testid} 应可 Esc 关闭`).toHaveCount(0);
    }
    // 注：AccessRequestModal 的触发点是外部门只读行的「申请授权」，本 mock 的台账里没有
    // 该类行，故本门未覆盖它——如实标注，不假装已验证（其源码 Esc 处理与其余 5 个同构）。
  });

  test('G7 toast 容器不占满全屏，不触发既有 .fixed.inset-0 选择器的 strict-mode', async ({ page }) => {
    await mockNodeMode(page, KB_ADMIN);
    await seedFeedback(page);
    await page.goto(MANAGE_ROUTE);
    await page.getByRole('button', { name: '标记已处理' }).first().click();
    await expect(page.locator(POLITE)).toContainText('已标记为已处理');

    // manage-node-owner.spec.ts:130 用的是无 .first() 限定的 `.fixed.inset-0`；
    // toast 若套用这个惯用类组合，那条断言会 strict-mode violation。
    expect(await page.locator('.fixed.inset-0').count(),
      'toast 显示期间不得存在任何 .fixed.inset-0 元素').toBe(0);
    const box = await page.locator(POLITE).locator('li').first().boundingBox();
    expect(box!.width, 'toast 应是角落卡片而非全屏遮罩').toBeLessThan(400);
  });
});
