"""Minimal client for ollama's HTTP API, used for the language and vision models."""

import base64
import functools
import json
import urllib.error
import urllib.request

from .server import server_url

# Loading a model and transcribing a dense page can take a while on a laptop.
TIMEOUT = 600  # seconds


def generate(model: str, prompt: str, images: list[bytes] = (), num_ctx: int = 16384) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image).decode() for image in images],
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": num_ctx},
    }
    if "thinking" in capabilities(model):
        payload["think"] = False  # much faster, and not needed for transcribing
    return _post("/api/generate", payload).get("response", "").strip()


@functools.cache
def capabilities(model: str) -> frozenset[str]:
    """What the model can do according to ollama, e.g. "completion", "vision", "thinking"."""
    return frozenset(_post("/api/show", {"model": model}).get("capabilities") or ())


def show(model: str) -> dict:
    """Ollama's details about a model, e.g. its capabilities, or a speech model's voices."""
    return _post("/api/show", {"model": model})


def model_names() -> list[str]:
    """Installed models as ollama lists them, e.g. "gemma4:12b", "phonon:latest"."""
    return sorted(m["name"] for m in _get("/api/tags").get("models", []))


def installed_models() -> set[str]:
    """Names of installed models, with and without the ":latest" tag."""
    names = {m["name"] for m in _get("/api/tags").get("models", [])}
    return names | {n.removesuffix(":latest") for n in names}


def unload(model: str) -> None:
    """Free the model's memory, e.g. for phonon. Failure is harmless: ollama unloads idle models itself."""
    try:
        _post("/api/generate", {"model": model, "keep_alive": 0})
    except RuntimeError:
        pass


def _get(path: str) -> dict:
    return _request(urllib.request.Request(server_url().rstrip("/") + path))


def _post(path: str, payload: dict) -> dict:
    return _request(urllib.request.Request(
        server_url().rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    ))


def _request(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("error", str(e))
        except ValueError:
            message = str(e)
        raise RuntimeError(f"ollama: {message}") from None
    except (OSError, ValueError) as e:  # connection problems, timeouts, bad JSON
        raise RuntimeError(f"ollama: {e}") from None
