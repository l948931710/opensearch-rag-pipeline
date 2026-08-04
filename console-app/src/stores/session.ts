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
  /** 上传「归属部门」下拉选项(= managed + 生产子线细化);旧后端缺字段时回退 managed。
   *  可选:测试夹具/旧调用点可缺省,消费侧(useKb.uploadTargetDepts)已兜底回退。 */
  uploadTargetDepts?: string[]
}

/** 会话单一事实来源：token + 身份 + 就绪/错误态。token 不落 localStorage（避免持久泄露）；
 *  桌面 URL-token 登录会镜像到 **sessionStorage**（tab 级、关 tab 即清、401 即清——
 *  #F-console-urltoken 的有意决策，威胁模型见 useAuth.ts；B5/P2-06 修正本行过时表述）。 */
export const useSession = defineStore('session', () => {
  const token = ref<string>('')
  const identity = ref<Identity | null>(null)
  const ready = ref(false)
  const error = ref('')
  /** 前端专用 principal 代际：仅 userId **真正变化**（含 reset）时递增；同用户 401 续签不动。
   *  消费方（org 快照缓存、上传链异步边界）据此丢弃跨身份的过期结果——服务端 register 有
   *  upload_token uid 门兜底，这里只负责不让旧身份的异步回写污染新身份 UI。 */
  const principalEpoch = ref(0)

  const role = computed<Role>(() => identity.value?.role ?? 'employee')
  const canManage = computed(() => !!identity.value?.canManage)

  function setToken(t: string) { token.value = t || '' }
  function setIdentity(i: Identity | null) {
    if ((i?.userId ?? '') !== (identity.value?.userId ?? '')) principalEpoch.value++
    identity.value = i
  }
  function reset() {
    token.value = ''; identity.value = null; ready.value = false; error.value = ''
    principalEpoch.value++
  }

  return { token, identity, ready, error, principalEpoch, role, canManage, setToken, setIdentity, reset }
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
    uploadTargetDepts: Array.isArray(d.upload_target_depts) && d.upload_target_depts.length
      ? d.upload_target_depts
      : (Array.isArray(d.managed_owner_depts) ? d.managed_owner_depts : []),
  }
}
