#!/usr/bin/env bash
# 仁谱 一键安装 · macOS / Linux
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT="$(dirname "$HERE")"

echo "── 仁谱 一键安装"
echo

# 1. 装 Python 依赖
if ! python3 -c "import Crypto.Cipher" 2>/dev/null; then
  echo "· 装 pycryptodome…"
  python3 -m pip install --user --upgrade pycryptodome
fi
echo "· pycryptodome: ✅"

# 2. 把 bin/ 加 PATH（写到当前 shell rc）
SHELL_RC="$HOME/.bashrc"
if [ -n "$ZSH_VERSION" ] || [ "$SHELL" = "/bin/zsh" ]; then
  SHELL_RC="$HOME/.zshrc"
fi
LINE="export PATH=\"$PARENT/bin:\$PATH\""

if grep -F "$PARENT/bin" "$SHELL_RC" 2>/dev/null >/dev/null; then
  echo "· PATH 已含 $PARENT/bin"
else
  echo "" >> "$SHELL_RC"
  echo "# 仁谱 Renpu" >> "$SHELL_RC"
  echo "$LINE" >> "$SHELL_RC"
  echo "· PATH 已写入 $SHELL_RC"
fi

# 3. 把 skill 注册到 workbuddy
SKILL_DST="$HOME/.workbuddy/skills/renpu"
mkdir -p "$HOME/.workbuddy/skills/"
if [ -d "$SKILL_DST" ]; then
  echo "· skill 已注册：$SKILL_DST"
else
  cp -r "$PARENT" "$SKILL_DST"
  echo "· skill 已注册：$SKILL_DST"
fi

echo
echo "✅ 装好啦。运行："
echo "   source $SHELL_RC"
echo "   renpu doctor"
echo "   renpu init && renpu ingest --demo"