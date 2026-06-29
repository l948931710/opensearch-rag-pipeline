<script setup lang="ts">
import { ref, computed } from 'vue'
import { UserCog, ShieldCheck, Plus, X } from 'lucide-vue-next'
import { deptLabel } from '@/lib/kb'
import { useKb, type AdminItem } from '@/composables/useKb'
import LoadError from './LoadError.vue'
import { useDialog } from '@/composables/useDialog'

// Phase F 成员/角色管理（kb_admin 专属）：维护部门管理员 + 其可管理 owner_dept（写授权）。
// 三分授权：读组 ≠ 可管理(dept_admin_grant) ≠ 可授权(本面=kb_admin)。kb_admin 行只读受保护。
const { adminGrants, grantableDepts, isBusy, grantDeptAdmin, revokeAdminGrant, loadAdminGrants, loadErrors } = useKb()
const { confirm } = useDialog()

const formUser = ref('')
const formName = ref('')
const formDepts = ref<string[]>([])
const formNote = ref('')
const formOpen = ref(false)
const triedSubmit = ref(false)   // 提交过一次后才显内联校验红框（aria-invalid，G9）
const userInvalid = computed(() => triedSubmit.value && !formUser.value.trim())
const deptsInvalid = computed(() => triedSubmit.value && !formDepts.value.length)

const deptAdmins = computed(() => adminGrants.value.filter((a) => a.role === 'dept_admin'))
const kbAdmins = computed(() => adminGrants.value.filter((a) => a.role === 'kb_admin'))

function toggleDept(d: string) {
  formDepts.value = formDepts.value.includes(d) ? formDepts.value.filter((x) => x !== d) : [...formDepts.value, d]
}
function startEdit(a: AdminItem) {
  formUser.value = a.user_id; formName.value = a.user_name; formDepts.value = [...a.managed_owner_depts]; formNote.value = ''
  formOpen.value = true
}
async function submit() {
  triedSubmit.value = true
  if (!formUser.value.trim() || !formDepts.value.length) return   // 内联红框 + aria-invalid 已提示，不再弹原生 alert
  const ok = await grantDeptAdmin(formUser.value.trim(), formName.value.trim(), formDepts.value, formNote.value.trim())
  if (ok) { formUser.value = ''; formName.value = ''; formDepts.value = []; formNote.value = ''; formOpen.value = false; triedSubmit.value = false }
}
async function onRevokeAll(a: AdminItem) {
  const okGo = await confirm({
    title: '撤销全部管理权限', confirmText: '撤销全部', danger: true,
    message: `撤销「${a.user_name || a.user_id}」的全部部门管理权限？\n该用户将降为普通员工（即时失去管理入口）。`,
  })
  if (okGo) void revokeAdminGrant(a.user_id, '')
}
async function onRevokeDept(a: AdminItem, d: string) {
  const okGo = await confirm({
    title: '撤销部门管理权限', confirmText: '撤销', danger: true,
    message: `撤销「${a.user_name || a.user_id}」对【${deptLabel(d)}】的管理权限？`,
  })
  if (okGo) void revokeAdminGrant(a.user_id, d)
}
</script>

<template>
  <section>
    <p class="mb-2.5 ml-0.5 text-[11px] font-bold uppercase tracking-[0.08em] text-faint">成员 / 角色管理</p>
    <LoadError class="mb-2.5" :message="loadErrors['adminGrants']" @retry="loadAdminGrants()" />
    <div class="overflow-hidden rounded-[15px] border border-border bg-card">
      <div class="flex items-center gap-2.5 border-b border-border bg-accent-soft px-[18px] py-3">
        <UserCog :size="16" :stroke-width="1.75" class="text-accent-text" />
        <span class="text-sm font-semibold text-foreground">部门管理员</span>
        <span class="rounded-full bg-accent-strong px-2 py-px text-[11px] font-bold text-primary-foreground">{{ deptAdmins.length }}</span>
        <div class="flex-1" />
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-lg bg-primary px-3 py-[6px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90"
          @click="formOpen = !formOpen"
        ><Plus :size="14" :stroke-width="2" /> 授予</button>
      </div>

      <!-- 授予/编辑表单 -->
      <div v-if="formOpen" class="space-y-2.5 border-b border-border bg-panel px-[18px] py-3.5">
        <div class="flex flex-wrap gap-2.5">
          <div class="flex flex-col gap-1">
            <input
              v-model="formUser" placeholder="钉钉 staffId" :aria-invalid="userInvalid"
              class="w-44 rounded-lg border bg-card px-3 py-1.5 text-sm text-foreground placeholder:text-faint"
              :class="userInvalid ? 'border-st-fail' : 'border-border'"
            />
            <span v-if="userInvalid" class="text-[11px] text-st-fail">请填钉钉 staffId</span>
          </div>
          <input v-model="formName" placeholder="显示名（可空）" class="w-40 rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-foreground placeholder:text-faint" />
        </div>
        <div>
          <div class="mb-1 text-[11.5px]" :class="deptsInvalid ? 'text-st-fail' : 'text-faint'">
            可管理部门（多选，提交即覆盖该管理员全部授权）<span v-if="deptsInvalid"> · 至少选一个</span>
          </div>
          <div class="flex flex-wrap gap-1.5" :aria-invalid="deptsInvalid">
            <button
              v-for="d in grantableDepts" :key="d" type="button"
              class="rounded-full border px-2.5 py-1 text-[12px] transition"
              :class="formDepts.includes(d) ? 'border-accent-strong bg-accent-soft font-medium text-accent-text' : 'border-border text-muted-foreground hover:border-ring'"
              @click="toggleDept(d)"
            >{{ deptLabel(d) }}</button>
          </div>
        </div>
        <input v-model="formNote" placeholder="备注（可空，如授权依据）" class="w-full rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-foreground placeholder:text-faint" />
        <div class="flex gap-2">
          <button type="button" :disabled="isBusy(`member:${formUser.trim()}`)" class="rounded-lg bg-primary px-4 py-[7px] text-[12.5px] font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50" @click="submit">提交授予</button>
          <button type="button" class="rounded-lg border border-border px-4 py-[7px] text-[12.5px] text-foreground transition hover:border-border-strong" @click="formOpen = false">取消</button>
        </div>
      </div>

      <!-- 部门管理员行 -->
      <div
        v-for="a in deptAdmins" :key="a.user_id"
        class="flex flex-wrap items-center gap-x-3.5 gap-y-2 border-t border-border px-[18px] py-3"
      >
        <span class="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent-text"><UserCog :size="16" :stroke-width="1.75" /></span>
        <div class="min-w-0 flex-1">
          <div class="truncate text-[13.5px] font-semibold text-foreground">{{ a.user_name || a.user_id }} <span class="ml-1 text-[11px] font-normal text-faint">{{ a.user_id }}</span></div>
          <div class="mt-1 flex flex-wrap gap-1.5">
            <span v-for="d in a.managed_owner_depts" :key="d" class="inline-flex items-center gap-1 rounded-md bg-panel px-2 py-0.5 text-[11.5px] text-muted-foreground">
              {{ deptLabel(d) }}
              <button type="button" class="text-faint transition hover:text-st-busy disabled:opacity-50" :disabled="isBusy(`member:${a.user_id}`)" @click="onRevokeDept(a, d)"><X :size="11" :stroke-width="2.5" /></button>
            </span>
            <span v-if="!a.managed_owner_depts.length" class="text-[11.5px] text-faint">（无可管理部门）</span>
          </div>
        </div>
        <button type="button" class="self-start rounded-lg border border-border px-3 py-[6px] text-[12px] text-foreground transition hover:border-border-strong disabled:opacity-50" :disabled="isBusy(`member:${a.user_id}`)" @click="startEdit(a)">编辑</button>
        <button type="button" class="self-start rounded-lg border border-border px-3 py-[6px] text-[12px] text-foreground transition hover:border-border-strong disabled:opacity-50" :disabled="isBusy(`member:${a.user_id}`)" @click="onRevokeAll(a)">撤销全部</button>
      </div>
      <div v-if="!deptAdmins.length" class="border-t border-border px-[18px] py-6 text-center text-sm text-muted-foreground">暂无部门管理员，点「授予」添加</div>

      <!-- 知识库管理员（只读，受保护） -->
      <div
        v-for="a in kbAdmins" :key="a.user_id"
        class="flex items-center gap-2.5 border-t border-border bg-st-live/5 px-[18px] py-2.5"
      >
        <ShieldCheck :size="15" :stroke-width="1.75" class="shrink-0 text-st-live" />
        <span class="text-[13px] text-foreground">{{ a.user_name || a.user_id }} <span class="ml-1 text-[11px] text-faint">{{ a.user_id }}</span></span>
        <span class="rounded-md bg-st-live/10 px-1.5 py-0.5 text-[10.5px] font-medium text-st-live">知识库管理员</span>
        <div class="flex-1" />
        <span class="hidden text-[11px] text-faint sm:inline">全部门 · 受保护（调整走运维脚本）</span>
      </div>
    </div>
  </section>
</template>
