"""Send an audio file to a phone on the same Wi-Fi: a tiny web server for that one file.

The GUI shows a QR code with the link; the phone's camera opens a small page with a player
and a download button. The server listens on the local network only while the dialog is
open, serves nothing but that page and file, and only under a random, unguessable path.

Every request is logged to the terminal (stderr), so a failing download can be diagnosed.
"""

import html
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CHUNK = 1 << 16
TYPES = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".opus": "audio/ogg", ".ogg": "audio/ogg",
         ".flac": "audio/flac", ".wav": "audio/wav", ".aiff": "audio/aiff"}


def local_address() -> str | None:
    """This computer's address on the local network, or None without one."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # a reserved address; nothing is sent, it only picks the route
            address = s.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") else address


def log(message: str) -> None:
    print(f"[paper2audio phone] {time.strftime('%H:%M:%S')} {message}", file=sys.stderr, flush=True)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # Phones often open a connection and drop it unused; that's not worth a traceback.
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            log(f"{client_address[0]} closed a connection ({type(error).__name__})")
        else:
            super().handle_error(request, client_address)


class PhoneShare:
    """Serves one file until stop(). on_download(state) is called from the server's thread:
    "started" and "done" for a download, or a short reason when something goes wrong."""

    def __init__(self, path: Path, name: str, on_download: Callable[[str], None] = lambda state: None):
        address = local_address()
        if address is None:
            raise OSError("this computer isn't connected to a network")
        self.path = path
        self.name = name  # the file name the phone saves it as
        self._on_download = on_download
        token = secrets.token_urlsafe(12)
        # Plain links (letters, digits and dashes), which no browser rewrites.
        slug = (re.sub(r"[^A-Za-z0-9]+", "-", Path(name).stem).strip("-")[:60] or "paper") + path.suffix.lower()
        self._file_link = f"/{token}/file/{slug}"  # downloads (Content-Disposition: attachment)
        self._play_link = f"/{token}/play/{slug}"  # plays in the browser
        share = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 (name required by http.server)
                # Only the token and the kind of link count: browsers may spell the rest
                # differently when they request it again.
                parts = urllib.parse.urlsplit(self.path).path.split("/")
                if parts[1:2] != [token]:
                    log(f"{self.client_address[0]} {self.command} {self.path[:80]} -> 404 (wrong link)")
                    self.send_error(404)
                    on_download("refused a request for an unknown link")
                elif len(parts) > 3 and parts[2] in ("file", "play"):
                    share._send_file(self, attachment=parts[2] == "file")
                else:
                    share._send_page(self)

            def do_HEAD(self) -> None:  # noqa: N802
                self.do_GET()

            def log_message(self, *args) -> None:
                pass

        self._server = _Server(("0.0.0.0", 0), Handler)
        self.url = f"http://{address}:{self._server.server_port}/{token}/"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        log(f"sharing {path.name} at {self.url}")

    def _send_page(self, request: BaseHTTPRequestHandler) -> None:
        size_mb = self.path.stat().st_size / 1e6
        name, title = html.escape(self.name), html.escape(Path(self.name).stem)
        page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
body {{ font: 17px -apple-system, system-ui, sans-serif; margin: 0; padding: 28px 22px; color: #222; background: #f6f6f6; }}
h1 {{ font-size: 21px; line-height: 1.3; margin: 0 0 4px; }}
.sub {{ color: #777; font-size: 14px; margin-bottom: 22px; }}
audio {{ width: 100%; margin-bottom: 18px; }}
a.button {{ display: block; text-align: center; padding: 14px; border-radius: 12px; background: #4072e5;
  color: #fff; text-decoration: none; font-weight: 600; margin-bottom: 10px; }}
a.secondary {{ background: #fff; color: #4072e5; box-shadow: 0 0 0 1px #d5d5d5; }}
p {{ color: #666; font-size: 14px; line-height: 1.45; }}
@media (prefers-color-scheme: dark) {{ body {{ background: #1b1b1b; color: #eee; }} a.secondary {{ background: #2b2b2b; box-shadow: none; }} }}
</style></head><body>
<h1>{title}</h1><div class="sub">paper2audio · {size_mb:.1f} MB</div>
<audio controls preload="metadata" src="{self._play_link}"></audio>
<a class="button" href="{self._file_link}" download="{name}">Download</a>
<a class="button secondary" href="{self._play_link}">Open the audio file</a>
<p>iPhone: after <b>Download</b>, the file is in the Files app, under Downloads.
If the download doesn't work, tap <b>Open the audio file</b>, then the share button and
<b>Save to Files</b>. Android: the download goes to the Downloads folder.</p>
</body></html>""".encode()
        request.send_response(200)
        request.send_header("Content-Type", "text/html; charset=utf-8")
        request.send_header("Content-Length", str(len(page)))
        request.send_header("Cache-Control", "no-store")
        request.end_headers()
        if request.command != "HEAD":
            request.wfile.write(page)
        log(f"{request.client_address[0]} {request.command} page -> 200")

    def _send_file(self, request: BaseHTTPRequestHandler, attachment: bool) -> None:
        client, kind = request.client_address[0], "download" if attachment else "play"
        stat = self.path.stat()
        size = stat.st_size
        etag = f'"{stat.st_mtime_ns:x}-{size:x}"'
        last_modified = formatdate(stat.st_mtime, usegmt=True)
        start, end = 0, size - 1
        ranged = request.headers.get("Range", "").strip()
        if_range = request.headers.get("If-Range", "").strip()
        if ranged and if_range and if_range not in (etag, last_modified) and not _same_time(if_range, stat.st_mtime):
            ranged = ""  # the file changed since the phone started: send all of it
        if ranged:
            spec = ranged.removeprefix("bytes=")
            first, _, last = spec.partition("-")
            try:
                if not ranged.startswith("bytes=") or "," in spec:
                    raise ValueError
                start, end = (int(first), int(last) if last else size - 1) if first else (max(0, size - int(last)), size - 1)
                end = min(end, size - 1)
                if start > end:
                    raise ValueError
            except ValueError:
                log(f"{client} {request.command} {kind} Range: {ranged} -> 416")
                request.send_response(416)
                request.send_header("Content-Range", f"bytes */{size}")
                request.send_header("Content-Length", "0")
                request.end_headers()
                return
        length = end - start + 1
        request.send_response(206 if ranged else 200)
        request.send_header("Content-Type", TYPES.get(self.path.suffix.lower(), "application/octet-stream"))
        request.send_header("Content-Length", str(length))
        request.send_header("Accept-Ranges", "bytes")
        request.send_header("ETag", etag)
        request.send_header("Last-Modified", last_modified)
        if ranged:
            request.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if attachment:
            ascii_name = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", self.name)
            request.send_header("Content-Disposition",
                                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(self.name)}")
        request.end_headers()
        what = f"{request.command} {kind}" + (f" Range: {ranged}" if ranged else "")
        if request.command == "HEAD":
            log(f"{client} {what} -> {206 if ranged else 200}")
            return
        big = length > 1_000_000
        if big:
            self._on_download("started")
        sent = 0
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                while sent < length:
                    chunk = f.read(min(CHUNK, length - sent))
                    if not chunk:
                        break
                    request.wfile.write(chunk)
                    sent += len(chunk)
        except (ConnectionError, TimeoutError) as e:
            log(f"{client} {what} -> {206 if ranged else 200}, broke off after {sent:,} of {length:,} bytes "
                f"({type(e).__name__})")
            request.close_connection = True
            if big:
                self._on_download(f"saw the phone stop after {sent / 1e6:.1f} of {length / 1e6:.1f} MB")
            return
        log(f"{client} {what} -> {206 if ranged else 200}, sent {sent:,} bytes")
        if end == size - 1 and big:
            self._on_download("done")

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        log("stopped sharing")


def _same_time(http_date: str, mtime: float) -> bool:
    try:
        return int(parsedate_to_datetime(http_date).timestamp()) == int(mtime)
    except (TypeError, ValueError):
        return False
