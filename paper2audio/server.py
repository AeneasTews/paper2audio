"""Make sure an ollama server is running, starting a temporary one if needed."""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from .progress import Progress, print_progress

STARTUP_TIMEOUT = 30  # seconds
# Loading a model and describing a large figure can take a while on a laptop.
API_TIMEOUT = 300  # seconds


@contextmanager
def ollama_server(progress: Progress = print_progress):
    """Use the running ollama server, or start `ollama serve` and stop it again afterwards.

    Starting the server ourselves also matters on macOS: if `ollama run` finds no server,
    it launches the Ollama desktop app, which keeps running after we're done.
    """
    if _is_running():
        yield
        return

    progress("Starting ollama server...", None)
    process = subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_running(process)
        yield
    finally:
        progress("Stopping ollama server...", None)
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


def api(path: str, payload: dict | None = None) -> dict:
    """Call ollama's HTTP API: POST `payload` as JSON, or GET if there is none."""
    request = urllib.request.Request(
        server_url().rstrip("/") + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("error", str(e))
        except ValueError:
            message = str(e)
        raise RuntimeError(f"ollama: {message}") from None
    except (OSError, ValueError) as e:  # connection problems, timeouts, bad JSON
        raise RuntimeError(f"ollama: {e}") from None
