#!/bin/sh
# Set up everything paper2audio needs: ollama, uv, the paper2audio command, and the
# phonon speech and language models. Safe to re-run; steps that are already done are skipped.
#
# Usage: ./setup.sh [--no-gui] [--no-command] [MODEL]
#   MODEL         language model to download (default: recommended for this machine,
#                 asked interactively), or "none"
#   --no-gui      install only the paper2audio command, without the desktop app and its
#                 dependencies (NiceGUI, pywebview)
#   --no-command  skip uv and the paper2audio commands (used by the GUI's setup dialog,
#                 which must not reinstall itself while it runs). Set PAPER2AUDIO_PYTHON
#                 to a Python that has paper2audio installed to run its helpers without uv.
#
# If no ollama server is running, a temporary one is started for the downloads and
# stopped again at the end. A server that's already running is used and left alone.
set -eu
cd "$(dirname "$0")"

INSTALL_COMMAND=1
INSTALL_GUI=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-command) INSTALL_COMMAND=0 ;;
        --no-gui) INSTALL_GUI=0 ;;
        -*) echo "ERROR: unknown option $1" >&2; exit 2 ;;
        *) break ;;
    esac
    shift
done
# Runs Python with paper2audio installed (a function, so a path with spaces works).
run_python() {
    if [ -n "${PAPER2AUDIO_PYTHON:-}" ]; then
        "$PAPER2AUDIO_PYTHON" "$@"
    else
        uv run --quiet python "$@"
    fi
}

status() { echo ">>> $*"; }
available() { command -v "$1" >/dev/null 2>&1; }

# 1. ollama. The official installer always reinstalls (on macOS it stops Ollama and
#    deletes the existing app), so only run it when ollama is missing.
if available ollama; then
    status "ollama is already installed ($(ollama --version 2>/dev/null | tail -n 1))"
else
    status "Installing ollama with the official installer..."
    # OLLAMA_NO_START: on macOS, don't launch the Ollama app afterwards; it would keep running.
    curl -fsSL https://ollama.com/install.sh | OLLAMA_NO_START=1 sh
fi

# 2. An ollama server for the downloads. If none is running, start a temporary one and
#    stop exactly that one when the script ends (also on Ctrl+C, or when the GUI's setup
#    dialog cancels). A server that was already running is never stopped.
SERVER_PID=
stop_server() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
        SERVER_PID=
    fi
}
trap stop_server EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if ! ollama list >/dev/null 2>&1; then
    status "Starting a temporary ollama server..."
    ollama serve >/dev/null 2>&1 &
    SERVER_PID=$!
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

if [ "$INSTALL_COMMAND" = 1 ]; then
    # 3. uv, used to install the Python package.
    if ! available uv; then
        status "Installing uv with the official installer..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        PATH="$HOME/.local/bin:$PATH"
    fi

    # 4. The paper2audio and paper2audio-gui commands (editable, so code changes apply immediately).
    if [ "$INSTALL_GUI" = 1 ]; then
        status "Installing the paper2audio and paper2audio-gui commands..."
        uv tool install --editable ".[gui]" --force --quiet
    else
        status "Installing the paper2audio command (without the desktop app)..."
        uv tool install --editable . --force --quiet
    fi
fi

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

RECOMMENDED=$(run_python -m paper2audio.hardware)
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
    case " $(run_python -c 'from paper2audio.hardware import MODELS; print(*MODELS)') " in
        *" $LLM_MODEL "*) ;;  # picked automatically at runtime
        *) status "To use $LLM_MODEL, pass --llm-model=$LLM_MODEL or set llm_model in ~/.config/paper2audio/config.toml" ;;
    esac
fi

if [ "$INSTALL_COMMAND" = 1 ] && ! available paper2audio; then
    status "Note: run 'uv tool update-shell' and restart your terminal to put paper2audio on your PATH."
fi
if [ "$INSTALL_COMMAND" = 1 ] && [ "$INSTALL_GUI" = 1 ]; then
    status "Setup complete. Try: paper2audio convert --input=paper.pdf --output=paper.mp3, or paper2audio-gui"
else
    status "Setup complete. Try: paper2audio convert --input=paper.pdf --output=paper.mp3"
fi
