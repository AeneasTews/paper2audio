"""The GUI's setup dialog: shows what's missing and installs it by running setup.sh."""

import os
import queue
import re
import signal
import subprocess
import threading
from collections.abc import Callable

from nicegui import ui

from . import requirements
from .requirements import Requirement

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PERCENT = re.compile(r"(\d{1,3})%")


class SetupDialog:
    """A modal dialog. Create it inside a page; call show() with the result of requirements.check()."""

    def __init__(self, on_installed: Callable[[], None]):
        self._on_installed = on_installed  # e.g. re-check and reload voices and models
        self._events: queue.Queue = queue.Queue()
        self._process: subprocess.Popen | None = None
        self._requirements: list[Requirement] = []
        self._selected: dict[str, bool] = {}
        self._state = "review"  # review, installing, done, failed, cancelled
        self._output_tail: list[str] = []

        with ui.dialog().props("persistent") as self._dialog, ui.card().classes("w-[540px] max-w-full p-6 gap-4"):
            with ui.row().classes("items-center gap-3 no-wrap"):
                ui.icon("build_circle", size="32px").classes("text-primary")
                with ui.column().classes("gap-0"):
                    ui.label("Set up paper2audio").classes("text-xl font-bold")
                    self._subtitle = ui.label("").classes("text-sm opacity-70")
            self._body = ui.column().classes("w-full gap-2")
            with ui.column().classes("w-full gap-1") as self._progress_area:
                self._step = ui.label("").classes("text-sm font-medium")
                self._progress = ui.linear_progress(value=0, show_value=False, size="6px").props("rounded")
                self._detail = ui.label("").classes("text-xs opacity-60 truncate w-full")
                with ui.expansion("Details").classes("w-full text-sm").props("dense"):
                    self._log = ui.log(max_lines=300).classes("w-full h-40 text-xs")
            self._progress_area.set_visibility(False)
            self._message = ui.label("").classes("text-sm whitespace-pre-line")
            with ui.row().classes("w-full justify-end gap-2"):
                self._secondary = ui.button("Not now", on_click=self._on_secondary).props("flat no-caps")
                self._primary = ui.button("Install", icon="download", on_click=self._on_primary) \
                    .props("unelevated no-caps")
        ui.timer(0.1, self._poll)

    # --- public --------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def is_open(self) -> bool:
        return self._dialog.value

    def show(self, reqs: list[Requirement]) -> None:
        """Open the dialog (or refresh it, if open) with the current state of the requirements."""
        self._requirements = reqs
        if not self.running and self._state in ("review", "done", "cancelled", "failed"):
            if self._state != "done" or any(not r.installed for r in reqs):
                self._state = "review"
            self._selected = {r.key: True for r in reqs if not r.installed}
        self._render()
        self._dialog.open()

    def stop(self) -> None:
        """Stop a running installation (e.g. when the window closes)."""
        if self.running:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    # --- rendering -----------------------------------------------------------------------

    def _render(self) -> None:
        missing = [r for r in self._requirements if not r.installed]
        self._body.clear()
        with self._body:
            for req in self._requirements:
                self._render_row(req)
            if any(r.key == "ollama" for r in missing) and self._state == "review":
                with ui.row().classes("items-start gap-2 no-wrap mt-1"):
                    ui.icon("info", size="18px").classes("opacity-60 mt-[2px]")
                    ui.label(
                        "Ollama is installed with its official installer. It may ask for your password; "
                        "if that doesn't work from here, run ./setup.sh in a terminal instead."
                    ).classes("text-xs opacity-70")

        installing = self._state == "installing"
        self._progress_area.set_visibility(self._state != "review")
        if self._state == "review" and not missing:
            self._subtitle.set_text("Everything paper2audio needs is installed.")
            self._message.set_text("")
            self._secondary.set_visibility(False)
            self._set_primary("Close", "check")
            self._progress_area.set_visibility(False)
        elif self._state == "review":
            self._subtitle.set_text("A few things are missing. Install them now? Everything runs on your computer.")
            self._message.set_text("")
            self._secondary.set_visibility(True)
            self._secondary.set_text("Not now")
            self._set_primary("Install", "download", enabled=bool(self._to_install()) or self._needs_ollama())
        elif installing:
            self._subtitle.set_text("Installing… you can keep this window open or come back later.")
            self._secondary.set_visibility(True)
            self._secondary.set_text("Cancel")
            self._set_primary("Installing…", "hourglass_top", enabled=False)
        elif self._state == "done":
            self._subtitle.set_text("All set.")
            self._message.set_text("Everything is installed. You can convert papers now.")
            self._secondary.set_visibility(False)
            self._set_primary("Done", "check")
        elif self._state == "cancelled":
            self._subtitle.set_text("Installation stopped.")
            self._message.set_text("Nothing more will be downloaded. You can try again any time.")
            self._secondary.set_visibility(True)
            self._secondary.set_text("Close")
            self._set_primary("Try again", "refresh")
        else:  # failed
            self._subtitle.set_text("Something went wrong.")
            tail = "\n".join(self._output_tail[-4:])
            self._message.set_text(
                f"{tail}\n\nSee Details for the full output. You can also run ./setup.sh in a terminal."
                if tail else "See Details for the output. You can also run ./setup.sh in a terminal."
            )
            self._secondary.set_visibility(True)
            self._secondary.set_text("Close")
            self._set_primary("Try again", "refresh")

    def _render_row(self, req: Requirement) -> None:
        with ui.row().classes("w-full items-center gap-3 no-wrap rounded-lg px-3 py-2 p2a-setup-row"):
            if req.installed:
                ui.icon("check_circle", size="24px").classes("text-positive")
            elif self._state == "installing":
                ui.spinner(size="24px")
            elif req.required:
                ui.checkbox(value=True).props("dense disable").tooltip("Required")
            else:
                ui.checkbox(
                    value=self._selected.get(req.key, True),
                    on_change=lambda e, key=req.key: self._toggle(key, e.value),
                ).props("dense")
            with ui.column().classes("gap-0 flex-1 min-w-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(req.title).classes("font-medium")
                    if not req.installed and req.required:
                        ui.badge("required").props("outline color=grey-7")
                    if not req.installed and req.size:
                        ui.badge(req.size).props("outline color=grey-7")
                ui.label(req.purpose).classes("text-xs opacity-70")
            if req.installed:
                ui.label("Installed").classes("text-xs text-positive")

    def _set_primary(self, text: str, icon: str, enabled: bool = True) -> None:
        self._primary.set_text(text)
        self._primary.props(f"icon={icon}")
        if enabled:
            self._primary.enable()
        else:
            self._primary.disable()

    def _toggle(self, key: str, value: bool) -> None:
        self._selected[key] = value
        self._render()

    # --- actions -------------------------------------------------------------------------

    def _to_install(self) -> list[str]:
        """Models to pull: missing ones that are required or still selected."""
        return [
            r.key for r in self._requirements
            if not r.installed and r.key != "ollama" and (r.required or self._selected.get(r.key, True))
        ]

    def _needs_ollama(self) -> bool:
        return any(r.key == "ollama" and not r.installed for r in self._requirements)

    def _on_primary(self) -> None:
        if self._state in ("review", "failed", "cancelled") and any(not r.installed for r in self._requirements):
            self._start()
        else:
            self._state = "review"
            self._dialog.close()

    def _on_secondary(self) -> None:
        if self._state == "installing":
            self.stop()
            self._step.set_text("Stopping…")
            return
        self._state = "review"
        self._dialog.close()

    def _start(self) -> None:
        try:
            self._process = requirements.start_setup(self._to_install())
        except (RuntimeError, OSError) as e:
            self._state = "failed"
            self._output_tail = [str(e)]
            self._render()
            return
        self._state = "installing"
        self._output_tail = []
        self._log.clear()
        self._progress.set_value(0)
        self._progress.props("indeterminate")
        self._step.set_text("Starting…")
        self._detail.set_text("")
        threading.Thread(target=self._read_output, args=(self._process,), daemon=True).start()
        self._render()

    def _read_output(self, process: subprocess.Popen) -> None:
        """Runs in a background thread. Download progress uses carriage returns, so split on those too."""
        buffer = b""
        while chunk := process.stdout.read1(4096):
            buffer += chunk
            *lines, buffer = re.split(rb"[\r\n]", buffer)
            for line in lines:
                self._emit(line)
        self._emit(buffer)
        self._events.put(("finished", process.wait()))

    def _emit(self, raw: bytes) -> None:
        line = ANSI_ESCAPE.sub("", raw.decode(errors="replace")).strip()
        if not line:
            return
        percent = PERCENT.search(line)
        if percent and not line.startswith(">>>"):
            self._events.put(("download", line, int(percent.group(1))))
        else:
            self._events.put(("line", line))

    def _poll(self) -> None:
        latest_download = None
        try:
            while True:
                event = self._events.get_nowait()
                if event[0] == "download":
                    latest_download = event  # only the newest progress line matters
                else:
                    self._handle(event)
        except queue.Empty:
            pass
        if latest_download and self._state == "installing":  # not after it has finished
            _, line, percent = latest_download
            self._progress.props(remove="indeterminate")
            self._progress.set_value(min(percent, 100) / 100)
            self._detail.set_text(line)

    def _handle(self, event: tuple) -> None:
        if event[0] == "line":
            line = event[1]
            self._log.push(line)
            self._output_tail = (self._output_tail + [line])[-20:]
            if line.startswith(">>> "):
                self._step.set_text(line[4:])
                self._detail.set_text("")
                self._progress.props("indeterminate")
        elif event[0] == "finished":
            code = event[1]
            self._process = None
            self._progress.props(remove="indeterminate")
            if code == 0:
                self._state = "done"
                self._progress.set_value(1)
                self._step.set_text("Setup complete")
                self._detail.set_text("")
                self._on_installed()
            elif code < 0:  # killed by a signal: cancelled
                self._state = "cancelled"
                self._step.set_text("Stopped")
            else:
                self._state = "failed"
                self._step.set_text(f"Setup failed (exit code {code})")
            self._render()
