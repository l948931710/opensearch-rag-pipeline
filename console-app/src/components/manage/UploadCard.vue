<script setup lang="ts">
import { ref } from 'vue'
import { UploadCloud, FileUp, X } from 'lucide-vue-next'
import { UPLOAD_ACCEPT, PERM_LABEL, deptLabel } from '@/lib/kb'
import { useKb } from '@/composables/useKb'
import StatusPill from './StatusPill.vue'

const {
  verCtx, newTitle, newOwner, newPerm, ownerDepts, selectedNames,
  dupWarn, uploadBusy, uploadMsg, uploadErr, uploadOk, contentDupMsg, uploadQueue,
  onFileSelected, doUpload, exitVersionMode,
} = useKb()

const fileInput = ref<HTMLInputElement | null>(null)
function onChange(e: Event) { onFileSelected((e.target as HTMLInputElement).files) }
function clearFiles() { if (fileInput.value) fileInput.value.value = ''; onFileSelected(null) }
function backToNew() { exitVersionMode(); clearFiles() }
</script>

<template>
  <section class="rounded-xl border border-border bg-card p-5">
    <div class="flex items-center justify-between">
      <h2 class="flex items-center gap-2 text-sm font-bold text-foreground">
        <UploadCloud :size="17" :stroke-width="1.75" class="text-primary" />
        {{ verCtx ? '上传新版本' : '上传文档' }}
      </h2>
      <button v-if="verCtx" type="button" class="text-xs text-muted-foreground transition hover:text-foreground" @click="backToNew">
        ← 改为新建文档
      </button>
    </div>

    <!-- 升版态：展示继承信息（可见范围继承不可改） -->
    <div v-if="verCtx" class="mt-3 rounded-lg bg-secondary/50 px-3 py-2.5 text-xs text-muted-foreground">
      升版目标：<span class="font-medium text-foreground">{{ verCtx.title || verCtx.doc_id }}</span>
      · 归属 {{ deptLabel(verCtx.owner_dept) }}
      · 可见范围 {{ PERM_LABEL[verCtx.permission_level] || verCtx.permission_level }}（继承不可改）
      · 当前 v{{ verCtx.current_version_no }}
    </div>

    <!-- 文件选择 -->
    <div class="mt-3">
      <input ref="fileInput" type="file" class="hidden" :accept="UPLOAD_ACCEPT" :multiple="!verCtx" @change="onChange" />
      <button
        type="button"
        class="flex w-full items-center justify-center gap-2 rounded-lg border border-dashed border-input bg-secondary/30 px-4 py-5 text-sm text-muted-foreground transition hover:border-ring hover:bg-secondary/60"
        @click="fileInput?.click()"
      >
        <FileUp :size="18" :stroke-width="1.75" />
        {{ selectedNames.length ? '重新选择' : (verCtx ? '选择 1 个文件' : '选择文件（可多选）') }}
      </button>
      <div v-if="selectedNames.length" class="mt-2 flex flex-wrap items-center gap-1.5">
        <span v-for="(n, i) in selectedNames" :key="i" class="inline-flex max-w-full items-center rounded bg-secondary px-2 py-1 text-xs text-foreground">
          <span class="truncate">{{ n }}</span>
        </span>
        <button type="button" class="grid size-6 place-items-center rounded text-muted-foreground transition hover:bg-secondary hover:text-foreground" title="清空" @click="clearFiles">
          <X :size="13" :stroke-width="2" />
        </button>
      </div>
      <p class="mt-1.5 text-xs text-muted-foreground">支持 PDF / DOCX / XLSX / PPTX / JPG / PNG，单文件 ≤ 50MB。</p>
    </div>

    <p v-if="dupWarn" class="mt-2 rounded-lg border border-st-warn/30 bg-st-warn/10 px-3 py-2 text-xs text-st-warn">{{ dupWarn }}</p>

    <!-- 新建表单（升版态隐藏归属/可见范围，强制继承） -->
    <div v-if="!verCtx" class="mt-3 grid gap-3 sm:grid-cols-3">
      <label class="flex flex-col gap-1 text-xs text-muted-foreground sm:col-span-3">
        标题（可空，默认文件名）
        <input v-model="newTitle" type="text" placeholder="如：货代发票审批作业指导书"
          class="rounded-md border border-input bg-card px-2.5 py-1.5 text-sm text-foreground focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15" />
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted-foreground">
        归属部门
        <select v-model="newOwner" class="rounded-md border border-input bg-card px-2.5 py-1.5 text-sm text-foreground focus:border-ring focus:outline-none">
          <option value="" disabled>选择部门</option>
          <option v-for="o in ownerDepts" :key="o" :value="o">{{ deptLabel(o) }}</option>
        </select>
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted-foreground sm:col-span-2">
        可见范围
        <select v-model="newPerm" class="rounded-md border border-input bg-card px-2.5 py-1.5 text-sm text-foreground focus:border-ring focus:outline-none">
          <option value="dept_internal">仅本部门</option>
          <option value="public">全公司（可能需审批）</option>
          <option value="restricted">受限（仅归档，不进检索）</option>
        </select>
      </label>
    </div>

    <!-- 提交 + 状态 -->
    <div class="mt-4 flex flex-wrap items-center gap-3">
      <button
        type="button"
        class="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
        :disabled="uploadBusy || !selectedNames.length || (!verCtx && !newOwner)"
        @click="doUpload()"
      >
        {{ uploadBusy ? '上传中…' : (verCtx ? '上传新版本' : '上传') }}
      </button>
      <span v-if="uploadMsg" class="text-sm" :class="uploadOk ? 'text-st-live' : 'text-muted-foreground'">{{ uploadMsg }}</span>
    </div>
    <p v-if="uploadErr" class="mt-2 text-sm text-destructive">{{ uploadErr }}</p>
    <p v-if="contentDupMsg" class="mt-2 rounded-lg border border-st-warn/30 bg-st-warn/10 px-3 py-2 text-xs text-st-warn">{{ contentDupMsg }}</p>

    <!-- 批量队列 -->
    <div v-if="uploadQueue.length" class="mt-3 space-y-1.5">
      <div v-for="(row, i) in uploadQueue" :key="i" class="rounded-lg border border-border bg-secondary/30 px-3 py-2">
        <div class="flex items-center justify-between gap-2 text-sm">
          <span class="min-w-0 flex-1 truncate text-foreground">{{ row.name }}</span>
          <StatusPill :badge="row.status" />
          <span class="shrink-0 text-xs text-muted-foreground">{{ row.msg }}</span>
        </div>
        <p v-if="row.dupMsg" class="mt-1 text-xs text-st-warn">{{ row.dupMsg }}</p>
      </div>
    </div>
  </section>
</template>
