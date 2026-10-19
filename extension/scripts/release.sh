#!/usr/bin/env bash
# Compila, empacota (.vsix) e instala uma cópia normal no Antigravity
# (substitui o link do modo desenvolvimento, se houver).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
CLI="${ANTIGRAVITY_CLI:-/opt/antigravity/bin/antigravity-ide}"
cd "$SRC"
npx tsc -p .
npx vsce package --allow-missing-repository --skip-license
VSIX="$(ls -t ./*.vsix | head -1)"
"$CLI" --install-extension "$VSIX" --force
echo "Instalado $VSIX. Recarregue a janela do Antigravity."
