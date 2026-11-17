"""Invisible OCR text layer for PDFs: renders pages with pypdfium2, reads
words with Tesseract, and overlays them as invisible (Tr 3) selectable text.

Used by compress_pdf.py when --ocr is requested. All heavy imports are lazy
so the compressor core keeps working without the OCR stack installed.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
from pathlib import Path

OCR_DPI = 300
MAX_RENDER_PX = 4000       # cap OCR render size for oversized pages (maps)

_INSTALL_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def tesseract_cmd() -> str | None:
    """Path to a usable tesseract binary: bundled (exe), PATH, or installs."""
    if getattr(sys, "frozen", False):
        p = Path(sys._MEIPASS) / "tesseract" / "tesseract.exe"
        if p.is_file():
            return str(p)
    found = shutil.which("tesseract")
    if found:
        return found
    for cand in _INSTALL_PATHS:
        if Path(cand).is_file():
            return cand
    return None


def available() -> bool:
    return tesseract_cmd() is not None


def languages() -> list[str]:
    """Language codes whose traineddata ships next to the engine."""
    cmd = tesseract_cmd()
    if not cmd:
        return []
    tessdata = Path(cmd).parent / "tessdata"
    langs = sorted(p.stem for p in tessdata.glob("*.traineddata")
                   if p.stem not in ("osd", "equ") and not p.stem.startswith("script"))
    return langs or ["eng"]


def add_text_layer(pdf, lang: str = "eng", dpi: int = OCR_DPI, log=None):
    """Overlay invisible OCR text on every page of an open pikepdf Pdf.

    Builds one overlay page per source page (blank where OCR was skipped) so
    the merge is strictly index-aligned. Returns (pages_with_text, words).
    Raises RuntimeError only when the engine is missing; per-page failures
    are logged and leave that page without a text layer.
    """
    import pikepdf
    cmd = tesseract_cmd()
    if not cmd:
        raise RuntimeError(
            "Tesseract not installed — from https://github.com/UB-Mannheim/"
            "tesseract/wiki or: winget install UB-Mannheim.TesseractOCR")
    import pytesseract
    import pypdfium2 as pdfium
    from reportlab.pdfgen import canvas

    pytesseract.pytesseract.tesseract_cmd = cmd
    os.environ["TESSDATA_PREFIX"] = str(Path(cmd).parent / "tessdata")

    # pdfium renders a finished PDF: snapshot the current (post-compress) state
    snap = io.BytesIO()
    pdf.save(snap)
    snap.seek(0)
    rpdf = pdfium.PdfDocument(snap)

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    pages_ok = words_ok = 0
    filled: list[tuple[int, float, float, bool]] = []
    try:
        for i in range(len(pdf.pages)):
            srcpage, rpage = pdf.pages[i], rpdf[i]
            w_pt, h_pt = rpage.get_size()
            scale = min(dpi / 72.0, MAX_RENDER_PX / max(w_pt, h_pt))
            c.setPageSize((w_pt, h_pt))
            ok = False
            if int(getattr(srcpage, "rotation", 0) or 0) % 360:
                if log:
                    log(f"  OCR page {i+1}: skipped (rotated)")
            else:
                try:
                    pil = rpage.render(scale=scale).to_pil()
                    data = pytesseract.image_to_data(
                        pil, lang=lang, output_type=pytesseract.Output.DICT)
                    del pil
                    t = c.beginText()
                    t.setTextRenderMode(3)          # invisible: selectable
                    words = 0
                    ps = 1.0 / scale                  # px -> pt
                    for j, text in enumerate(data["text"]):
                        if not text.strip():
                            continue
                        hp = max(data["height"][j] * ps, 6)
                        t.setFont("Helvetica", hp * 0.85)
                        t.setTextOrigin(data["left"][j] * ps,
                                        h_pt - data["top"][j] * ps - hp * 0.8)
                        t.textOut(text)
                        words += 1
                    c.drawText(t)
                    ok = words > 0
                    pages_ok += ok
                    words_ok += words
                    if log:
                        log(f"  OCR page {i+1}: {words} words"
                            + ("" if words else " (nothing found)"))
                except Exception as exc:
                    if log:
                        log(f"  OCR page {i+1}: failed ({exc})")
            c.showPage()
            filled.append((i, w_pt, h_pt, ok))
    finally:
        rpdf.close()
    if not pages_ok:
        return 0, 0
    c.save()
    buf.seek(0)
    with pikepdf.Pdf.open(buf) as overlay:
        for slot, (i, w_pt, h_pt, use) in enumerate(filled):
            if not use:
                continue
            srcpage = pdf.pages[i]
            box = srcpage.get("/CropBox", srcpage.mediabox)
            ll, bb = float(box[0]), float(box[1])
            srcpage.add_overlay(overlay.pages[slot],
                                rect=pikepdf.Rectangle(ll, bb, ll + w_pt, bb + h_pt))
    return pages_ok, words_ok
