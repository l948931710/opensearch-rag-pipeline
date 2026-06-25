import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export type Role = 'employee' | 'dept_admin' | 'kb_admin'

export interface Identity {
  userId: string
  name: string
  role: Role
  aclGroups: string[]
  canManage: boolean
  managedOwnerDepts: string[]
}

/** 会话单一事实来源：token + 身份 + 就绪/错误态。token 只存内存（不落 localStorage，避免持久泄露）。 */
export const useSession = defineStore('session', () => {
  const token = ref<string>('')
  const identity = ref<Identity | null>(null)
  const ready = ref(false)
  const error = ref('')

  const role = computed<Role>(() => identity.value?.role ?? 'employee')
  const canManage = computed(() => !!identity.value?.canManage)

  function setToken(t: string) { token.value = t || '' }
  function setIdentity(i: Identity | null) { identity.value = i }
  function reset() { token.value = ''; identity.value = null; ready.value = false; error.value = '' }

  return { token, identity, ready, error, role, canManage, setToken, setIdentity, reset }
})

/** 后端 /api/auth/dingtalk 或 /api/kb/whoami 的下划线响应 → 前端 Identity（camelCase）。 */
export function toIdentity(d: Record<string, any>): Identity {
  return {
    userId: d.user_id ?? '',
    name: d.display_name ?? d.name ?? '',
    role: (d.role ?? 'employee') as Role,
    aclGroups: Array.isArray(d.acl_groups) ? d.acl_groups : [],
    canManage: !!d.can_manage_kb,
    managedOwnerDepts: Array.isArray(d.managed_owner_depts) ? d.managed_owner_depts : [],
  }
}
