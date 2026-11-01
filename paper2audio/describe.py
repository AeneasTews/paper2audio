"""Describe figures with a local ollama vision model, so they can be read aloud."""

import base64
import json
import sys
import urllib.error
import urllib.request

from .server import server_url

DEFAULT_MODEL = "gemma3"
# Loading the model and describing a large figure can take a while on a laptop.
TIMEOUT = 300  # seconds
# The model answers with this for logos, decorations and other images not worth describing.
SKIP = "SKIP"
PROMPT = f"""\
This image is a figure from a scientific paper. The paper is being converted to audio \
for a listener who cannot see the figure. Describe it in a short paragraph (2 to 5 \
sentences) of plain spoken English: what kind of figure it is, what it shows (axes, \
groups, labels, components), and the main takeaway or trend.

Write plain prose only: no Markdown, no lists, no headings, and no preamble such as \
"This image shows". Spell out symbols where needed so the text reads naturally aloud.

If the image is only a logo, icon, photo of an author, or decoration that adds nothing \
to the paper's content, reply with exactly {SKIP}."""


def describe_images(images: list[bytes], model: str) -> list[str | None]:
    """Describe each PNG image. Returns None for images that were skipped or failed."""
    if not images:
        return []
    _check_model(model)
    descriptions: list[str | None] = []
    try:
        for i, image in enumerate(images, 1):
            print(f"Describing image {i}/{len(images)}...", file=sys.stderr)
            descriptions.append(_describe(image, model))
    finally:
        _unload(model)  # free the memory for phonon
    return descriptions


def _describe(image: bytes, model: str) -> str | None:
    try:
        response = _post("/api/generate", {
            "model": model,
            "prompt": PROMPT,
            "images": [base64.b64encode(image).decode()],
            "stream": False,
            "options": {"temperature": 0.2},
        })
    except RuntimeError as e:  # one unreadable image shouldn't fail the whole paper
        print(f"  skipped ({e})", file=sys.stderr)
        return None
    text = response.get("response", "").strip()
    if not text or text.strip(" .").upper() == SKIP:
        print("  skipped (not a figure)", file=sys.stderr)
        return None
    return text


def _check_model(model: str) -> None:
    try:
        info = _post("/api/show", {"model": model})
    except RuntimeError as e:
        if "not found" in str(e):
            raise RuntimeError(
                f"vision model {model!r} is not installed. Run 'ollama pull {model}', "
                "pick another one with --vision-model, or skip images with --no-images"
            ) from None
        raise
    capabilities = info.get("capabilities")
    if capabilities is not None and "vision" not in capabilities:
        raise RuntimeError(f"model {model!r} can't read images; pick a vision model with --vision-model")


def _unload(model: str) -> None:
    try:
        _post("/api/generate", {"model": model, "keep_alive": 0})
    except RuntimeError:
        pass  # ollama unloads idle models by itself after a few minutes anyway


def _post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        server_url().rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
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
