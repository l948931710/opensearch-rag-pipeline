// 构建期预压缩（Perf-3）：对 next-dist/assets 的文本资产产出 .gz/.br 旁路文件，
// 由 routes/console.py 按 Accept-Encoding 直发压缩字节。
//
// 为什么不用运行时压缩中间件：console 由 uvicorn 直出公网（无反代），而 Starlette
// GZipMiddleware 会包裹/缓冲 StreamingResponse —— /api/ask/stream 的 SSE 逐帧刷出
// 会被攒成坨，打字机流式直接劣化。构建期旁路文件 + 静态路由内容协商 = SSE 零接触。
//
// woff2/图片本身已是压缩格式（再压无收益）→ 只压文本类扩展名；压缩后不小于原文件
// 则不落盘（服务端按"旁路文件存在与否"协商，缺席自动回退原文件）。
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { gzipSync, brotliCompressSync, constants } from 'node:zlib'

const assetsDir = fileURLToPath(new URL('../../opensearch_pipeline/webconsole/next-dist/assets/', import.meta.url))
const COMPRESSIBLE = new Set(['.js', '.css', '.svg', '.json', '.map', '.txt'])

let files = 0
let rawTotal = 0
let brTotal = 0
for (const f of readdirSync(assetsDir)) {
  const ext = extname(f)
  if (!COMPRESSIBLE.has(ext)) continue
  const p = join(assetsDir, f)
  const buf = readFileSync(p)
  const gz = gzipSync(buf, { level: 9 })
  const br = brotliCompressSync(buf, {
    params: {
      [constants.BROTLI_PARAM_QUALITY]: 11,
      [constants.BROTLI_PARAM_SIZE_HINT]: buf.length,
    },
  })
  if (gz.length < buf.length) writeFileSync(p + '.gz', gz)
  if (br.length < buf.length) writeFileSync(p + '.br', br)
  files++
  rawTotal += buf.length
  brTotal += Math.min(br.length, buf.length)
}
console.log(`[compress-dist] ${files} 个文本资产：${(rawTotal / 1024).toFixed(0)}KB → br ${(brTotal / 1024).toFixed(0)}KB`)
