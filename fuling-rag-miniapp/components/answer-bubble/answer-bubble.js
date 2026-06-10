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

Component({
  props: {
    // Array of blocks from /api/ask:
    //   { type:'text', format:'plain', text:'...' }
    //   { type:'image', url:'...', caption:'...', alt:'...', ref_doc_index:1 }
    blocks: [],
    // When true, reveal everything immediately (no typewriter animation).
    // Useful for re-rendered history messages.
    instant: false,
    // Bubbled up so the page can auto-scroll as text grows.
    onGrow: null,
  },

  data: {
    // Render model: each block annotated with a stable key, and text blocks
    // carry a `revealed` field that the typewriter grows.
    viewBlocks: [],
    typing: false,
  },

  didMount() {
    this._buildAndStart(this.props.blocks);
  },

  didUpdate(prevProps) {
    // Restart the typewriter only when the blocks array identity changed
    // (e.g. loading placeholder -> real answer).
    if (prevProps.blocks !== this.props.blocks) {
      this._buildAndStart(this.props.blocks);
    }
  },

  didUnmount() {
    if (this._tw) {
      this._tw.cancel();
      this._tw = null;
    }
  },

  methods: {
    _buildAndStart(blocks) {
      if (this._tw) {
        this._tw.cancel();
        this._tw = null;
      }

      const src = Array.isArray(blocks) ? blocks : [];
      // Build the view model. Text blocks are parsed once into styled
      // paragraphs/runs and start unrevealed; images render immediately.
      // `hidden` lets us drop broken images.
      const parsed = [];   // per-text-segment parsed paragraphs
      const viewBlocks = src.map((b, i) => {
        if (b.type === 'image') {
          return {
            type: 'image',
            key: 'b' + i,
            url: b.url,
            caption: b.caption || '',
            alt: b.alt || '',
            hidden: false,
          };
        }
        return {
          type: 'text',
          key: 'b' + i,
          paras: parseTextBlock(b.text || ''),
          revealedParas: [],
          hidden: false,
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
        this.setData({ typing: false });
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

      this._tw = createTypewriter({
        segments,
        intervalMs: 30,
        charsPerTick: 1,
        onTick: applyRevealed,
        onDone: () => {
          this.setData({ typing: false });
          if (typeof this.props.onGrow === 'function') {
            this.props.onGrow();
          }
        },
      });

      if (this.props.instant) {
        this._tw.finishNow();
      } else {
        this._tw.start();
      }
    },

    // Tap an image -> full-screen preview of ALL answer images, focused on the tapped one.
    onPreview(e) {
      const current = e.target.dataset.url;
      const urls = this.data.viewBlocks
        .filter((b) => b.type === 'image' && !b.hidden && b.url)
        .map((b) => b.url);
      if (!urls.length) {
        return;
      }
      dd.previewImage({
        urls,
        current,
        fail(err) {
          console.error('[answer-bubble.previewImage]', err);
        },
      });
    },

    // Hide an image that failed to load so it does not leave a broken box.
    onImgError(e) {
      const key = e.target.dataset.key;
      const idx = this.data.viewBlocks.findIndex((b) => b.key === key);
      if (idx >= 0) {
        this.setData({ ['viewBlocks[' + idx + '].hidden']: true });
      }
    },
  },
});
