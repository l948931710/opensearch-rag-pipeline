<script setup lang="ts">
import { ref } from 'vue'
import { UploadCloud, FileUp, X } from 'lucide-vue-next'
import { UPLOAD_ACCEPT, PERM_LABEL, deptLabel } from '@/lib/kb'
import { useKb } from '@/composables/useKb'
import StatusPill from './StatusPill.vue'

const {
  verCtx, newTitle, newOwner, newPerm, ownerDepts, selectedNames,
  dupWarn, uploadBusy, uploadMsg, uploadErr, uploadOk, contentDupMsg, uploadQueue,
  onFileSelected, doUpload, exitVersionMode, maxUploadMb,
} = useKb()

const fileInput = ref<HTMLInputElement | null>(null)
const dragging = ref(false)
function onChange(e: Event) { onFileSelected((e.target as HTMLInputElement).files) }
function clearFiles() { if (fileInput.value) fileInput.value.value = ''; onFileSelected(null) }
function backToNew() { exitVersionMode(); clearFiles() }
function onDrop(e: DragEvent) {
  dragging.value = false
  const files = e.dataTransfer?.files
  if (files && files.length) onFileSelected(files)   // 升版态由 onFileSelected 自动只取首个
}
</script>

<template>
  <section class="rounded-[15px] border border-border bg-card p-[18px]">
    <div class="flex items-center gap-2.5">
      <UploadCloud :size="18" :stroke-width="1.75" class="text-accent-text" />
      <h2 class="text-[14.5px] font-semibold text-foreground">{{ verCtx ? '上传新版本' : '上传文档' }}</h2>
      <span v-if="verCtx" class="rounded-md border border-st-busy/30 bg-st-busy/[0.14] px-2 py-0.5 text-[11px] font-semibold text-st-busy">升版模式</span>
      <div class="flex-1" />
      <button v-if="verCtx" type="button" class="text-[12.5px] text-muted-foreground transition hover:text-foreground" @click="backToNew">
        ← 改为新建文档
      </button>
    </div>

    <!-- 升版态：展示继承信息（可见范围继承不可改） -->
    <div v-if="verCtx" class="mt-3 rounded-lg bg-secondary/50 px-3 py-2.5 text-xs text-muted-foreground">
      升版目标：<span class="font-medium text-foreground">{{ verCtx.title || verCtx.doc_id }}</span>
      · 归属 {{ deptLabel(verCtx.owner_dept) }}
      · 可见范围 {{ verCtx.permission_level ? (PERM_LABEL[verCtx.permission_level] || verCtx.permission_level) : '继承自原文档' }}（不可改）
      <span v-if="verCtx.current_version_no"> · 当前 v{{ verCtx.current_version_no }}</span>
    </div>

    <!-- 文件选择（Atlas 横向 dropzone） -->
    <div class="mt-3.5">
      <input ref="fileInput" type="file" class="hidden" :accept="UPLOAD_ACCEPT" :multiple="!verCtx" @change="onChange" />
      <button
        type="button"
        class="dropzone flex w-full items-center gap-[15px] rounded-[13px] border-[1.5px] border-dashed border-border-strong bg-panel px-[22px] py-5 text-left hover:border-accent-strong hover:bg-accent-soft"
        :data-drag="dragging ? '1' : '0'"
        @click="fileInput?.click()"
        @dragover.prevent="dragging = true"
        @dragenter.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
      >
        <span class="grid size-[42px] shrink-0 place-items-center rounded-[11px] border border-border bg-surface text-accent-text">
          <FileUp :size="20" :stroke-width="1.75" />
        </span>
        <span class="min-w-0 flex-1">
          <span class="block text-sm font-semibold text-foreground">{{ dragging ? '松开以选择文件' : '拖拽文件到此，或点击选择' }}</span>
          <span class="mt-0.5 block text-xs text-faint">{{ verCtx ? '仅需选择 1 个文件作为新版本' : `支持批量 · PDF / DOCX / XLSX / PPTX / JPG / PNG · 单文件 ≤ ${maxUploadMb}MB` }}</span>
        </span>
        <span class="shrink-0 rounded-[9px] border border-border bg-surface px-[15px] py-2 text-[12.5px] font-semibold text-accent-text">选择文件</span>
      </button>
      <div v-if="selectedNames.length" class="mt-2 flex flex-wrap items-center gap-1.5">
        <span v-for="(n, i) in selectedNames" :key="i" class="inline-flex max-w-full items-center rounded bg-secondary px-2 py-1 text-xs text-foreground">
          <span class="truncate">{{ n }}</span>
        </span>
        <button type="button" class="grid size-6 place-items-center rounded text-muted-foreground transition hover:bg-secondary hover:text-foreground" title="清空" aria-label="清空已选文件" @click="clearFiles">
          <X :size="13" :stroke-width="2" />
        </button>
      </div>
    </div>

    <p v-if="dupWarn" class="mt-2 rounded-lg border border-st-warn/30 bg-st-warn/10 px-3 py-2 text-xs text-st-warn">{{ dupWarn }}</p>

    <!-- 新建表单（升版态隐藏归属/可见范围，强制继承） -->
    <div v-if="!verCtx" class="mt-3 grid gap-3 sm:grid-cols-3">
      <!-- 标题仅单文件可设（批量上传按文件名入库、不读此框，故多选时隐藏避免误以为生效，B9） -->
      <label v-if="selectedNames.length <= 1" class="flex flex-col gap-1 text-xs text-muted-foreground sm:col-span-3">
        标题（可空，默认文件名）
        <input v-model="newTitle" type="text" placeholder="如：货代发票审批作业指导书"
          class="rounded-md border border-input bg-card px-2.5 py-1.5 text-sm text-foreground focus:border-ring focus:outline-none focus:ring-2 focus:ring-ring/15" />
      </label>
      <p v-else class="text-xs text-faint sm:col-span-3">已选 {{ selectedNames.length }} 个文件，将批量上传，标题各取文件名（如需自定义标题请逐个上传）。</p>
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
          <StatusPill :badge="row.status" kind="queue" />
          <span class="shrink-0 text-xs text-muted-foreground">{{ row.msg }}</span>
        </div>
        <p v-if="row.dupMsg" class="mt-1 text-xs text-st-warn">{{ row.dupMsg }}</p>
      </div>
    </div>
  </section>
</template>
