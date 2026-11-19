#!/usr/bin/env bash
# ai-bridge installer: run from the repo root with  ./install.sh
# Installs the `bridge` and `ai-bridge-server` commands with uv, registers the
# bridge in Claude Code, the Claude desktop app and Antigravity, and runs a check.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$(pwd)"

say() { printf '\n== %s\n' "$*"; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) echo "Only macOS and Linux are supported."; exit 1 ;;
esac

export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  say "uv is not installed"
  echo "uv manages Python for this project. Official installer: https://docs.astral.sh/uv/"
  read -r -p "Download and run the official uv installer now? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
    echo "Downloaded to /tmp/uv-install.sh (sha256: $(shasum -a 256 /tmp/uv-install.sh 2>/dev/null | cut -d' ' -f1 || sha256sum /tmp/uv-install.sh | cut -d' ' -f1))"
    sh /tmp/uv-install.sh
  else
    echo "Install uv first, then run ./install.sh again."; exit 1
  fi
fi

for tool in agy claude; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    case "$tool" in
      agy) echo "Note: the Antigravity CLI (agy) is not installed. Gemini features need it:"
           echo "      https://antigravity.google/docs/cli/install  (then run 'agy' once to log in)" ;;
      claude) echo "Note: Claude Code (claude) is not installed. claude_ask and Claude Code use need it:"
              echo "      https://code.claude.com/docs/en/setup" ;;
    esac
  fi
done

say "Installing the bridge commands"
uv tool install --quiet --editable "$REPO" --force
hash -r

say "Registering the bridge"
bridge install

say "Checking the installation"
bridge doctor || true

cat <<'MSG'

Next steps:
  1. Restart the Claude desktop app (Cmd+Q) and Antigravity so they load the bridge.
  2. Run 'bridge doctor --deep' for a live test of Gemini and Claude.
  3. Run 'bridge ui' for the settings panel, or 'bridge --help' for all commands.
MSG
