"""Make sure an ollama server is running, starting a temporary one if needed."""

import os
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager

STARTUP_TIMEOUT = 30  # seconds


@contextmanager
def ollama_server():
    """Use the running ollama server, or start `ollama serve` and stop it again afterwards.

    Starting the server ourselves also matters on macOS: if `ollama run` finds no server,
    it launches the Ollama desktop app, which keeps running after we're done.
    """
    if _is_running():
        yield
        return

    print("Starting ollama server...", file=sys.stderr)
    process = subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_running(process)
        yield
    finally:
        print("Stopping ollama server...", file=sys.stderr)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _wait_until_running(process: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while not _is_running():
        if process.poll() is not None:
            raise RuntimeError(f"'ollama serve' exited with code {process.returncode}")
        if time.monotonic() > deadline:
            raise RuntimeError(f"ollama server did not start within {STARTUP_TIMEOUT}s")
        time.sleep(0.2)


def _is_running() -> bool:
    try:
        with urllib.request.urlopen(server_url(), timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def server_url() -> str:
    """Server address, following ollama's OLLAMA_HOST conventions (default 127.0.0.1:11434)."""
    host = os.environ.get("OLLAMA_HOST", "").strip() or "127.0.0.1:11434"
    scheme, sep, rest = host.partition("://")
    if not sep:
        scheme, rest = "http", host
    hostport = rest.split("/", 1)[0]
    if hostport.startswith("0.0.0.0"):  # bind-all address; connect via localhost
        hostport = "127.0.0.1" + hostport[len("0.0.0.0"):]
    if ":" not in hostport:
        hostport += ":11434" if not sep else (":443" if scheme == "https" else ":80")
    return f"{scheme}://{hostport}/"
