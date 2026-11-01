#!/bin/sh
# Set up everything paper2audio needs: ollama, uv, the paper2audio command, and the
# phonon speech and language models. Safe to re-run; steps that are already done are skipped.
#
# Usage: ./setup.sh [MODEL]   MODEL: language model to download (default: recommended for
#                             this machine, asked interactively), or "none"
set -eu
cd "$(dirname "$0")"

status() { echo ">>> $*"; }
available() { command -v "$1" >/dev/null 2>&1; }

# 1. ollama. The official installer always reinstalls (on macOS it stops Ollama and
#    deletes the existing app), so only run it when ollama is missing.
if available ollama; then
    status "ollama is already installed ($(ollama --version 2>/dev/null | tail -n 1))"
else
    status "Installing ollama with the official installer..."
    curl -fsSL https://ollama.com/install.sh | sh
fi

# 2. Make sure the ollama server is running.
if ! ollama list >/dev/null 2>&1; then
    status "Starting ollama..."
    if [ "$(uname -s)" = "Darwin" ]; then
        open -a Ollama --args hidden
    else
        nohup ollama serve >/dev/null 2>&1 &
    fi
    i=0
    until ollama list >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            echo "ERROR: ollama server did not start. Try running 'ollama serve' manually." >&2
            exit 1
        fi
        sleep 1
    done
fi

# 3. uv, used to install the Python package.
if ! available uv; then
    status "Installing uv with the official installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
fi

# 4. The paper2audio command (editable, so code changes apply immediately).
status "Installing the paper2audio command..."
uv tool install --editable . --force --quiet

# 5. The models: phonon for speech, and a language model that reads the pages.
#    The language model is recommended by memory size; pass another one as an argument
#    (any ollama model), or "none" to only use the rule-based cleanup.
installed() { ollama list | awk 'NR > 1 { print $1 }' | grep -qx -e "$1" -e "$1:latest"; }
pull() {
    if installed "$1"; then
        status "$1 model is already available"
    else
        status "Downloading the $1 model..."
        ollama pull "$1"
    fi
}

RECOMMENDED=$(uv run --quiet python -m paper2audio.hardware)
LLM_MODEL=${1:-}
if [ -z "$LLM_MODEL" ] && [ -t 0 ]; then
    echo
    echo "Which language model should read the papers? (memory needed, download size)"
    echo "  gemma4:26b   32 GB, 16 GB download"
    echo "  gemma4:12b   16 GB, 7.7 GB download"
    echo "  gemma4:e4b    8 GB, 6.6 GB download (9.5 GB on Macs)"
    echo "  gemma3:4b     8 GB, 3.4 GB download (older, smallest)"
    echo "  none         no language model: formulas and figures aren't handled"
    echo "  or any other ollama model, e.g. one you already have"
    printf "Model [%s]: " "${RECOMMENDED:-none}"
    read -r LLM_MODEL
fi
LLM_MODEL=${LLM_MODEL:-${RECOMMENDED:-none}}

pull phonon
if [ "$LLM_MODEL" != none ]; then
    pull "$LLM_MODEL"
    case " $(uv run --quiet python -c 'from paper2audio.hardware import MODELS; print(*MODELS)') " in
        *" $LLM_MODEL "*) ;;  # picked automatically at runtime
        *) status "To use $LLM_MODEL, pass --llm-model=$LLM_MODEL or set llm_model in ~/.config/paper2audio/config.toml" ;;
    esac
fi

if ! available paper2audio; then
    status "Note: run 'uv tool update-shell' and restart your terminal to put paper2audio on your PATH."
fi
status "Setup complete. Try: paper2audio convert --input=paper.pdf --output=paper.mp3"
