import { describe, expect, it } from 'vitest'
import { useDialog } from '@/composables/useDialog'

// 单例对话框：confirm/promptText 返回 Promise，由 onConfirm/onCancel settle。
describe('useDialog', () => {
  it('confirm：onConfirm → true，onCancel → false；settle 后关闭', async () => {
    const { confirm, dialog, onConfirm, onCancel } = useDialog()
    const p1 = confirm({ message: '退役？', danger: true })
    expect(dialog.value.open).toBe(true)
    expect(dialog.value.kind).toBe('confirm')
    expect(dialog.value.danger).toBe(true)
    onConfirm()
    expect(await p1).toBe(true)
    expect(dialog.value.open).toBe(false)

    const p2 = confirm({ message: '再问' })
    onCancel()
    expect(await p2).toBe(false)
  })

  it('promptText：onConfirm → 输入值，onCancel → null', async () => {
    const { promptText, dialog, onConfirm, onCancel } = useDialog()
    const p1 = promptText({ message: '理由', placeholder: 'x' })
    expect(dialog.value.kind).toBe('prompt')
    dialog.value.value = '离职收回'
    onConfirm()
    expect(await p1).toBe('离职收回')

    const p2 = promptText({ message: '理由' })
    onCancel()
    expect(await p2).toBeNull()
  })

  it('promptText 空输入确认 → 空串（可空理由，与原生 prompt 行为一致）', async () => {
    const { promptText, onConfirm } = useDialog()
    const p = promptText({ message: '理由' })
    onConfirm()
    expect(await p).toBe('')
  })
})
