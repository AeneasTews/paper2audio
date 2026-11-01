# paper2audio

Turn scientific papers into audio files, so you can listen to them while cycling, walking or commuting instead of reading them on a screen.

paper2audio extracts the text from a PDF, cleans it up so it sounds natural when spoken, and reads it aloud with [ollama](https://ollama.com)'s **phonon** speech model. Everything runs locally.

```sh
paper2audio convert --input=paper.pdf --output=paper.mp3
```

## Requirements

- macOS or Linux
- Python 3.10+
- [ollama](https://ollama.com) with the `phonon` model, and [uv](https://docs.astral.sh/uv/). `setup.sh` installs these if they're missing.

## Installation

From the project folder:

```sh
./setup.sh
```

The script is safe to re-run; anything already set up is skipped. It:

1. installs ollama with the [official installer](https://ollama.com/download) if the `ollama` command isn't found
2. starts the ollama server if it isn't running
3. downloads the `phonon` model if it isn't there yet
4. installs uv with its official installer if it's missing
5. installs the `paper2audio` command (`uv tool install --editable .`), so changes to the code take effect immediately

If you already have everything, `uv tool install -e .` on its own is enough.

Alternatively, run it without installing via `uv run paper2audio ...` from the project folder.

## Usage

```sh
# MP3 (small files, good for phones)
paper2audio convert --input=paper.pdf --output=paper.mp3

# WAV (uncompressed)
paper2audio convert --input=paper.pdf --output=paper.wav

# Pick a different phonon voice (default: Zoey)
paper2audio convert --input=paper.pdf --output=paper.mp3 --voice=<name>
```

| Option     | Description                                   |
| ---------- | --------------------------------------------- |
| `--input`  | PDF file to read (required)                   |
| `--output` | Audio file to write, `.mp3` or `.wav` (required) |
| `--voice`  | phonon voice to use (default: `Zoey`)         |

Progress is printed while the paper is spoken:

```
Extracting text from paper.pdf...
Speaking chunk 1/8...
...
Speaking chunk 8/8...
Wrote paper.mp3 (33.4 min)
```

As a rough guide, a 15-page paper becomes about 30 minutes of audio and takes around a minute to generate.

## How it works

1. **Extract** (`paper2audio/extract.py`): reads the text of each PDF page with [pypdf](https://pypdf.readthedocs.io).
2. **Clean** (`paper2audio/clean.py`): makes the text suitable for listening:
   - removes headers and footers that repeat on most pages
   - drops everything from the *References* / *Bibliography* heading onward
   - strips citation markers like `[13]`, URLs, email addresses and footnote symbols (`∗ † ‡`)
   - removes lines without any letters (page numbers, `_____` separators)
   - rejoins words hyphenated across line breaks and glues lines back into paragraphs
3. **Server** (`paper2audio/server.py`): if no ollama server is running, starts `ollama serve` for the duration of the run and shuts it down afterwards, also on Ctrl+C. An already-running server (e.g. the Ollama desktop app) is used and left alone.
4. **Speak** (`paper2audio/tts.py`): sends the text to `ollama run phonon` in chunks of about 4,000 characters and stitches the audio together. If phonon rejects a chunk, that chunk is skipped instead of failing the whole run.
5. **Save** (`paper2audio/audio.py`): writes WAV, or encodes MP3 at 64 kbps with [lameenc](https://github.com/chrisstaite/lameenc). ffmpeg is not needed.

## Known limitations

- Only PDFs with a text layer work. Scanned papers would need OCR first.
- Some content is still read aloud: author lists and affiliations on the title page, table rows, figure captions and acknowledgements.
- Math comes out of the PDF garbled and is read as-is.
- Cleanup uses simple layout heuristics, so unusual layouts (e.g. some two-column papers) may need tweaks to `clean.py`.
- phonon fails a whole input with `Error: input has no text to speak` if any paragraph contains nothing pronounceable, such as a line of underscores or `* * *`. The cleanup step removes these, but keep it in mind when piping text to phonon directly.
