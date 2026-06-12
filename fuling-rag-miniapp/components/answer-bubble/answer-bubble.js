// answer-bubble: renders an interleaved [text|image] block array with a
// client-side typewriter over the text blocks, and tap-to-preview on images.
//
// Text blocks are parsed ONCE into styled runs (utils/markdown.js — bold /
// step ruler / lists / headings) and the typewriter reveals plain characters
// only, so a `**` pair can never be half-shown mid-reveal (the AXML port of
// the prototype's typewriter-bold fix).
//
// Alipay-engine Component lifecycle: didMount / didUpdate / didUnmount.

import { createTypewriter } from '../../utils/typewriter';
import { parseTextBlock, plainText, sliceParas } from '../../utils/markdown';
import { resignImages } from '../../utils/api';

// 内容指纹（廉价稳定）：块数/类型/逐块文本长度 + 首文本前 16 字。
// 页面整列表 setData 会把所有消息克隆过桥 —— blocks 引用必变，身份比较不可靠，
// 必须能识别「同内容、新身份」，否则旧答案会被误判为新答案重启打字机。
function blocksFingerprint(blocks) {
  const src = Array.isArray(blocks) ? blocks : [];
  const head = src.length && src[0].type === 'text' ? String(src[0].text || '').slice(0, 16) : '';
  return src.map((b) => b.type + ':' + String(b.text || b.url || '').length).join('|') + '#' + head;
}

Component({
  props: {
    // Array of blocks from /api/ask:
    //   { type:'text', format:'plain', text:'...' }
    //   { type:'image', url:'...', caption:'...', alt:'...', ref_doc_index:1 }
    blocks: [],
    // 低匹配 guard 提示条（/api/ask resp.guard）
    guard: false,
    // When true, reveal everything immediately (no typewriter animation).
    // 初始为 true = 历史消息直出；运行中翻转 true = 「停止」跳过打字机。
    instant: false,
    // Bubbled up so the page can auto-scroll as text grows.
    onGrow: null,
    // Fired when the typewriter finishes (or finishNow) — page flips 停止→发送.
    onTypingEnd: null,
  },

  data: {
    // Render model: each block annotated with a stable key; text blocks carry
    // `revealedParas` grown by the typewriter; image blocks carry `failed`.
    viewBlocks: [],
    typing: false,
  },

  didMount() {
    this._buildAndStart(this.props.blocks);
  },

  didUpdate(prevProps) {
    // Restart the typewriter only when the blocks CONTENT changed
    // (e.g. loading placeholder -> real answer).
    if (prevProps.blocks !== this.props.blocks) {
      // 身份变 ≠ 内容变：指纹相同（典型场景：页面追加新提问时旧消息被克隆出
      // 新身份）直接跳过重建 —— 否则旧答案重启打字机「自己重打一遍」。
      if (this._fp && this._fp === blocksFingerprint(this.props.blocks)) {
        return;
      }
      this._buildAndStart(this.props.blocks);
      return;
    }
    // 「停止」：instant 在打字途中翻转 true → 整段直出剩余内容（已完成时无动作）
    if (!prevProps.instant && this.props.instant && this._tw && this.data.typing) {
      this._tw.finishNow();
    }
  },

  didUnmount() {
    if (this._tw) {
      this._tw.cancel();
      this._tw = null;
    }
  },

  methods: {
    _fireTypingEnd() {
      this.setData({ typing: false });
      if (typeof this.props.onTypingEnd === 'function') {
        this.props.onTypingEnd();
      }
    },

    _buildAndStart(blocks) {
      if (this._tw) {
        this._tw.cancel();
        this._tw = null;
      }
      this._fp = blocksFingerprint(blocks);

      const src = Array.isArray(blocks) ? blocks : [];
      // Build the view model. Text blocks are parsed once into styled
      // paragraphs/runs and start unrevealed; images render immediately.
      const parsed = [];   // per-text-segment parsed paragraphs
      const viewBlocks = src.map((b, i) => {
        if (b.type === 'image') {
          return {
            type: 'image',
            key: 'b' + i,
            url: b.url,
            ossKey: b.oss_key || '',
            caption: b.caption || '',
            alt: b.alt || '',
            failed: false,
            reloading: false,
          };
        }
        return {
          type: 'text',
          key: 'b' + i,
          paras: parseTextBlock(b.text || ''),
          revealedParas: [],
        };
      });

      // Index map: position in viewBlocks for each text segment, so the
      // typewriter's per-segment output maps back to the right block.
      // Segments are the PLAIN text (markers already stripped into runs),
      // the typewriter only ever uses .length and .slice on them.
      const textPositions = [];
      const segments = [];
      viewBlocks.forEach((vb, idx) => {
        if (vb.type === 'text') {
          textPositions.push(idx);
          parsed.push(vb.paras);
          segments.push(plainText(vb.paras));
        }
      });
      this._parsed = parsed;

      this.setData({ viewBlocks, typing: segments.length > 0 });

      if (!segments.length) {
        this._fireTypingEnd();
        return;
      }

      const applyRevealed = (revealedSegments) => {
        const patch = {};
        revealedSegments.forEach((txt, segIdx) => {
          const vbIdx = textPositions[segIdx];
          patch['viewBlocks[' + vbIdx + '].revealedParas'] =
            sliceParas(parsed[segIdx], txt.length);
        });
        this.setData(patch);
        if (typeof this.props.onGrow === 'function') {
          this.props.onGrow();
        }
      };

      // 自适应配速：固定 30ms/字（33 字/s）会让 600+ 字答案播 18s+ —— 比生成还久。
      // 目标任意长度 ≤ ~7s 播完：短答案保持逐字细腻，长答案加大每拍步进。
      const totalChars = segments.reduce((n, s) => n + s.length, 0);
      this._tw = createTypewriter({
        segments,
        intervalMs: 24,
        charsPerTick: Math.max(1, Math.ceil((totalChars * 24) / 7000)),
        onTick: applyRevealed,
        onDone: () => {
          this._fireTypingEnd();
        },
      });

      if (this.props.instant) {
        this._tw.finishNow();
      } else {
        this._tw.start();
      }
    },

    // Tap an image -> full-screen preview of ALL answer images, focused on the
    // tapped one. dd.previewImage 原生预览自带双指缩放 —— ERP 截图小字必须能放大。
    onPreview(e) {
      const tapped = e.currentTarget.dataset.url;
      const urls = this.data.viewBlocks
        .filter((b) => b.type === 'image' && !b.failed && b.url)
        .map((b) => b.url);
      if (!urls.length) {
        return;
      }
      dd.previewImage({
        urls,
        // Alipay 引擎的 current 是索引数字（微信才是 URL 字符串）
        current: Math.max(0, urls.indexOf(tapped)),
        fail(err) {
          console.error('[answer-bubble.previewImage]', err);
        },
      });
    },

    // 图片加载失败（OSS 签名 URL 过期/网络抖动）→ 原位降级为占位态，绝不静默移除
    onImgError(e) {
      const key = e.currentTarget.dataset.key;
      const idx = this.data.viewBlocks.findIndex((b) => b.key === key);
      if (idx >= 0) {
        this.setData({ ['viewBlocks[' + idx + '].failed']: true });
      }
    },

    // 点按占位重载：有 oss_key 走 /api/resign-images 换新签名 URL（1h 过期后的真恢复）；
    // 无 oss_key 的旧数据退回「failed 翻 false 重挂载原 URL」（网络抖动场景可恢复）。
    onImgRetry(e) {
      const key = e.currentTarget.dataset.key;
      const idx = this.data.viewBlocks.findIndex((b) => b.key === key);
      if (idx < 0 || this.data.viewBlocks[idx].reloading) {
        return;
      }
      const blk = this.data.viewBlocks[idx];
      if (!blk.ossKey) {
        this.setData({ ['viewBlocks[' + idx + '].failed']: false });
        return;
      }
      this.setData({ ['viewBlocks[' + idx + '].reloading']: true });
      resignImages([blk.ossKey])
        .then((resp) => {
          const fresh = resp && resp.urls && resp.urls[blk.ossKey];
          const patch = {};
          patch['viewBlocks[' + idx + '].reloading'] = false;
          if (fresh) {
            patch['viewBlocks[' + idx + '].url'] = fresh;
            patch['viewBlocks[' + idx + '].failed'] = false;
          }
          this.setData(patch);
          if (!fresh) {
            dd.showToast({ type: 'none', content: '图片暂时无法加载', duration: 2000 });
          }
        })
        .catch(() => {
          this.setData({ ['viewBlocks[' + idx + '].reloading']: false });
          dd.showToast({ type: 'none', content: '网络异常，请稍后再试', duration: 2000 });
        });
    },
  },
});
