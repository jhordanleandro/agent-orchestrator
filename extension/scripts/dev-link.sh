#!/usr/bin/env bash
# Modo desenvolvimento: troca a cópia instalada da extensão no Antigravity por
# um link para esta pasta. Depois disso:
#   - `npm run dev` recompila a cada mudança em src/ (o editor oferece recarregar);
#   - mudanças em media/ recarregam o painel sozinhas.
# Para voltar a uma cópia empacotada normal: `npm run release`.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
EXT_DIR="${ANTIGRAVITY_EXTENSIONS_DIR:-$HOME/.antigravity-ide/extensions}"
CLI="${ANTIGRAVITY_CLI:-/opt/antigravity/bin/antigravity-ide}"
ID="jhordan.agent-orchestrator"

cd "$SRC"
npx tsc -p .

# Garante que a extensão está registrada (extensions.json) antes de trocar a pasta.
REL="$(python3 - "$EXT_DIR/extensions.json" "$ID" <<'PY'
import json, sys
for e in json.load(open(sys.argv[1])):
    if e["identifier"]["id"] == sys.argv[2]:
        print(e.get("relativeLocation", "")); break
PY
)"
if [ -z "$REL" ]; then
  echo "Extensão ainda não instalada: instalando o pacote uma vez para registrar…"
  bash "$SRC/scripts/release.sh"
  REL="$(python3 -c "import json,sys; print(next(e['relativeLocation'] for e in json.load(open('$EXT_DIR/extensions.json')) if e['identifier']['id']=='$ID'))")"
fi

TARGET="$EXT_DIR/${REL:?}"
if [ -L "$TARGET" ]; then
  echo "Já está em modo desenvolvimento: $TARGET -> $(readlink "$TARGET")"
  exit 0
fi
if [ -d "$TARGET" ]; then
  rm -rf -- "${TARGET:?}"
fi
ln -s "$SRC" "$TARGET"
echo "Pronto: $TARGET -> $SRC"
echo "Recarregue a janela do Antigravity uma vez (Developer: Reload Window)."
echo "Depois, deixe 'npm run dev' rodando enquanto edita a extensão."
