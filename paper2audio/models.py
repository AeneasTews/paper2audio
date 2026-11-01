"""Ask ollama which models and voices are available, e.g. to offer them in the GUI."""

from .server import api


def vision_models() -> list[str]:
    """Names of the installed models that can read images, e.g. 'gemma3:latest'."""
    names = sorted(model["name"] for model in api("/api/tags").get("models", []))
    return [name for name in names if "vision" in _capabilities(name)]


def voices(model: str) -> list[str]:
    """Voices of a speech model like phonon, as listed by `ollama show`."""
    info = api("/api/show", {"model": model})
    return sorted(_names(info.get("voices")), key=str.lower)


def _capabilities(model: str) -> list[str]:
    try:
        return api("/api/show", {"model": model}).get("capabilities") or []
    except RuntimeError:  # e.g. a model that was removed in the meantime
        return []


def _names(entries) -> list[str]:
    """Accept a list of names, a list of objects with a name, or a mapping keyed by name."""
    if isinstance(entries, dict):
        return [str(name) for name in entries]
    names = []
    for entry in entries or []:
        if isinstance(entry, dict):
            entry = entry.get("name") or entry.get("id")
        if entry:
            names.append(str(entry))
    return names
