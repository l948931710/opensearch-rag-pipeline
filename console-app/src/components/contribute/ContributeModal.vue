<script setup lang="ts">
import { X, Plus } from 'lucide-vue-next'
import { useContribute } from '@/composables/useContribute'

// 贡献弹窗：问题 / 你的答案·知识内容 / 归属分类。提交后需部门管理员采纳才会入库。
const {
  modalOpen, formQuestion, formContent, formDept, submitBusy, submitErr,
  CONTRIB_DEPT_OPTS, closeModal, submitContribution,
} = useContribute()
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
          <label class="mb-1.5 block text-[11px] font-bold uppercase tracking-[0.04em] text-faint">归属分类</label>
          <select
            v-model="formDept"
            class="w-full cursor-pointer rounded-[9px] border border-border bg-bg px-[11px] py-[9px] text-[13.5px] text-foreground outline-none focus:border-ring focus:ring-2 focus:ring-ring/15"
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
