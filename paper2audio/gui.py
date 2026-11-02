"""Desktop GUI: a PDF reader with annotations next to the conversion settings, in a native window.

Run with `paper2audio-gui [paper.pdf]`. Needs the optional GUI dependencies:
`uv tool install -e '.[gui]'`.

Built with NiceGUI in native mode (pywebview), so the window is drawn by the system's web
engine. The reader itself (paper2audio/reader/reader.js) runs in the web view; this module
serves it page images rendered with pypdfium2, text positions for selecting text, the
outline, and annotation storage, and runs the conversion.
"""

import html
import io
import json
import platform
import queue
import re
import secrets
import shutil
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

from . import annotations, audio, config, document, hardware, models, pipeline, requirements, server, tts
from .setup_dialog import SetupDialog

READER_DIR = Path(__file__).parent / "reader"
MAX_RENDER_WIDTH = 8192  # pixels; enough for 400% zoom on a Retina screen
SPEEDS = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
NO_MODEL = "none"  # the rule-based cleanup, without a language model
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


@app.get("/annotations/{token}")
def _get_annotations(token: str) -> list:
    document = _document(token)
    return annotations.load(document.annotation_id) if document.annotation_id else []


@app.put("/annotations/{token}")
def _put_annotations(token: str, body: list = Body(...)) -> dict:
    document = _document(token)
    if not document.annotation_id:
        raise HTTPException(400, "annotations are only stored for PDFs")
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
  {_button("emit-open", "folder-open", f"Open a paper… ({_MOD}O)", needs_pdf=False)}
  {_button("emit-open_remote", "link", f"Open from arXiv, DOI or URL… ({_SHIFT_MOD}O)", needs_pdf=False)}
  <div class="p2a-tab">{icon("file-text")}<span id="p2a-tab-title">No paper open</span></div>
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
  <div id="p2a-scroll"><div id="p2a-pages"></div></div>
  <div id="p2a-popup"></div>
</div>
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


class PaperApp:
    """State and UI of the window. Background work talks to it through an event queue."""

    def __init__(self, initial_file: Path | None):
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.options_loader: threading.Thread | None = None
        self.input_path: Path | None = initial_file
        self.input_source: str | None = None  # arXiv ID, DOI or URL the input was downloaded from
        self.fetcher: threading.Thread | None = None
        self.fetch_status = None  # the open-remote dialog's status label, while it's open
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
        self.setup = SetupDialog(on_installed=self._refresh_options)
        ui.on("p2a_open", self._choose_input)
        ui.on("p2a_open_remote", self._show_open_remote)
        ui.on("p2a_setup", self._open_setup)
        ui.on("p2a_convert", self._on_convert)
        ui.on("p2a_close", self._close_window)
        ui.on("p2a_shortcuts", _show_shortcuts)
        ui.on("p2a_report", self._show_report)
        ui.on("p2a_notify", lambda e: ui.notify(e.args, type="warning"))
        ui.timer(0.1, self._poll_events)
        if self.input_path:
            ui.timer(0.2, lambda: self._set_input(self.input_path), once=True)  # after reader.js is ready
        else:
            ui.timer(0.2, lambda: ui.run_javascript(
                "P2A.showMessage('file-text', 'Open a PDF, text or Markdown file to start.')"), once=True)
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

                self.output_section = Section("p2a-sec-output", "Output", "file-text")
                with self.output_section.body:
                    self.player = ui.element("audio").props("controls preload=metadata")
                    ui.html('<span class="p2a-link">Show in folder</span>', sanitize=False).on("click", self._reveal)
                self.output_section.element.set_visibility(False)

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
            allow_multiple=False,
            file_types=("Papers (" + ";".join(f"*{s}" for s in pipeline.SUPPORTED_INPUTS) + ")", "All files (*.*)"),
        )
        if paths:
            self._set_input(Path(paths[0]))

    def _set_input(self, path: Path, source: str | None = None) -> None:
        """Show a paper. source: the arXiv ID, DOI or URL it was downloaded from, if any."""
        self.input_path = path
        self.input_source = source
        self.output_section.element.set_visibility(False)
        self.info_source.set_text(source or "Local file")
        self.info_file.set_text(path.name)
        self.info_folder.set_text(str(path.parent))
        token = secrets.token_urlsafe(8)
        _documents.clear()  # one paper at a time
        suffix = path.suffix.lower()
        title = path.stem
        try:
            if suffix == ".pdf":
                document_ = Document(path, annotations.document_id(path))
                with _pdfium_lock:
                    pdf = pdfium.PdfDocument(path)
                    try:
                        sizes = [list(pdf[i].get_size()) for i in range(len(pdf))]
                        meta_title = (pdf.get_metadata_dict().get("Title") or "").strip()
                    finally:
                        pdf.close()
                _documents[token] = document_
                title = meta_title if 3 <= len(meta_title) <= 200 else path.stem
                self.subtitle_label.set_text(f"PDF · {len(sizes)} pages")
                self.info_pages.set_text(str(len(sizes)))
                ui.run_javascript(f"P2A.open({json.dumps({'token': token, 'title': title, 'pages': sizes})})")
            elif suffix in pipeline.STRUCTURED_READERS:
                _documents[token] = Document(path)
                try:
                    blocks = _read_blocks(path, path.stat().st_mtime)
                    title = next((text for kind, text, _ in blocks if kind == "title"), path.stem)[:200]
                except Exception:
                    pass  # the preview shows the error
                kind = "arXiv HTML" if path.name.startswith("arxiv-") and suffix == ".html" else FORMAT_NAMES[suffix]
                self.subtitle_label.set_text(kind)
                self.info_pages.set_text("—")
                ui.run_javascript(f"P2A.showBlocks({json.dumps(title)}, {json.dumps(f'/blocks/{token}')})")
            else:
                _documents[token] = Document(path)
                self.subtitle_label.set_text("Markdown" if suffix == ".md" else "Text")
                self.info_pages.set_text("—")
                ui.run_javascript(f"P2A.showText({json.dumps(path.name)}, {json.dumps(f'/textfile/{token}')})")
            self.title_label.set_text(title)
        except (OSError, pdfium.PdfiumError) as e:
            self.title_label.set_text(path.name)
            self.subtitle_label.set_text("Can't open this file")
            ui.run_javascript(f"P2A.showMessage('file-text', {json.dumps(f'Can’t open {path.name}: {e}')})")
        self._set_output(str(_default_output(path, title, downloaded=source is not None)))
        self._update_state()

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
                self.fetcher = threading.Thread(target=self._fetch, args=(source,), daemon=True)
                self.fetcher.start()

            with ui.element("div").classes("p2a-actions"):
                ui.html("Cancel", tag="button", sanitize=False).classes("p2a-push").on("click", dialog.close)
                open_button = ui.html(f"{icon('link')} Open", tag="button", sanitize=False) \
                    .classes("p2a-push p2a-primary").on("click", submit)
            self.fetch_dialog, self.fetch_open_button = dialog, open_button
        dialog.on("hide", lambda: setattr(self, "fetch_status", None))
        dialog.open()
        ui.run_javascript("setTimeout(() => document.querySelector('.p2a-report input[type=text]')?.focus(), 100)")

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
        self.output_input.props["value"] = value
        self.output_input.update()

    def _output_changed(self, event) -> None:
        self.output_value = event.args

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
            f"- Input type: {self.input_path.suffix if self.input_path else 'none'}"
            f"{' (downloaded)' if self.input_source else ''}",
        ]
        missing = [r.title for r in self.requirements if not r.installed]
        if missing:
            lines.append(f"- Missing: {', '.join(missing)}")
        text = "\n".join(lines)
        if self.log_lines:
            text += "\n\n<details><summary>Conversion log</summary>\n\n```\n" + "\n".join(self.log_lines[-40:]) + "\n```\n</details>"
        return text.replace(str(Path.home()), "~")

    def _reveal(self) -> None:
        if self.output_path:
            _reveal_in_file_manager(self.output_path)

    # --- conversion --------------------------------------------------------------------

    def _on_convert(self) -> None:
        if self._busy():
            self.cancel.set()
            self.status.set_text("Stopping after the current step…")
            _set_disabled(self.convert_button, True)
            return
        output_path = Path(self.output_value or "").expanduser()
        error = self._validate(self.input_path, output_path)
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
        self.output_path = output_path
        self.cancel.clear()
        self.log_lines = []
        self.log.set_text("")
        self._set_progress(0)
        self.output_section.element.set_visibility(False)
        self.worker = threading.Thread(
            target=self._run,
            args=(self.input_path, output_path, settings),
            daemon=True,
        )
        self.worker.start()
        self._update_state()

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

    def _run(self, input_path: Path, output_path: Path, settings: config.Settings) -> None:
        """Runs in the worker thread; talks to the UI only through the event queue."""

        def progress(message: str, fraction: float | None) -> None:
            if self.cancel.is_set():
                raise Cancelled
            self.events.put(("progress", message, fraction))

        try:
            minutes = pipeline.convert(input_path, output_path, settings, progress)
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
            self._set_input(*args)
            return
        if kind == "progress":
            message, fraction = args
            self._log(message)
            if not message.startswith(" "):  # indented lines are details, keep the main status
                self.status.set_text(message)
            if fraction is not None:
                self._set_progress(fraction)
            return
        self.worker = None
        if kind == "done":
            self._set_progress(1)
            whole_minutes, seconds = divmod(round(args[0] * 60), 60)
            self.status.set_text(f"Done: {whole_minutes}:{seconds:02d} of audio.")
            token = secrets.token_urlsafe(8)
            _audio_files.clear()
            _audio_files[token] = self.output_path
            self.player.props["src"] = f"/audio/{token}"
            self.player.update()
            self.output_section.element.set_visibility(True)
        elif kind == "cancelled":
            self._set_progress(0)
            self.status.set_text("Stopped.")
            self._log("Stopped.")
        elif kind == "error":
            self.status.set_text("Failed.")
            self._log(f"Error: {args[0]}")
            ui.notify(f"Conversion failed: {args[0]}", type="negative", multi_line=True,
                      timeout=0, close_button=True)
        self._update_state()

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
    ("Close the window", f"{_MOD}W"),
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
    ui.run(
        native=True,  # a desktop window, not a browser tab; also binds to 127.0.0.1 only
        title="paper2audio",
        window_size=(1500, 940),
        dark=None,  # follow the system's light/dark setting
        reload=False,
        show_welcome_message=False,
    )


if __name__ == "__main__":
    main()
