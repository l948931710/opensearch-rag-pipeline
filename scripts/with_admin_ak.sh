#!/usr/bin/env bash
# with_admin_ak.sh — 临时把阿里云 admin AK 从 macOS Keychain 取出，
# 注入到子命令 env 里跑一次，命令结束立刻 unset。
#
# 用法:
#   ./scripts/with_admin_ak.sh aliyun ram CreateUser --UserName foo
#   ./scripts/with_admin_ak.sh ossutil cp local.txt oss://bucket/
#   ./scripts/with_admin_ak.sh -- env | grep ALIBABA   # 自检
#
# 取出 AK 时 macOS 会弹 Touch ID/密码框 (因为 Keychain 条目用 `-T ""` 写入)。
# 拒绝授权 → 取不出 AK，命令以非零退出。
#
# 不会做的事:
#   - 不写文件 / 不写 history (注入仅在子进程 env 里存在)
#   - 不打印 AK 值 (仅打印长度/前缀)
#   - 命令结束立刻 unset (出错也走 trap)

set -euo pipefail

if [ $# -eq 0 ]; then
  echo "用法: $0 <command> [args...]" >&2
  echo "例:  $0 aliyun ram ListUsers" >&2
  exit 1
fi

# ─── 1. 从 Keychain 取 AK (会弹 Touch ID/密码框) ─────────────────────
echo "🔐 从 Keychain 取 admin AK (如有 Touch ID 提示请授权)..." >&2

if ! AK_ID=$(security find-generic-password -a "$USER" -s "aliyun-admin-ak-id" -w 2>/dev/null); then
  echo "✗ Keychain 取 aliyun-admin-ak-id 失败 (Touch ID 拒绝 / 条目不存在)" >&2
  exit 2
fi

if ! AK_SECRET=$(security find-generic-password -a "$USER" -s "aliyun-admin-ak-secret" -w 2>/dev/null); then
  echo "✗ Keychain 取 aliyun-admin-ak-secret 失败" >&2
  exit 2
fi

# 校验取出值看起来合理 (不打印值)
if [ "${#AK_ID}" -ne 24 ] || [ "${AK_ID:0:4}" != "LTAI" ]; then
  echo "✗ AK ID 格式异常 (长度=${#AK_ID}, 前 4='${AK_ID:0:4}'); 期望长度 24 + 前缀 LTAI" >&2
  exit 3
fi
if [ "${#AK_SECRET}" -ne 30 ]; then
  echo "✗ AK Secret 长度异常 (${#AK_SECRET}); 期望 30" >&2
  exit 3
fi

echo "✓ AK 取出 (ID 长度 ${#AK_ID}, Secret 长度 ${#AK_SECRET})" >&2

# ─── 2. 导出到 env 准备给子命令 (export 仅本进程及子进程可见) ─────────
export ALIBABA_CLOUD_ACCESS_KEY_ID="$AK_ID"
export ALIBABA_CLOUD_ACCESS_KEY_SECRET="$AK_SECRET"

# 兼容别名 (一些工具用旧名字)
export ALIYUN_ACCESS_KEY_ID="$AK_ID"
export ALIYUN_ACCESS_KEY_SECRET="$AK_SECRET"

# ─── 3. trap: 不管成功失败都 unset ─────────────────────────────────
cleanup() {
  unset ALIBABA_CLOUD_ACCESS_KEY_ID ALIBABA_CLOUD_ACCESS_KEY_SECRET
  unset ALIYUN_ACCESS_KEY_ID ALIYUN_ACCESS_KEY_SECRET
  unset AK_ID AK_SECRET
  echo "✓ AK env 已清理" >&2
}
trap cleanup EXIT INT TERM

# ─── 4. 跑子命令 ────────────────────────────────────────────────────
echo "▶ 执行: $*" >&2
echo "" >&2

# "$@" 保留参数引号
"$@"
