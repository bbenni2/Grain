"""
Export module — Web, Social, Archive, Print export profiles.

Export profiles:
  web:     JPEG, max 2000px long side, quality 85, sRGB, EXIF preserved
  social:  Square crop 1080×1080 (center or saliency), JPEG quality 90
  archive: TIFF 16-bit (if rawpy) or 8-bit, full resolution
  print:   TIFF, 300 DPI, full resolution

EXIF preservation via exiftool subprocess; fallback: piexif.
XMP sidecar creation for Lightroom/Capture One compatibility.
"""

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from pipeline import Label, PhotoRecord, SessionReport
from pipeline.presets import apply_preset, develop_raw_darktable, develop_raw_rawpy, load_preset_by_name

console = Console()

RAW_EXTENSIONS = {".rw2", ".cr2", ".cr3", ".nef", ".arw"}

try:
    import piexif
    _PIEXIF_AVAILABLE = True
except ImportError:
    _PIEXIF_AVAILABLE = False

try:
    import rawpy
    _RAWPY_AVAILABLE = True
except ImportError:
    _RAWPY_AVAILABLE = False


# ---------------------------------------------------------------------------
# EXIF preservation
# ---------------------------------------------------------------------------

def _exiftool_available() -> bool:
    return shutil.which("exiftool") is not None


def _copy_exif_exiftool(src: Path, dst: Path) -> bool:
    """Copy EXIF from src to dst using exiftool. Returns True on success."""
    try:
        result = subprocess.run(
            ["exiftool", "-TagsFromFile", str(src),
             "-all:all", "-overwrite_original", str(dst)],
            capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _copy_exif_piexif(src: Path, dst: Path) -> bool:
    """Fallback EXIF copy using piexif. Only works for JPEG→JPEG."""
    if not _PIEXIF_AVAILABLE:
        return False
    try:
        exif_data = piexif.load(str(src))
        piexif.insert(piexif.dump(exif_data), str(dst))
        return True
    except Exception:
        return False


def preserve_exif(src: Path, dst: Path) -> None:
    """Best-effort EXIF preservation: exiftool first, piexif fallback."""
    if _exiftool_available():
        _copy_exif_exiftool(src, dst)
    elif src.suffix.lower() in (".jpg", ".jpeg"):
        _copy_exif_piexif(src, dst)


# ---------------------------------------------------------------------------
# XMP sidecar
# ---------------------------------------------------------------------------

def write_xmp_sidecar(record: PhotoRecord) -> bool:
    """
    Write an XMP sidecar file alongside the archive original.
    Includes pipeline label, scores, keywords. Lightroom/C1 compatible.
    """
    path = record.archive_path or record.original_path
    xmp_path = path.with_suffix(".xmp")

    label_map = {"TOP": "Red", "KEEP": "Green", "REJECT": "Blue", "UNKNOWN": ""}
    xmp_label = label_map.get(record.label.value, "")

    rating = 0
    if record.label.value == "TOP":
        rating = 5
    elif record.label.value == "KEEP":
        rating = 3

    keywords = []
    if record.ai_scores and record.ai_scores.keywords:
        keywords = record.ai_scores.keywords

    kw_xml = "\n".join(f'         <rdf:li>{kw}</rdf:li>' for kw in keywords)
    kw_block = f"""      <dc:subject>
         <rdf:Bag>
{kw_xml}
         </rdf:Bag>
      </dc:subject>""" if keywords else ""

    score = record.ai_scores.final_score if record.ai_scores else record.local_scores.composite
    mood = record.ai_scores.mood if record.ai_scores else ""
    notes = record.ai_scores.notes if record.ai_scores else ""

    xmp_content = f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
   <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
      <rdf:Description rdf:about=""
            xmlns:xmp="http://ns.adobe.com/xap/1.0/"
            xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:lr="http://ns.adobe.com/lightroom/1.0/">
         <xmp:Rating>{rating}</xmp:Rating>
         <xmp:Label>{xmp_label}</xmp:Label>
         <xmp:Nickname>{record.filename}</xmp:Nickname>
{kw_block}
         <dc:description>
            <rdf:Alt>
               <rdf:li xml:lang="x-default">Score:{score:.0f} | {mood} | {notes}</rdf:li>
            </rdf:Alt>
         </dc:description>
         <lr:hierarchicalSubject>
            <rdf:Bag>
               <rdf:li>Pipeline/{record.label.value}</rdf:li>
            </rdf:Bag>
         </lr:hierarchicalSubject>
      </rdf:Description>
   </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""

    try:
        xmp_path.write_text(xmp_content, encoding="utf-8")
        record.xmp_written = True
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Image loading for export
# ---------------------------------------------------------------------------

def _load_for_export(record: PhotoRecord, darktable_cfg: dict) -> Optional[Image.Image]:
    """Load original photo as Pillow Image for export processing."""
    src = record.archive_path or record.original_path
    ext = src.suffix.lower()

    if ext in RAW_EXTENSIONS:
        # Try darktable-cli first
        if darktable_cfg.get("cli_path") or shutil.which("darktable-cli"):
            with tempfile.TemporaryDirectory() as tmpdir:
                out_tiff = Path(tmpdir) / (src.stem + ".tiff")
                ok = develop_raw_darktable(
                    src, out_tiff,
                    style=darktable_cfg.get("style", ""),
                    cli_path=darktable_cfg.get("cli_path", ""),
                    timeout=darktable_cfg.get("timeout", 120),
                )
                if ok and out_tiff.exists():
                    img = Image.open(out_tiff)
                    img.load()
                    return img

        # Fallback: rawpy
        img = develop_raw_rawpy(src)
        return img

    # JPEG/PNG
    try:
        img = Image.open(src)
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        return img
    except Exception as e:
        console.print(f"[red]Ladefehler {src.name}: {e}[/red]")
        return None


# ---------------------------------------------------------------------------
# Smart square crop (center-of-interest)
# ---------------------------------------------------------------------------

def _smart_square_crop(img: Image.Image) -> Image.Image:
    """
    Square crop using OpenCV saliency if available,
    otherwise center crop.
    """
    w, h = img.size
    side = min(w, h)

    # Try saliency-based crop
    try:
        import cv2
        arr = np.array(img.convert("RGB"))
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        saliency = cv2.saliency.StaticSaliencyFineGrained_create()
        success, sal_map = saliency.computeSaliency(bgr)
        if success:
            # Find centroid of high-saliency region
            threshold = sal_map.max() * 0.7
            ys, xs = np.where(sal_map > threshold)
            if len(xs) > 0:
                cx = int(xs.mean())
                cy = int(ys.mean())
                # Clamp crop box
                x0 = max(0, min(cx - side // 2, w - side))
                y0 = max(0, min(cy - side // 2, h - side))
                return img.crop((x0, y0, x0 + side, y0 + side))
    except Exception:
        pass

    # Fallback: center crop
    x0 = (w - side) // 2
    y0 = (h - side) // 2
    return img.crop((x0, y0, x0 + side, y0 + side))


# ---------------------------------------------------------------------------
# Export profiles
# ---------------------------------------------------------------------------

def _export_web(img: Image.Image, out_path: Path, cfg: dict) -> None:
    """Web export: JPEG, max 2000px long side, quality 85."""
    max_size = cfg.get("max_size", 2000)
    quality = cfg.get("quality", 85)

    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if max(w, h) > max_size:
        scale = max_size / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), "JPEG", quality=quality, optimize=True)


def _export_social(img: Image.Image, out_path: Path, cfg: dict) -> None:
    """Social export: square crop, 1080×1080, JPEG quality 90."""
    size = cfg.get("size", 1080)
    quality = cfg.get("quality", 90)

    img = _smart_square_crop(img)
    img = img.resize((size, size), Image.LANCZOS)

    if img.mode != "RGB":
        img = img.convert("RGB")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path), "JPEG", quality=quality, optimize=True)


def _export_archive_tiff(img: Image.Image, out_path: Path, cfg: dict) -> None:
    """Archive TIFF export (8-bit; 16-bit requires numpy/rawpy pipeline)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(str(out_path), "TIFF", compression="lzw")


def _export_print(img: Image.Image, out_path: Path, cfg: dict) -> None:
    """Print TIFF export — full resolution, 300 DPI tag."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.save(str(out_path), "TIFF", compression="lzw", dpi=(300, 300))


# ---------------------------------------------------------------------------
# Main export entry point
# ---------------------------------------------------------------------------

def export(
    records: list,
    report: SessionReport,
    export_root: Path,
    style: str = "web",
    preset_name: str = "default",
    presets_dir: Optional[Path] = None,
    export_cfg: Optional[dict] = None,
    darktable_cfg: Optional[dict] = None,
    preserve_exif_flag: bool = True,
    write_xmp: bool = True,
    labels_to_export: Optional[list] = None,
) -> list:
    """
    Export photos matching labels_to_export using the given style profile.
    Applies preset before export.
    Returns list of exported PhotoRecords.
    """
    if labels_to_export is None:
        labels_to_export = [Label.TOP, Label.KEEP]

    if export_cfg is None:
        export_cfg = {}

    if darktable_cfg is None:
        darktable_cfg = {}

    if presets_dir is None:
        presets_dir = Path("./presets")

    style_cfg = export_cfg.get(style, {})
    export_root = Path(os.path.expanduser(str(export_root)))

    # Load preset
    preset = load_preset_by_name(preset_name, presets_dir)

    to_export = [r for r in records if r.label in labels_to_export and not r.was_skipped]

    if not to_export:
        console.print(f"[yellow]Keine Fotos für Export ({style}) vorhanden.[/yellow]")
        return []

    console.print(
        f"[bold]🗂 Export:[/bold] {len(to_export)} Fotos → "
        f"[cyan]{style}[/cyan] mit Preset [cyan]{preset_name}[/cyan]"
    )

    exported = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"Exportiere ({style})…", total=len(to_export))

        for record in to_export:
            progress.update(task, advance=1, description=f"[cyan]{record.filename}[/cyan]")

            img = _load_for_export(record, darktable_cfg)
            if img is None:
                continue

            # Apply preset
            img = apply_preset(img, preset)

            src = record.archive_path or record.original_path

            # Determine output path and subfolder
            session_folder = src.parent.name  # YYYY-MM-DD_EventName
            out_dir = export_root / style / session_folder

            if style == "web":
                out_path = out_dir / (src.stem + "_web.jpg")
                _export_web(img, out_path, style_cfg)
                record.export_paths.web = out_path
            elif style == "social":
                out_path = out_dir / (src.stem + "_social.jpg")
                _export_social(img, out_path, style_cfg)
                record.export_paths.social = out_path
            elif style == "archive":
                out_path = out_dir / (src.stem + ".tiff")
                _export_archive_tiff(img, out_path, style_cfg)
                record.export_paths.archive = out_path
            elif style == "print":
                out_path = out_dir / (src.stem + "_print.tiff")
                _export_print(img, out_path, style_cfg)
                record.export_paths.print = out_path
            else:
                console.print(f"[red]Unbekannter Export-Style: {style}[/red]")
                continue

            # EXIF preservation
            if preserve_exif_flag and src.suffix.lower() in (".jpg", ".jpeg"):
                preserve_exif(src, out_path)

            # XMP sidecar (only once, on archive copy, not per export)
            if write_xmp and not record.xmp_written:
                write_xmp_sidecar(record)

            exported.append(record)

    report.export_count = len(exported)
    report.export_style = style

    console.print(f"[green]✅ {len(exported)} Fotos exportiert → {export_root / style}[/green]")
    return exported
