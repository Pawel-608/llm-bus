#!/usr/bin/env sh
# Install the llm-bus CLI globally (editable, so code changes apply immediately).
set -eu
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found, installing..." >&2
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

uv tool install --editable --force .

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "NOTE: add ~/.local/bin to your PATH (run: uv tool update-shell)" >&2 ;;
esac

echo "installed: $(command -v llm-bus)"
echo "db:        ${LLM_BUS_DB:-$HOME/.llm_bus/bus.db}"
