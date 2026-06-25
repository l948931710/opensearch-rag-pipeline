// 上传文档：web-view 承载 H5 上传页（小程序容器选不了 office 文档，必须 web-view 浏览器上下文）。
// 免登已由小程序做好 → 把 bearer token 透传给 H5（/console 的 token 模式），H5 无需再 requestAuthCode。
// ⚠️ web-view 的 src 域名须在钉钉后台登记为「业务域名」(HTTPS)。裸 IP HTTP 仅 IDE 关闭校验时可测；
//    线上等 rag.fulingplastics.com.cn 备案+证书+业务域名登记后生效。

import { ensureLogin } from '../../utils/auth';
import { BASE_URL } from '../../utils/config';

Page({
  data: { src: '', err: '' },

  onLoad(q) {
    // 带 doc_id（从文档详情「上传新版本」进入）→ 透传给 /console，H5 列表加载后自动进升版态。
    const docId = (q && q.doc_id) || '';
    const docTitle = (q && q.title) || '';
    const owner = (q && q.owner) || '';
    ensureLogin()
      .then((g) => {
        if (!g.token) {
          this.setData({ err: '未登录' });
          return;
        }
        let url = BASE_URL + '/console?token=' + encodeURIComponent(g.token) +
          '&name=' + encodeURIComponent(g.displayName || '');
        if (docId) {
          // owner 透传 → 即使目标文档不在 my-docs 首屏，/console 也能据 doc_id+owner 进升版态
          // （可见范围由后端 upload-url 强制继承，前端无需 permission_level）。
          url += '&doc_id=' + encodeURIComponent(docId) +
            '&title=' + encodeURIComponent(docTitle) +
            '&owner=' + encodeURIComponent(owner);
        }
        this.setData({ src: url });
      })
      .catch(() => {
        this.setData({ err: '登录失败，请在钉钉中重试' });
      });
  },

  // web-view 加载失败（最常见：上传页域名未登记为「业务域名」，或网络/证书问题）。
  // 给出可操作兜底，而非静默空白——引导改用电脑端浏览器完成上传。
  onWvError(e) {
    console.log('[kb-upload] web-view error', e && e.detail);
    this.setData({ src: '', err: '上传页加载失败（手机端上传需后台登记业务域名）。请在电脑端浏览器打开知识库管理完成上传。' });
  },
});
