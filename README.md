# paper2audio

Turn scientific papers into audio files, so you can listen to them while cycling, walking or commuting instead of reading them on a screen.

paper2audio reads a PDF, plain text or Markdown file and turns it into speech with [ollama](https://ollama.com)'s **phonon** model. For PDFs, a local language model (Gemma 4 by default) looks at each page and writes out what should be spoken: formulas in words, figures and tables described, headers, page numbers and citations left out. You choose which parts of the paper you want to hear. Everything runs locally.

```sh
paper2audio convert --input=paper.pdf --output=paper.mp3 --speed=1.5
```

Or use the desktop app, which shows the paper next to the settings:

```sh
paper2audio-gui
```

## Requirements

- macOS or Linux
- Python 3.11+
- [ollama](https://ollama.com) with the `phonon` model and a language model, and [uv](https://docs.astral.sh/uv/). `setup.sh` installs these if they're missing.

## Installation

From the project folder:

```sh
./setup.sh            # asks which language model to download
./setup.sh gemma4:12b # or name one directly ("none" for no language model)
```

The script is safe to re-run; anything already set up is skipped. It:

1. installs ollama with the [official installer](https://ollama.com/download) if the `ollama` command isn't found
2. starts the ollama server if it isn't running
3. installs uv with its official installer if it's missing
4. installs the `paper2audio` and `paper2audio-gui` commands (`uv tool install --editable '.[gui]'`), so changes to the code take effect immediately
5. downloads the `phonon` speech model and the language model

If you already have everything, `uv tool install -e '.[gui]'` on its own is enough. Leave out `[gui]` if you only want the command line tool; the desktop app's extra packages ([NiceGUI](https://nicegui.io) and [pywebview](https://pywebview.flowrl.com)) are then not installed.

### Choosing a language model

The model that fits your machine's memory is recommended (on Macs that's RAM, on Linux the GPU's memory if there is an NVIDIA card). You can pick a smaller one to save download size:

| Model | Memory | Download | Notes |
| --- | --- | --- | --- |
| `gemma4:26b` | 32 GB | 16 GB | Recommended where it fits: reliable and fast for its size |
| `gemma4:12b` | 16 GB | 7.7 GB | Good quality; occasionally stops early on a page and is retried automatically |
| `gemma4:e4b` | 8 GB | 6.6 GB (9.5 GB on Macs) | Smaller download |
| `gemma3:4b` | 8 GB | 3.4 GB | Smallest model that can read images; older |
| `none` | – | – | Rule-based cleanup only: formulas and figures aren't handled |

Any other ollama model works too, with `--llm-model` or in the config file. Models that can't read images get only the extracted text, so they can't describe figures.

## Usage

```sh
# Any common audio format: .mp3, .m4a, .opus, .ogg, .flac, .wav, .aiff
paper2audio convert --input=paper.pdf --output=paper.mp3
paper2audio convert --input=paper.pdf --output=paper.opus

# Faster (or slower) playback, without the voice getting higher
paper2audio convert --input=paper.pdf --output=paper.mp3 --speed=1.5

# Plain text and Markdown work too
paper2audio convert --input=notes.txt --output=notes.mp3
paper2audio convert --input=article.md --output=article.mp3

# Choose what to hear
paper2audio convert --input=paper.pdf --output=paper.mp3 --include=summary,authors --exclude=figures

# Another language model, or none
paper2audio convert --input=paper.pdf --output=paper.mp3 --llm-model=gemma4:12b
paper2audio convert --input=paper.pdf --output=paper.mp3 --no-llm
```

| Option | Description |
| --- | --- |
| `--input` | File to read: `.pdf`, `.txt` or `.md` (required) |
| `--output` | Audio file to write: `.mp3`, `.m4a`, `.opus`, `.ogg`, `.flac`, `.wav` or `.aiff` (required) |
| `--speed` | Playback speed from 0.5 to 4, e.g. `1.5` (default: 1) |
| `--voice` | phonon voice (default: `Zoey`) |
| `--llm-model` | ollama model that reads the pages (default: the one recommended for this machine, if installed) |
| `--no-llm` | Don't use a language model, only the rule-based cleanup |
| `--include` | Comma-separated sections to read in addition to the defaults |
| `--exclude` | Comma-separated sections to leave out |

### Changing the speed of an existing file

```sh
paper2audio speedup --input=paper.mp3 --speed=1.5                       # writes paper_1.5x.mp3
paper2audio speedup --input=lecture.m4a --speed=1.25 --output=lecture.opus
```

The input can be any audio file ffmpeg can read; the output can be any of the formats above. Each speed change is applied to already-encoded audio, so for the best quality set `--speed` when converting instead of speeding up a file several times.

### Sections

| Section | Default | What it is |
| --- | --- | --- |
| `title` | on | The paper's title |
| `authors` | off | Authors and affiliations |
| `abstract` | on | The abstract |
| `figures` | on | Spoken descriptions of figures and tables |
| `formulas` | on | Formulas, spoken in words |
| `footnotes` | on | Footnotes |
| `citations` | off | In-text citations like [12] or (Smith et al., 2020) |
| `acknowledgements` | off | The acknowledgements |
| `references` | off | The reference list |
| `appendix` | off | Everything after the reference list |
| `summary` | off | A short generated summary, read before the paper |

`figures`, `formulas`, `footnotes`, `appendix` and `summary` need a language model. Without one, the rule-based cleanup handles the others with simple heuristics.

### Config file

Defaults for every run go in `~/.config/paper2audio/config.toml` (or `$XDG_CONFIG_HOME/paper2audio/config.toml`). Command-line options override it.

```toml
voice = "Zoey"
speed = 1.5
llm_model = "gemma4:12b"   # or "none"

[sections]
authors = true
summary = true
citations = false
```

### Speed

With `gemma4:26b` or `gemma4:12b` on an M-series Mac, each page takes roughly 10 to 30 seconds, so a 10-page paper takes a few minutes before speaking starts. Speaking is fast: about 30 minutes of audio takes around a minute. Without a language model, the whole conversion takes about a minute.

## Desktop app

```sh
paper2audio-gui              # or: paper2audio-gui paper.pdf
```

A PDF reader with the conversion settings next to it, laid out like a reference manager's reader: a toolbar on top, a sidebar on the left, the paper in the middle and an item pane on the right.

- **Reading:** pages are rendered at exactly the size they're shown on screen, including on high-DPI (Retina) displays, so text stays sharp at any zoom. Zoom with the toolbar, **⌘ +** / **⌘ −** / **⌘ 0** (Ctrl on Windows and Linux), Ctrl/⌘ + scroll wheel, or by pinching on the trackpad. Text and Markdown files are shown as text.
- **Selecting text:** text in PDFs can be selected and copied like in any PDF reader.
- **Annotations:** select text and pick a color in the popup to highlight it, or use the toolbar's highlight, underline and note tools (with eight colors). Every annotation gets a card in the sidebar, where you can add a comment, change its color or delete it (or select it and press Delete). Annotations are saved automatically in your user data folder (`~/Library/Application Support/paper2audio/annotations` on macOS), keyed by the PDF's contents. The PDF itself is never changed.
- **Sidebar:** annotations (searchable), page thumbnails, and the PDF's outline. Sections with subsections can be folded with their arrow (Alt-click folds everything under them), or all at once with the expand/collapse buttons.
- **Right pane:** the same settings as the command line: voice, speed, language model (*Automatic* picks one for your machine as described above, or choose an installed model, or *None* for the rule-based cleanup) and which [sections](#sections) to read. Defaults come from the [config file](#config-file). **Convert to audio** becomes **Stop** while it runs; it stops after the current page. When it's done, a player for the audio appears, with **Show in folder** to find the file. The output defaults to the paper's name with `.mp3` next to the paper; any of the supported formats works.
- **Keyboard shortcuts:** ⌘O open, ⌘W close, ⌘↩ convert or stop, ⌘+ / ⌘− / ⌘0 zoom, ⌥⌘G go to page, ⌥⌘1 / 2 / 3 highlight, underline and note tools, arrow keys, Space, Page Up/Down and Home/End to move through the paper, Esc and Delete for annotations (Ctrl instead of ⌘ on Windows and Linux). ⌘/ or the keyboard button in the toolbar shows the full list.
- **Report an issue:** the bug button in the toolbar opens a form; **Open on GitHub** then opens a new issue on this repository in your browser, filled in with your description and (optionally) system info and the conversion log, with your home folder shown as `~`. You review and submit it on GitHub.
- **Setup:** when the window opens, it checks in the background for what `setup.sh` would install: ollama, the phonon model and the language model recommended for this machine (or the one set in the config file). If something is missing, a dialog lists it and **Install** runs `setup.sh --no-command` in the background, showing the download progress. The language model is optional and can be unticked. The wrench button in the toolbar opens this dialog at any time and reloads the voices and models. If the ollama installer needs a password it can't ask for here, run `./setup.sh` in a terminal instead.

It opens as a native window (no browser needed) and follows the system's light or dark mode. Under the hood it's a [NiceGUI](https://nicegui.io) app served only on `127.0.0.1` and shown with the system's web view. Closing the window during a conversion stops it after the current step, so a temporary ollama server is still shut down.

Icons are from [Lucide](https://lucide.dev) (ISC license, see `paper2audio/reader/icons/`).

## How it works

1. **Server** (`server.py`): if no ollama server is running, starts `ollama serve` for the duration of the run and shuts it down afterwards, also on Ctrl+C. An already-running server (e.g. the Ollama desktop app) is used and left alone.
2. **Model** (`hardware.py`, `llm.py`): picks the language model, from the command line, the config file, or memory size. If none is installed, the rule-based cleanup is used.
3. **Transcribe** (`transcribe.py`, PDFs with a language model): renders each page as an image (`extract.py`, with [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)) and sends it to the model with the page's text extracted by [pypdf](https://pypdf.readthedocs.io). The prompt is built from the chosen sections. The end of the previous page is passed along, so sentences across page breaks stay whole. If a page's output looks incomplete, it's retried once. Pages after the references are skipped unless the references or appendix are wanted. The optional summary is written from the full transcript.
4. **Clean** (`clean.py`): the rule-based cleanup for text, Markdown and PDFs without a language model:
   - removes repeated headers and footers, citation markers, URLs, emails and footnote symbols
   - drops the references, acknowledgements and front matter as configured
   - removes lines without any letters (page numbers, `_____` separators)
   - rejoins hyphenated words and glues lines back into paragraphs
   - strips Markdown syntax and skips code blocks; list items and table rows are read one at a time
5. **Speak** (`tts.py`): sends the text to `ollama run phonon` in chunks of about 4,000 characters and stitches the audio together. If phonon rejects a chunk, that chunk is skipped instead of failing the whole run.
6. **Save** (`audio.py`): applies the speed with ffmpeg's `atempo` filter, which keeps the pitch of the voice, and encodes the chosen format (64 kbps for MP3/AAC, 32 kbps Opus, lossless FLAC/WAV/AIFF). This uses [PyAV](https://pyav.basswood-io.com), which bundles ffmpeg's libraries, so ffmpeg doesn't need to be installed. phonon has no speed setting of its own (ollama only accepts speed 1).

`paper2audio/pipeline.py` runs these steps; both `cli.py` and the desktop app (`gui.py`) call it.

## Known limitations

- Language models can make mistakes: figure descriptions may miss or misread details, and a formula can occasionally be read wrongly. Smaller models make more mistakes.
- Without a language model, math comes out of the PDF garbled, figures aren't described, and front matter is detected by simple heuristics.
- Scanned PDFs (no text layer) are untested. A language model that can read images may handle them; the rule-based cleanup can't.
- phonon fails a whole input with `Error: input has no text to speak` if any paragraph contains nothing pronounceable, such as a line of underscores or `* * *`. The cleanup removes these, but keep it in mind when piping text to phonon directly.
