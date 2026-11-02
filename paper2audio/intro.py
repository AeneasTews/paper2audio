"""The GUI's welcome dialog: what the app does, which models it uses and what it needs.

Shown once, the first time the app opens; the help button in the toolbar shows it again.
"""

import html
import sys
from collections.abc import Callable

from nicegui import ui

from . import hardware, ocr, requirements, tts

MOD = "⌘" if sys.platform == "darwin" else "Ctrl+"

# (model, memory needed in GB, note), like the table in the README
LANGUAGE_MODELS = [
    ("gemma4:26b", 32, "the best choice where it fits: reliable, and fast for its size"),
    ("gemma4:12b", 16, "good quality"),
    ("gemma4:e4b", 8, "smaller and faster; formulas and figures are less reliable"),
]


class Intro:
    """A dialog with three pages. on_close runs when it's closed, however that happens."""

    def __init__(self, icon: Callable[[str], str], on_close: Callable[[], None]):
        self._icon = icon
        self._on_close = on_close
        self._page = 0
        self._pages = [self._welcome, self._models, self._computer]
        self._memory: float | None = None
        self._recommended: str | None = None
        with ui.dialog() as self._dialog, ui.card().classes("p2a-dialog p2a-intro"):
            self._body = ui.html("", sanitize=False).classes("p2a-intro-body")
            with ui.element("div").classes("p2a-intro-footer"):
                self._dots = ui.html("", sanitize=False).classes("p2a-intro-dots")
                with ui.element("div").classes("p2a-actions"):
                    self._back = ui.html("Back", tag="button", sanitize=False).classes("p2a-push").on("click", self._previous)
                    self._next = ui.html("", tag="button", sanitize=False).classes("p2a-push p2a-primary").on("click", self._forward)
        self._dialog.on("hide", self._on_close)

    @property
    def is_open(self) -> bool:
        return self._dialog.value

    def show(self) -> None:
        if self._memory is None:
            self._memory = hardware.memory_gb()
            self._recommended = hardware.recommended_model()
        self._page = 0
        self._render()
        self._dialog.open()

    def _previous(self) -> None:
        self._page = max(0, self._page - 1)
        self._render()

    def _forward(self) -> None:
        if self._page == len(self._pages) - 1:
            self._dialog.close()
        else:
            self._page += 1
            self._render()

    def _render(self) -> None:
        self._body.set_content(self._pages[self._page]())
        self._dots.set_content("".join(f'<span class="{"p2a-on" if i == self._page else ""}"></span>'
                                       for i in range(len(self._pages))))
        self._back.set_visibility(self._page > 0)
        self._next.set_content("Get started" if self._page == len(self._pages) - 1 else "Next")

    # --- pages ---------------------------------------------------------------------------

    def _feature(self, icon: str, title: str, text: str) -> str:
        return (f'<div class="p2a-intro-feature">{self._icon(icon)}'
                f'<div><div class="p2a-intro-name">{title}</div><div class="p2a-hint">{text}</div></div></div>')

    def _welcome(self) -> str:
        return f"""
        <div class="p2a-intro-title">Welcome to paper2audio</div>
        <p>Turn scientific papers into audio, to listen to them while walking, cycling or
        commuting. Formulas are spoken in words, figures and tables are described, and
        headers, page numbers and citations are left out. Everything runs on this computer.</p>
        {self._feature("file-text", "Read and annotate",
                       "PDFs, arXiv papers, HTML, JATS XML, EPUB, LaTeX, text and Markdown. "
                       "Highlight, underline and add notes; they're saved automatically.")}
        {self._feature("library", "Library and tabs",
                       "Each paper opens in its own tab. Papers you open are remembered, and you can "
                       "sort them into collections. Your Zotero library shows up too, if you use Zotero.")}
        {self._feature("search", "Find any paper",
                       f"Press <kbd>{MOD}K</kbd> to search all your papers by title, author or year.")}
        {self._feature("audio-lines", "Convert to audio",
                       "Pick the voice, speed and which parts to read in the pane on the right, then "
                       f"press <b>Convert to audio</b> (<kbd>{MOD}↩</kbd>). The audio plays right there.")}
        """

    def _models(self) -> str:
        return f"""
        <div class="p2a-intro-title">The models</div>
        <p>Three models do the work, all run locally by <a href="https://ollama.com" target="_blank">ollama</a>:</p>
        {self._feature("audio-lines", f"{tts.MODEL}: the voice",
                       "Reads the text aloud, in one of several voices.")}
        {self._feature("file-text", f"{ocr.MODEL}: reading the pages",
                       f"Transcribes each PDF page, including formulas and tables "
                       f"({requirements.DOWNLOAD_SIZES[ocr.MODEL]} download).")}
        {self._feature("cpu", "A language model (Gemma 4)",
                       "Turns formulas into words, describes figures and tables, finds the sections "
                       "you chose and can write a short summary. Optional: without it, a rule-based "
                       "cleanup reads the plain text, and formulas and figures are skipped.")}
        <p class="p2a-hint">How long it takes, on an Apple Silicon Mac with gemma4:12b or 26b: about
        50 seconds per PDF page, so around 10 minutes for a 10-page paper. arXiv HTML, EPUB and
        LaTeX are faster. Speaking itself is quick: about a minute for 30 minutes of audio.</p>
        """

    def _computer(self) -> str:
        rows = []
        for model, memory, note in LANGUAGE_MODELS:
            mark = " p2a-on" if model == self._recommended else ""
            badge = ' <span class="p2a-intro-badge">recommended here</span>' if mark else ""
            rows.append(f'<tr class="{mark}"><td><b>{model}</b>{badge}</td><td>{memory} GB</td>'
                        f'<td>{requirements.DOWNLOAD_SIZES[model]}</td><td class="p2a-hint">{note}</td></tr>')
        if self._memory is None:
            here = "This computer's memory couldn't be read."
        elif self._recommended:
            here = (f"This computer has <b>{self._memory:.0f} GB</b> of memory, enough for "
                    f"<b>{html.escape(self._recommended)}</b>.")
        else:
            here = (f"This computer has <b>{self._memory:.0f} GB</b> of memory: too little for the "
                    "language models, so papers are read with the rule-based cleanup.")
        return f"""
        <div class="p2a-intro-title">What it needs</div>
        <ul class="p2a-intro-list">
          <li>macOS or Linux, with Python 3.11 or newer</li>
          <li>Memory for the language model: on Macs the RAM, on Linux the graphics card's
              memory if there's an NVIDIA card, otherwise the RAM</li>
          <li>Disk space for the downloads below, plus {requirements.DOWNLOAD_SIZES[ocr.MODEL]} for {ocr.MODEL}</li>
        </ul>
        <table class="p2a-intro-table">
          <tr><th>Language model</th><th>Memory</th><th>Download</th><th></th></tr>
          {"".join(rows)}
        </table>
        <div class="p2a-intro-here">{self._icon("cpu")}<span>{here}</span></div>
        <p class="p2a-hint">Next, the app checks whether ollama and the models are installed,
        and offers to install what's missing. The wrench button in the toolbar does this again
        at any time, and the help button shows this introduction.</p>
        """
