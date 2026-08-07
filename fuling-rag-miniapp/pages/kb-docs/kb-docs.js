// 知识库管理工作台 — 角色分界的简化版（console ManageView 同源理念）：
//   dept_admin：本部门文档列表 + 本部门统计（上传/升版；新文档等 kb_admin 放行）
//   kb_admin  ：全库文档列表 + 全库统计 + 「待我审批」队列（放行/驳回）
// 列表 GET /api/kb/my-docs（后端按角色現查作用域）；统计 GET /api/kb/stats（同作用域
// 口径，fail-open）；审批 GET /api/kb/pending-approvals + POST approve/reject（仅 kb_admin）。

import { ensureLogin } from '../../utils/auth';
import {
  getMyDocs, getKbStats, getPendingApprovals, approveDoc, rejectDoc,
} from '../../utils/api';

// 状态徽章 → 颜色类（与后端 _kb_status_badge 文案对齐）
const BADGE_CLASS = {
  已上线: 'ok',
  处理中: 'busy',
  排队中: 'queue',
  待审核: 'warn',
  已隔离: 'fail',
  处理失败: 'fail',
  已退役: 'muted',
  内容未变: 'muted',
};

const PERM_LABEL = { public: '公开', dept_internal: '部门内', restricted: '受限' };

function decorate(raw) {
  return {
    docId: raw.doc_id,
    title: raw.title || raw.original_filename || raw.doc_id,
    ownerDept: raw.owner_dept || '',
    permLabel: PERM_LABEL[raw.permission_level] || raw.permission_level || '',
    versionNo: raw.current_version_no || 1,
    badge: raw.status_badge || '',
    badgeClass: BADGE_CLASS[raw.status_badge] || 'muted',
    updatedAt: (raw.updated_at || '').slice(0, 16),
  };
}

Page({
  data: {
    items: [],
    loading: true,
    loadingMore: false,
    hasMore: false,
    query: '',
    // '' 正常 / 'login' 未登录 / 'forbidden' 非管理员 / 'error' 加载失败
    errorKind: '',
    // ── 角色工作台 ──
    role: '',        // employee / dept_admin / kb_admin（globalData，登录后可用）
    roleLabel: '',   // 知识库管理员 / 部门管理员（顶栏身份徽章文案）
    roleClass: '',   // kb（实心深绿）/ dept（描边绿）—— 两种管理员的视觉区分
    scopeDesc: '',   // 管理全部部门文档 / 仅管理本部门文档（徽章旁作用域说明）
    stats: null,     // {total, online, pending, newMonth}；取数失败保持 null 整条隐藏
    approvals: [],   // kb_admin 待审批行；非 kb_admin 恒空
  },

  onLoad() {
    this._loadedOnce = false;
    this._approving = false;   // 审批动作互斥（防双击重复提交）
    this._reload();
  },

  onShow() {
    // 从详情/上传返回时刷新（升版、审批会改状态）；首次 onLoad 已加载，避免重复。
    if (this._loadedOnce) {
      this._reload();
    }
  },

  // 登录后从 globalData 取角色（登录时 /api/auth/dingtalk 已下发；真正鉴权在服务端每个接口）
  _applyRole() {
    const role = getApp().globalData.role || '';
    if (role !== this.data.role) {
      const ROLE_LABEL = { kb_admin: '知识库管理员', dept_admin: '部门管理员' };
      const SCOPE_DESC = { kb_admin: '管理全部部门文档', dept_admin: '仅管理本部门文档' };
      this.setData({
        role,
        roleLabel: ROLE_LABEL[role] || '',
        roleClass: role === 'kb_admin' ? 'kb' : 'dept',
        scopeDesc: SCOPE_DESC[role] || '',
      });
    }
    return role;
  },

  // 统计条：辅助数据 fail-open —— 失败仅隐藏，不打扰文档列表
  _loadStats() {
    getKbStats()
      .then((s) => {
        if (!s) {
          return;
        }
        const badge = s.by_badge || {};
        this.setData({
          stats: {
            total: s.total || 0,
            online: badge['已上线'] || 0,
            pending: badge['待审核'] || 0,
            newMonth: s.new_this_month || 0,
          },
        });
      })
      .catch(() => {});
  },

  // 待审批队列：仅 kb_admin 调用；403/失败按空处理（区块隐藏）
  _loadApprovals() {
    getPendingApprovals()
      .then((r) => {
        this.setData({
          approvals: ((r && r.items) || []).map((a) => ({
            docId: a.doc_id,
            versionNo: a.version_no || 1,
            title: a.title || a.original_filename || a.doc_id,
            ownerDept: a.owner_dept || '',
            ownerName: a.owner_name || '',
            dateLabel: (a.created_at || '').slice(5, 16),
          })),
        });
      })
      .catch(() => {});
  },

  _reload(q) {
    if (typeof q !== 'string') {
      q = (this.data.query || '').trim();
    }
    this._activeQ = q;
    const seq = (this._seq = (this._seq || 0) + 1);   // 竞态守卫：仅最新一次搜索的结果生效
    this.setData({ loading: true, errorKind: '', items: [] });
    ensureLogin()
      .then(() => {
        const role = this._applyRole();
        this._loadStats();
        if (role === 'kb_admin') {
          this._loadApprovals();
        }
        return getMyDocs(0, q);
      })
      .then((resp) => {
        if (seq !== this._seq) {
          return;   // 已有更新的搜索发起 → 丢弃过期结果，避免旧结果覆盖新结果
        }
        this._loadedOnce = true;
        this.setData({
          loading: false,
          items: ((resp && resp.items) || []).map(decorate),
          hasMore: !!(resp && resp.has_more),
        });
      })
      .catch((err) => {
        if (seq !== this._seq) {
          return;
        }
        this._loadedOnce = true;
        const status = err && err.status;
        const kind = status === 401 ? 'login' : status === 403 ? 'forbidden' : 'error';
        this.setData({ loading: false, errorKind: kind });
      });
  },

  onRetry() {
    this._reload();
  },

  // 文档名搜索：输入防抖 300ms 再查（避免逐字一请求）；竞态由 _reload 的 seq 守卫兜底。
  onSearchInput(e) {
    const v = (e && e.detail && e.detail.value) || '';
    this.setData({ query: v });
    if (this._searchTimer) {
      clearTimeout(this._searchTimer);
    }
    this._searchTimer = setTimeout(() => {
      this._reload(v.trim());
    }, 300);
  },

  onSearchClear() {
    if (this._searchTimer) {
      clearTimeout(this._searchTimer);
    }
    this.setData({ query: '' });
    this._reload('');
  },

  onLoadMore() {
    if (this.data.loadingMore || !this.data.hasMore) {
      return;
    }
    const seq = this._seq;   // 绑定当前搜索：加载更多途中若发起新搜索 → seq 变 → 丢弃旧分页结果
    this.setData({ loadingMore: true });
    getMyDocs(this.data.items.length, this._activeQ)
      .then((resp) => {
        if (seq !== this._seq) {
          return;
        }
        this.setData({
          loadingMore: false,
          items: this.data.items.concat(((resp && resp.items) || []).map(decorate)),
          hasMore: !!(resp && resp.has_more),
        });
      })
      .catch(() => {
        if (seq !== this._seq) {
          return;
        }
        this.setData({ loadingMore: false });
        dd.showToast({ type: 'none', content: '加载失败，请稍后重试', duration: 2000 });
      });
  },

  // ── kb_admin 审批（简化版：列表内直接放行/驳回，dd.confirm 二次确认）──
  _decide(e, approve) {
    if (this._approving) {
      return;
    }
    const ds = e.currentTarget.dataset;
    const docId = ds.docId;
    const versionNo = Number(ds.versionNo) || 1;
    const title = ds.title || docId;
    if (!docId) {
      return;
    }
    dd.confirm({
      title: approve ? '通过审批' : '驳回上线',
      content: approve
        ? '「' + title + '」将放行进入处理管线，完成后自动上线。'
        : '「' + title + '」将被驳回，不会上线；上传人可修改后重新提交。',
      confirmButtonText: approve ? '通过' : '驳回',
      cancelButtonText: '取消',
      success: (res) => {
        if (!res.confirm) {
          return;
        }
        this._approving = true;
        const call = approve ? approveDoc : rejectDoc;
        call({ doc_id: docId, version_no: versionNo })
          .then((r) => {
            this._approving = false;
            // ⚠️ 2026-08-06 补评审：**2xx ≠ 审批成功**。后端对「状态不匹配」返回
            // 0 行（approve 的 approved:0 / reject 的 rejected:0，后者现已改 409）——
            // 本页此前一律 toast「已放行/已驳回」，谎报了一个没发生的动作。
            // 这与 Web 控制台 2026-08-06 修掉的「现网 20 个僵尸条目」是同一个 bug，
            // 只是那边修了、小程序两条路径都还开着。必须看计数。
            const n = r && (approve ? r.approved : r.rejected);
            if (!n) {
              dd.showToast({
                type: 'none',
                content: (r && r.note)
                  || (approve ? '未放行：该文档当前状态不可放行（可能已退役）'
                              : '未驳回：该版本当前状态不可驳回（可能已退役撤销）'),
                duration: 2500,
              });
              this._reload();       // 拉服务端真值，别把已消失的单留在队列里
              return;
            }
            dd.showToast({
              type: 'success',
              content: approve ? '已放行，进入处理管线' : '已驳回',
              duration: 2000,
            });
            // 审批改变文档状态：整页刷新（列表 + 统计 + 队列同步）
            this._reload();
          })
          .catch((err) => {
            this._approving = false;
            console.error('[kb-docs._decide]', err);
            const msg = (err && err.data && err.data.detail) || '操作失败，请稍后重试';
            dd.showToast({ type: 'none', content: String(msg).slice(0, 60), duration: 2500 });
            // 409（驳回竞态）也走这里：刷新让队列收敛到服务端真值。
            // ⚠️ 后端 409 会先于小程序发版上线，空窗期本页表现为「错误 toast + 刷新」——
            // 比此前谎报「已驳回」好；发版后 detail 文案也会更准确。
            this._reload();
          });
      },
    });
  },

  onApprove(e) {
    this._decide(e, true);
  },

  onReject(e) {
    this._decide(e, false);
  },

  onOpenDoc(e) {
    const ds = e.currentTarget.dataset;
    if (ds.docId) {
      dd.navigateTo({
        url: '/pages/kb-doc-detail/kb-doc-detail?doc_id=' + encodeURIComponent(ds.docId) +
          '&title=' + encodeURIComponent(ds.title || ''),
      });
    }
  },

  onUpload() {
    // 进 web-view 上传页（小程序容器选不了 office 文档 → web-view 浏览器上下文 input[type=file]）。
    // ⚠️ web-view 域名须登记为「业务域名」(HTTPS)；裸 IP HTTP 仅 IDE 关闭校验可测，
    //    线上等 rag.fulingplastics.com.cn 备案+证书+业务域名登记后生效。
    dd.navigateTo({ url: '/pages/kb-upload/kb-upload' });
  },
});
