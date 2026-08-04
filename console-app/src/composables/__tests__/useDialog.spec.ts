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

// ── 附录B：_resolve 是模块级单槽，被顶掉的 Promise 此前永久悬挂（2026-08-03）──────
describe('useDialog 并发弹窗：绝不留悬挂 Promise', () => {
  it('第二个 confirm 顶掉第一个 → 前者按「取消」了结，不再永久 pending', async () => {
    const { confirm, onConfirm } = useDialog()
    let firstSettled: boolean | null = null
    const p1 = confirm({ message: '第一个' }).then((v) => { firstSettled = v; return v })
    const p2 = confirm({ message: '第二个' })       // 覆盖 _resolve
    await Promise.resolve(); await Promise.resolve()
    expect(firstSettled).toBe(false)                 // ← 修复前这里恒为 null（永不 resolve）
    expect(await p1).toBe(false)
    onConfirm()
    expect(await p2).toBe(true)                      // 后者仍正常工作
  })

  it('被顶掉的确认框绝不返回 true —— 危险操作不得凭空「被确认」', async () => {
    const { confirm, notice, onConfirm } = useDialog()
    const danger = confirm({ message: '确定退役该文档？', danger: true })
    notice({ message: '后台任务失败' })              // 无关弹窗顶掉它
    onConfirm()                                       // 用户点的是 notice 的「知道了」
    expect(await danger).toBe(false)
  })

  it('promptText 被顶掉 → null（取消语义），不是空串', async () => {
    const { promptText, confirm, onConfirm } = useDialog()
    const p = promptText({ message: '填写理由' })
    confirm({ message: '别的' })
    onConfirm()
    expect(await p).toBeNull()
  })

  it('notice 被顶掉 → 照常 resolve（void），调用方 await 不挂死', async () => {
    const { notice, confirm, onCancel } = useDialog()
    let done = false
    const p = notice({ message: '提示' }).then(() => { done = true })
    confirm({ message: '别的' })
    await Promise.resolve(); await Promise.resolve()
    expect(done).toBe(true)
    onCancel()
    await p
  })

  it('顶掉旧框不会把新框关掉（state.open 归新框所有）', async () => {
    const { confirm, dialog, onConfirm } = useDialog()
    void confirm({ message: '旧' })
    const p2 = confirm({ message: '新' })
    expect(dialog.value.open).toBe(true)
    expect(dialog.value.message).toBe('新')
    onConfirm(); await p2
  })
})
