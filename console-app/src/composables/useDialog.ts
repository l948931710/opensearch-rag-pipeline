import { ref } from 'vue'

// 应用内自定义 确认/输入 对话框（替代原生 confirm()/prompt()——后者不可访问、样式不一致、被浏览器拦截）。
// 单例：状态挂在模块作用域，<ConfirmDialog> 全局挂一份，各处 await confirm()/promptText() 即用。
export interface DialogState {
  open: boolean
  kind: 'confirm' | 'prompt'
  title: string
  message: string
  confirmText: string
  cancelText: string
  placeholder: string
  value: string          // prompt 输入值（v-model）
  maxlength: number
  danger: boolean        // 危险操作 → 确认按钮红色
}

const state = ref<DialogState>({
  open: false, kind: 'confirm', title: '确认', message: '',
  confirmText: '确认', cancelText: '取消', placeholder: '', value: '', maxlength: 500, danger: false,
})
let _resolve: ((v: boolean | string | null) => void) | null = null

function _settle(v: boolean | string | null) {
  state.value.open = false
  const r = _resolve
  _resolve = null
  if (r) r(v)
}

export interface ConfirmOpts { title?: string; message: string; confirmText?: string; cancelText?: string; danger?: boolean }
export interface PromptOpts extends ConfirmOpts { placeholder?: string; maxlength?: number }

export function useDialog() {
  /** 确认框：resolve(true=确认 / false=取消)。 */
  function confirm(opts: ConfirmOpts): Promise<boolean> {
    return new Promise((res) => {
      _resolve = res as (v: boolean | string | null) => void
      state.value = {
        open: true, kind: 'confirm', title: opts.title || '确认', message: opts.message,
        confirmText: opts.confirmText || '确认', cancelText: opts.cancelText || '取消',
        placeholder: '', value: '', maxlength: 500, danger: !!opts.danger,
      }
    })
  }

  /** 输入框：resolve(字符串=确认 / null=取消)。空输入按确认仍返回 ''（与原生 prompt 一致：可空理由）。 */
  function promptText(opts: PromptOpts): Promise<string | null> {
    return new Promise((res) => {
      _resolve = res as (v: boolean | string | null) => void
      state.value = {
        open: true, kind: 'prompt', title: opts.title || '', message: opts.message,
        confirmText: opts.confirmText || '确认', cancelText: opts.cancelText || '取消',
        placeholder: opts.placeholder || '', value: '', maxlength: opts.maxlength || 500, danger: !!opts.danger,
      }
    })
  }

  function onConfirm() { _settle(state.value.kind === 'prompt' ? state.value.value : true) }
  function onCancel() { _settle(state.value.kind === 'prompt' ? null : false) }

  return { dialog: state, confirm, promptText, onConfirm, onCancel }
}
