"""
Thumbnail loading — fast, PyQt-free.

Returns JPEG bytes (scaled) for any supported photo so the GUI can build a
QPixmap via QPixmap.loadFromData(). Kept free of any Qt import so the pipeline
package stays usable from the CLI and headless contexts.

Strategy (fast path first):
  - RAW (.rw2/.cr2/.cr3/.nef/.arw/.dng): use rawpy.extract_thumb() to pull the
    camera's embedded JPEG preview — orders of magnitude faster than a full
    demosaic, and plenty for a gallery thumbnail.
  - JPEG/PNG/TIFF: open with Pillow, downscale.

Everything degrades gracefully: any failure returns None and the caller shows a
placeholder tile instead.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageOps
    _PIL = True
except ImportError:
    _PIL = False

try:
    import rawpy
    _RAWPY = True
except ImportError:
    _RAWPY = False

RAW_EXTENSIONS = {".rw2", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".raf"}


def _pil_to_jpeg_bytes(img: "Image.Image", max_px: int, quality: int = 82) -> bytes:
    """Downscale a PIL image to fit max_px on its long side and encode as JPEG."""
    img = ImageOps.exif_transpose(img)        # honour camera orientation
    img = img.convert("RGB")
    w, h = img.size
    scale = max_px / max(w, h)
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def load_thumbnail_bytes(path: Path, max_px: int = 320) -> Optional[bytes]:
    """
    Return JPEG bytes for a thumbnail of `path`, scaled to `max_px` on the long
    side, or None if the file can't be read. Never raises.
    """
    if not _PIL:
        return None
    try:
        path = Path(path)
        if not path.exists():
            return None
        ext = path.suffix.lower()

        if ext in RAW_EXTENSIONS and _RAWPY:
            try:
                with rawpy.imread(str(path)) as raw:
                    thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = Image.open(io.BytesIO(thumb.data))
                    return _pil_to_jpeg_bytes(img, max_px)
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    img = Image.fromarray(thumb.data)
                    return _pil_to_jpeg_bytes(img, max_px)
            except Exception:
                pass  # fall through to PIL open (handles DNG with no thumb, etc.)

        with Image.open(path) as img:
            img.load()
            return _pil_to_jpeg_bytes(img, max_px)
    except Exception:
        return None
