"""Ask ollama which models and voices are available, e.g. to offer them in the GUI."""

from . import llm, tts


def language_models() -> list[str]:
    """Installed models that can generate text and could read the pages, e.g. 'gemma4:12b'."""
    names = [n for n in llm.model_names() if n.split(":")[0] != tts.MODEL]
    return [n for n in names if "completion" in _capabilities(n)]


def voices(model: str) -> list[str]:
    """Voices of a speech model like phonon, as listed by `ollama show`."""
    return sorted(_names(llm.show(model).get("voices")), key=str.lower)


def _capabilities(model: str) -> frozenset[str]:
    try:
        return llm.capabilities(model)
    except RuntimeError:  # e.g. a model that was removed in the meantime
        return frozenset()


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
