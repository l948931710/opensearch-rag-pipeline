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

/* 本模块只收**已有消费方**的常量。骨架原语（SKEL / 行高常量）曾在此轮一并定义，
   但当时零消费方、且行高数字自称「实测」却与真实渲染对不上（StatCard 主值行实测 26px
   而非 24、表格行 35.25–37.25px 而非 40）—— 无人校验的伪实测值放进共享模块必然腐坏。
   已移除：等首个消费方（骨架分区化那一轮）出现时，按当时实测重建。 */
