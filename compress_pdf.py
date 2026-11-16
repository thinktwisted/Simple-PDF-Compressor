#!/usr/bin/env python3
"""Compress a PDF by downsampling / re-encoding its embedded images and
tidying up the object structure.

Usage:
    python compress_pdf.py scan.pdf
    python compress_pdf.py scan.pdf -o small.pdf -q 55 --max-dim 1400

Requires: pikepdf and Pillow   (pip install pikepdf pillow)

Notes:
- Images are only replaced when the new version is actually smaller, so the
  output can never grow because of the image pass.
- 1-bit stencil masks are left untouched; re-encoding them as JPEG would make
  text look worse, not better.
- If the input was encrypted, the output is written unencrypted.
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

try:
    import pikepdf
    from pikepdf import Name, Pdf, PdfImage, Stream
except ImportError:
    sys.exit("This script needs pikepdf:  pip install pikepdf")

try:
    from PIL import Image
except ImportError:
    sys.exit("This script needs Pillow:  pip install pillow")

MIN_PIXELS = 4096  # leave small images (icons, bullets) alone


def encode_jpeg(pil: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    pil.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def jpeg_safe(pil: Image.Image) -> tuple[Image.Image, bool]:
    """Convert to a mode JPEG can store. Returns (image, colorspace_changed)."""
    if pil.mode == "CMYK":
        return pil.convert("RGB"), True
    if pil.mode in ("RGBA", "LA", "PA"):
        flat = Image.new("RGB", pil.size, "white")
        flat.paste(pil, mask=pil.getchannel("A"))
        return flat, True
    if pil.mode == "P":
        return pil.convert("RGB"), True
    if pil.mode not in ("RGB", "L"):
        return pil.convert("L"), True
    return pil, False


def decode_baked(stream: Stream) -> bool:
    """True when pikepdf already folded /Decode into the returned pixels.

    pikepdf applies the array while decoding for every filter except plain
    JPEG / JPEG2000, so those two must keep their /Decode entry untouched.
    """
    try:
        filters = [str(f) for f in PdfImage(stream).filters]
    except Exception:
        return False
    return not any(f in ("/DCTDecode", "/JPXDecode") for f in filters)


def apply_image(stream: Stream, pil: Image.Image, data: bytes,
                size: tuple[int, int], changed_cs: bool, baked: bool) -> None:
    stream.write(data, filter=Name.DCTDecode)
    stream.Width, stream.Height = size
    stream.BitsPerComponent = 8
    if changed_cs:
        stream.ColorSpace = Name.DeviceRGB if pil.mode == "RGB" else Name.DeviceGray
    if (changed_cs or baked) and Name.Decode in stream:
        del stream[Name.Decode]


def downsample(stream: Stream, size: tuple[int, int]) -> Image.Image | None:
    # /SMask and /Mask stay attached to the rewritten stream, so compositing
    # them into the pixels here would apply transparency a second time.
    keep_mask = stream.get(Name.SMask) is None and stream.get(Name.Mask) is None
    try:
        pil = PdfImage(stream).as_pil_image(apply_mask=keep_mask)
        if (pil.width, pil.height) != size:
            pil = pil.resize(size, Image.LANCZOS)
    except Exception:
        return None  # undecodable, oversized (bomb guard), or damaged dict
    return pil


def compress_images(pdf: Pdf, quality: int, max_dim: int, log=None) -> int:
    """Rewrite image XObjects. Returns total bytes saved. log: optional callable(str)."""
    images = [o for o in pdf.objects
              if isinstance(o, Stream)
              and o.get(Name.Subtype) == Name.Image
              and not o.get(Name.ImageMask)]
    mask_objs = [o.get(Name.SMask) for o in images
                 if o.get(Name.SMask) is not None
                 and isinstance(o.get(Name.SMask), Stream)]

    saved = 0
    mask_ids = {m.objgen for m in mask_objs}
    rewritten_masks = set()
    for o in images:
        if o.objgen in mask_ids:
            continue  # handled together with its base image
        try:
            w, h = int(o.Width), int(o.Height)
        except (AttributeError, TypeError, ValueError):
            continue  # damaged image dictionary: skip, don't lose the whole file
        if w * h < MIN_PIXELS:
            continue
        scale = 1.0 if max_dim <= 0 else min(1.0, max_dim / max(w, h))
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        pil = downsample(o, size)
        if pil is None:
            if log:
                log(f"  image {w}x{h}: skipped (could not decode)")
            continue
        try:
            total_old = len(o.read_raw_bytes())
        except Exception:
            continue
        sm = o.get(Name.SMask)
        sm = sm if isinstance(sm, Stream) else None
        mpil = None
        if sm is not None:
            if sm.objgen in rewritten_masks:
                continue  # shared mask already rewritten at another geometry
            try:
                same_geom = (int(sm.Width), int(sm.Height)) == (w, h)
            except (AttributeError, TypeError, ValueError):
                same_geom = False
            if not same_geom:
                continue  # don't risk desynchronising base and mask dimensions
            mpil = downsample(sm, size)
            if mpil is None:
                if log:
                    log(f"  mask {w}x{h}: skipped with its base image")
                continue
            try:
                total_old += len(sm.read_raw_bytes())
            except Exception:
                continue
        try:
            pil, changed_cs = jpeg_safe(pil)
            new = encode_jpeg(pil, quality)
            new_mask = None
            if mpil is not None:
                mpil, _ = jpeg_safe(mpil)
                if mpil.mode != "L":
                    mpil = mpil.convert("L")
                new_mask = encode_jpeg(mpil, quality)
        except Exception:
            if log:
                log(f"  image {w}x{h}: skipped (cannot JPEG-encode)")
            continue  # e.g. Pillow's 65500 px edge limit

        total_new = len(new) + (len(new_mask) if new_mask else 0)
        if total_new >= total_old:
            continue  # never make the file bigger

        apply_image(o, pil, new, size, changed_cs, decode_baked(o))
        if new_mask is not None:
            apply_image(sm, mpil, new_mask, size, True, decode_baked(sm))
            rewritten_masks.add(sm.objgen)
        saved += total_old - total_new
        if log:
            log(f"  image {w}x{h} -> {size[0]}x{size[1]}: "
                f"{total_old // 1024} -> {total_new // 1024} KiB"
                + (" (+mask)" if new_mask is not None else ""))
    return saved


def compress_file(src, dst, *, quality: int = 60, max_dim: int = 1600,
                  no_images: bool = False, linearize: bool = False,
                  password: str = "", log=None) -> tuple[int, int]:
    """Compress one PDF; returns (before_bytes, after_bytes). Raises pikepdf errors."""
    src, dst = Path(src), Path(dst)
    with Pdf.open(src, password=password) as pdf:
        if pdf.is_encrypted:
            msg = "note: input was encrypted; output is saved unencrypted"
            (log or (lambda m: print(m, file=sys.stderr)))(msg)
        if not no_images:
            saved = compress_images(pdf, quality, max_dim, log)
            if log and saved:
                log(f"  saved {saved / 1024 / 1024:.2f} MiB in images")
        pdf.save(dst,
                 compress_streams=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate,
                 recompress_flate=True,
                 linearize=linearize)
    return src.stat().st_size, dst.stat().st_size


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Compress a PDF file (image downsample + re-encode, stream cleanup).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("input", type=Path, help="PDF file to compress")
    ap.add_argument("-o", "--output", type=Path,
                    help="output file (default: <input>_compressed.pdf)")
    ap.add_argument("-q", "--quality", type=int, default=60,
                    help="JPEG quality for re-encoded images, 1-100")
    ap.add_argument("--max-dim", type=int, default=1600,
                    help="longest image edge in pixels (0 = keep resolution)")
    ap.add_argument("--no-images", action="store_true",
                    help="lossless pass only: do not touch images")
    ap.add_argument("--linearize", action="store_true",
                    help="linearize output (faster first page, slightly larger)")
    ap.add_argument("--password", default="", help="password if the input is encrypted")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="report each replaced image")
    args = ap.parse_args(argv)

    if not 1 <= args.quality <= 100:
        ap.error("--quality must be between 1 and 100")
    out = args.output or args.input.with_name(args.input.stem + "_compressed.pdf")
    if out.resolve() == args.input.resolve():
        ap.error("refusing to overwrite the input file")

    try:
        before, after = compress_file(args.input, out,
                                      quality=args.quality, max_dim=args.max_dim,
                                      no_images=args.no_images,
                                      linearize=args.linearize,
                                      password=args.password,
                                      log=print if args.verbose else None)
    except pikepdf.PasswordError:
        sys.exit(f"{args.input}: encrypted or wrong password")
    except pikepdf.PdfError as exc:
        sys.exit(f"{args.input}: {exc}")
    except OSError as exc:
        sys.exit(f"{args.input}: {exc}")

    if after >= before:
        print(f"No reduction achieved: {before/1024:.0f} KiB -> {after/1024:.0f} KiB "
              f"(try --no-images or a lower -q). Output: {out}")
    else:
        print(f"Compressed {before/1024:.0f} KiB -> {after/1024:.0f} KiB "
              f"({100 * (1 - after / before):.0f}% smaller). Output: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
