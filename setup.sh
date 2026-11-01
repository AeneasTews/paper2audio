#!/bin/sh
# Set up everything paper2audio needs: ollama, the phonon and vision models, uv, and the
# paper2audio and paper2audio-gui commands. Safe to re-run; steps that are already done are skipped.
#
#   ./setup.sh                         everything
#   ./setup.sh --models "phonon"       only these models (default: "phonon gemma3")
#   ./setup.sh --no-command            skip uv and the commands (used by the GUI's setup
#                                      dialog, which must not reinstall itself while running)
set -eu

MODELS="phonon gemma3"  # speech, and the vision model that describes figures
INSTALL_COMMAND=1
while [ $# -gt 0 ]; do
    case "$1" in
        --models) [ $# -ge 2 ] || { echo "ERROR: --models needs a list of models" >&2; exit 2; }
                  MODELS="$2"; shift ;;
        --no-command) INSTALL_COMMAND=0 ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "ERROR: unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done
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

# 3. The phonon speech model and the vision model.
for MODEL in $MODELS; do
    if ollama list | awk 'NR > 1 { print $1 }' | grep -q "^$MODEL:"; then
        status "$MODEL model is already available"
    else
        status "Downloading the $MODEL model..."
        ollama pull "$MODEL"
    fi
done

if [ "$INSTALL_COMMAND" = 0 ]; then
    status "Setup complete."
    exit 0
fi

# 4. uv, used to install the Python package.
if ! available uv; then
    status "Installing uv with the official installer..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
fi

# 5. The paper2audio command (editable, so code changes apply immediately).
status "Installing the paper2audio and paper2audio-gui commands..."
uv tool install --editable ".[gui]" --force --quiet

if ! available paper2audio; then
    status "Note: run 'uv tool update-shell' and restart your terminal to put paper2audio on your PATH."
fi
status "Setup complete. Try: paper2audio convert --input=paper.pdf --output=paper.mp3, or paper2audio-gui"
