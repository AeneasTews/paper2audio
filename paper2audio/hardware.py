"""Pick the language model that fits this machine.

Run as `python -m paper2audio.hardware` to print the recommended model (used by setup.sh).
"""

import os
import platform
import subprocess

# Largest model first: (minimum memory in GiB, model). Memory means unified RAM on Macs and
# GPU memory on machines with an NVIDIA card, otherwise system RAM.
TIERS = [
    (32, "gemma4:26b"),  # mixture of experts with ~4B active parameters: fast for its size
    (16, "gemma4:12b"),
    (8, "gemma4:e4b"),
]
# Smaller alternatives, for a smaller download. Any other ollama model works too (--llm-model).
ALTERNATIVES = ["gemma3:4b"]
MODELS = [model for _, model in TIERS] + ALTERNATIVES


def recommended_model() -> str | None:
    """The largest model that fits, or None if the machine is too small for any."""
    memory = memory_gb()
    if memory is None:
        return None
    return next((model for minimum, model in TIERS if memory >= minimum), None)


def memory_gb() -> float | None:
    if platform.system() == "Darwin":
        result = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True)
        return int(result.stdout) / 2**30 if result.returncode == 0 else None
    return _nvidia_memory_gb() or _system_memory_gb()


def _nvidia_memory_gb() -> float | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return max(int(line) for line in result.stdout.split()) / 1024  # MiB, largest GPU


def _system_memory_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (ValueError, OSError):
        return None


if __name__ == "__main__":
    print(recommended_model() or "")
