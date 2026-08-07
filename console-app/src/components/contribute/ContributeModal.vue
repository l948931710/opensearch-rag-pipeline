<script setup lang="ts">
import { computed } from 'vue'
import { X, Plus } from '@lucide/vue'
import { useContribute } from '@/composables/useContribute'

// 贡献弹窗：问题 / 你的答案·知识内容 / 归属分类。提交后需部门管理员采纳才会入库。
// ε-5 R1：入库受阻行重投时带成因警示（formWarning）——警示色条挂表单顶部，
// 与 submitErr 的失败红分开（这是提交前的注意事项，不是错误）。
// 归属选择两轴（方案 M9，Sam 裁决 F/G）：myDepts 非空 ⇒ node 轴（1 个只读展示、多个下拉），
// 否则回落组码轴下拉（与改造前一致）。**不复用 OrgTreeSelect**——它固定请求
// /api/kb/org-tree（employee 恒 403，且响应含管辖字段）。
const {
  modalOpen, formQuestion, formContent, formDept, formDeptId, formWarning, submitBusy, submitErr,
  myDepts, CONTRIB_DEPT_OPTS, closeModal, submitContribution,
} = useContribute()

const nodeAxis = computed(() => myDepts.value.length > 0)
const soleDeptName = computed(() => (myDepts.value.length === 1 ? myDepts.value[0].name : ''))
</script>

<template>
  <Teleport to="body">
    <div
      v-if="modalOpen"
      class="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-0 backdrop-blur-sm sm:items-center sm:p-4"
      @click.self="closeModal"
    >
      <div class="flex max-h-[88vh] w-full flex-col overflow-hidden rounded-t-2xl border border-border bg-card shadow-2xl sm:max-w-[520px] sm:rounded-2xl">
        <!-- 头 -->
        <div class="flex items-start gap-3 border-b border-border px-[22px] py-4">
          <span class="grid size-9 shrink-0 place-items-center rounded-xl bg-accent-soft text-accent-text">
            <Plus :size="18" :stroke-width="2" />
          </span>
          <div class="min-w-0 flex-1">
            <div class="text-base font-semibold text-foreground">贡献知识</div>
            <div class="mt-0.5 text-[12.5px] text-muted-foreground">提交后需部门管理员采纳才会入库</div>
          </div>
          <button
            type="button" aria-label="关闭"
            class="grid size-[30px] shrink-0 place-items-center rounded-lg text-faint transition hover:bg-bg hover:text-foreground"
            @click="closeModal"
          ><X :size="16" :stroke-width="2" /></button>
        </div>

        <!-- 表单 -->
        <div class="flex-1 overflow-y-auto px-[22px] py-[18px]">
          <!-- 重投警示（ε-5 R1）：按成因分流——隔离子态明说「原样重投会再次被隔离」 -->
          <p
            v-if="formWarning" data-testid="contribute-warning"
            class="mb-4 rounded-[9px] bg-st-busy/10 px-3 py-2.5 text-[12px] leading-relaxed text-st-busy"
          >{{ formWarning }}</p>
          <label class="mb-1.5 block text-[11px] font-bold uppercase tracking-[0.04em] text-faint">问题</label>
          <input
            v-model="formQuestion" type="text" placeholder="要回答的问题，例如：如何申请生产环境密钥？"
            class="mb-4 w-full rounded-[9px] border border-border bg-bg px-[11px] py-[9px] text-[13.5px] text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15"
          />
          <label class="mb-1.5 block text-[11px] font-bold uppercase tracking-[0.04em] text-faint">你的答案 / 知识内容</label>
          <textarea
            v-model="formContent" rows="5" placeholder="写下步骤或要点，越具体越容易被采纳…"
            class="mb-4 w-full resize-none rounded-[10px] border border-border bg-bg px-3 py-2.5 text-[13px] leading-relaxed text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15"
          />
          <label class="mb-1.5 block text-[11px] font-bold uppercase tracking-[0.04em] text-faint">归属部门</label>
          <!-- node 轴：只能在【自己所在部门】里选（裁决 F/G）——直挂上级节点的人看到的是
               自己上级的直接下级集，避免把贡献挂在无人管辖的中心节点上 -->
          <template v-if="nodeAxis">
            <p
              v-if="soleDeptName" data-testid="contribute-dept-sole"
              class="w-full rounded-[9px] border border-border bg-panel/60 px-[11px] py-[9px] text-[13.5px] text-foreground"
            >{{ soleDeptName }}</p>
            <select
              v-else v-model="formDeptId" data-testid="contribute-dept-node"
              class="ui-select w-full cursor-pointer rounded-[9px] border border-border bg-bg px-[11px] py-[9px] text-[13.5px] text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15"
            >
              <option v-for="d in myDepts" :key="d.dept_id" :value="d.dept_id">{{ d.name }}</option>
            </select>
          </template>
          <select
            v-else v-model="formDept" data-testid="contribute-dept-legacy"
            class="ui-select w-full cursor-pointer rounded-[9px] border border-border bg-bg px-[11px] py-[9px] text-[13.5px] text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15"
          >
            <option v-for="d in CONTRIB_DEPT_OPTS" :key="d.id" :value="d.id">{{ d.name }}</option>
          </select>
          <p v-if="submitErr" class="mt-3 text-[12.5px] text-st-fail">{{ submitErr }}</p>
        </div>

        <!-- 底 -->
        <div class="flex items-center gap-2.5 border-t border-border px-[22px] py-3.5">
          <span class="text-[12px] text-faint">被采纳后计入你的贡献</span>
          <div class="flex-1" />
          <button
            type="button"
            class="rounded-lg border border-border px-4 py-2 text-[13px] font-medium text-foreground transition hover:border-border-strong"
            @click="closeModal"
          >取消</button>
          <button
            type="button" :disabled="submitBusy"
            class="rounded-lg bg-primary px-4 py-2 text-[13px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
            @click="submitContribution"
          >{{ submitBusy ? '提交中…' : '提交贡献' }}</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
