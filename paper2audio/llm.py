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


def generate_keyed(model: str, prompt: str, items: list[str], field: str) -> list[str | None]:
    """Ask for one string per item, answered as JSON keyed by item number.

    Keys (rather than a plain list) mean a skipped item can't shift the answers for the
    others. Items the model doesn't answer come back as None.
    """
    schema = {
        "type": "object",
        "properties": {"answers": {"type": "array", "items": {
            "type": "object",
            "properties": {"item": {"type": "integer"}, field: {"type": "string"}},
            "required": ["item", field],
        }}},
        "required": ["answers"],
    }
    numbered = "\n".join(f"[{i}] {item}" for i, item in enumerate(items))
    payload = {
        "model": model,
        "prompt": f'{prompt}\n\nAnswer with JSON: {{"answers": [{{"item": <number>, "{field}": ...}}, ...]}}, '
                  f"one entry for each of the {len(items)} items.\n\n{numbered}",
        "stream": False,
        "format": schema,
        "options": {"temperature": 0, "num_ctx": 16384},
    }
    if "thinking" in capabilities(model):
        payload["think"] = False
    try:
        answers = json.loads(_post("/api/generate", payload).get("response", "")).get("answers", [])
    except ValueError:
        return [None] * len(items)
    by_item = {a["item"]: a[field] for a in answers if isinstance(a, dict) and isinstance(a.get("item"), int)}
    return [by_item.get(i) for i in range(len(items))]


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
