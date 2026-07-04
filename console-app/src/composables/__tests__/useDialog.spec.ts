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

  it('notice：单按钮告知框（cancelText 空），onConfirm resolve 后关闭', async () => {
    const { notice, dialog, onConfirm } = useDialog()
    const p = notice({ title: '退役失败', message: '无权退役', danger: true })
    expect(dialog.value.open).toBe(true)
    expect(dialog.value.kind).toBe('notice')
    expect(dialog.value.title).toBe('退役失败')
    expect(dialog.value.danger).toBe(true)
    expect(dialog.value.cancelText).toBe('')   // 单按钮：ConfirmDialog 据 kind 隐藏取消键
    onConfirm()
    await expect(p).resolves.toBeUndefined()
    expect(dialog.value.open).toBe(false)
  })

  it('notice：onCancel（Esc/点遮罩）同样 resolve —— 视为「已知悉」，不落 false 也不悬挂', async () => {
    const { notice, dialog, onCancel } = useDialog()
    const p = notice({ message: '仅告知' })
    onCancel()
    await expect(p).resolves.toBeUndefined()   // 关键回归：confirm 的 onCancel→false，notice 的必须仍 resolve
    expect(dialog.value.open).toBe(false)
  })

  it('notice 默认值：title「提示」/ confirmText「知道了」/ 非危险', async () => {
    const { notice, dialog, onConfirm } = useDialog()
    const p = notice({ message: 'x' })
    expect(dialog.value.title).toBe('提示')
    expect(dialog.value.confirmText).toBe('知道了')
    expect(dialog.value.danger).toBe(false)
    onConfirm()
    await p
  })
})
