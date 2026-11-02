"""Read the papers and collections of a local Zotero library, so the GUI can show them.

Zotero keeps everything in zotero.sqlite in its data directory (usually ~/Zotero), and
PDFs in its storage/ folder or, for linked files, wherever they are. While Zotero runs it
holds an exclusive lock on the database, so it's copied to a temporary folder first and
read from there. Zotero itself is never changed.

The annotations made in Zotero's PDF reader are read too (highlights, underlines, notes and
image selections; ink drawings are left out), so the GUI can show them next to its own.

The data directory is found in the Zotero profile's prefs.js; config.toml's `zotero_dir`
overrides it.
"""

import configparser
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# itemAttachments.linkMode
IMPORTED_FILE, IMPORTED_URL, LINKED_FILE = 0, 1, 2


@dataclass
class Item:
    key: str            # Zotero's item key, unique within its library
    library: int
    title: str
    authors: list[str]  # last names
    year: str
    path: Path          # the PDF
    collections: list[int] = field(default_factory=list)


@dataclass
class Snapshot:
    data_dir: Path
    libraries: list[tuple[int, str]]               # (libraryID, name)
    collections: list[tuple[int, str, int | None, int]]  # (collectionID, name, parentID, libraryID)
    items: list[Item]
    missing: int  # items whose PDF isn't on this computer (e.g. not downloaded yet)
    annotations: dict[str, list["Annotation"]] = field(default_factory=dict)  # by the PDF's path


@dataclass(frozen=True)
class Annotation:
    """An annotation as Zotero stores it. Positions are in PDF points, from the bottom left."""
    key: str
    type: str           # "highlight", "underline", "note", "text" or "image"
    color: str
    text: str           # the highlighted text
    comment: str
    page: int           # page index
    rects: tuple[tuple[float, float, float, float], ...]  # (x1, y1, x2, y2)
    created: str        # ISO 8601, UTC


# itemAnnotations.type; 4 (ink) isn't shown
ANNOTATION_TYPES = {1: "highlight", 2: "note", 3: "image", 5: "underline", 6: "text"}


def _profiles_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Zotero"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Zotero" / "Zotero"
    return Path.home() / ".zotero" / "zotero"


def _default_profile() -> Path | None:
    """The profile Zotero starts with: the one marked Default=1 in profiles.ini."""
    base = _profiles_dir()
    ini = configparser.ConfigParser(interpolation=None)
    try:
        ini.read(base / "profiles.ini", encoding="utf-8")
    except (OSError, configparser.Error):
        return None
    profiles = [s for s in ini.sections() if s.lower().startswith("profile") and ini[s].get("Path")]
    if not profiles:
        return None
    chosen = next((s for s in profiles if ini[s].get("Default") == "1"), profiles[0])
    path = Path(ini[chosen]["Path"])
    return base / path if ini[chosen].get("IsRelative", "1") == "1" else path


def _prefs(profile: Path | None) -> dict[str, str]:
    """String preferences from prefs.js, e.g. extensions.zotero.dataDir."""
    if profile is None:
        return {}
    try:
        text = (profile / "prefs.js").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    prefs = {}
    for name, value in re.findall(r'user_pref\("(extensions\.zotero\.[\w.]+)",\s*("(?:[^"\\]|\\.)*")\);', text):
        try:
            prefs[name] = json.loads(value)
        except ValueError:
            pass
    return prefs


def find(override: str | None = None) -> tuple[Path, Path | None] | None:
    """(data directory, base directory for linked files), or None if there's no Zotero library."""
    prefs = _prefs(_default_profile())
    base = prefs.get("extensions.zotero.baseAttachmentPath")
    base_dir = Path(base) if base else None
    candidates = [Path(override).expanduser()] if override else [
        Path(prefs["extensions.zotero.dataDir"]) if prefs.get("extensions.zotero.dataDir") else None,
        Path.home() / "Zotero",
    ]
    for candidate in candidates:
        if candidate and (candidate / "zotero.sqlite").is_file():
            return candidate, base_dir
    return None


def database_mtime(data_dir: Path) -> float:
    """Changes whenever Zotero writes to its database; for noticing when to read it again."""
    times = []
    for name in ("zotero.sqlite", "zotero.sqlite-wal"):
        try:
            times.append((data_dir / name).stat().st_mtime)
        except OSError:
            pass
    return max(times, default=0.0)


def read(data_dir: Path, base_dir: Path | None = None) -> Snapshot:
    """Read Zotero's papers that have a PDF, with their collections. Raises OSError or
    sqlite3.Error if the database can't be read."""
    with tempfile.TemporaryDirectory(prefix="paper2audio-zotero-") as temp:
        copy = Path(temp) / "zotero.sqlite"
        shutil.copyfile(data_dir / "zotero.sqlite", copy)
        wal = data_dir / "zotero.sqlite-wal"  # recent changes not yet written to the database
        if wal.is_file() and wal.stat().st_size:
            shutil.copyfile(wal, Path(temp) / "zotero.sqlite-wal")
        connection = sqlite3.connect(copy)
        try:
            return _read(connection, data_dir, base_dir)
        finally:
            connection.close()


def _read(db: sqlite3.Connection, data_dir: Path, base_dir: Path | None) -> Snapshot:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    fields_table = "fieldsCombined" if "fieldsCombined" in tables else "fields"
    deleted_items = "SELECT itemID FROM deletedItems" if "deletedItems" in tables else "SELECT NULL WHERE 0"
    deleted_collections = ("SELECT collectionID FROM deletedCollections" if "deletedCollections" in tables
                           else "SELECT NULL WHERE 0")

    # Libraries: the personal one and groups (feeds are left out).
    libraries = []
    groups = dict(db.execute("SELECT libraryID, name FROM groups")) if "groups" in tables else {}
    for library_id, kind in db.execute("SELECT libraryID, type FROM libraries ORDER BY libraryID"):
        if kind == "user":
            libraries.append((library_id, "My Library"))
        elif kind == "group":
            libraries.append((library_id, groups.get(library_id, f"Group {library_id}")))
    library_ids = {library_id for library_id, _ in libraries}

    # The first PDF of each item that's on this computer. Standalone PDFs are items themselves.
    pdfs: dict[int, Path] = {}
    attachment_paths: dict[int, Path] = {}  # every PDF on this computer, for its annotations
    missing = set()
    rows = db.execute(f"""
        SELECT a.itemID, a.parentItemID, a.linkMode, a.path, i.key, i.libraryID
        FROM itemAttachments a JOIN items i ON i.itemID = a.itemID
        WHERE a.contentType = 'application/pdf' AND a.linkMode IN (0, 1, 2)
          AND a.itemID NOT IN ({deleted_items})
          AND (a.parentItemID IS NULL OR a.parentItemID NOT IN ({deleted_items}))
        ORDER BY a.itemID""")
    for attachment_id, parent_id, link_mode, stored, attachment_key, library_id in rows:
        item_id = parent_id or attachment_id
        if library_id not in library_ids or not stored:
            continue
        path = _attachment_path(stored, link_mode, attachment_key, data_dir, base_dir)
        if path is None or not path.is_file():
            if item_id not in pdfs:
                missing.add(item_id)
            continue
        attachment_paths[attachment_id] = path
        if item_id in pdfs:
            continue
        missing.discard(item_id)
        pdfs[item_id] = path
    keys = {item_id: (key, library_id) for item_id, key, library_id in _chunked(
        db, "SELECT itemID, key, libraryID FROM items WHERE itemID IN ({})", list(pdfs))}

    values: dict[int, dict[str, str]] = {}
    for item_id, name, value in _chunked(db, f"""
            SELECT d.itemID, f.fieldName, v.value FROM itemData d
            JOIN {fields_table} f ON f.fieldID = d.fieldID JOIN itemDataValues v ON v.valueID = d.valueID
            WHERE f.fieldName IN ('title', 'date') AND d.itemID IN ({{}})""", list(pdfs)):
        values.setdefault(item_id, {})[name] = str(value)
    authors: dict[int, list[str]] = {}
    for item_id, first, last in _chunked(db, """
            SELECT ic.itemID, c.firstName, c.lastName FROM itemCreators ic JOIN creators c ON c.creatorID = ic.creatorID
            WHERE ic.itemID IN ({}) ORDER BY ic.itemID, ic.orderIndex""", list(pdfs)):
        authors.setdefault(item_id, []).append(last or first or "")

    collections = [(cid, name, parent, library_id) for cid, name, parent, library_id in db.execute(f"""
        SELECT collectionID, collectionName, parentCollectionID, libraryID FROM collections
        WHERE collectionID NOT IN ({deleted_collections}) ORDER BY collectionName COLLATE NOCASE""")
        if library_id in library_ids]
    in_collections: dict[int, list[int]] = {}
    known = {c[0] for c in collections}
    for cid, item_id in db.execute("SELECT collectionID, itemID FROM collectionItems"):
        if cid in known:
            in_collections.setdefault(item_id, []).append(cid)

    items = []
    for item_id, path in pdfs.items():
        key, library_id = keys[item_id]
        date = values.get(item_id, {}).get("date", "")
        year = re.search(r"\d{4}", date)
        items.append(Item(
            key=key, library=library_id,
            title=values.get(item_id, {}).get("title") or path.stem,
            authors=[a for a in authors.get(item_id, []) if a],
            year=year.group(0) if year else "",
            path=path,
            collections=in_collections.get(item_id, []),
        ))
    annotations = _annotations(db, attachment_paths, deleted_items) if "itemAnnotations" in tables else {}
    return Snapshot(data_dir, libraries, collections, items, len(missing), annotations)


def _annotations(db: sqlite3.Connection, attachment_paths: dict[int, Path], deleted_items: str) -> dict[str, list[Annotation]]:
    result: dict[str, list[Annotation]] = {}
    for attachment_id, kind, key, color, text, comment, position, added in _chunked(db, f"""
            SELECT a.parentItemID, a.type, i.key, a.color, a.text, a.comment, a.position, i.dateAdded
            FROM itemAnnotations a JOIN items i ON i.itemID = a.itemID
            WHERE a.parentItemID IN ({{}}) AND a.itemID NOT IN ({deleted_items})
            ORDER BY a.parentItemID, a.sortIndex""", list(attachment_paths)):
        if kind not in ANNOTATION_TYPES:
            continue
        try:
            place = json.loads(position)
            page = int(place["pageIndex"])
            rects = tuple(tuple(float(v) for v in r[:4]) for r in place.get("rects") or [] if len(r) >= 4)
        except (TypeError, ValueError, KeyError):
            continue
        if not rects:
            continue
        result.setdefault(str(attachment_paths[attachment_id]), []).append(Annotation(
            key=key, type=ANNOTATION_TYPES[kind], color=color or "", text=text or "", comment=comment or "",
            page=page, rects=rects, created=(added or "").replace(" ", "T") + "Z" if added else ""))
    return result


def _attachment_path(stored: str, link_mode: int, key: str, data_dir: Path, base_dir: Path | None) -> Path | None:
    if stored.startswith("storage:"):
        return data_dir / "storage" / key / stored.removeprefix("storage:")
    if stored.startswith("attachments:"):
        return base_dir / stored.removeprefix("attachments:") if base_dir else None
    if link_mode == LINKED_FILE:
        return Path(stored)
    return None


def _chunked(db: sqlite3.Connection, query: str, ids: list[int], size: int = 500):
    """Run a query with `IN ({})` for many IDs, in chunks (SQLite limits the number of parameters)."""
    for start in range(0, len(ids), size):
        chunk = ids[start:start + size]
        yield from db.execute(query.format(",".join("?" * len(chunk))), chunk)
