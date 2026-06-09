// Tiny client-side typewriter.
//
// dd.httpRequest is buffered (no streaming), so the full answer text arrives at
// once. To keep the chat feeling alive we reveal it progressively on the client.
//
// Usage:
//   const tw = createTypewriter({
//     segments: ['第一段文本', '第二段文本'],   // array of text blocks
//     onTick: (revealedSegments) => this.setData({ revealed: revealedSegments }),
//     onDone: () => {},
//     intervalMs: 30,     // ~30ms / character
//     charsPerTick: 1,
//   });
//   tw.start();
//   ...
//   tw.cancel();          // safe to call any time (e.g. didUnmount)
//
// onTick receives an array parallel to `segments`, each entry being the portion
// revealed so far. This lets a component re-render interleaved text/image blocks
// while only the text grows.

export function createTypewriter(opts) {
  const segments = (opts && opts.segments) || [];
  const onTick = (opts && opts.onTick) || function () {};
  const onDone = (opts && opts.onDone) || function () {};
  const intervalMs = (opts && opts.intervalMs) || 30;
  const charsPerTick = (opts && opts.charsPerTick) || 1;

  let timer = null;
  let segIndex = 0;
  let charIndex = 0;
  let cancelled = false;

  function buildRevealed() {
    const out = [];
    for (let i = 0; i < segments.length; i++) {
      if (i < segIndex) {
        out.push(segments[i]);
      } else if (i === segIndex) {
        out.push(segments[i].slice(0, charIndex));
      } else {
        out.push('');
      }
    }
    return out;
  }

  function step() {
    if (cancelled) {
      return;
    }
    // Advance past empty / finished segments.
    while (segIndex < segments.length && charIndex >= segments[segIndex].length) {
      segIndex += 1;
      charIndex = 0;
    }
    if (segIndex >= segments.length) {
      onTick(segments.slice());
      onDone();
      return;
    }
    charIndex = Math.min(charIndex + charsPerTick, segments[segIndex].length);
    onTick(buildRevealed());
    timer = setTimeout(step, intervalMs);
  }

  return {
    start() {
      cancelled = false;
      segIndex = 0;
      charIndex = 0;
      if (!segments.length) {
        onDone();
        return;
      }
      step();
    },
    /** Reveal everything immediately and stop. */
    finishNow() {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      onTick(segments.slice());
      onDone();
    },
    cancel() {
      cancelled = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    },
  };
}
