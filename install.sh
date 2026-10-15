#!/bin/sh
# Install dbb.
#   curl -fsSL https://raw.githubusercontent.com/franshiro/db-backup/main/install.sh | sh
#   curl -fsSL .../install.sh | sh -s -- --prefix /usr/local/bin
#   ./install.sh                 (from a local clone)
#   ./install.sh --uninstall
# Default prefix: ~/.local/bin (no sudo needed). Needs only python3.
set -e
REPO_RAW="${DBB_REPO_RAW:-https://raw.githubusercontent.com/franshiro/db-backup/main}"
PREFIX="$HOME/.local/bin"
UNINSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done
TARGET="$PREFIX/dbb"

if [ "$UNINSTALL" = 1 ]; then
  rm -f "$TARGET" && echo "✓ removed $TARGET (data in ~/.dbb kept)"
  exit 0
fi

command -v python3 >/dev/null || { echo "✗ python3 not found" >&2; exit 1; }
mkdir -p "$PREFIX"

# local clone: install.sh sits next to dbb.py; piped from curl: download it
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
if [ -f "$0" ] && [ -f "$HERE/dbb.py" ]; then
  install -m 755 "$HERE/dbb.py" "$TARGET"
else
  TMP="$(mktemp)"
  curl -fsSL "$REPO_RAW/dbb.py" -o "$TMP"
  python3 -c "import sys; compile(open(sys.argv[1]).read(), 'dbb.py', 'exec')" "$TMP"
  install -m 755 "$TMP" "$TARGET"
  rm -f "$TMP"
fi
echo "✓ installed $TARGET ($("$TARGET" --version | cut -d' ' -f1-2))"

case ":$PATH:" in
  *":$PREFIX:"*) echo "  run: dbb" ;;
  *) echo "! $PREFIX is not in PATH. Add to ~/.zshrc:"
     echo "    export PATH=\"$PREFIX:\$PATH\"" ;;
esac
