#!/bin/zsh
# 把 agent/ui-plugin 装进 dsh 的 profile node_modules（软链，改代码即生效，
# 但 dsh 需要重启才会重新打包 client bundle）。
#
# 为什么要装而不是直接写在 ~/.dsh 里：那是 dsh 的目录，清了 profile 就没了；
# 仓库里这份才是事实源。软链让两边指向同一批文件。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/agent/ui-plugin"
DST="$HOME/.dsh/profiles/node_modules/@quant/quant-ui"

[ -f "$SRC/lib/client.js" ] || { echo "缺 $SRC/lib/client.js"; exit 1; }
mkdir -p "$(dirname "$DST")"
rm -rf "$DST"
ln -s "$SRC" "$DST"
echo "已链接 $DST → $SRC"
echo "重启 dsh 生效：~/quant-platform/scripts/dsh-web.sh restart"
