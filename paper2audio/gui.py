"""Desktop GUI: a PDF reader with annotations next to the conversion settings, in a native window,
with tabs for several papers and a library (with Zotero's papers merged in) as the first tab.

Run with `paper2audio-gui [paper.pdf]`. Needs the optional GUI dependencies:
`uv tool install -e '.[gui]'`.

Built with NiceGUI in native mode (pywebview), so the window is drawn by the system's web
engine. The reader itself (paper2audio/reader/reader.js) runs in the web view; this module
serves it page images rendered with pypdfium2, text positions for selecting text, the
outline, and annotation storage, keeps the library (library.py, zotero.py), and runs the
conversion.
"""

import html
import io
import json
import platform
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from importlib import metadata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    import pypdfium2 as pdfium
    from fastapi import Body, HTTPException, Response
    from fastapi.responses import FileResponse, PlainTextResponse
    from nicegui import app, ui
    from nicegui.elements.mixins.text_element import TextElement
except ImportError as e:  # GUI extras are optional; the CLI works without them
    sys.exit(f"The GUI needs extra packages ({e.name}). Install them with: uv tool install -e '.[gui]'")

from . import annotations, audio, config, document, hardware, models, pipeline, requirements, server, share, tts, zotero
from . import library as library_store
from .intro import Intro
from .setup_dialog import SetupDialog

READER_DIR = Path(__file__).parent / "reader"
APP_NAME = "paper2audio"
APP_ICON = Path(__file__).parent / "assets" / "icon.png"  # drawn by tools/make_icon.py
MAX_RENDER_WIDTH = 8192  # pixels; enough for 400% zoom on a Retina screen
SPEEDS = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
NO_MODEL = "none"  # the rule-based cleanup, without a language model
ZOTERO_CHECK_SECONDS = 10  # how often to look whether Zotero's database changed
ISSUES_URL = "https://github.com/AeneasTews/paper2audio/issues/new"
MAX_ISSUE_URL = 7500  # browsers and GitHub reject much longer URLs

# Lucide icons (ISC license, see reader/icons/SOURCE.md), inlined so they follow the text color.
ICONS = {p.stem: p.read_text(encoding="utf-8").split("-->", 1)[-1].strip()
         for p in (READER_DIR / "icons").glob("*.svg")}


def icon(name: str, cls: str = "") -> str:
    return f'<span class="p2a-icon {cls}">{ICONS[name]}</span>'


app.add_static_files("/p2a-reader", READER_DIR, max_cache_age=0)

# --- serving the document, its text, annotations and the finished audio --------------------


@dataclass
class Document:
    path: Path
    annotation_id: str = ""  # hash of the PDF, for annotation storage


# Files are served by random token, never by path, so the local web server can't be used
# to read arbitrary files.
_documents: dict[str, Document] = {}
_audio_files: dict[str, Path] = {}
_pdfium_lock = threading.Lock()  # pdfium is not thread-safe


def _document(token: str) -> Document:
    document = _documents.get(token)
    if document is None:
        raise HTTPException(404)
    return document


@lru_cache(maxsize=96)
def _render_page(path: Path, mtime: float, index: int, width: int) -> bytes:
    with _pdfium_lock:
        pdf = pdfium.PdfDocument(path)
        try:
            page = pdf[index]
            image = page.render(scale=width / page.get_width()).to_pil()
        finally:
            pdf.close()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")  # lossless: no JPEG fringes around text
    return buffer.getvalue()


@lru_cache(maxsize=64)
def _page_text(path: Path, mtime: float, index: int) -> list:
    """Runs of text with their position: [x, y, width, height, font size, text].

    x, width and font size are fractions of the page width; y and height of its height.
    """
    with _pdfium_lock:
        pdf = pdfium.PdfDocument(path)
        try:
            page = pdf[index]
            left, bottom, right, top = page.get_cropbox()
            width, height = right - left, top - bottom
            textpage = page.get_textpage()
            runs = []
            for i in range(textpage.count_rects()):
                l, b, r, t = textpage.get_rect(i)
                text = textpage.get_text_bounded(l, b, r, t)
                text = text.replace("\x02", "-").replace("\r", "").replace("\n", " ")  # \x02: hyphen at a line end
                if not text.strip():
                    continue
                runs.append([
                    round((l - left) / width, 5), round((top - t) / height, 5),
                    round((r - l) / width, 5), round((t - b) / height, 5),
                    round((t - b) / width, 5), text,
                ])
        finally:
            pdf.close()
    return runs


@app.get("/pdf/{token}/{index}.png")
def _page_image(token: str, index: int, w: int = 1200) -> Response:
    path = _document(token).path
    width = max(60, min(w, MAX_RENDER_WIDTH))
    try:
        png = _render_page(path, path.stat().st_mtime, index, width)
    except (OSError, IndexError, pdfium.PdfiumError) as e:
        raise HTTPException(404, str(e)) from None
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/pdf/{token}/{index}/text")
def _text_runs(token: str, index: int) -> list:
    path = _document(token).path
    try:
        return _page_text(path, path.stat().st_mtime, index)
    except (OSError, IndexError, pdfium.PdfiumError) as e:
        raise HTTPException(404, str(e)) from None


@app.get("/pdf/{token}/outline")
def _outline(token: str) -> list:
    path = _document(token).path
    with _pdfium_lock:
        pdf = pdfium.PdfDocument(path)
        try:
            items = []
            for bookmark in pdf.get_toc(max_depth=6):
                destination = bookmark.get_dest()
                page = destination.get_index() if destination else None
                items.append([bookmark.level, bookmark.get_title(), page])
        finally:
            pdf.close()
    return items


# Annotations made in Zotero, by the PDF's path; read-only, shown next to the reader's own.
_zotero_annotations: dict[str, list[zotero.Annotation]] = {}


@app.get("/annotations/{token}")
def _get_annotations(token: str) -> list:
    document = _document(token)
    if not document.annotation_id:
        return []
    own = annotations.load(document.annotation_id)
    from_zotero = _zotero_annotations.get(str(document.path))
    if from_zotero:
        try:
            own += _convert_zotero(document.path, document.path.stat().st_mtime, tuple(from_zotero))
        except (OSError, pdfium.PdfiumError):
            pass
    return own


@lru_cache(maxsize=32)
def _convert_zotero(path: Path, mtime: float, items: tuple[zotero.Annotation, ...]) -> list[dict]:
    """Zotero's annotations in the reader's form: positions as fractions of the page, from the top left."""
    with _pdfium_lock:
        pdf = pdfium.PdfDocument(path)
        try:
            boxes = {}
            for index in {a.page for a in items if 0 <= a.page < len(pdf)}:
                page = pdf[index]
                boxes[index] = (page.get_cropbox(), page.get_rotation())
        finally:
            pdf.close()

    def corners(index: int, x1: float, y1: float, x2: float, y2: float) -> tuple[float, float, float, float]:
        (left, bottom, right, top), rotation = boxes[index]
        width, height = (right - left) or 1, (top - bottom) or 1
        points = []
        for x, y in ((x1, y1), (x2, y2)):
            u, v = (x - left) / width, (top - y) / height  # unrotated, from the top left
            points.append({90: (1 - v, u), 180: (1 - u, 1 - v), 270: (v, 1 - u)}.get(rotation % 360, (u, v)))
        (a, b), (c, d) = points
        return min(a, c), min(b, d), abs(c - a), abs(d - b)

    result = []
    for a in items:
        if a.page not in boxes:
            continue
        color = a.color if annotations.COLOR.fullmatch(a.color) else annotations.DEFAULT_COLOR
        common = {"id": f"zotero-{a.key}", "zotero": True, "color": color, "page": a.page,
                  "text": a.text, "comment": a.comment, "created": a.created}
        if a.type in ("note", "text"):  # a note, or text typed onto the page: shown as a note
            x, y, _, _ = corners(a.page, a.rects[0][0], a.rects[0][3], a.rects[0][0], a.rects[0][3])
            result.append({**common, "type": "note", "position": [x, y],
                           "comment": a.comment or a.text, "text": ""})
        else:  # highlight, underline, or an image selection (shown as a highlighted area)
            result.append({**common, "type": "underline" if a.type == "underline" else "highlight",
                           "rects": [list(corners(a.page, *r)) for r in a.rects]})
    return result


@app.put("/annotations/{token}")
def _put_annotations(token: str, body: list = Body(...)) -> dict:
    document = _document(token)
    if not document.annotation_id:
        raise HTTPException(400, "annotations are only stored for PDFs")
    body = [a for a in body if not (isinstance(a, dict) and a.get("zotero"))]  # Zotero's own aren't copied
    try:
        annotations.save(document.annotation_id, body, document.path.name)
    except (OSError, ValueError) as e:
        raise HTTPException(500, str(e)) from None
    return {"ok": True}


# Math in preview text is wrapped in these, so the reader can set it in italics.
MATH_START, MATH_END = "\u2063", "\u2064"  # invisible separator / invisible plus
# What the structured formats are called in the pane
FORMAT_NAMES = {".html": "HTML", ".htm": "HTML", ".xhtml": "HTML", ".xml": "JATS XML", ".epub": "EPUB", ".tex": "LaTeX"}


@lru_cache(maxsize=8)
def _read_blocks(path: Path, mtime: float) -> list:
    """A structured paper's blocks for the preview: [kind, text, in the appendix]."""
    blocks = pipeline.STRUCTURED_READERS[path.suffix.lower()](path)
    result = []
    for block in blocks:
        text = block.text.replace(document.NOTE_MARK, "")
        text = document.CITATION.sub(r"\1", text)  # keep citations, drop the markers around them
        # Math as readable Unicode (e.g. "√(d_k)") instead of LaTeX, marked for italics.
        if block.kind == "equation":
            text = document._latex_to_text(text)
        else:
            text = document.MATH.sub(lambda m: f"{MATH_START}{document._latex_to_text(m.group(1))}{MATH_END}", text)
        text = text.strip() if block.kind in ("table", "equation") else " ".join(text.split())
        if text:
            result.append([block.kind, text, block.appendix])
    return result


@app.get("/blocks/{token}")
def _blocks(token: str) -> list:
    path = _document(token).path
    try:
        return _read_blocks(path, path.stat().st_mtime)
    except Exception as e:  # malformed files: show the reason in the preview
        raise HTTPException(422, f"{type(e).__name__}: {e}") from None


@app.get("/textfile/{token}")
def _text_file(token: str) -> PlainTextResponse:
    path = _document(token).path
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))


@app.get("/audio/{token}")
def _audio_file(token: str) -> FileResponse:
    path = _audio_files.get(token)
    if path is None or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


# --- static parts of the window -------------------------------------------------------------

_MOD = "⌘" if sys.platform == "darwin" else "Ctrl+"
_SHIFT_MOD = "⇧⌘" if sys.platform == "darwin" else "Ctrl+Shift+"


def _button(action: str, icon_name: str, title: str, needs_pdf: bool = True, extra: str = "", cls: str = "") -> str:
    needs = " data-needs-pdf disabled" if needs_pdf else ""
    return (f'<button class="p2a-btn {cls}" data-action="{action}" title="{html.escape(title)}"{needs}>'
            f'{icon(icon_name)}{extra}</button>')


TOP_HTML = f"""
<div id="p2a-tabs">
  <div id="p2a-tablist"></div>
  {_button("quick-open", "search", f"Find a paper… ({_MOD}K)", needs_pdf=False)}
  {_button("emit-open", "folder-open", f"Open a paper… ({_MOD}O)", needs_pdf=False)}
  {_button("emit-open_remote", "link", f"Open from arXiv, DOI or URL… ({_SHIFT_MOD}O)", needs_pdf=False)}
</div>
<div id="p2a-toolbar">
  <div class="p2a-toolbar-group">
    {_button("sidebar", "panel-left", "Toggle sidebar", needs_pdf=False, cls="p2a-on")}
    <div class="p2a-sep"></div>
    {_button("zoom-out", "zoom-out", f"Zoom out ({_MOD}−)")}
    {_button("zoom-in", "zoom-in", f"Zoom in ({_MOD}+), or pinch on the trackpad")}
    {_button("fit", "move-horizontal", f"Fit to width ({_MOD}0)")}
    <span id="p2a-zoom"></span>
    <div class="p2a-sep"></div>
    {_button("prev", "chevron-up", "Previous page")}
    {_button("next", "chevron-down", "Next page")}
    <input id="p2a-page-input" type="text" inputmode="numeric" disabled>
    <span id="p2a-page-count"></span>
  </div>
  <div class="p2a-toolbar-center">
    {_button("tool-highlight", "highlighter", "Highlight text")}
    {_button("tool-underline", "underline", "Underline text")}
    {_button("tool-note", "sticky-note", "Add note")}
    <button class="p2a-btn" data-action="colors" title="Annotation color" data-needs-pdf disabled>
      <span class="p2a-swatch" id="p2a-color-swatch" style="background:#ffd400"></span>{icon("chevron-down")}
    </button>
  </div>
  <div class="p2a-toolbar-group">
    {_button("emit-intro", "circle-help", "Introduction: features, models and requirements", needs_pdf=False)}
    {_button("emit-shortcuts", "keyboard", f"Keyboard shortcuts ({_MOD}/)", needs_pdf=False)}
    {_button("emit-report", "bug", "Report an issue", needs_pdf=False)}
    {_button("emit-setup", "wrench", "Check and install ollama and models", needs_pdf=False)}
    {_button("pane", "panel-right", "Toggle audio pane", needs_pdf=False, cls="p2a-on")}
  </div>
</div>
<div id="p2a-colors"></div>
"""

SIDEBAR_AND_VIEW_HTML = f"""
<div id="p2a-sidebar">
  <div class="p2a-sidebar-tabs">
    {_button("tab-thumbnails", "layout-grid", "Pages", needs_pdf=False)}
    {_button("tab-annotations", "message-square-text", "Annotations", needs_pdf=False, cls="p2a-on")}
    {_button("tab-outline", "list-tree", "Outline", needs_pdf=False)}
  </div>
  <div class="p2a-sidebar-search" id="p2a-sidebar-search">
    <label class="p2a-search">{icon("search")}<input id="p2a-annotation-search" type="text" placeholder="Search annotations"></label>
  </div>
  <div class="p2a-sidebar-body" id="p2a-sidebar-body"></div>
</div>
<div id="p2a-view">
  <div id="p2a-popup"></div>
</div>
"""

# The audio player, in the Audio section. Greyed out until there's audio; reader.js runs it.
PLAYER_HTML = f"""
<div class="p2a-player p2a-disabled" id="p2a-player">
  <audio id="p2a-audio" preload="metadata"></audio>
  <div class="p2a-player-top">
    <span class="p2a-player-name" id="p2a-player-name">No audio yet</span>
    <button class="p2a-btn" data-action="audio-reveal" title="Show in folder" disabled>{icon("folder-open")}</button>
  </div>
  <div class="p2a-player-seek">
    <span id="p2a-audio-time">0:00</span>
    <input type="range" id="p2a-audio-seek" min="0" max="1000" step="1" value="0" disabled>
    <span id="p2a-audio-duration">0:00</span>
  </div>
  <div class="p2a-player-controls">
    <button class="p2a-btn p2a-skip" data-action="audio-back" title="Back 10 seconds" disabled>{icon("rotate-ccw")}<span class="p2a-skip-label">10</span></button>
    <button class="p2a-btn p2a-play" data-action="audio-play" title="Play" disabled>{icon("play")}</button>
    <button class="p2a-btn p2a-skip" data-action="audio-forward" title="Forward 10 seconds" disabled>{icon("rotate-cw")}<span class="p2a-skip-label">10</span></button>
  </div>
  <div class="p2a-player-share">
    <button class="p2a-push" data-action="audio-phone" title="Download it on your phone over Wi-Fi, with a QR code" disabled>{icon("smartphone")} Send to phone</button>
    <button class="p2a-push" data-action="audio-save" title="Save a copy of the audio file somewhere else" disabled>{icon("download")} Save a copy…</button>
  </div>
</div>
"""

# The Library tab: collections on the left, their papers in the middle, details on the right.
LIBRARY_HTML = f"""
<div id="p2a-library">
  <div class="p2a-lib-toolbar">
    {_button("lib-new-collection", "folder-plus", "New collection", needs_pdf=False)}
    {_button("lib-add-files", "file-plus", "Add papers to the library…", needs_pdf=False)}
    {_button("lib-sync", "refresh-cw", "Reload the Zotero library", needs_pdf=False)}
    <div class="p2a-grow"></div>
    {_button("emit-intro", "circle-help", "Introduction: features, models and requirements", needs_pdf=False)}
    <label class="p2a-search">{icon("search")}<input id="p2a-lib-search" type="text" placeholder="Filter by title, author, year"></label>
  </div>
  <div class="p2a-lib-main">
    <div id="p2a-lib-tree"></div>
    <div id="p2a-lib-items"></div>
    <div id="p2a-lib-info"></div>
  </div>
</div>
<div id="p2a-menu"></div>
<div id="p2a-picker"><div class="p2a-picker-box">
  <label class="p2a-search">{icon("search")}<input id="p2a-picker-input" type="text" placeholder="Find a paper by title, author, year or file name"></label>
  <div id="p2a-picker-list"></div>
  <div class="p2a-picker-hint"><kbd>↑</kbd><kbd>↓</kbd> choose <kbd>↩</kbd> open in a tab <kbd>esc</kbd> close</div>
</div></div>
"""


def _head_html() -> str:
    return (
        '<link rel="stylesheet" href="/p2a-reader/reader.css">'
        f"<script>window.P2A_ICONS = {json.dumps(ICONS)};</script>"
        '<script src="/p2a-reader/reader.js"></script>'
    )


# --- small native controls --------------------------------------------------------------------


class NativeSelect:
    """An HTML <select>; looks like the system's own popup menu in the web view."""

    def __init__(self, options: list, value: str, on_change=None):
        self.value = value
        self._on_change = on_change
        self.element = ui.element("select")
        self.element.on("change", self._changed, js_handler="(e) => emit(e.target.value)")
        self.set_options(options, value)

    def _changed(self, event) -> None:
        self.value = event.args
        if self._on_change:
            self._on_change()

    def options_label(self) -> str:
        """The label of the selected option."""
        return self._labels.get(self.value, self.value)

    def set_options(self, options: list, value: str) -> None:
        """options: values, or (value, label) pairs."""
        self.value = value
        self._labels = {}
        self.element.clear()
        with self.element:
            for option in options:
                option_value, label = option if isinstance(option, tuple) else (option, option)
                self._labels[option_value] = label
                item = TextElement(tag="option", text=label)
                item.props["value"] = option_value
                if option_value == value:
                    item.props["selected"] = True


class Section:
    """A collapsible section of the right pane, with a header like 'Audio ▾'."""

    def __init__(self, section_id: str, title: str, icon_name: str, collapsed: bool = False):
        self.element = ui.element("div").classes("p2a-section" + (" p2a-collapsed" if collapsed else ""))
        self.element.props["id"] = section_id
        with self.element:
            ui.html(f'{icon(icon_name)}<span class="p2a-grow">{html.escape(title)}</span>'
                    f'{icon("chevron-down", "p2a-twisty")}', sanitize=False).classes("p2a-section-head").on(
                "click", js_handler="(e) => e.currentTarget.parentElement.classList.toggle('p2a-collapsed')")
            self.body = ui.element("div").classes("p2a-section-body")


def _row(label: str):
    """A 'label: value' row; returns the value cell to fill."""
    with ui.element("div").classes("p2a-row"):
        ui.label(label).classes("p2a-label")
        return ui.element("div").classes("p2a-value")


def _set_disabled(element, disabled: bool) -> None:
    if bool(element.props.get("disabled")) != disabled:
        element.props["disabled"] = disabled
        element.update()


class Cancelled(Exception):
    pass


@dataclass
class Tab:
    """An open paper: what the right pane shows for it."""
    id: str  # the paper's library ID
    path: Path
    source: str | None  # arXiv ID, DOI or URL it was downloaded from, if any
    token: str = ""
    title: str = ""
    subtitle: str = ""
    pages: str = "—"
    output: str = ""  # where to save the audio
    audio: str | None = None  # token of the finished audio, for the player
    status: str = ""
    progress: float = 0.0


_library: library_store.Library | None = None


def _get_library() -> library_store.Library:
    global _library
    if _library is None:
        _library = library_store.Library()
    return _library


class PaperApp:
    """State and UI of the window. Background work talks to it through an event queue."""

    def __init__(self, initial_file: Path | None):
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.options_loader: threading.Thread | None = None
        self.initial_file = initial_file
        self.tabs: dict[str, Tab] = {}  # in the order of the tab strip
        self.active: Tab | None = None  # None: the Library tab
        self.converting: Tab | None = None
        self.library = _get_library()
        self.catalog: dict[str, dict] = {}  # papers the library view shows, by ID; only these can be opened from it
        self.zotero_snapshot: zotero.Snapshot | None = None
        self.zotero_mtime = 0.0
        self.zotero_status = ""
        self.zotero_loader: threading.Thread | None = None
        self.fetcher: threading.Thread | None = None
        self.fetch_status = None  # the open-remote dialog's status label, while it's open
        self.phone_status = None  # the send-to-phone dialog's status label, while it's open
        self.output_path: Path | None = None
        self.language_models: list[str] = []
        self.recommended_model: str | None = None
        self.requirements: list[requirements.Requirement] = []
        self.setup_prompted = False  # the setup dialog opens by itself only once per window
        self.open_setup_after_load = False
        self.log_lines: list[str] = []
        self.setup_was_running = False

    # --- layout ------------------------------------------------------------------------

    def build(self) -> None:
        # Defaults from the config file (~/.config/paper2audio/config.toml), like the CLI.
        try:
            self.defaults = config.load()
            config_problem = ""
        except ValueError as e:
            self.defaults = config.Settings()
            config_problem = f"Config file ignored: {e}"
        self.sections = set(self.defaults.sections)
        ui.add_head_html(_head_html())
        with ui.element("div").props("id=p2a-app"):
            ui.html(TOP_HTML, sanitize=False).style("display: contents")
            with ui.element("div").props("id=p2a-main"):
                ui.html(SIDEBAR_AND_VIEW_HTML, sanitize=False).style("display: contents")
                self._build_pane()
            ui.html(LIBRARY_HTML, sanitize=False).style("display: contents")
        self.setup = SetupDialog(on_installed=self._refresh_options)
        self.intro = Intro(icon, on_close=self._intro_closed)
        self.setup_after_intro = False
        ui.on("p2a_open", self._choose_input)
        ui.on("p2a_open_remote", self._show_open_remote)
        ui.on("p2a_setup", self._open_setup)
        ui.on("p2a_convert", self._on_convert)
        ui.on("p2a_close", self._close_window)
        ui.on("p2a_shortcuts", _show_shortcuts)
        ui.on("p2a_report", self._show_report)
        ui.on("p2a_reveal", self._reveal)
        ui.on("p2a_audio_save", self._save_audio_copy)
        ui.on("p2a_audio_phone", self._send_to_phone)
        ui.on("p2a_intro", self.intro.show)
        ui.on("p2a_notify", lambda e: ui.notify(e.args, type="warning"))
        ui.on("p2a_tab", lambda e: self._activate(e.args, from_reader=True))
        ui.on("p2a_tab_close", lambda e: self._close_tab(e.args))
        ui.on("p2a_lib", self._library_action)
        ui.timer(0.1, self._poll_events)
        ui.timer(0.2, self._startup, once=True)  # after reader.js is ready
        ui.timer(ZOTERO_CHECK_SECONDS, self._check_zotero)
        self._update_state()
        self._refresh_options()
        if config_problem:
            ui.notify(config_problem, type="warning", multi_line=True, timeout=0, close_button=True)

    def _build_pane(self) -> None:
        with ui.element("div").props("id=p2a-pane"):
            with ui.element("div").classes("p2a-pane-body"):
                with ui.element("div").classes("p2a-pane-header"):
                    self.title_label = ui.label("No paper open").classes("p2a-pane-title")
                    self.subtitle_label = ui.label("Open a paper to convert it to audio.").classes("p2a-pane-subtitle")

                audio_section = Section("p2a-sec-audio", "Audio", "audio-lines")
                with audio_section.body:
                    with _row("Voice"):
                        self.voice_select = NativeSelect([self.defaults.voice], self.defaults.voice)
                    with _row("Speed"):
                        speeds = sorted(set(SPEEDS) | {float(self.defaults.speed)})
                        self.speed_select = NativeSelect([(f"{v:g}", f"{v:g}×") for v in speeds], f"{self.defaults.speed:g}")
                    with _row("Language model"):
                        self.model_select = NativeSelect(self._model_options(), self._default_model_value(),
                                                         on_change=self._update_state)
                    self.hint = ui.label("").classes("p2a-hint")
                    with ui.element("label").classes("p2a-check p2a-section-option"):
                        self.disclaimer_box = ui.element("input").props("type=checkbox")
                        self.disclaimer_box.props["checked"] = self.defaults.disclaimer
                        self.disclaimer = self.defaults.disclaimer
                        self.disclaimer_box.on("change", lambda e: setattr(self, "disclaimer", bool(e.args)),
                                               js_handler="(e) => emit(e.target.checked)")
                        with ui.element("span"):
                            ui.label("Disclaimer").classes("p2a-section-name")
                            ui.label("Start with a short spoken note that the audio is machine-generated "
                                     "and can contain mistakes").classes("p2a-hint")
                    with _row("Save as"):
                        self.output_input = ui.element("input").props('type=text placeholder="paper.mp3"')
                        self.output_input.on("change", self._output_changed, js_handler="(e) => emit(e.target.value)")
                        self.output_value = ""
                        self.output_button = ui.html(icon("folder-open"), tag="button", sanitize=False) \
                            .classes("p2a-btn").props('title="Choose where to save"').on("click", self._choose_output)
                    with ui.element("div").classes("p2a-actions"):
                        self.convert_button = ui.html("", tag="button", sanitize=False).classes("p2a-push p2a-primary") \
                            .on("click", self._on_convert)
                    with ui.element("div").classes("p2a-progress"):
                        self.progress_bar = ui.element("div").style("width: 0%")
                    self.status = ui.label("").classes("p2a-status")
                    ui.html(PLAYER_HTML, sanitize=False).style("display: contents")

                sections_section = Section("p2a-sec-sections", "Sections", "list-tree")
                with sections_section.body:
                    ui.label("What to read aloud. For PDFs, some need a language model; HTML, XML, "
                             "EPUB and LaTeX mark them in the file itself.").classes("p2a-hint")
                    self.section_boxes = {}
                    for name, (_, description) in config.SECTIONS.items():
                        with ui.element("label").classes("p2a-check p2a-section-option"):
                            box = ui.element("input").props("type=checkbox")
                            box.props["checked"] = name in self.defaults.sections
                            box.on("change", lambda e, name=name: self._section_changed(name, e.args),
                                   js_handler="(e) => emit(e.target.checked)")
                            self.section_boxes[name] = box
                            with ui.element("span"):
                                ui.label(name.capitalize()).classes("p2a-section-name")
                                ui.label(description + (" (needs a language model)" if name == "summary"
                                                        else " (PDFs: needs a language model)" if name in config.LLM_ONLY_SECTIONS
                                                        else ""))\
                                    .classes("p2a-hint")


                log_section = Section("p2a-sec-log", "Log", "list-tree", collapsed=True)
                with log_section.body:
                    self.log = TextElement(tag="pre", text="").classes("p2a-log")

                info_section = Section("p2a-sec-info", "Info", "file-text")
                with info_section.body:
                    with _row("Source"):
                        self.info_source = ui.label("—")
                    with _row("File"):
                        self.info_file = ui.label("—")
                    with _row("Pages"):
                        self.info_pages = ui.label("—")
                    with _row("Folder"):
                        self.info_folder = ui.label("—")


    # --- documents -----------------------------------------------------------------------

    async def _choose_input(self) -> None:
        paths = await app.native.main_window.create_file_dialog(
            allow_multiple=True,
            file_types=("Papers (" + ";".join(f"*{s}" for s in pipeline.SUPPORTED_INPUTS) + ")", "All files (*.*)"),
        )
        for i, path in enumerate(paths or []):
            self._open(Path(path), activate=i == len(paths) - 1)

    def _startup(self) -> None:
        """Show the library, reopen the tabs of last time, and read Zotero's library.
        The first time, also the introduction."""
        if not self.library.intro_seen:
            self.intro.show()
        self._push_library()
        session = self.library.session
        for path in session["tabs"]:
            if Path(path).is_file():
                self._open(Path(path), activate=False, record=False)
        if self.initial_file:
            self._open(self.initial_file)
        else:
            active = library_store.paper_id(session["active"]) if session["active"] else None
            self._activate(active if active in self.tabs else "library")
        self._sync_zotero()

    def _open(self, path: Path, source: str | None = None, activate: bool = True, record: bool = True) -> None:
        """Open a paper in a new tab, or switch to its tab if it's open already.
        source: the arXiv ID, DOI or URL it was downloaded from, if any."""
        path = path.expanduser().resolve()
        tab_id = library_store.paper_id(path)
        if tab_id in self.tabs:
            if activate:
                self._activate(tab_id)
            return
        known = self.library.papers.get(tab_id) or {}
        source = source or known.get("source")
        if not path.is_file():
            if source:  # a download that's no longer in the cache: download it again
                self._start_fetch(source)
            else:
                ui.notify(f"“{path.name}” isn’t at {path.parent} anymore.", type="warning", multi_line=True)
            return
        tab = Tab(tab_id, path, source, token=secrets.token_urlsafe(8))
        entry = self.catalog.get(tab_id) or {}
        suffix = path.suffix.lower()
        title, authors = path.stem, []
        message = None
        try:
            if suffix == ".pdf":
                document_ = Document(path, annotations.document_id(path))
                sizes, meta_title, authors = _pdf_info(path)
                _documents[tab.token] = document_
                title = meta_title or path.stem
                tab.subtitle, tab.pages = f"PDF · {len(sizes)} pages", str(len(sizes))
                payload = {"kind": "pdf", "doc": {"token": tab.token, "pages": sizes}}
            elif suffix in pipeline.STRUCTURED_READERS:
                _documents[tab.token] = Document(path)
                try:
                    blocks = _read_blocks(path, path.stat().st_mtime)
                    title = next((text for kind, text, _ in blocks if kind == "title"), path.stem)[:200]
                except Exception:
                    pass  # the preview shows the error
                tab.subtitle = "arXiv HTML" if path.name.startswith("arxiv-") and suffix == ".html" else FORMAT_NAMES[suffix]
                payload = {"kind": "blocks", "url": f"/blocks/{tab.token}"}
            else:
                _documents[tab.token] = Document(path)
                tab.subtitle = "Markdown" if suffix == ".md" else "Text"
                payload = {"kind": "text", "url": f"/textfile/{tab.token}"}
        except (OSError, pdfium.PdfiumError) as e:
            tab.subtitle = "Can't open this file"
            message = f"Can’t open {path.name}: {e}"
            payload = {"kind": "message", "message": message}
        if entry.get("zotero") is not None or known.get("title"):
            # Zotero's metadata, or what was recorded from it earlier, is better than the PDF's
            title = entry.get("title") or known["title"]
        tab.title = title
        tab.output = str(_default_output(path, title, downloaded=source is not None))
        # Audio made earlier: the file recorded for this paper, or one at the default place.
        for earlier in (known.get("audio"), tab.output):
            if earlier and Path(earlier).is_file():
                tab.audio = secrets.token_urlsafe(8)
                _audio_files[tab.audio] = Path(earlier)
                tab.output = earlier
                break
        self.tabs[tab_id] = tab
        ui.run_javascript(f"P2A.openTab({json.dumps({'id': tab_id, 'title': title, 'activate': activate, **payload})})")
        if record and message is None:
            self.library.record(path, title, authors, source=source, opened=True)
            self._save_library()
        if activate:
            self._activate(tab_id)
        else:
            self._save_session()

    def _activate(self, tab_id: str, from_reader: bool = False) -> None:
        """Show a tab ("library" for the Library). from_reader: the reader switched already."""
        tab = self.tabs.get(tab_id)
        self.active = tab
        if not from_reader:
            ui.run_javascript(f"P2A.activate({json.dumps(tab.id if tab else 'library')}, false)")
        self._render_pane()
        self._save_session()
        if tab is None:
            self._check_zotero()

    def _close_tab(self, tab_id: str) -> None:
        tab = self.tabs.pop(tab_id, None)
        if tab is None:
            return
        _documents.pop(tab.token, None)
        if tab.audio:
            _audio_files.pop(tab.audio, None)
        if self.active is tab:
            self.active = None  # the reader activates another tab next
        self._save_session()

    def _save_session(self) -> None:
        self.library.session = {"tabs": [str(t.path) for t in self.tabs.values()],
                                "active": str(self.active.path) if self.active else None}
        self._save_library(push=False)

    def _save_library(self, push: bool = True) -> None:
        try:
            self.library.save()
        except OSError as e:
            ui.notify(f"Couldn't save the library: {e}", type="negative")
        if push:
            self._push_library()

    def _render_pane(self) -> None:
        """Show the active paper in the right pane."""
        tab = self.active
        if tab is not None:
            self.title_label.set_text(tab.title)
            self.subtitle_label.set_text(tab.subtitle)
            self.info_source.set_text(tab.source or "Local file")
            self.info_file.set_text(tab.path.name)
            self.info_folder.set_text(str(tab.path.parent))
            self.info_pages.set_text(tab.pages)
            self._set_output(tab.output)
            audio_file = _audio_files.get(tab.audio) if tab.audio else None
            ui.run_javascript(f"P2A.setAudio({json.dumps(f'/audio/{tab.audio}' if audio_file else None)}, "
                              f"{json.dumps(audio_file.name if audio_file else '')})")
        self._render_status()
        self._update_state()

    def _render_status(self) -> None:
        """The conversion's progress; while another paper converts, that one's."""
        converting = self.converting if self._busy() else None
        if converting is not None and converting is not self.active:
            self.status.set_text(f"Converting “{converting.title}”: {converting.status}")
            self._set_progress(converting.progress)
        elif self.active is not None:
            self.status.set_text(self.active.status)
            self._set_progress(self.active.progress)

    # --- the library ---------------------------------------------------------------------

    def _push_library(self) -> None:
        """Send the library, merged with Zotero's, to the library view and the quick search."""
        papers: dict[str, dict] = {}
        for pid, paper in self.library.papers.items():
            papers[pid] = {
                "id": pid, "title": paper["title"], "authors": paper["authors"], "year": paper["year"],
                "path": paper["path"], "added": paper["added"], "opened": paper["opened"],
                "collections": paper["collections"], "zotero": None, "zcollections": [],
            }
        snapshot = self.zotero_snapshot
        for item in snapshot.items if snapshot else []:
            pid = library_store.paper_id(item.path)
            entry = papers.setdefault(pid, {
                "id": pid, "path": str(item.path), "added": None, "opened": None, "collections": [],
            })
            entry.update(title=item.title, authors=item.authors, year=item.year or entry.get("year", ""),
                         zotero=item.library, zcollections=item.collections)
        for entry in papers.values():
            entry["missing"] = not Path(entry["path"]).is_file()
        self.catalog = papers
        data = {
            "papers": list(papers.values()),
            "collections": [{"id": cid, **c} for cid, c in self.library.collections.items()],
            "zotero": None if snapshot is None and not self.zotero_status else {
                "status": self.zotero_status,
                "libraries": [{"id": lid, "name": name} for lid, name in snapshot.libraries] if snapshot else [],
                "collections": [{"id": cid, "name": name, "parent": parent, "library": lid}
                                for cid, name, parent, lid in snapshot.collections] if snapshot else [],
            },
            "home": str(Path.home()),
        }
        ui.run_javascript(f"P2A.setLibrary({json.dumps(data)})")

    async def _library_action(self, event) -> None:
        """Changes made in the library view. Papers are only referred to by ID, never by path."""
        args = event.args if isinstance(event.args, dict) else {}
        action = args.get("action")
        ids = [i for i in args.get("ids") or [] if isinstance(i, str) and i in self.catalog]
        collection = args.get("collection") if isinstance(args.get("collection"), str) else None
        name = str(args.get("name") or "")
        if action == "open":
            for i, pid in enumerate(ids):
                self._open(Path(self.catalog[pid]["path"]), activate=i == len(ids) - 1)
            return
        if action == "reveal":
            if ids and not self.catalog[ids[0]]["missing"]:
                _reveal_in_file_manager(Path(self.catalog[ids[0]]["path"]))
            return
        if action == "sync":
            self._sync_zotero()
            return
        if action == "add_files":
            await self._add_files(collection)
            return
        if action == "delete_collection":
            self._confirm_delete_collection(collection)
            return
        if action == "add":
            for pid in ids:  # Zotero papers become local papers once added
                entry = self.catalog[pid]
                self.library.record(Path(entry["path"]), entry["title"], entry["authors"], entry["year"])
            self.library.add(ids, collection)
        elif action == "remove_from_collection" and collection:
            self.library.remove_from_collection(ids, collection)
        elif action == "remove":
            self.library.remove_from_library(ids)
        elif action == "forget":
            self.library.forget_opened(ids)
        elif action == "new_collection":
            parent = args.get("parent") if isinstance(args.get("parent"), str) else None
            self.library.create_collection(str(args.get("id") or ""), name, parent)
        elif action == "rename_collection" and collection:
            self.library.rename_collection(collection, name)
        elif action == "move_collection" and collection:
            parent = args.get("parent") if isinstance(args.get("parent"), str) else None
            if not self.library.move_collection(collection, parent):
                ui.notify("A collection can't be moved into itself.", type="warning")
        else:
            return
        self._save_library()

    async def _add_files(self, collection: str | None) -> None:
        paths = await app.native.main_window.create_file_dialog(
            allow_multiple=True,
            file_types=("Papers (" + ";".join(f"*{s}" for s in pipeline.SUPPORTED_INPUTS) + ")", "All files (*.*)"),
        )
        ids = []
        for path in paths or []:
            path = Path(path).resolve()
            title, authors = path.stem, []
            if path.suffix.lower() == ".pdf":
                try:
                    _, meta_title, authors = _pdf_info(path)
                    title = meta_title or title
                except (OSError, pdfium.PdfiumError):
                    pass
            ids.append(self.library.record(path, title, authors))
        if ids:
            self.library.add(ids, collection)
            self._save_library()

    def _confirm_delete_collection(self, cid: str | None) -> None:
        if cid not in self.library.collections:
            return
        inner = len(self.library.descendants(cid)) - 1
        with ui.dialog() as dialog, ui.card().classes("p2a-dialog"):
            ui.label(f"Delete the collection “{self.library.collections[cid]['name']}”"
                     + (f" and the {inner} collection{'s' * (inner > 1)} in it?" if inner else "?"))
            ui.label("The papers stay in your library, and the files aren't touched.").classes("p2a-hint")

            def delete() -> None:
                dialog.close()
                self.library.delete_collection(cid)
                self._save_library()

            with ui.element("div").classes("p2a-actions"):
                ui.html("Cancel", tag="button", sanitize=False).classes("p2a-push").on("click", dialog.close)
                ui.html("Delete", tag="button", sanitize=False).classes("p2a-push p2a-danger").on("click", delete)
        dialog.open()

    # --- Zotero --------------------------------------------------------------------------

    def _sync_zotero(self) -> None:
        if (self.defaults.zotero_dir or "").lower() == "none":
            return
        if self.zotero_loader and self.zotero_loader.is_alive():
            return
        self.zotero_loader = threading.Thread(target=self._load_zotero, daemon=True)
        self.zotero_loader.start()

    def _load_zotero(self) -> None:
        """Runs in a background thread."""
        found = zotero.find(self.defaults.zotero_dir)
        if found is None:
            self.events.put(("zotero", None, 0.0, ""))  # no Zotero: the library view leaves it out
            return
        mtime = zotero.database_mtime(found[0])
        try:
            snapshot = zotero.read(*found)
        except (OSError, sqlite3.Error) as e:
            self.events.put(("zotero", None, mtime, f"Couldn't read the Zotero library: {e}"))
            return
        status = f"{snapshot.missing} paper{'s' * (snapshot.missing != 1)} without a downloaded PDF not shown" \
            if snapshot.missing else ""
        self.events.put(("zotero", snapshot, mtime, status))

    def _check_zotero(self) -> None:
        """Read Zotero's library again when it changed, while the Library tab or one of
        Zotero's papers is shown (for its annotations)."""
        snapshot = self.zotero_snapshot
        tab = self.active
        if not snapshot or zotero.database_mtime(snapshot.data_dir) == self.zotero_mtime:
            return
        if tab is None or (self.catalog.get(tab.id) or {}).get("zotero") is not None \
                or str(tab.path) in _zotero_annotations:
            self._sync_zotero()

    # --- opening by arXiv ID, DOI or URL --------------------------------------------------

    def _show_open_remote(self) -> None:
        if self.fetcher and self.fetcher.is_alive():
            return
        with ui.dialog() as dialog, ui.card().classes("p2a-dialog p2a-report"):
            ui.label("Open from arXiv, DOI or URL").classes("p2a-dialog-title")
            ui.label("arXiv papers are read from their HTML version, which keeps every formula; "
                     "open-access papers from Europe PMC. Downloads are cached in ~/.cache/paper2audio.") \
                .classes("p2a-hint")
            value = {"source": ""}
            field = ui.element("input").props('type=text placeholder="arxiv:1706.03762, 10.1371/journal.pbio.1002533 or https://…"')
            field.on("input", lambda e: value.update(source=e.args), js_handler="(e) => emit(e.target.value)")
            field.on("keydown", lambda e: submit() if e.args == "Enter" else None, js_handler="(e) => emit(e.key)")
            self.fetch_status = ui.label("").classes("p2a-hint")

            def submit() -> None:
                source = value["source"].strip()
                if not source or (self.fetcher and self.fetcher.is_alive()):
                    return
                self.fetch_status.set_text("Looking up…")
                _set_disabled(open_button, True)
                self._start_fetch(source)

            with ui.element("div").classes("p2a-actions"):
                ui.html("Cancel", tag="button", sanitize=False).classes("p2a-push").on("click", dialog.close)
                open_button = ui.html(f"{icon('link')} Open", tag="button", sanitize=False) \
                    .classes("p2a-push p2a-primary").on("click", submit)
            self.fetch_dialog, self.fetch_open_button = dialog, open_button
        dialog.on("hide", lambda: setattr(self, "fetch_status", None))
        dialog.open()
        ui.run_javascript("setTimeout(() => document.querySelector('.p2a-report input[type=text]')?.focus(), 100)")

    def _start_fetch(self, source: str) -> None:
        if self.fetcher and self.fetcher.is_alive():
            return
        if self.fetch_status is None:
            ui.notify(f"Downloading {source}…")
        self.fetcher = threading.Thread(target=self._fetch, args=(source,), daemon=True)
        self.fetcher.start()

    def _fetch(self, source: str) -> None:
        """Runs in a background thread: download the paper, then open it."""
        try:
            path = pipeline.resolve(source, lambda message, fraction: self.events.put(("fetch_progress", message)))
        except RuntimeError as e:
            self.events.put(("fetch_error", str(e)))
        else:
            self.events.put(("fetched", path, source))

    def _set_output(self, value: str) -> None:
        self.output_value = value
        if self.active is not None:
            self.active.output = value
        self.output_input.props["value"] = value
        self.output_input.update()

    def _output_changed(self, event) -> None:
        self.output_value = event.args
        if self.active is not None:
            self.active.output = event.args

    def _section_changed(self, name: str, checked) -> None:
        (self.sections.add if checked else self.sections.discard)(name)

    def _default_model_value(self) -> str:
        """The configured model, "none", or "" for picking one automatically."""
        return self.defaults.llm_model or ""

    def _model_options(self) -> list[tuple[str, str]]:
        if self.recommended_model is None:
            automatic = "Automatic"
        else:
            candidates = hardware.MODELS[hardware.MODELS.index(self.recommended_model):]
            picked = next((m for m in candidates if any(m == n.removesuffix(":latest") or m == n
                                                        for n in self.language_models)), None)
            automatic = f"Automatic ({picked})" if picked else "Automatic (none installed)"
        options = [("", automatic)]
        options += [(name, name) for name in self.language_models]
        configured = self.defaults.llm_model
        if configured and configured != NO_MODEL and configured not in self.language_models:
            options.append((configured, f"{configured} (not installed)"))
        options.append((NO_MODEL, "None (rule-based cleanup)"))
        return options

    def _settings(self) -> config.Settings:
        return config.Settings(
            voice=self.voice_select.value,
            speed=float(self.speed_select.value),
            llm_model=self.model_select.value or None,
            sections=set(self.sections),
            disclaimer=self.disclaimer,
        )

    async def _choose_output(self) -> None:
        current = Path(self.output_value) if self.output_value else None
        result = await app.native.main_window.create_file_dialog(
            dialog_type=_save_dialog(),
            directory=str(current.parent) if current else "",
            save_filename=current.name if current else "paper.mp3",
        )
        if result:
            self._set_output(result if isinstance(result, str) else result[0])

    def _close_window(self) -> None:
        if not self._busy():
            app.shutdown()  # closes the window; shutdown() then stops background work
            return
        with ui.dialog() as dialog, ui.card().classes("p2a-dialog"):
            ui.label("A conversion is running. Stop it and close the window?")
            with ui.element("div").classes("p2a-actions"):
                ui.html("Cancel", tag="button", sanitize=False).classes("p2a-push").on("click", dialog.close)
                ui.html("Stop and close", tag="button", sanitize=False).classes("p2a-push p2a-danger") \
                    .on("click", lambda: (dialog.close(), app.shutdown()))
        dialog.open()

    # --- reporting issues ----------------------------------------------------------------

    def _show_report(self) -> None:
        with ui.dialog() as dialog, ui.card().classes("p2a-dialog p2a-report"):
            ui.label("Report an issue").classes("p2a-dialog-title")
            ui.label("This opens a new issue on GitHub in your browser, filled in with what you write "
                     "here. You can review it there before submitting (a GitHub account is needed).") \
                .classes("p2a-hint")
            fields = {"title": "", "body": "", "include": True}
            title = ui.element("input").props('type=text placeholder="Short summary, e.g. Highlight is offset on page 3"')
            title.on("input", lambda e: fields.update(title=e.args), js_handler="(e) => emit(e.target.value)")
            body = ui.element("textarea").props('rows=6 placeholder="What happened? What did you expect? Steps to reproduce…"')
            body.on("input", lambda e: fields.update(body=e.args), js_handler="(e) => emit(e.target.value)")
            with ui.element("label").classes("p2a-check"):
                include = ui.element("input").props("type=checkbox checked")
                include.on("change", lambda e: fields.update(include=bool(e.args)),
                           js_handler="(e) => emit(e.target.checked)")
                ui.label("Include system info and the conversion log (your home folder is shown as ~)")

            def submit() -> None:
                if not fields["title"].strip():
                    ui.notify("Please add a short title.", type="warning")
                    return
                url = _issue_url(fields["title"].strip(), fields["body"].strip(),
                                 self._diagnostics() if fields["include"] else "")
                webbrowser.open(url)
                dialog.close()
                ui.notify("Opened GitHub in your browser.")

            with ui.element("div").classes("p2a-actions"):
                ui.html("Cancel", tag="button", sanitize=False).classes("p2a-push").on("click", dialog.close)
                ui.html(f"{icon('bug')} Open on GitHub", tag="button", sanitize=False) \
                    .classes("p2a-push p2a-primary").on("click", submit)
        dialog.open()
        ui.run_javascript("setTimeout(() => document.querySelector('.p2a-report input[type=text]')?.focus(), 100)")

    def _diagnostics(self) -> str:
        try:
            version = metadata.version("paper2audio")
        except metadata.PackageNotFoundError:
            version = "unknown"
        ollama = "not found"
        if shutil.which("ollama"):
            try:
                result = subprocess.run(["ollama", "--version"], capture_output=True, text=True, timeout=3,
                                        stdin=subprocess.DEVNULL, errors="replace")
                output = (result.stdout or result.stderr).strip()
                line = output.splitlines()[-1] if output else "?"
                ollama = "".join(ch for ch in line if ch.isprintable())[:80] or "?"
            except (OSError, ValueError, subprocess.TimeoutExpired, IndexError):
                ollama = "error running ollama --version"
        lines = [
            f"- paper2audio: {version}",
            f"- OS: {platform.platform()}",
            f"- Python: {platform.python_version()}",
            f"- ollama: {ollama}",
            f"- Voice: {self.voice_select.value}; speed: {self.speed_select.value}; "
            f"language model: {self.model_select.value or 'automatic'}",
            f"- Sections: {', '.join(sorted(self.sections))}",
            f"- Input type: {self.active.path.suffix if self.active else 'none'}"
            f"{' (downloaded)' if self.active and self.active.source else ''}",
            f"- Open tabs: {len(self.tabs)}",
        ]
        missing = [r.title for r in self.requirements if not r.installed]
        if missing:
            lines.append(f"- Missing: {', '.join(missing)}")
        text = "\n".join(lines)
        if self.log_lines:
            text += "\n\n<details><summary>Conversion log</summary>\n\n```\n" + "\n".join(self.log_lines[-40:]) + "\n```\n</details>"
        return text.replace(str(Path.home()), "~")

    def _audio_for(self, token) -> tuple[Path, str] | None:
        """The audio file for a player token, and the paper's title."""
        path = _audio_files.get(str(token))
        if path is None or not path.is_file():
            ui.notify("The audio file isn't there anymore.", type="warning")
            return None
        tab = next((t for t in self.tabs.values() if t.audio == str(token)), None)
        return path, tab.title if tab else path.stem

    async def _save_audio_copy(self, event) -> None:
        found = self._audio_for(event.args)
        if found is None:
            return
        path, title = found
        folder = Path.home() / "Downloads"
        name = re.sub(r"[^\w\s.,()-]", "", title).strip()[:80] or path.stem
        result = await app.native.main_window.create_file_dialog(
            dialog_type=_save_dialog(), directory=str(folder if folder.is_dir() else Path.home()),
            save_filename=name + path.suffix)
        if not result:
            return
        target = Path(result if isinstance(result, str) else result[0])
        try:
            if target.resolve() != path.resolve():
                shutil.copyfile(path, target)
        except OSError as e:
            ui.notify(f"Couldn't save the copy: {e}", type="negative", multi_line=True)
            return
        ui.notify(f"Saved “{target.name}”.")

    def _send_to_phone(self, event) -> None:
        found = self._audio_for(event.args)
        if found is None:
            return
        path, title = found
        name = (re.sub(r"[^\w\s.,()-]", "", title).strip()[:80] or path.stem) + path.suffix
        try:
            sharing = share.PhoneShare(path, name, lambda state: self.events.put(("phone", state)))
        except OSError as e:
            ui.notify(f"Can't send to a phone: {e}.", type="warning")
            return
        try:
            import segno
            qr = segno.make(sharing.url, error="m").svg_inline(scale=5, dark="#000", light="#fff", border=2)
        except ImportError:  # optional: the link alone also works
            qr = ""
        size_mb = path.stat().st_size / 1e6
        with ui.dialog() as dialog, ui.card().classes("p2a-dialog p2a-phone"):
            ui.label("Send to your phone").classes("p2a-dialog-title")
            with ui.element("div").classes("p2a-phone-body"):
                if qr:
                    ui.html(qr, sanitize=False).classes("p2a-qr")
                with ui.element("div").classes("p2a-phone-text"):
                    ui.label("Scan the code with your phone's camera and open the link. "
                             "The phone has to be on the same Wi-Fi as this computer.")
                    ui.label("It opens a page with a player and a Download button. iPhone: the download "
                             "is then in the Files app, under Downloads. Android: in the Downloads folder.") \
                        .classes("p2a-hint")
                    ui.label(f"{name} · {size_mb:.1f} MB" if size_mb < 10 else f"{name} · {size_mb:.0f} MB").classes("p2a-hint")
                    self.phone_status = ui.label("Waiting for your phone…").classes("p2a-phone-status")
            ui.label(sharing.url).classes("p2a-hint p2a-phone-url")
            ui.label("The file is shared on this network only while this window is open.").classes("p2a-hint")
            with ui.element("div").classes("p2a-actions"):
                ui.html("Done", tag="button", sanitize=False).classes("p2a-push p2a-primary").on("click", dialog.close)

        def closed() -> None:
            sharing.stop()
            self.phone_status = None

        dialog.on("hide", closed)
        dialog.open()

    def _reveal(self, event) -> None:
        """Show the audio file the player has loaded (it may be another tab's, still playing)."""
        path = _audio_files.get(str(event.args))
        if path is not None and path.is_file():
            _reveal_in_file_manager(path)

    # --- conversion --------------------------------------------------------------------

    def _on_convert(self) -> None:
        if self._busy():
            self.cancel.set()
            self.status.set_text("Stopping after the current step…")
            _set_disabled(self.convert_button, True)
            return
        output_path = Path(self.output_value or "").expanduser()
        error = self._validate(self.active.path if self.active else None, output_path)
        if error:
            ui.notify(error, type="warning")
        elif output_path.exists():
            self._confirm_overwrite(output_path)
        else:
            self._start(output_path)

    def _confirm_overwrite(self, output_path: Path) -> None:
        with ui.dialog() as dialog, ui.card().classes("p-5 gap-4"):
            ui.label(f"“{output_path.name}” already exists. Replace it?")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.html("Cancel", tag="button", sanitize=False).classes("p2a-push").on("click", dialog.close)
                ui.html("Replace", tag="button", sanitize=False).classes("p2a-push p2a-danger").on(
                    "click", lambda: (dialog.close(), self._start(output_path)))
        dialog.open()

    def _start(self, output_path: Path) -> None:
        settings = self._settings()
        if self.active is None:
            return
        self.output_path = output_path
        self.converting = tab = self.active
        tab.status, tab.progress = "", 0.0
        self.cancel.clear()
        self.log_lines = []
        self.log.set_text("")
        self.worker = threading.Thread(
            target=self._run,
            args=(tab.path, output_path, settings, self._tags(tab)),
            daemon=True,
        )
        self.worker.start()
        self._render_pane()

    @staticmethod
    def _validate(input_path: Path | None, output_path: Path) -> str | None:
        if input_path is None or not input_path.is_file():
            return "Open a paper first."
        if input_path.suffix.lower() not in pipeline.SUPPORTED_INPUTS:
            return f"The paper must be one of: {', '.join(pipeline.SUPPORTED_INPUTS)}"
        if output_path.suffix.lower() not in audio.SUPPORTED_FORMATS:
            return f"The audio file must end in one of: {', '.join(audio.SUPPORTED_FORMATS)}"
        if not output_path.parent.is_dir():
            return f"The folder {output_path.parent} doesn't exist."
        return None

    def _tags(self, tab: Tab) -> dict[str, str]:
        """Title and authors for the audio file, so a phone's player shows the paper's name."""
        authors = (self.catalog.get(tab.id) or self.library.papers.get(tab.id) or {}).get("authors") or []
        artist = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
        return {"title": tab.title, "artist": artist, "album": "paper2audio"}

    def _run(self, input_path: Path, output_path: Path, settings: config.Settings, tags: dict[str, str]) -> None:
        """Runs in the worker thread; talks to the UI only through the event queue."""

        def progress(message: str, fraction: float | None) -> None:
            if self.cancel.is_set():
                raise Cancelled
            self.events.put(("progress", message, fraction))

        try:
            minutes = pipeline.convert(input_path, output_path, settings, progress, tags)
        except Cancelled:
            self.events.put(("cancelled",))
        except RuntimeError as e:
            self.events.put(("error", str(e)))
        except Exception as e:  # anything unexpected should still end up in the window
            self.events.put(("error", f"{type(e).__name__}: {e}"))
        else:
            self.events.put(("done", minutes))

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(*self.events.get_nowait())
        except queue.Empty:
            pass
        if self.setup.running != self.setup_was_running:  # an install started or finished
            self.setup_was_running = self.setup.running
            self._update_state()

    def _log(self, line: str) -> None:
        self.log_lines = (self.log_lines + [line])[-300:]
        self.log.set_text("\n".join(self.log_lines))

    def _set_progress(self, fraction: float) -> None:
        self.progress_bar.style(replace=f"width: {fraction * 100:.1f}%")

    def _handle_event(self, kind: str, *args) -> None:
        if kind == "options":
            self._set_options(*args)
            return
        if kind == "fetch_progress":
            if self.fetch_status:
                self.fetch_status.set_text(args[0])
            return
        if kind == "fetch_error":
            if self.fetch_status:
                self.fetch_status.set_text(f"Couldn't open it: {args[0]}")
                _set_disabled(self.fetch_open_button, False)
            else:
                ui.notify(f"Couldn't open the paper: {args[0]}", type="negative", multi_line=True)
            return
        if kind == "fetched":
            if self.fetch_status:
                self.fetch_dialog.close()
            self._open(*args)
            return
        if kind == "phone":
            if self.phone_status:
                state = args[0]
                self.phone_status.set_text("Sent ✓ You can close this window." if state == "done"
                                           else "Downloading…" if state == "started" else f"The server {state}.")
            return
        if kind == "zotero":
            self.zotero_snapshot, self.zotero_mtime, self.zotero_status = args
            found = self.zotero_snapshot.annotations if self.zotero_snapshot else {}
            if found != _zotero_annotations:
                _zotero_annotations.clear()
                _zotero_annotations.update(found)
                ui.run_javascript("P2A.refreshZotero()")  # open papers show the changes
            self._push_library()
            return
        tab = self.converting
        if tab is None:
            return
        if kind == "progress":
            message, fraction = args
            self._log(message)
            if not message.startswith(" "):  # indented lines are details, keep the main status
                tab.status = message
            if fraction is not None:
                tab.progress = fraction
            self._render_status()
            return
        self.worker = None
        if kind == "done":
            whole_minutes, seconds = divmod(round(args[0] * 60), 60)
            tab.status, tab.progress = f"Done: {whole_minutes}:{seconds:02d} of audio.", 1.0
            if tab.audio:
                _audio_files.pop(tab.audio, None)
            tab.audio = secrets.token_urlsafe(8)
            _audio_files[tab.audio] = self.output_path
            self.library.set_audio(tab.path, self.output_path)
            self._save_library()
            if tab is not self.active:
                ui.notify(f"Finished “{tab.title}”: {self.output_path.name}")
        elif kind == "cancelled":
            tab.status, tab.progress = "Stopped.", 0.0
            self._log("Stopped.")
        elif kind == "error":
            tab.status = "Failed."
            self._log(f"Error: {args[0]}")
            ui.notify(f"Converting “{tab.title}” failed: {args[0]}", type="negative", multi_line=True,
                      timeout=0, close_button=True)
        self.converting = None
        self._render_pane()

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _loading_options(self) -> bool:
        return self.options_loader is not None and self.options_loader.is_alive()

    def _update_state(self) -> None:
        busy, loading = self._busy(), self._loading_options()
        locked = busy or loading
        _set_disabled(self.output_input, busy)
        _set_disabled(self.output_button, busy)
        _set_disabled(self.voice_select.element, locked)
        _set_disabled(self.speed_select.element, busy)
        _set_disabled(self.model_select.element, locked)
        for box in [*self.section_boxes.values(), self.disclaimer_box]:
            _set_disabled(box, busy)
        # Converting while the options load or setup runs could race with the ollama server.
        _set_disabled(self.convert_button, not busy and (loading or self.setup.running))
        label = f"{icon('square')} Stop" if busy else f"{icon('audio-lines')} Convert to audio"
        if self.convert_button.content != label:
            self.convert_button.set_content(label)
            self.convert_button.classes(replace="p2a-push " + ("p2a-danger" if busy else "p2a-primary"))

    def shutdown(self) -> None:
        """Called when the window closes: stop work, so a temporary ollama server is shut down."""
        self.cancel.set()
        self.setup.stop()
        for thread in (self.worker, self.options_loader):
            if thread is not None:
                thread.join(timeout=120)

    # --- voices, models and setup ------------------------------------------------------

    def _open_setup(self) -> None:
        """Re-check what's installed, then show the setup dialog."""
        if self.setup.running or self.setup.is_open:
            self.setup.show(self.requirements)
            return
        self.open_setup_after_load = True
        if not self._loading_options():
            self._refresh_options()

    def _intro_closed(self) -> None:
        if not self.library.intro_seen:
            self.library.intro_seen = True
            self._save_library(push=False)
        if self.setup_after_intro:
            self.setup_after_intro = False
            self.setup.show(self.requirements)

    def _refresh_options(self) -> None:
        """Check the setup and ask ollama for language models and phonon's voices, in the background."""
        if self._loading_options() or self._busy():
            return
        self.hint.set_text("Checking ollama, voices and models…")
        self.options_loader = threading.Thread(target=self._load_options, daemon=True)
        self.options_loader.start()
        self._update_state()

    def _load_options(self) -> None:
        """Runs in a background thread; starts a temporary ollama server if none is running."""
        voices: list[str] = []
        language_models: list[str] = []
        problems: list[str] = []
        reqs: list[requirements.Requirement] | None = None
        recommended = hardware.recommended_model()
        try:
            with server.ollama_server(lambda message, fraction: None):
                reqs = requirements.check()
                try:
                    voices = models.voices(tts.MODEL)
                except RuntimeError:
                    problems.append(f"The {tts.MODEL} speech model isn't installed. Use the setup button (wrench) in the toolbar.")
                try:
                    language_models = models.language_models()
                except RuntimeError as e:
                    problems.append(f"Couldn't list models ({e}).")
        except FileNotFoundError:
            problems.append("ollama isn't installed. Use the setup button (wrench) in the toolbar.")
        except (RuntimeError, OSError) as e:
            problems.append(f"Couldn't reach ollama: {e}")
        if reqs is None:
            reqs = requirements.check()
        self.events.put(("options", voices, language_models, recommended, problems, reqs))

    def _set_options(self, voices: list[str], language_models: list[str], recommended: str | None,
                     problems: list[str], reqs: list[requirements.Requirement]) -> None:
        self.options_loader = None
        self.requirements = reqs
        missing = any(not r.installed for r in reqs)
        if self.setup.is_open or self.open_setup_after_load or (missing and not self.setup_prompted):
            self.setup_prompted = True
            self.open_setup_after_load = False
            if self.intro.is_open:
                self.setup_after_intro = True  # the introduction ends by pointing to it
            else:
                self.setup.show(reqs)

        voices = voices or [self.defaults.voice]  # the configured voice, if listing failed
        self.voice_select.set_options(voices, _pick(voices, self.voice_select.value, self.defaults.voice))

        self.language_models = language_models
        self.recommended_model = recommended
        options = self._model_options()
        current = self.model_select.value
        self.model_select.set_options(options, current if any(v == current for v, _ in options) else "")
        if self.model_select.options_label().startswith("Automatic (none") and not problems:
            problems.append("No language model installed: formulas and figures are skipped. "
                            "Use the setup button (wrench) in the toolbar to install one.")
        self.hint.set_text("\n".join(problems))
        self._update_state()


SHORTCUTS = [
    ("Open a paper", f"{_MOD}O"),
    ("Open from arXiv, DOI or URL", f"{_SHIFT_MOD}O"),
    ("Find a paper and open it", f"{_MOD}K"),
    ("Close the tab (on the Library: the window)", f"{_MOD}W"),
    ("Next / previous tab", "⌃Tab  ⌃⇧Tab"),
    ("Library / first, second… tab", f"{_MOD}1  {_MOD}2  …  {_MOD}9"),
    ("Convert to audio / stop", f"{_MOD}↩"),
    ("Zoom in / out / fit width", f"{_MOD}+  {_MOD}−  {_MOD}0"),
    ("Zoom smoothly", f"pinch  or  {_MOD}scroll"),
    ("Go to page", f"{_MOD}{'⌥' if sys.platform == 'darwin' else 'Alt+'}G"),
    ("Previous / next page", "←  →"),
    ("Scroll by a screen", "Space  ⇧Space  PgUp  PgDn"),
    ("First / last page", "Home  End"),
    ("Highlight / underline / note tool", f"{_MOD}{'⌥' if sys.platform == 'darwin' else 'Alt+'}1  2  3"),
    ("Copy selected text", f"{_MOD}C"),
    ("Delete selected annotation", "Delete"),
    ("Cancel tool or selection", "Esc"),
    ("Show this list", f"{_MOD}/  or  ?"),
]


def _show_shortcuts() -> None:
    with ui.dialog() as dialog, ui.card().classes("p2a-dialog"):
        ui.label("Keyboard shortcuts").classes("p2a-dialog-title")
        with ui.element("div").classes("p2a-shortcuts"):
            for action, keys in SHORTCUTS:
                ui.label(action)
                # Lowercase words ("or", "pinch") are plain text, everything else a key.
                ui.html("".join(f"<span>{html.escape(k)}</span>" if k.islower() else f"<kbd>{html.escape(k)}</kbd>"
                                for k in keys.split("  ")), sanitize=False)
        with ui.element("div").classes("p2a-actions"):
            ui.html("Close", tag="button", sanitize=False).classes("p2a-push p2a-primary").on("click", dialog.close)
    dialog.open()


def _issue_url(title: str, body: str, diagnostics: str) -> str:
    """A link to GitHub's new-issue form, filled in. Long text is shortened to fit in a URL:
    first the diagnostics, then the description."""
    def build(body_text: str, diagnostics_text: str) -> str:
        full = body_text + (f"\n\n### Environment\n\n{diagnostics_text}" if diagnostics_text else "")
        full += "\n\n_Reported from the paper2audio app._"
        return f"{ISSUES_URL}?{urllib.parse.urlencode({'title': title[:200], 'body': full})}"

    for _ in range(40):  # each round cuts a third, so this always ends well before 40
        url = build(body, diagnostics)
        if len(url) <= MAX_ISSUE_URL:
            return url
        if len(diagnostics) > 300:
            diagnostics = diagnostics[: len(diagnostics) * 2 // 3] + "\n…(shortened)"
        elif diagnostics:
            diagnostics = ""
        else:
            body = body[: len(body) * 2 // 3] + "\n…(shortened)"
    return build("", "")


def _pdf_info(path: Path) -> tuple[list, str, list[str]]:
    """A PDF's page sizes, and its title and authors' last names if its metadata has them."""
    with _pdfium_lock:
        pdf = pdfium.PdfDocument(path)
        try:
            sizes = [list(pdf[i].get_size()) for i in range(len(pdf))]
            metadata = pdf.get_metadata_dict()
        finally:
            pdf.close()
    title = (metadata.get("Title") or "").strip()
    return sizes, title if 3 <= len(title) <= 200 else "", _last_names(metadata.get("Author") or "")


def _last_names(authors: str) -> list[str]:
    """'Ashish Vaswani; Noam Shazeer and Niki Parmar' -> ['Vaswani', 'Shazeer', 'Parmar']."""
    names = [n.strip() for n in re.split(r";|\band\b|&", authors) if n.strip()]
    if len(names) == 1 and names[0].count(",") >= 2:  # 'A. Vaswani, N. Shazeer, N. Parmar'
        names = [n.strip() for n in names[0].split(",") if n.strip()]
    result = []
    for name in names[:50]:
        last = name.split(",")[0] if "," in name else name.split()[-1]  # 'Vaswani, Ashish' or 'Ashish Vaswani'
        if 1 < len(last) <= 60:
            result.append(last.strip())
    return result


def _default_output(path: Path, title: str, downloaded: bool) -> Path:
    """Where to save the audio by default: next to a local paper; for a download (which sits
    in the cache folder), in the Downloads folder, named after the paper's title."""
    if not downloaded:
        return path.with_suffix(".mp3")
    folder = Path.home() / "Downloads"
    folder = folder if folder.is_dir() else Path.home()
    name = re.sub(r"[^\w\s.,()-]", "", title).strip()[:80] or path.stem
    return folder / f"{name}.mp3"


def _pick(options: list[str], previous: str | None, default: str) -> str:
    """Keep the previous choice if it's still offered, else the default, else the first option.

    Matching ignores case and the ':latest' tag, so 'gemma3' finds 'gemma3:latest'.
    """
    def key(name: str) -> str:
        return name.lower().removesuffix(":latest")

    for wanted in (previous or "", default):
        for option in options:
            if key(option) == key(wanted):
                return option
    return options[0]


def _save_dialog() -> int:
    import webview  # comes with the GUI extras; only used in native mode

    return webview.FileDialog.SAVE if hasattr(webview, "FileDialog") else webview.SAVE_DIALOG


def _reveal_in_file_manager(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def main() -> None:
    requirements.extend_path()
    initial = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 and Path(sys.argv[1]).is_file() else None
    windows: list[PaperApp] = []

    @ui.page("/")
    def index() -> None:
        # Normally there's a single window; every page load gets its own state so that a
        # reload can't leave UI elements pointing at a page that's gone.
        paper_app = PaperApp(initial)
        windows.append(paper_app)
        paper_app.build()

    def shutdown() -> None:
        for paper_app in windows:
            paper_app.shutdown()

    app.on_shutdown(shutdown)
    app.native.window_args["min_size"] = (1100, 680)
    # pywebview disables text selection by default (it injects `user-select: none`); the
    # reader needs it for selecting and annotating PDF text.
    app.native.window_args["text_select"] = True
    app.native.start_args["icon"] = str(APP_ICON)  # the Dock icon on macOS, the window icon on Linux
    ui.run(
        native=True,  # a desktop window, not a browser tab; also binds to 127.0.0.1 only
        title=APP_NAME,
        favicon=APP_ICON,  # the window icon on Windows
        window_size=(1500, 940),
        dark=None,  # follow the system's light/dark setting
        reload=False,
        show_welcome_message=False,
    )


if __name__ == "__main__":
    main()
