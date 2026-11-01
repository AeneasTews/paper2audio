"""Store the GUI's highlights, underlines and notes per paper.

Annotations live in the user's data folder, keyed by a hash of the PDF's contents, so they
follow the paper when it's moved or renamed and the PDF itself is never modified.
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

TYPES = {"highlight", "underline", "note"}
MAX_ANNOTATIONS = 5000


def data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "paper2audio" / "annotations"


def document_id(pdf_path: Path) -> str:
    digest = hashlib.sha256()
    with open(pdf_path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def load(document: str) -> list[dict]:
    path = data_dir() / f"{document}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return validate(data.get("annotations", []) if isinstance(data, dict) else [])


def save(document: str, annotations: list[dict], pdf_name: str = "") -> None:
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"file": pdf_name, "annotations": validate(annotations)}, ensure_ascii=False, indent=1)
    # Write to a temporary file first, so a crash can't leave a half-written file behind.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, delete=False, suffix=".tmp") as f:
        f.write(content)
    os.replace(f.name, folder / f"{document}.json")


def validate(annotations) -> list[dict]:
    """Keep only well-formed annotations, with the fields the reader uses."""
    if not isinstance(annotations, list):
        raise ValueError("annotations must be a list")
    result = []
    for a in annotations[:MAX_ANNOTATIONS]:
        if not isinstance(a, dict) or a.get("type") not in TYPES:
            continue
        try:
            clean = {
                "id": str(a["id"])[:64],
                "type": a["type"],
                "color": str(a.get("color", "#ffd400"))[:16],
                "page": int(a["page"]),
                "text": str(a.get("text", ""))[:20000],
                "comment": str(a.get("comment", ""))[:20000],
                "created": str(a.get("created", ""))[:40],
            }
            if a["type"] == "note":
                x, y = (float(v) for v in a["position"])
                clean["position"] = [x, y]
            else:
                clean["rects"] = [[float(v) for v in r[:4]] for r in a["rects"][:500] if len(r) >= 4]
        except (KeyError, TypeError, ValueError):
            continue
        result.append(clean)
    return result
