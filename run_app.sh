#!/usr/bin/env bash
# One script for macOS / Linux: installs everything on first run, then starts the app against the GPU server.
# Usage, from the project folder:  bash run_app.sh
#
# The server addresses and key are read from server.env next to this script (created on first run,
# never committed). The models run on that server, so this computer needs no GPU.
set -euo pipefail
cd "$(dirname "$0")"

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# 2. Latest code (only when this is a git checkout)
if [ -d .git ]; then
    git pull --ff-only || echo "Could not update the code; continuing with the current version."
fi

# 3. Packages (first run creates .venv; later runs only add what changed)
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install -q -p .venv/bin/python -e ".[ui]"

# 4. Server settings
if [ ! -f server.env ]; then
    cat > server.env <<'EOF'
# Fill in the values from the server owner, save, then run run_app.sh again.
VLLM_API_BASE=https://<pod-id>-8000.proxy.runpod.net/v1
VLLM_TEXT_API_BASE=https://<pod-id>-8001.proxy.runpod.net/v1
VLLM_OCR_API_BASE=https://<pod-id>-8002.proxy.runpod.net/v1
VLLM_API_KEY=
EOF
    echo "Created server.env. Fill in the server addresses and key, then run this script again."
    open -e server.env 2>/dev/null || true
    exit 1
fi
while IFS='=' read -r name value; do
    case "$name" in VLLM_*) export "$name=${value%$'\r'}" ;; esac
done < server.env
case "${VLLM_API_BASE:-}" in *"<pod-id>"*) VLLM_API_KEY="" ;; esac
if [ -z "${VLLM_API_KEY:-}" ]; then
    echo "server.env still has placeholders or no key. Fill it in and run again."
    open -e server.env 2>/dev/null || true
    exit 1
fi
export VLLM_TIMEOUT_SECONDS=300 GRADIO_SHARE=false

# 5. Check the server, then start the app
.venv/bin/python scripts/check_vllm.py || { echo "The model server check failed: is the server running and are the addresses right?"; exit 1; }
echo "Opening http://127.0.0.1:7860 (press Ctrl+C here to stop)"
( sleep 8; open http://127.0.0.1:7860 2>/dev/null || xdg-open http://127.0.0.1:7860 2>/dev/null || true ) &
cd src
exec ../.venv/bin/python -m doc_extractor.app
