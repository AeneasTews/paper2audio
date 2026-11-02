"""The GUI's paper library: papers that were opened (Recent), papers added to the library,
and collections (folders, which can be nested) to sort them into, like in Zotero.

Papers are stored by their location on disk; the files themselves are never copied or
modified. A paper can be in several collections. Zotero's library is merged in read-only
(see zotero.py): its papers show up next to the local ones and can be added to local
collections too.

Everything is kept in one JSON file next to the annotations.
"""

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from . import annotations

MAX_RECENT = 300  # papers that were only opened (not added to the library) are forgotten after this many
MAX_NAME = 200
COLLECTION_ID = re.compile(r"[A-Za-z0-9_-]{4,64}")


def library_path() -> Path:
    return annotations.data_dir().parent / "library.json"


def paper_id(path: Path | str) -> str:
    """A paper's ID: derived from its location, so the same file always gets the same ID."""
    return hashlib.sha1(str(path).encode("utf-8", "surrogatepass")).hexdigest()[:16]


class Library:
    def __init__(self, path: Path | None = None):
        self.path = path or library_path()
        self.papers: dict[str, dict] = {}  # id: {path, title, authors, year, source, added, opened, collections, audio}
        self.collections: dict[str, dict] = {}  # id: {name, parent}
        self.session: dict = {"tabs": [], "active": None}  # open tabs, as paths, to restore them
        self.intro_seen = False  # the GUI's introduction was shown
        self.load()

    # --- storage -----------------------------------------------------------------------

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for pid, paper in (data.get("papers") or {}).items():
            if isinstance(paper, dict) and isinstance(paper.get("path"), str):
                self.papers[pid] = {
                    "path": paper["path"],
                    "title": str(paper.get("title") or Path(paper["path"]).stem)[:MAX_NAME * 2],
                    "authors": [str(a) for a in paper.get("authors") or []][:50],
                    "year": str(paper.get("year") or "")[:10],
                    "source": paper.get("source") if isinstance(paper.get("source"), str) else None,
                    "added": _number(paper.get("added")),
                    "opened": _number(paper.get("opened")),
                    "collections": [c for c in paper.get("collections") or [] if isinstance(c, str)],
                    "audio": paper.get("audio") if isinstance(paper.get("audio"), str) else None,
                }
        for cid, collection in (data.get("collections") or {}).items():
            if isinstance(collection, dict) and COLLECTION_ID.fullmatch(cid):
                self.collections[cid] = {
                    "name": str(collection.get("name") or "Untitled")[:MAX_NAME],
                    "parent": collection.get("parent") if isinstance(collection.get("parent"), str) else None,
                }
        for collection in self.collections.values():  # parents that are gone: move to the top level
            if collection["parent"] not in self.collections:
                collection["parent"] = None
        for paper in self.papers.values():
            paper["collections"] = [c for c in paper["collections"] if c in self.collections]
        self.intro_seen = data.get("intro_seen") is True
        session = data.get("session")
        if isinstance(session, dict):
            tabs = [t for t in session.get("tabs") or [] if isinstance(t, str)]
            active = session.get("active") if isinstance(session.get("active"), str) else None
            self.session = {"tabs": tabs, "active": active}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps({"version": 1, "papers": self.papers, "collections": self.collections,
                              "session": self.session, "intro_seen": self.intro_seen},
                             ensure_ascii=False, indent=1)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False, suffix=".tmp") as f:
            f.write(content)
        os.replace(f.name, self.path)

    # --- papers ------------------------------------------------------------------------

    def record(self, path: Path, title: str = "", authors: list[str] | None = None, year: str = "",
               source: str | None = None, opened: bool = False) -> str:
        """Remember a paper (or update what's known about it); returns its ID."""
        pid = paper_id(path)
        paper = self.papers.setdefault(pid, {
            "path": str(path), "title": path.stem, "authors": [], "year": "", "source": None,
            "added": None, "opened": None, "collections": [], "audio": None,
        })
        if title:
            paper["title"] = title[:MAX_NAME * 2]
        if authors:
            paper["authors"] = authors[:50]
        if year:
            paper["year"] = year
        if source:
            paper["source"] = source
        if opened:
            paper["opened"] = time.time()
        self._forget_old()
        return pid

    def set_audio(self, path: Path, audio: Path) -> None:
        """Remember the audio made from a paper, so the player finds it after a restart."""
        self.papers[self.record(path)]["audio"] = str(audio)

    def add(self, ids: list[str], collection: str | None = None) -> None:
        """Add papers to the library, and to a collection if one is given."""
        if collection is not None and collection not in self.collections:
            return
        now = time.time()
        for pid in ids:
            paper = self.papers.get(pid)
            if paper is None:
                continue
            paper["added"] = paper["added"] or now
            if collection and collection not in paper["collections"]:
                paper["collections"].append(collection)

    def remove_from_collection(self, ids: list[str], collection: str) -> None:
        for pid in ids:
            if pid in self.papers and collection in self.papers[pid]["collections"]:
                self.papers[pid]["collections"].remove(collection)

    def remove_from_library(self, ids: list[str]) -> None:
        """Take papers out of the library and all collections; they stay in Recent if they were opened."""
        for pid in ids:
            paper = self.papers.get(pid)
            if paper is None:
                continue
            paper["added"] = None
            paper["collections"] = []
            if not paper["opened"]:
                del self.papers[pid]

    def forget_opened(self, ids: list[str]) -> None:
        """Remove papers from Recent; papers in the library stay there."""
        for pid in ids:
            paper = self.papers.get(pid)
            if paper is None:
                continue
            paper["opened"] = None
            if not paper["added"]:
                del self.papers[pid]

    def _forget_old(self) -> None:
        only_opened = sorted((p["opened"] or 0, pid) for pid, p in self.papers.items() if not p["added"])
        for _, pid in only_opened[:max(0, len(only_opened) - MAX_RECENT)]:
            del self.papers[pid]

    # --- collections -------------------------------------------------------------------

    def create_collection(self, cid: str, name: str, parent: str | None) -> bool:
        if not COLLECTION_ID.fullmatch(cid) or cid in self.collections:
            return False
        if parent is not None and parent not in self.collections:
            parent = None
        self.collections[cid] = {"name": _clean_name(name), "parent": parent}
        return True

    def rename_collection(self, cid: str, name: str) -> None:
        if cid in self.collections:
            self.collections[cid]["name"] = _clean_name(name)

    def move_collection(self, cid: str, parent: str | None) -> bool:
        """Move a collection into another one (or to the top level). Refuses to make a loop."""
        if cid not in self.collections or (parent is not None and parent not in self.collections):
            return False
        ancestor = parent
        while ancestor is not None:
            if ancestor == cid:
                return False
            ancestor = self.collections[ancestor]["parent"]
        self.collections[cid]["parent"] = parent
        return True

    def descendants(self, cid: str) -> list[str]:
        """The collection and everything nested in it."""
        result, todo = [], [cid]
        while todo:
            current = todo.pop()
            result.append(current)
            todo += [c for c, info in self.collections.items() if info["parent"] == current]
        return result

    def delete_collection(self, cid: str) -> None:
        """Delete a collection and its subcollections. The papers stay in the library."""
        if cid not in self.collections:
            return
        gone = set(self.descendants(cid))
        for c in gone:
            del self.collections[c]
        for paper in self.papers.values():
            paper["collections"] = [c for c in paper["collections"] if c not in gone]


def _clean_name(name: str) -> str:
    return " ".join(str(name).split())[:MAX_NAME] or "Untitled"


def _number(value) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
