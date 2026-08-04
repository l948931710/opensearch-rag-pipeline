<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { X, Loader2 } from '@lucide/vue'
import { apiJson } from '@/lib/api'
import { deptLabel } from '@/lib/kb'
import { useKb } from '@/composables/useKb'
import OrgTreeSelect from './OrgTreeSelect.vue'
import type { PickedNode } from './OrgTreePicker.vue'

/**
 * 「编辑信息」弹窗（阶段 B doc-meta 端点的 UI 面）：标题 / 分类 / 归属节点 / 可见节点集，
 * 一次保存同事务落库（CAS=acl_revision）。也是 **legacy→node 迁移的唯一入口**
 * （node-grants 端点对 legacy 文档 409 并把用户导到这里）。
 *
 * D5（Sam 2026-08-01 拍板）：改标题/分类 = HA3 字段级刷新（下轮 stage-3 重推），
 * chunk 正文里嵌的旧标题不变——文案里如实说明。
 */
const { docMetaCtx, closeDocMeta, loadDocs, isKbAdmin, isDeptAdmin, nodeAclGrant } = useKb()

const meta = ref<any | null>(null)
const loading = ref(false)
const loadErr = ref('')
const saving = ref(false)
const saveErr = ref('')
const savedNote = ref('')

const title = ref('')
const cat1 = ref('')
const cat2 = ref('')
const ownerNode = ref<PickedNode[]>([])       // 单选（数组 ≤1）
const visibleNodes = ref<PickedNode[]>([])
const migrate = ref(false)                    // legacy 文档：勾选后启用迁移到组织树

watch(docMetaCtx, async (d) => {
  meta.value = null; loadErr.value = ''; saveErr.value = ''; savedNote.value = ''
  title.value = ''; cat1.value = ''; cat2.value = ''
  ownerNode.value = []; visibleNodes.value = []; migrate.value = false
  if (!d) return
  loading.value = true
  try {
    const m = await apiJson<any>(`/api/kb/doc-meta?doc_id=${encodeURIComponent(d.doc_id)}`, { auth: true })
    meta.value = m
    title.value = m.title || ''
    cat1.value = m.category_l1 || ''
    cat2.value = m.category_l2 || ''
    if (m.acl_mode === 'node') {
      if (m.owner_dept_id) ownerNode.value = [{ dept_id: m.owner_dept_id, subtree: true }]
      visibleNodes.value = ((m.node_grants || []) as any[]).map(
        (g) => ({ dept_id: g.dept_id, subtree: (g.scope || 'subtree') === 'subtree' }))
    }
  } catch (e: any) {
    loadErr.value = e?.detail || '加载文档信息失败'
  } finally { loading.value = false }
})

const isNode = computed(() => meta.value?.acl_mode === 'node')
const showNodeEditors = computed(() => isNode.value || (migrate.value && nodeAclGrant.value))

async function save() {
  const d = docMetaCtx.value
  if (!d || !meta.value) return
  saveErr.value = ''; savedNote.value = ''
  const body: any = { doc_id: d.doc_id, expected_acl_revision: meta.value.acl_revision }
  if (title.value.trim() && title.value.trim() !== (meta.value.title || '')) body.title = title.value.trim()
  if (cat1.value.trim() !== (meta.value.category_l1 || '')) body.category_l1 = cat1.value.trim()
  if (cat2.value.trim() !== (meta.value.category_l2 || '')) body.category_l2 = cat2.value.trim()
  if (showNodeEditors.value) {
    if (!ownerNode.value.length) { saveErr.value = '请选择归属节点'; return }
    body.owner_dept_id = ownerNode.value[0].dept_id
    body.visible_nodes = [...visibleNodes.value]
  }
  saving.value = true
  try {
    const r = await apiJson<any>('/api/kb/doc-meta', { method: 'POST', auth: true, body: JSON.stringify(body) })
    if (!r.changed?.length) { savedNote.value = '没有变更'; return }
    savedNote.value = `已保存（${r.changed.join('、')}）`
    if (r.changed.includes('title') || r.changed.includes('category')) {
      savedNote.value += '；标题/分类将在下轮索引重推后于检索侧生效（正文内旧标题字样不变）'
    }
    closeDocMeta()
    void loadDocs()
  } catch (e: any) {
    saveErr.value = e?.detail || (e?.status === 409 ? '文档信息已被他人修改，请关闭后重试' : '保存失败')
  } finally { saving.value = false }
}
</script>

<template>
  <div v-if="docMetaCtx" class="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4"
       @click.self="closeDocMeta()">
    <div class="max-h-[86vh] w-full max-w-lg overflow-y-auto rounded-2xl border border-border bg-card p-5 shadow-xl">
      <div class="mb-3 flex items-start justify-between gap-3">
        <div>
          <h3 class="text-[15px] font-semibold text-foreground">编辑信息</h3>
          <p class="mt-0.5 truncate text-xs text-muted-foreground">{{ docMetaCtx.title || docMetaCtx.doc_id }}</p>
        </div>
        <button type="button" class="rounded-md p-1 text-muted-foreground hover:bg-panel" aria-label="关闭"
                @click="closeDocMeta()"><X :size="16" /></button>
      </div>

      <div v-if="loading" class="flex items-center gap-2 py-6 text-sm text-muted-foreground">
        <Loader2 :size="14" class="animate-spin" /> 加载文档信息…
      </div>
      <p v-else-if="loadErr" class="py-4 text-sm text-danger">{{ loadErr }}</p>

      <template v-else-if="meta">
        <label class="mb-3 flex flex-col gap-1 text-xs text-muted-foreground">
          标题
          <input v-model="title" type="text"
                 class="rounded-md border border-input bg-card px-2.5 py-1.5 text-sm text-foreground focus:border-ring focus:outline-none" />
        </label>
        <div class="mb-3 grid grid-cols-2 gap-2">
          <label class="flex flex-col gap-1 text-xs text-muted-foreground">
            一级分类
            <input v-model="cat1" type="text"
                   class="rounded-md border border-input bg-card px-2.5 py-1.5 text-sm text-foreground focus:border-ring focus:outline-none" />
          </label>
          <label class="flex flex-col gap-1 text-xs text-muted-foreground">
            二级分类
            <input v-model="cat2" type="text"
                   class="rounded-md border border-input bg-card px-2.5 py-1.5 text-sm text-foreground focus:border-ring focus:outline-none" />
          </label>
        </div>
        <p class="mb-3 text-[11px] text-faint">改标题/分类在下轮索引重推后于检索侧生效；正文内嵌的旧标题字样保持不变。</p>

        <!-- legacy 文档：迁移开关 -->
        <div v-if="!isNode" class="mb-3 rounded-[11px] border border-border bg-surface p-3">
          <p class="mb-1 text-xs text-muted-foreground">
            当前归属：<b>{{ deptLabel(meta.owner_dept || '') || meta.owner_dept || '—' }}</b>（组码模式）
          </p>
          <label v-if="nodeAclGrant" class="flex items-center gap-2 text-xs text-foreground">
            <input v-model="migrate" type="checkbox" />
            迁移到组织树授权（选归属节点 + 可见范围；原组码共享将被撤销并留审计）
          </label>
          <p v-else class="text-[11px] text-faint">组织树授权通道未开启，暂不可迁移。</p>
        </div>

        <template v-if="showNodeEditors">
          <!-- 折叠式选择器（2026-08-03 改版）：弹窗不再被两棵整树撑到 86vh。
               归属对 dept_admin 限管辖子树——与后端 D6「改归属须同管源与目标」对齐。 -->
          <div class="mb-3">
            <p class="mb-1.5 text-xs" :class="ownerNode.length ? 'text-muted-foreground' : 'text-st-busy'">
              归属节点（单选）
            </p>
            <OrgTreeSelect v-model="ownerNode" mode="owner" data-testid="meta-owner-select"
                           :restrict-to-managed="isDeptAdmin" :disabled="saving" />
          </div>
          <div class="mb-3">
            <p class="mb-1.5 text-xs text-muted-foreground">可见范围（多选，整体替换）</p>
            <OrgTreeSelect v-model="visibleNodes" mode="visibility" data-testid="meta-visible-select"
                           :disabled="saving" />
            <p v-if="ownerNode.length && !visibleNodes.some(p => p.dept_id === ownerNode[0].dept_id)"
               class="mt-1.5 text-[11px] text-st-busy">
              ⚠️ 归属部门不在可见范围内 —— 属主部门自己将看不到这篇文档
            </p>
          </div>
        </template>

        <p v-if="isNode && isKbAdmin" class="mb-3 text-[11px] text-faint">
          如需迁回组码模式（回滚），请联系运维走 doc-meta 的 target_acl_mode 契约（kb_admin 专属操作）。
        </p>

        <p v-if="saveErr" class="mb-2 text-xs text-danger">{{ saveErr }}</p>
        <p v-if="savedNote" class="mb-2 text-xs text-st-live">{{ savedNote }}</p>
        <div class="flex justify-end gap-2">
          <button type="button"
                  class="rounded-lg border border-border px-4 py-2 text-[13px] text-foreground transition hover:bg-panel"
                  @click="closeDocMeta()">取消</button>
          <button type="button"
                  class="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-[13px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50"
                  :disabled="saving || loading"
                  @click="save">
            <Loader2 v-if="saving" :size="13" class="animate-spin" />{{ saving ? '保存中…' : '保存' }}
          </button>
        </div>
      </template>
    </div>
  </div>
</template>
