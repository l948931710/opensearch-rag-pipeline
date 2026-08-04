/**
 * 看板分区的共享视觉常量（2026-08-04 抽取）。
 *
 * 此前这套常量在 KbAdminDashboard / OpsMetricsPanel / DeptDashboard 三个文件里各写了一遍，
 * 字符串逐字相同 —— 三处改一处就会漏两处，`SUBHEAD` 已经因此漂移过（见下）。
 *
 * 这里只放**跨组件复用**的分区骨架类名；单个看板独有的布局（如 DeptDashboard 的三卡网格）
 * 仍留在各自组件里，不要为了「都放进来」把这里变成第二个样式垃圾场。
 */

/** 主分区外框：一个有边框的「区域面板」（暖底 bg-panel + 描边），白色指标卡浮于其上。 */
export const SECTION = 'rounded-2xl border border-border bg-panel/60 p-4 sm:p-5'

/** 区域标题行：绿色竖条 + 区名 + 下方细分隔线（真标题，非装饰性 uppercase 眉标）。 */
export const ZONE_HEAD =
  'mb-4 flex items-center gap-2 border-b border-border/70 pb-3 text-[13px] font-semibold tracking-tight text-foreground'

/** 区域标题前的绿色竖条。 */
export const ZONE_TICK = 'h-3.5 w-1 shrink-0 rounded-full bg-accent-strong'

/**
 * 分区内的次级小标题。
 *
 * ⚠️ 不带 `ml-0.5`。抽取前三处的实际渲染是：17 个实例在基准位、3 个带 2px 左边距
 * （KbAdminDashboard「状态分布」经**调用点** override 带上，DeptDashboard 的两处经
 * **常量本身**带上）——同一个 2px 由两条互不知情的代码路径造成。若只按「常量逐字比对」
 * 去掉 DeptDashboard 常量里的 `ml-0.5`，KbAdmin 那处调用点 override 不受影响，
 * 两个看板本来恰好对齐的「状态分布」反而会错开 2px。故抽取时取多数（17:3）的无偏移版，
 * 并同步移除 KbAdminDashboard 的调用点 override，让 20 个实例落在同一基准。
 */
export const SUBHEAD = 'mb-2 text-[12.5px] font-medium text-muted-foreground'

/** 指标卡网格（2 列 / sm 起 4 列）。 */
export const GRID = 'grid grid-cols-2 gap-3 sm:grid-cols-4'

/** 使用成效的三卡网格（DeptDashboard 专用，合并为 3 卡后独立于 GRID）。 */
export const USAGE_GRID = 'grid grid-cols-1 gap-3 sm:grid-cols-3'

/**
 * 成对子项的共享面板：一个框、两半、中间竖线分隔（趋势|原因、最常用|未答好）。
 * 嵌在 SECTION 里 → 用纯白 bg-surface 与暖底面板拉开层次。
 */
export const SPLIT =
  'grid overflow-hidden rounded-2xl border border-border bg-surface divide-y divide-border sm:grid-cols-2 sm:divide-y-0 sm:divide-x'

/**
 * 骨架条的类名 —— **与 StatCard.vue:29 逐字一致**。
 *
 * 消费方：`SkeletonBlock.vue`、`StatusDistBar.vue`。**`StatCard.vue:29` 有意保留字面量不改**：
 * 它被 `__tests__/statcard.spec.ts:11,27,33` 与 e2e 的 `motion-tokens-zero-visual-change.spec.ts`
 * G5 两道既有硬门盯着，为了「单一来源」去动它是拿受保护断言换整洁，不值得。
 * 所以这里是「新骨架的单一来源」，不是「全应用的单一来源」——别把注释写过头。
 *
 * 刻意复用 Tailwind 类名串而不是自定义 CSS 类：`animate-pulse` 实际是
 * `2s cubic-bezier(.4,0,.6,1)` 的 opacity 1↔.5（不是直觉的 ease-in-out），
 * 手写等价 CSS 极易写偏；且 `bg-border/70` 在 Tailwind v4 下是 color-mix 不是简单 alpha。
 * 复用类名串则等价性由 Tailwind 保证，`motion-tokens-zero-visual-change.spec.ts` 的 G5
 * 与 `statcard.spec.ts:11,27,33` 两道既有守卫也继续有效。
 */
export const SKEL = 'animate-pulse rounded-md bg-border/70'

/**
 * 骨架占位高度（px）。**这组数字是独立审计在 1440px 视口对已填充数据的真实组件实测所得**，
 * 不是从源码里的 `h-[Npx]` 字面量抄的——上一次按字面量取值被评审逐个证伪（写 24 实测 26、
 * 写 40 实测 35.25–37.25）。改这里之前请重新实测，别照着 class 猜。
 *
 * 用途是让骨架**预留出与真实内容等高的版面**：审计实测 governance 到达时页面
 * `main.scrollHeight` 在单帧内 900 → 2616（+1716px），已在阅读的用户脚下会整块位移。
 */
export const SKEL_H = {
  /** VitalsList 单行 */
  vitals: 55.5,
  /** OrgCoverageTable tbody tr */
  orgRow: 35.75,
  /** BarList 单项（div.py-1.5） */
  barItem: 59.25,
  /** StatCard 整卡（最丰富变体：pill + subValue + hint） */
  statCard: 171.25,
} as const
