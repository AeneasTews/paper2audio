# paper2audio

Turn scientific papers into audio files, so you can listen to them while cycling, walking or commuting instead of reading them on a screen.

paper2audio extracts the text from a PDF, plain text or Markdown file, cleans it up so it sounds natural when spoken, and reads it aloud with [ollama](https://ollama.com)'s **phonon** speech model. Figures in PDFs are described by a local vision model and read along with the text. Everything runs locally.

```sh
paper2audio convert --input=paper.pdf --output=paper.mp3
```

Or use the desktop app, which shows the paper next to the settings:

```sh
paper2audio-gui
```

## Requirements

- macOS or Linux
- Python 3.10+
- [ollama](https://ollama.com) with the `phonon` model and a vision model for figures (default `gemma3`), and [uv](https://docs.astral.sh/uv/). `setup.sh` installs these if they're missing.

## Installation

From the project folder:

```sh
./setup.sh
```

The script is safe to re-run; anything already set up is skipped. It:

1. installs ollama with the [official installer](https://ollama.com/download) if the `ollama` command isn't found
2. starts the ollama server if it isn't running
3. downloads the `phonon` model and the `gemma3` vision model if they aren't there yet
4. installs uv with its official installer if it's missing
5. installs the `paper2audio` and `paper2audio-gui` commands (`uv tool install --editable '.[gui]'`), so changes to the code take effect immediately

If you already have everything, `uv tool install -e '.[gui]'` on its own is enough. Leave out `[gui]` if you only want the command line tool; the GUI's extra packages ([NiceGUI](https://nicegui.io), [pywebview](https://pywebview.flowrl.com) and [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)) are then not installed.

Alternatively, run it without installing via `uv run paper2audio ...` from the project folder.

## Usage

```sh
# MP3 (small files, good for phones)
paper2audio convert --input=paper.pdf --output=paper.mp3

# WAV (uncompressed)
paper2audio convert --input=paper.pdf --output=paper.wav

# Plain text and Markdown work too
paper2audio convert --input=notes.txt --output=notes.mp3
paper2audio convert --input=article.md --output=article.mp3

# Pick a different phonon voice (default: Zoey)
paper2audio convert --input=paper.pdf --output=paper.mp3 --voice=<name>

# Describe figures with a different ollama vision model, or skip them
paper2audio convert --input=paper.pdf --output=paper.mp3 --vision-model=qwen2.5vl
paper2audio convert --input=paper.pdf --output=paper.mp3 --no-images
```

| Option     | Description                                   |
| ---------- | --------------------------------------------- |
| `--input`  | File to read: `.pdf`, `.txt` or `.md` (required) |
| `--output` | Audio file to write, `.mp3` or `.wav` (required) |
| `--voice`  | phonon voice to use (default: `Zoey`)         |
| `--vision-model` | ollama vision model that describes figures in PDFs (default: `gemma3`) |
| `--no-images` | Don't describe figures, read only the text |

Progress is printed while the paper is spoken:

```
Extracting text from paper.pdf...
Extracting images from paper.pdf...
Describing image 1/4...
...
Speaking chunk 1/8...
...
Speaking chunk 8/8...
Wrote paper.mp3 (33.4 min)
```

As a rough guide, a 15-page paper becomes about 30 minutes of audio and takes around a minute to generate, plus a few seconds per figure for the descriptions.

## Desktop app

```sh
paper2audio-gui              # or: paper2audio-gui paper.pdf
```

A PDF reader with the conversion settings next to it, laid out like a reference manager's reader: a toolbar on top, a sidebar on the left, the paper in the middle and an item pane on the right.

- **Reading:** pages are rendered at exactly the size they're shown on screen, including on high-DPI (Retina) displays, so text stays sharp at any zoom. Zoom with the toolbar, **⌘ +** / **⌘ −** / **⌘ 0** (Ctrl on Windows and Linux), Ctrl/⌘ + scroll wheel, or by pinching on the trackpad. Text and Markdown files are shown as text.
- **Selecting text:** text in PDFs can be selected and copied like in any PDF reader.
- **Annotations:** select text and pick a color in the popup to highlight it, or use the toolbar's highlight, underline and note tools (with eight colors). Every annotation gets a card in the sidebar, where you can add a comment, change its color or delete it (or select it and press Delete). Annotations are saved automatically in your user data folder (`~/Library/Application Support/paper2audio/annotations` on macOS), keyed by the PDF's contents. The PDF itself is never changed.
- **Sidebar:** annotations (searchable), page thumbnails, and the PDF's outline. Sections with subsections can be folded with their arrow (Alt-click folds everything under them), or all at once with the expand/collapse buttons.
- **Right pane:** the same settings as the command line. Voice and vision model are picked from the voices and vision models ollama reports. **Convert to audio** becomes **Stop** while it runs. When it's done, a player for the audio appears, with **Show in folder** to find the file. The output defaults to the paper's name with `.mp3` next to the paper.
- **Keyboard shortcuts:** ⌘O open, ⌘W close, ⌘↩ convert or stop, ⌘+ / ⌘− / ⌘0 zoom, ⌥⌘G go to page, ⌥⌘1 / 2 / 3 highlight, underline and note tools, arrow keys, Space, Page Up/Down and Home/End to move through the paper, Esc and Delete for annotations (Ctrl instead of ⌘ on Windows and Linux). ⌘/ or the keyboard button in the toolbar shows the full list.
- **Report an issue:** the bug button in the toolbar opens a form; **Open on GitHub** then opens a new issue on this repository in your browser, filled in with your description and (optionally) system info and the conversion log, with your home folder shown as `~`. You review and submit it on GitHub.
- **Setup:** when the window opens, it checks in the background for what `setup.sh` would install: ollama, the phonon model and a vision model. If something is missing, a dialog lists it and **Install** runs `setup.sh --no-command` in the background, showing the download progress. The optional vision model can be unticked. The wrench button in the toolbar opens this dialog at any time and reloads the voices and models. If the ollama installer needs a password it can't ask for here, run `./setup.sh` in a terminal instead.

It opens as a native window (no browser needed) and follows the system's light or dark mode. Under the hood it's a [NiceGUI](https://nicegui.io) app served only on `127.0.0.1` and shown with the system's web view. Closing the window during a conversion stops it after the current step, so a temporary ollama server is still shut down.

Icons are from [Lucide](https://lucide.dev) (ISC license, see `paper2audio/reader/icons/`).

## How it works

1. **Extract** (`paper2audio/extract.py`): reads the text and embedded images of each PDF page with [pypdf](https://pypdf.readthedocs.io). Images smaller than 100 pixels (icons) and repeats of an image from an earlier page (logos) are skipped. Text and Markdown files are read directly.
2. **Clean** (`paper2audio/clean.py`): makes the text suitable for listening:
   - removes headers and footers that repeat on most pages (PDF)
   - drops everything from the *References* / *Bibliography* heading onward
   - strips citation markers like `[13]`, URLs, email addresses and footnote symbols (`∗ † ‡`)
   - removes lines without any letters (page numbers, `_____` separators)
   - rejoins words hyphenated across line breaks and glues lines back into paragraphs (PDF)
   - unwraps hard-wrapped paragraphs (text and Markdown; blank lines separate paragraphs)
   - strips Markdown syntax (headings, emphasis, links, images, HTML, front matter) and skips code blocks; list items and table rows are read one at a time
   - places each figure description after the page the figure is on, at the next paragraph break so no sentence is cut in half (PDF)
3. **Server** (`paper2audio/server.py`): if no ollama server is running, starts `ollama serve` for the duration of the run and shuts it down afterwards, also on Ctrl+C. An already-running server (e.g. the Ollama desktop app) is used and left alone.
4. **Describe** (`paper2audio/describe.py`): sends each remaining figure to the vision model through ollama's API and asks for a short spoken-style description, read as "Figure description: ...". Figures after the references (appendix) aren't described, and the model can skip logos and decorations. The vision model is unloaded afterwards to free memory for phonon.
5. **Speak** (`paper2audio/tts.py`): sends the text to `ollama run phonon` in chunks of about 4,000 characters and stitches the audio together. If phonon rejects a chunk, that chunk is skipped instead of failing the whole run.
6. **Save** (`paper2audio/audio.py`): writes WAV, or encodes MP3 at 64 kbps with [lameenc](https://github.com/chrisstaite/lameenc). ffmpeg is not needed.

`paper2audio/pipeline.py` runs these steps; both `cli.py` and the desktop app (`gui.py`) call it.

## Known limitations

- Only PDFs with a text layer work. Scanned papers would need OCR first.
- Some content is still read aloud: author lists and affiliations on the title page, table rows, figure captions and acknowledgements.
- Math comes out of the PDF garbled and is read as-is.
- Only figures embedded as raster images (photos, screenshots, PNG/JPEG plots) are described. Figures drawn as vector graphics, as many plotting tools produce, aren't images in the PDF and are not picked up.
- Figure descriptions come from a small local model: they can miss details or misread values, so treat them as a summary rather than a transcription.
- Cleanup uses simple layout heuristics, so unusual layouts (e.g. some two-column papers) may need tweaks to `clean.py`.
- phonon fails a whole input with `Error: input has no text to speak` if any paragraph contains nothing pronounceable, such as a line of underscores or `* * *`. The cleanup step removes these, but keep it in mind when piping text to phonon directly.
