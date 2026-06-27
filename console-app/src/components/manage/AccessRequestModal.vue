<script setup lang="ts">
import { ref, computed } from 'vue'
import { Lock } from 'lucide-vue-next'
import { deptLabel, permLabel } from '@/lib/kb'
import { useKb } from '@/composables/useKb'

// 申请文档授权（申请人侧，Phase C）：在「全部门」浏览里对其他部门文档发起检索授权申请。
const { accessReqDoc, accessReqBusy, closeAccessRequest, submitAccessRequest } = useKb()
const reason = ref('')
const disabled = computed(() => accessReqBusy.value)

function submit() { void submitAccessRequest(reason.value); reason.value = '' }
function close() { reason.value = ''; closeAccessRequest() }
</script>

<template>
  <div
    v-if="accessReqDoc"
    class="fixed inset-0 z-[80] flex items-center justify-center bg-black/40 p-6"
    @click="close"
  >
    <div class="w-[460px] max-w-full overflow-hidden rounded-2xl border border-border bg-card shadow-xl" @click.stop>
      <div class="p-[22px] pb-0">
        <div class="mb-3 flex items-center gap-2.5">
          <span class="grid size-9 place-items-center rounded-[10px] bg-accent-soft text-accent-text"><Lock :size="18" :stroke-width="1.75" /></span>
          <span class="text-base font-semibold text-foreground">申请文档授权</span>
        </div>
        <p class="mb-3 text-[13px] leading-relaxed text-muted-foreground">
          为本部门申请访问《<span class="font-semibold text-foreground">{{ accessReqDoc.title || accessReqDoc.original_filename || accessReqDoc.doc_id }}</span>》（归属
          {{ deptLabel(accessReqDoc.owner_dept) }} · {{ permLabel(accessReqDoc.permission_level) }}）。提交后由文档所属部门管理员审批；通过后本部门将获得该文档检索授权（检索开放随后续维护生效）。
        </p>
        <label class="mb-1.5 block text-[11px] font-bold uppercase tracking-[0.04em] text-faint">申请理由</label>
        <textarea
          v-model="reason" rows="3" placeholder="说明为何需要访问此文档（会随申请一同送审）"
          class="w-full resize-none rounded-[10px] border border-input bg-surface px-3 py-2.5 text-[13px] leading-relaxed text-foreground focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15"
        />
      </div>
      <div class="flex justify-end gap-2.5 px-[22px] py-4">
        <button type="button" class="rounded-lg border border-border px-4 py-2 text-[13.5px] font-medium text-foreground transition hover:border-border-strong" @click="close">取消</button>
        <button
          type="button"
          class="rounded-lg bg-primary px-4 py-2 text-[13.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
          :disabled="disabled" @click="submit"
        >提交申请</button>
      </div>
    </div>
  </div>
</template>
