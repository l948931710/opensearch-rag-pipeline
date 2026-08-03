import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import { setActivePinia } from 'pinia'
import type { Identity } from '@/stores/session'
import VersionHistoryModal from '@/components/manage/VersionHistoryModal.vue'
import { useKb, __resetKb, type DocItem, type VersionItem } from '@/composables/useKb'

// 历史版本下载（2026-08-02）：每行「原件」按钮；无原件/已封存（PII 隔离）置灰。
beforeEach(() => { vi.restoreAllMocks(); __resetKb() })

function identity(over: Partial<Identity> = {}): Identity {
  return { userId: 'u1', name: '张三', role: 'kb_admin', aclGroups: ['marketing'], canManage: true, managedOwnerDepts: ['marketing'], ...over }
}
function activate(id: Identity = identity()) {
  const p = createTestingPinia({ createSpy: vi.fn, initialState: { session: { identity: id, token: 't', ready: true } } })
  setActivePinia(p)
  return p
}
function doc(over: Partial<DocItem> = {}): DocItem {
  return { doc_id: 'DOC1', title: '设备SOP', original_filename: 'sop.pdf', owner_dept: 'marketing', permission_level: 'dept_internal', current_version_no: 3, status: 'active', status_badge: '已上线', updated_at: '2026-08-01', can_manage: true, ...over } as DocItem
}
function ver(over: Partial<VersionItem>): VersionItem {
  return { version_no: 1, content_process_status: 'SUCCESS', chunk_status: '', index_status: 'SUCCESS', publish_status: '', status_badge: '已上线', error_message: '', created_at: '2026-06-10 08:00:00', has_raw: true, quarantined: false, ...over }
}

function mountWithVersions(versions: VersionItem[]) {
  const p = activate()
  const kb = useKb()
  ;(kb as any).verHistory.value = { doc: doc(), versions, loading: false, error: '' }
  return mount(VersionHistoryModal, { global: { plugins: [p] } })
}

describe('VersionHistoryModal — 历史版本下载', () => {
  it('每版一个下载按钮：正常可点；已封存/无原件置灰且 title 说明原因', () => {
    const w = mountWithVersions([
      ver({ version_no: 3 }),
      ver({ version_no: 2, quarantined: true, status_badge: '已隔离' }),
      ver({ version_no: 1, has_raw: false }),
    ])
    const btns = w.findAll('[data-testid="ver-download"]')
    expect(btns.length).toBe(3)
    expect((btns[0].element as HTMLButtonElement).disabled).toBe(false)
    expect((btns[1].element as HTMLButtonElement).disabled).toBe(true)
    expect(btns[1].attributes('title')).toContain('已安全封存')
    expect((btns[2].element as HTMLButtonElement).disabled).toBe(true)
    expect(btns[2].attributes('title')).toContain('无原件')
  })

  it('点击带准确版本号请求 doc-preview 并新标签打开签名 URL', async () => {
    const fetchSpy = vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ url: 'https://oss.example/raw/x?sig=1', available: true, filename: 'sop_v2.pdf' }),
      text: async () => '{}',
    }))
    vi.stubGlobal('fetch', fetchSpy)
    const fakeTab = { close: vi.fn(), location: { href: '' }, opener: {} }
    vi.spyOn(window, 'open').mockReturnValue(fakeTab as any)

    const w = mountWithVersions([ver({ version_no: 3 }), ver({ version_no: 2 })])
    await w.findAll('[data-testid="ver-download"]')[1].trigger('click')
    await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalled())

    const url = String((fetchSpy.mock.calls[0] as any[])[0])
    expect(url).toContain('/api/kb/doc-preview?doc_id=DOC1')
    expect(url).toContain('version=2')                       // 不是 current(3)，是被点的那版
    await vi.waitFor(() => expect(fakeTab.location.href).toBe('https://oss.example/raw/x?sig=1'))
    expect(fakeTab.opener).toBeNull()                        // 防 tabnabbing：占位标签断 opener
  })
})
