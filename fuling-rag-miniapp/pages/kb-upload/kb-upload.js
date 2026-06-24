// 上传文档：web-view 承载 H5 上传页（小程序容器选不了 office 文档，必须 web-view 浏览器上下文）。
// 免登已由小程序做好 → 把 bearer token 透传给 H5（/console 的 token 模式），H5 无需再 requestAuthCode。
// ⚠️ web-view 的 src 域名须在钉钉后台登记为「业务域名」(HTTPS)。裸 IP HTTP 仅 IDE 关闭校验时可测；
//    线上等 rag.fulingplastics.com.cn 备案+证书+业务域名登记后生效。

import { ensureLogin } from '../../utils/auth';
import { BASE_URL } from '../../utils/config';

Page({
  data: { src: '', err: '' },

  onLoad() {
    ensureLogin()
      .then((g) => {
        if (!g.token) {
          this.setData({ err: '未登录' });
          return;
        }
        const url = BASE_URL + '/console?token=' + encodeURIComponent(g.token) +
          '&name=' + encodeURIComponent(g.displayName || '');
        this.setData({ src: url });
      })
      .catch(() => {
        this.setData({ err: '登录失败，请在钉钉中重试' });
      });
  },
});
