#!/bin/zsh
# 把 agent/ 下的两个 dsh 插件装进 profile 的 node_modules（软链，改代码即生效，
# 但 dsh 需要重启才会重新打包 client bundle）。
#
#   quant-ui    前端定制层：左栏品牌、角色条、工具卡
#   tool-guard  工具白名单：persona 的物理边界（ctx.tools.restrict）
#
# 为什么要装而不是直接写在 ~/.dsh 里：那是 dsh 的目录，清了 profile 就没了；
# 仓库里这份才是事实源。软链让两边指向同一批文件。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DIR="$HOME/.dsh/profiles/node_modules/@quant"

mkdir -p "$DEST_DIR"

link() {
  local src="$1" name="$2"
  [ -e "$src" ] || { echo "缺 $src"; exit 1; }
  rm -rf "$DEST_DIR/$name"
  ln -s "$src" "$DEST_DIR/$name"
  echo "已链接 $DEST_DIR/$name → $src"
}

link "$ROOT/agent/ui-plugin" "quant-ui"
link "$ROOT/agent/dsh-plugin/tool-guard" "tool-guard"

echo "重启 dsh 生效：$ROOT/scripts/dsh-web.sh stop && $ROOT/scripts/dsh-web.sh start"
