# Simple PDF Compressor

A small, dependency-light toolkit that shrinks PDF files by downsampling and
re-encoding their embedded images, plus a full structural cleanup of the PDF
object streams — with optional local OCR to keep scanned documents
searchable. Includes a command-line script, a Windows GUI, and a
self-contained `.exe`; everything runs 100% locally, no document ever leaves
the machine.

```
Before: 10.9 MB scanned contract     After: 1.6 MB   (85% smaller, visually equivalent)
```

---

## Contents

| File | Role |
|---|---|
| `compress_pdf.py` | Compression core + CLI. No GUI dependency. |
| `ocr_layer.py` | Optional invisible-text OCR pass (Tesseract). |
| `pdf_compressor_gui.py` | Tkinter GUI front-end (also the PyInstaller entry point). |
| `PDFCompressor.exe` | Prebuilt Windows x64 binary (one-file, no Python required). |
| `app_icon.ico` | Application/window icon used by the build. |
| `requirements.txt` | Python dependencies (pip). |
| `.gitignore` | Keeps build output, PDFs, caches, and documents out of Git. |

---

## Quick start

### Windows GUI (no setup)

Double-click `PDFCompressor.exe`.

* Unsigned binary — SmartScreen may warn on first run: *More info → Run anyway*.
* Some antivirus tools flag fresh PyInstaller exes; allow-list the file.
* To run from source instead: `python pdf_compressor_gui.py` (needs the
  dependencies below).

### Command line

```bash
pip install -r requirements.txt           # minimal CLI: just pikepdf + Pillow
python compress_pdf.py scan.pdf                              # -> scan_compressed.pdf
python compress_pdf.py scan.pdf -o out.pdf -q 55 --max-dim 1400 -v
python compress_pdf.py vector.pdf --no-images                # lossless pass only
python compress_pdf.py secure.pdf --password s3cret          # output will be UNENCRYPTED
python compress_pdf.py scan.pdf --ocr                        # add searchable text (needs Tesseract)
python compress_pdf.py scan.pdf --ocr deu                    # OCR in another language
```

### CLI reference

| Flag | Default | Meaning |
|---|---|---|
| `input` | — | PDF file to compress |
| `-o, --output` | `<input>_compressed.pdf` | output path (refuses to overwrite input) |
| `-q, --quality` | `60` | JPEG quality for re-encoded images (1–100) |
| `--max-dim` | `1600` | longest image edge in pixels; `0` keeps resolution |
| `--no-images` | off | lossless pass only (stream/object-stream cleanup) |
| `--linearize` | off | web-optimized output (slightly larger, faster first page) |
| `--password` | — | password for encrypted inputs (output saved unencrypted, warned) |
| `--ocr [LANG]` | off | invisible OCR text layer; `LANG` = Tesseract code, default `eng` |
| `-v, --verbose` | off | log every image decision and OCR page |

### GUI reference

* **File list** — *Add files…*, *Remove*, *Clear*, or drag PDFs straight from
  Explorer onto the list (needs the optional `tkinterdnd2`; the app falls back
  gracefully without it).
* **Output** — next to each input, or all into one chosen folder. The app
  refuses to start a run whose outputs would overwrite each other or an input.
* **Image quality** — slider and an editable text box (1–100), kept in sync.
* **Max image size** — High 1600 / Medium 1200 / Low 900 px / keep original.
* **Lossless only** checkbox — skip the image pass.
* **Add searchable text (OCR)** checkbox + language picker — available when
  Tesseract is detected on the machine (PATH or Program Files); the control
  is disabled with an install hint when it is not.
* Compression runs on a worker thread; the window stays responsive with a
  per-file progress bar and a log of results. Closing is blocked while busy.

---

## How compression works

Two guaranteed passes plus an optional third:

1. **Image pass** (the big win on scanned/graphic PDFs)
   * Enumerate every indirect stream with `/Subtype /Image` (1-bit stencil
     masks and images under 64×64 px are left alone).
   * Decode with pikepdf → Pillow. `/SMask`/`/Mask` transparency is **not**
     baked into pixels (the mask entries stay attached — applying both would
     fade the image toward white).
   * Downsample with Lanczos when the longest edge exceeds `--max-dim`
     (1600 px ≈ 150 dpi on Letter/A4; visually sufficient for text scans).
   * Re-encode as optimized JPEG at `--quality`; CMYK/palette/16-bit/alpha
     variants are converted to RGB/gray first. If the JPEG is not smaller
     than the original encoding, the image is left untouched — file growth
     is impossible.
   * Alpha `SMask` images are resized and rewritten with identical geometry
     in lockstep with their base image; a mask shared with a second,
     differently-sized base is never rewritten a second time.
   * A non-identity `/Decode` array is either kept (JPEG sources, where
     pikepdf does *not* pre-apply it) or deleted (all other filters, where it
     *is* pre-applied into the pixels) — so tone inversion can never be
     double-applied.
2. **Structural pass** (always)
   * QPDF recompresses Flate streams, and the document is written with object
     streams generated. PDF/A preservation settings are kept by default.
3. **OCR pass** (`--ocr`, optional — works on image *and* vector pages)
   * Each page is rendered at up to 300 dpi (capped at 4000 px per edge)
     with pdfium, read by Tesseract, and the words are written back as an
     invisible text overlay (`Tr 3`): the page looks identical, but Ctrl-F
     and copy-paste work. Rotated pages are skipped (logged) rather than
     risk mispositioned words. Overhead is ~1–3 s per page and a few KB.

**What it cannot shrink:** pure vector content (CAD/traffic maps drawn as line
work) is already compressed; those bytes are the floor. Example: a 134-page
map appendix lands at ~5.5 MB no matter how low the image settings go —
though `--ocr` does make those maps' labels searchable.

---

## Architecture

```
pdf_compressor_gui.py          compress_pdf.py
┌──────────────────────┐       ┌──────────────────────────────┐
│ App (tkinter/frame)  │       │ CLI: main(argv)              │
│  file list + DnD     │       │  └─ compress_file(src, dst)  │
│  options + worker    │──────▶│      ├─ Pdf.open             │
│  queue + after()     │ calls │      ├─ compress_images(pdf) │
│  smoke() self-test   │       │      ├─ ocr_layer (opt.)     │
└──────────────────────┘       │      └─ pdf.save (qpdf opts) │
                               └──────────────────────────────┘
   pikepdf (QPDF 12.4.2) + Pillow 13 + [pdfium · Tesseract · reportlab]
```

* **Single core, two faces.** `compress_file()` is the only entry point;
  the GUI passes a log callback into the same code path the CLI prints from.
* **Threading model.** GUI worker thread runs pikepdf/Pillow only; all Tk
  access is confined to the main thread via a `queue.Queue` drained by an
  `after(80, poll)` loop. No shared mutable state across threads.
* **Crash containment.** Any single undecodable / oversized (Pillow JPEG
  limit is 65500 px edge; pikepdf's 500 Mpx decompression-bomb guard also
  applies) / damaged image is *skipped with a log line*; the document still
  compresses. Only unrecoverable errors (bad password, unreadable file) abort.
* **Self-test.** `PDFCompressor.exe --smoke` / `python pdf_compressor_gui.py
  --smoke` builds the widget tree, loads tkdnd, round-trips a real
  compression — plus an OCR round-trip when Tesseract is present — and exits
  0/1; used to verify the frozen binary.
* **OCR chain.** compress_file → ocr_layer.add_text_layer: pdfium render →
  pytesseract word boxes → reportlab invisible overlay → pikepdf
  `Page.add_overlay` merged back with CropBox-accurate placement.

### Invariants

* Every replaced payload is strictly smaller than its original; the tool
  cannot make a file bigger.
* Input files are never modified; overwrite attempts (input-as-output of
  another queued file, duplicate outputs) are rejected up front.
* `QpdfJob --check` exits clean on all produced outputs (verified).

---

## Tools used
| Tool | Version used | Purpose |
|---|---|---|
| Python | 3.12 | language runtime |
| [pikepdf](https://github.com/pikepdf/pikepdf) | 10.18.1 | PDF object model, image access (wraps **QPDF 12.4.2**) |
| [Pillow](https://python-pillow.org) | 13.0.0 | image decode / Lanczos resample / JPEG encode |
| tkinter | bundled | GUI widgets |
| [tkinterdnd2](https://github.com/Eliav2/tkinterdnd2) | 0.7.0 | Explorer drag & drop (tkdnd 2.10, OLE on Windows) |
| [Tesseract](https://github.com/UB-Mannheim/tesseract) | 5.4 | OCR engine, detected at runtime (optional) |
| [pytesseract](https://github.com/madmaze/pytesseract) / pypdfium2 / reportlab | latest | OCR pass: engine wrapper / page rendering / invisible-text overlay |
| [PyInstaller](https://pyinstaller.org) | 6.22.3 | one-file Windows exe (`--windowed --collect-all tkinterdnd2`) |
| ruff / bandit | latest | lint + static security scan (clean at medium+ severity) |

Rebuild the exe with:

```bash
pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed \
    --name PDFCompressor --icon app_icon.ico \
    --collect-all tkinterdnd2 pdf_compressor_gui.py
./dist/PDFCompressor.exe --smoke   # must exit 0
```

---

## Security & privacy notes

* Fully offline; no network calls, no telemetry, no third-party services.
  The OCR pass only ever invokes the locally installed Tesseract binary.
* PDF JavaScript is not executed at any point (QPDF rewrites structure only).
* `--password` on the command line is visible in process listings and shell
  history — prefer the GUI, or protect files before sharing.
* Encrypted inputs are decrypted on open; **output is written unencrypted**
  and the tool says so loudly. Set protection again afterwards if needed.
* Malformed/oversized images are skipped rather than processed; the
  decompression-bomb limits (500 Mpx per image, 65500 px JPEG edge) are
  enforced by pikepdf/Pillow, and OCR renders are capped at 4000 px/edge.

## Limitations / design trade-offs

* Images inside Form XObjects are reached (via the object graph), but
  `BI…EI` *inline* images are not rewritten.
* 1-bit scans converted by the resize path become grayscale JPEGs only when
  that is actually smaller; otherwise they stay as-is — black-and-white
  fidelity is preserved.
* Vector drawings cannot be compressed (the floor described above), though
  the OCR pass does make vector pages' text searchable.
* OCR words are positioned from Tesseract boxes; the invisible layer is
  right for search/copy, with only approximate highlight geometry.
  Rotated pages are skipped by the OCR pass. The exe finds Tesseract on
  PATH or in Program Files; install the engine separately for OCR.

## License

GPL-3.0-only — see [LICENSE](LICENSE). Document-processing libraries used are
permissively or GPL-compatible licensed (pikepdf MPL-2.0, Pillow MIT-style,
QPDF Apache-2.0, pdfium Apache-2.0, Tesseract Apache-2.0, reportlab BSD-3).
