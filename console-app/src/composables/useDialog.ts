import { ref } from 'vue'

// 应用内自定义 确认/输入 对话框（替代原生 confirm()/prompt()——后者不可访问、样式不一致、被浏览器拦截）。
// 单例：状态挂在模块作用域，<ConfirmDialog> 全局挂一份，各处 await confirm()/promptText() 即用。
export interface DialogState {
  open: boolean
  kind: 'confirm' | 'prompt' | 'notice'
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

/** 某种 kind 的「取消」返回值：prompt→null，confirm→false，notice 单按钮→关闭即已知悉→true。 */
function _cancelValue(kind: DialogState['kind']): boolean | string | null {
  return kind === 'prompt' ? null : kind !== 'notice' ? false : true
}

function _settle(v: boolean | string | null) {
  state.value.open = false
  const r = _resolve
  _resolve = null
  if (r) r(v)
}

/**
 * 新对话框顶掉旧的时，**先把旧 Promise 按「取消」了结**——绝不留悬挂。
 *
 * `_resolve` 是模块级**单槽**：第二次 confirm/promptText/notice 会直接覆盖它，
 * 于是第一个 `await` 永远不 resolve —— 调用方的后续代码和 `finally` 都不执行，
 * busy/loading 标志清不掉，那块 UI 就**永久卡死**（且没有任何报错线索）。
 * 触发面很浅：确认框弹出期间任一后台失败调 `notice()`、或按钮未禁用时的连点。
 *
 * 用「取消」而非「确认」是安全默认：被顶掉的确认框绝不能返回 true，
 * 否则一次无关的弹窗就能让退役/删除这类危险操作凭空「被确认」。
 * 这里**不动** `state.open`——新框紧接着就要接管它。
 */
function _supersede() {
  const r = _resolve
  _resolve = null
  if (r) r(_cancelValue(state.value.kind))
}

export interface ConfirmOpts { title?: string; message: string; confirmText?: string; cancelText?: string; danger?: boolean }
export interface PromptOpts extends ConfirmOpts { placeholder?: string; maxlength?: number }

export function useDialog() {
  /** 确认框：resolve(true=确认 / false=取消)。 */
  function confirm(opts: ConfirmOpts): Promise<boolean> {
    return new Promise((res) => {
      _supersede()
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
      _supersede()
      _resolve = res as (v: boolean | string | null) => void
      state.value = {
        open: true, kind: 'prompt', title: opts.title || '', message: opts.message,
        confirmText: opts.confirmText || '确认', cancelText: opts.cancelText || '取消',
        placeholder: opts.placeholder || '', value: '', maxlength: opts.maxlength || 500, danger: !!opts.danger,
      }
    })
  }

  /** 告知框（单按钮，替代原生 alert()）：仅确认键，关闭即 resolve。失败/结果类提示用它，别再用 alert。 */
  function notice(opts: ConfirmOpts): Promise<void> {
    return new Promise((res) => {
      _supersede()
      _resolve = () => res()
      state.value = {
        open: true, kind: 'notice', title: opts.title || '提示', message: opts.message,
        confirmText: opts.confirmText || '知道了', cancelText: '', placeholder: '', value: '', maxlength: 500, danger: !!opts.danger,
      }
    })
  }

  function onConfirm() { _settle(state.value.kind === 'prompt' ? state.value.value : true) }
  // notice 只有一个按钮，Esc/遮罩关闭同样视为「已知悉」。
  function onCancel() { _settle(_cancelValue(state.value.kind)) }

  return { dialog: state, confirm, promptText, notice, onConfirm, onCancel }
}
