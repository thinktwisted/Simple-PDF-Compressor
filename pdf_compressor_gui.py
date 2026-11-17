#!/usr/bin/env python3
"""Simple GUI front-end for compress_pdf.py.

Double-click / run with no arguments to open the window.
Hidden flag:  --smoke   headless self-test (returns exit code 0/1).
"""
from __future__ import annotations

import queue
import sys
import tempfile
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

import compress_pdf as core

try:
    import ocr_layer
except ImportError:
    ocr_layer = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:              # without it the app works, minus drag & drop
    DND_FILES = None
    TkinterDnD = None

RES_OPTIONS = [
    ("High — 1600 px (default)", 1600),
    ("Medium — 1200 px", 1200),
    ("Low — 900 px", 900),
    ("Keep original resolution", 0),
]


def human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.2f} MiB"
    return f"{n / 1024:.0f} KiB"


def enable_dnd(root: tk.Tk) -> bool:
    """Enable tkdnd on an existing root; True when drag & drop is usable."""
    if DND_FILES is None:
        return False
    try:
        root.TkdndVersion = TkinterDnD.require(root)
        return True
    except Exception:
        return False


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.master = master
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        master.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- file list ----
        box = ttk.LabelFrame(self, text="PDF files", padding=6)
        box.pack(fill="both", expand=False)
        frm = ttk.Frame(box)
        frm.pack(fill="both")
        self.list = tk.Listbox(frm, height=8, selectmode=tk.EXTENDED)
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=sb.set)
        self.list.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        btns = ttk.Frame(box)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Add files…", command=self.add_files).pack(side="left")
        ttk.Button(btns, text="Remove", command=self.remove_sel).pack(side="left", padx=4)
        ttk.Button(btns, text="Clear",
                   command=lambda: self.list.delete(0, "end")).pack(side="left")

        # ---- output location ----
        out = ttk.LabelFrame(self, text="Save compressed copies", padding=6)
        out.pack(fill="x", pady=(8, 0))
        self.out_mode = tk.StringVar(value="next")
        ttk.Radiobutton(out, text="Next to each input file",
                        variable=self.out_mode, value="next").pack(anchor="w")
        row = ttk.Frame(out)
        row.pack(fill="x", pady=(2, 0))
        ttk.Radiobutton(row, text="Into folder:", variable=self.out_mode,
                        value="dir").pack(side="left")
        self.out_dir = tk.StringVar()
        ttk.Entry(row, textvariable=self.out_dir, width=42).pack(
            side="left", padx=4, fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self.browse_dir).pack(side="left")

        # ---- options ----
        opt = ttk.LabelFrame(self, text="Compression", padding=6)
        opt.pack(fill="x", pady=(8, 0))
        r1 = ttk.Frame(opt)
        r1.pack(fill="x")
        ttk.Label(r1, text="Image quality:").pack(side="left")
        self.quality = tk.StringVar(value="60")
        self.scale = ttk.Scale(r1, from_=15, to=95, variable=self.quality,
                               orient="horizontal", length=220,
                               command=self._sync_from_scale)
        self.scale.pack(side="left", padx=6)
        self.quality_entry = ttk.Entry(
            r1, width=4, justify="center", textvariable=self.quality,
            validate="key", validatecommand=(self.register(self._valid_quality), "%P"))
        self.quality_entry.pack(side="left")
        r2 = ttk.Frame(opt)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="Max image size:").pack(side="left")
        self.res = ttk.Combobox(r2, state="readonly", width=28,
                                values=[t for t, _ in RES_OPTIONS])
        self.res.current(0)
        self.res.pack(side="left", padx=6)
        self.lossless = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Lossless only (don't touch images)",
                        variable=self.lossless).pack(anchor="w")
        r3 = ttk.Frame(opt)
        r3.pack(fill="x", pady=(6, 0))
        self.ocr_on = tk.BooleanVar(value=False)
        self.ocr_cb = ttk.Checkbutton(r3, text="Add searchable text (OCR)",
                                       variable=self.ocr_on)
        self.ocr_cb.pack(side="left")
        self.ocr_lang = ttk.Combobox(r3, state="readonly", width=7, values=["eng"])
        self.ocr_lang.current(0)
        self.ocr_lang.pack(side="left", padx=6)
        self.ocr_note = ttk.Label(r3, foreground="#666")
        self.ocr_note.pack(side="left")
        self.ocr_have = ocr_layer is not None and ocr_layer.available()
        if self.ocr_have:
            langs = ocr_layer.languages()
            self.ocr_lang["values"] = langs
            self.ocr_lang.set("eng" if "eng" in langs else langs[0])
            self.ocr_note.configure(text="adds ~1-3 s per page")
        else:
            self.ocr_cb.state(["disabled"])
            self.ocr_lang.state(["disabled"])
            self.ocr_note.configure(text="requires Tesseract (winget install "
                                          "UB-Mannheim.TesseractOCR)")

        # ---- run / status ----
        run = ttk.Frame(self)
        run.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(run, text="Compress", command=self.start)
        self.start_btn.pack(side="left")
        self.status = ttk.Label(run, text="Idle", anchor="e")
        self.status.pack(side="right")
        self.pbar = ttk.Progressbar(self, mode="determinate")
        self.pbar.pack(fill="x", pady=(6, 8))
        logf = ttk.LabelFrame(self, text="Log", padding=4)
        logf.pack(fill="both", expand=True)
        self.log = tk.Text(logf, height=8, state="disabled", wrap="word")
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")

        # ---- drag & drop ----
        self.dnd_active = False
        if getattr(self.master, "TkdndVersion", None):
            try:
                self.list.drop_target_register(DND_FILES)
                self.list.dnd_bind("<<Drop>>", self._on_drop)
                self.dnd_active = True
            except tk.TclError:
                pass
        hint = ttk.Label(box, foreground="#666",
                         text="Tip: drag PDF files here from Explorer")
        if self.dnd_active:
            hint.pack(anchor="w", pady=(2, 0))

        self.after(80, self._poll)

    # ------------------------------------------------------------ widgets
    def add_files(self):
        self._add_paths(filedialog.askopenfilenames(
            title="Select PDF files", filetypes=[("PDF files", "*.pdf")]))

    def _add_paths(self, paths) -> int:
        have = set(self.list.get(0, "end"))
        added = 0
        for p in paths:
            p = str(p)
            if p not in have and Path(p).suffix.lower() == ".pdf":
                self.list.insert("end", p)
                have.add(p)
                added += 1
        return added

    def remove_sel(self):
        for i in reversed(self.list.curselection()):
            self.list.delete(i)

    def browse_dir(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.out_dir.set(d)
            self.out_mode.set("dir")

    def _on_drop(self, event):
        items = list(self.master.tk.splitlist(event.data))
        added = self._add_paths(items)
        if not added and items:
            self._log(f"Dropped {len(items)} item(s): no new PDF files added")

    def _drop_paths(self, data: str) -> list[str]:
        return [str(p) for p in self.master.tk.splitlist(data)
                if str(p).lower().endswith(".pdf")]

    def _sync_from_scale(self, v: str):
        s = str(int(round(float(v))))
        if s != self.quality.get():
            self.quality.set(s)

    def _valid_quality(self, proposal: str) -> bool:
        return proposal == "" or (proposal.isdigit() and 1 <= int(proposal) <= 100)

    def _on_close(self):
        if self.busy:
            self.master.bell()
            self.status.configure(text="Wait for Done before closing.")
            return
        self.master.destroy()

    def _log(self, msg: str):
        self.q.put(("log", msg))

    # ------------------------------------------------------------ run
    def start(self):
        if self.busy:
            return
        files = [Path(p) for p in self.list.get(0, "end")]
        if not files:
            self._log("Add at least one PDF first.")
            return
        outdir = Path(self.out_dir.get()) if self.out_mode.get() == "dir" else None
        if self.out_mode.get() == "dir" and (not self.out_dir.get()
                                             or not outdir.is_dir()):
            self._log("Choose a valid output folder.")
            return
        qtxt = self.quality.get().strip()
        if not qtxt.isdigit() or not 1 <= int(qtxt) <= 100:
            self._log("Enter an image quality (1-100) or use the slider.")
            return
        quality = int(qtxt)
        lossless = self.lossless.get()
        dsts = [(outdir or p.parent) / f"{p.stem}_compressed.pdf" for p in files]
        if len({d.resolve() for d in dsts}) != len(files):
            self._log("Two selected inputs would write the same output; remove one.")
            return
        if {d.resolve() for d in dsts} & {p.resolve() for p in files}:
            self._log("A selected input is another file's output; it would be "
                      "overwritten. Remove it from the list.")
            return
        max_dim = RES_OPTIONS[self.res.current()][1]
        ocr = self.ocr_lang.get() if (self.ocr_on.get() and self.ocr_have) else None
        self.busy = True
        self.start_btn.state(["disabled"])
        self.pbar.configure(maximum=len(files), value=0)
        self.status.configure(text=f"Compressing 0/{len(files)}…")
        threading.Thread(target=self._worker,
                         args=(files, dsts, quality, max_dim, lossless, ocr),
                         daemon=True).start()

    def _worker(self, files, dsts, quality, max_dim, lossless, ocr):
        ok = 0
        for i, (src, dst) in enumerate(zip(files, dsts, strict=True), 1):
            self.q.put(("file", i, src.name))
            try:
                if dst.resolve() == src.resolve():
                    raise ValueError("refusing to overwrite the input file")
                before, after = core.compress_file(
                    src, dst, quality=quality, max_dim=max_dim,
                    no_images=lossless, ocr=ocr, log=self._log)
                if after >= before:
                    self.q.put(("result", f"{src.name}: no reduction "
                                          f"({human(before)} -> {human(after)}) -> {dst}"))
                else:
                    pct = 100 * (1 - after / before)
                    self.q.put(("result", f"{src.name}: {human(before)} -> "
                                          f"{human(after)}  ({pct:.0f}% smaller) -> {dst}"))
                ok += 1
            except Exception as exc:
                kind = "encrypted (needs a password, use the CLI)" if type(
                    exc).__name__ == "PasswordError" else str(exc)
                self.q.put(("error", f"{src.name}: FAILED — {kind}"))
        self.q.put(("done", ok, len(files)))

    # ------------------------------------------------------------ polling
    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log" or kind == "result":
                    self._append(item[1])
                elif kind == "error":
                    self._append(item[1])
                    self.master.bell()
                elif kind == "file":
                    self.pbar.configure(value=item[1] - 1)
                    self.status.configure(
                        text=f"Compressing {item[1] - 1}/{self.pbar['maximum']}… {item[2]}")
                elif kind == "done":
                    ok, total = item[1], item[2]
                    self.pbar.configure(value=total)
                    self.status.configure(text=f"Done — {ok}/{total} files")
                    self.busy = False
                    self.start_btn.state(["!disabled"])
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _append(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def smoke() -> int:
    """Headless check: tkinter runtime works + compression round-trips."""
    try:
        root = tk.Tk()
        root.withdraw()
        dnd = enable_dnd(root)
        app = App(root)
        if dnd and not app.dnd_active:
            return 1
        root.update_idletasks()
        if dnd:
            if app._drop_paths("C:/x/a.pdf {C:/two words/b.pdf} C:/x/icon.png") \
                    != ["C:/x/a.pdf", "C:/two words/b.pdf"]:
                return 1
        root.destroy()

        from PIL import Image, ImageDraw
        tmp = Path(tempfile.mkdtemp())
        im = Image.new("RGB", (1400, 1800), "white")
        d = ImageDraw.Draw(im)
        for y in range(0, 1800, 3):  # high-frequency noise -> big JPEG
            d.line([(0, y), (1400, y + 90)], fill=((y * 7) % 256,
                     (y * 3) % 256, (y * 11) % 256), width=2)
        src = tmp / "s.pdf"
        im.save(src, quality=95)
        before, after = core.compress_file(src, tmp / "out.pdf",
                                           quality=50, max_dim=1000)
        rc = 0 if after < before else 1
        if rc == 0 and ocr_layer is not None and ocr_layer.available():
            from PIL import ImageFont
            page = Image.new("L", (1700, 2200), 245)
            ImageDraw.Draw(page).text((200, 180), "OCR SMOKE TOKEN 42",
                                      fill=5, font=ImageFont.load_default(size=40))
            s2 = tmp / "s2.pdf"
            page.save(s2, quality=90)
            got: list[str] = []
            core.compress_file(s2, tmp / "o2.pdf", quality=50, max_dim=1000,
                               ocr="eng", log=got.append)
            if not any(m.strip().startswith("OCR:") and "0 words" not in m
                       for m in got):
                rc = 1
        return rc
    except Exception:
        traceback.print_exc()
        return 1


def main() -> int:
    if "--smoke" in sys.argv:
        return smoke()
    root = tk.Tk()
    enable_dnd(root)
    root.title("PDF Compressor")
    root.geometry("640x560")
    root.minsize(560, 480)
    App(root).pack(fill="both", expand=True)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
