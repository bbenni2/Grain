"""
Cull module — local scoring, pHash dedup, labeling.
Zero API calls. All computation is local.

Performance:
  - Each RAW file is loaded EXACTLY ONCE per photo (thumbnail extraction)
  - All scoring (sharpness, exposure, histogram, pHash, composition) shares the same image
  - ThreadPoolExecutor parallelises the per-photo work (I/O-bound → threads help)
"""

import io
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from pipeline import Label, LocalScores, PhotoRecord

console = Console()

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False
    warnings.warn("opencv-python not found — sharpness scoring disabled", stacklevel=2)

try:
    import rawpy
    _RAWPY_AVAILABLE = True
except ImportError:
    _RAWPY_AVAILABLE = False

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

try:
    import imagehash
    _IMAGEHASH_AVAILABLE = True
except ImportError:
    _IMAGEHASH_AVAILABLE = False
    warnings.warn("imagehash not found — pHash dedup disabled", stacklevel=2)

RAW_EXTENSIONS = {".rw2", ".cr2", ".cr3", ".nef", ".arw", ".tif", ".tiff"}

# ---------------------------------------------------------------------------
# Single-load image helper
# ---------------------------------------------------------------------------

def _load_pil(path: Path) -> Optional["Image.Image"]:
    """Load RAW/JPEG thumbnail as PIL Image — called ONCE per photo."""
    if not _PIL_AVAILABLE:
        return None
    ext = path.suffix.lower()
    try:
        if ext in RAW_EXTENSIONS and _RAWPY_AVAILABLE:
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = Image.open(io.BytesIO(thumb.data))
                    img.load()          # force decode while file is open
                    return img
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    return Image.fromarray(thumb.data)
                else:
                    raise ValueError("unknown thumb format")
        else:
            img = Image.open(path)
            img.load()
            return img
    except Exception:
        # RAW fallback: full decode (slow but safe)
        if ext in RAW_EXTENSIONS and _RAWPY_AVAILABLE:
            try:
                with rawpy.imread(str(path)) as raw:
                    rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
                    return Image.fromarray(rgb)
            except Exception:
                pass
    return None


def _pil_to_gray(img: "Image.Image", max_side: int = 1024) -> np.ndarray:
    """Downscale PIL image and convert to uint8 grayscale numpy array."""
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return np.array(img.convert("L"), dtype=np.uint8)


def _pil_to_color(img: "Image.Image", max_side: int = 512) -> np.ndarray:
    """Downscale PIL image and convert to uint8 RGB numpy array."""
    w, h = img.size
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return np.array(img.convert("RGB"), dtype=np.uint8)


# ---------------------------------------------------------------------------
# Scoring functions (unchanged logic, accept arrays not paths)
# ---------------------------------------------------------------------------

def _score_sharpness(gray: np.ndarray) -> tuple[float, bool]:
    """
    Center-weighted sharpness via Laplacian variance.

    Problem with global variance: a sharp background (sky, branches) rescues a
    blurry subject (bird, person) → the photo looks "sharp" overall but the
    actual subject is out of focus or motion-blurred.

    Fix: compute variance on the center 65 % crop (where subjects usually sit)
    and weight it at 70 %, full-image at 30 %.  If the center is soft while the
    background is sharp the weighted score drops significantly.
    """
    if not _CV2_AVAILABLE:
        return 50.0, False

    # Full-image sharpness
    var_full = cv2.Laplacian(gray, cv2.CV_64F).var()

    # Center crop sharpness (inner 65 % of each dimension)
    h, w = gray.shape
    cy, cx = h // 2, w // 2
    ch, cw = int(h * 0.325), int(w * 0.325)   # half-extents of 65 % crop
    center = gray[cy - ch: cy + ch, cx - cw: cx + cw]
    var_center = cv2.Laplacian(center, cv2.CV_64F).var() if center.size > 100 else var_full

    # Weighted combination — centre dominates
    variance = var_center * 0.70 + var_full * 0.30
    score = min(100.0, (variance / 500.0) * 100.0)
    return round(score, 2), variance < 100


def _score_exposure(gray: np.ndarray) -> tuple[float, bool, bool]:
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = gray.size
    shadow_clip    = hist[:5].sum() / total
    highlight_clip = hist[251:].sum() / total
    is_over  = highlight_clip > 0.05
    is_under = shadow_clip > 0.10
    penalty  = (min(shadow_clip, 0.2) + min(highlight_clip, 0.1)) * 300
    score    = max(0.0, 100.0 - penalty)
    mean_penalty = abs(gray.mean() - 128) / 128 * 20
    score    = max(0.0, score - mean_penalty)
    return round(score, 2), is_over, is_under


def _score_histogram(color: np.ndarray) -> float:
    scores = []
    for ch in range(3):
        channel = color[:, :, ch].flatten()
        hist, _ = np.histogram(channel, bins=64, range=(0, 256))
        scores.append((hist > 0).sum() / 64.0 * 100)
    return round(float(np.mean(scores)), 2)


def _compute_phash_pil(img: "Image.Image") -> Optional[str]:
    """Compute pHash directly from a PIL Image (no disk access)."""
    if not _IMAGEHASH_AVAILABLE or not _PIL_AVAILABLE:
        return None
    try:
        phash = imagehash.phash(img.convert("RGB"), hash_size=8)
        return str(phash)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-record analysis (called in worker threads)
# ---------------------------------------------------------------------------

def _analyze_record(
    record: PhotoRecord,
    sharpness_weight: float,
    exposure_weight: float,
    histogram_weight: float,
    composition_weight: float,
) -> None:
    """Score a single record. Modifies record.local_scores in-place. Thread-safe."""
    path = (record.archive_path if record.archive_path and record.archive_path.exists()
            else record.original_path)

    scores = record.local_scores

    # ── ONE disk read for everything ──────────────────────────────────────────
    img = _load_pil(path)

    if img is not None:
        gray  = _pil_to_gray(img, max_side=1024)
        color = _pil_to_color(img, max_side=512)
        # Composition uses a 600px gray — derive from same PIL to avoid 2nd load
        gray_600 = _pil_to_gray(img, max_side=600)
        phash = _compute_phash_pil(img)
    else:
        gray = color = gray_600 = None
        phash = None

    # Sharpness + Exposure
    if gray is not None:
        sharp_score, is_blurry    = _score_sharpness(gray)
        exp_score, is_over, is_under = _score_exposure(gray)
    else:
        sharp_score, is_blurry    = 50.0, False
        exp_score, is_over, is_under = 50.0, False, False

    # Histogram
    hist_score = _score_histogram(color) if color is not None else 50.0

    # Composition — uses pre-loaded array, no extra disk I/O
    try:
        from pipeline.compose import analyze_composition_from_array
        comp = analyze_composition_from_array(gray_600)
    except Exception:
        comp = {"rule_of_thirds": 50.0, "symmetry": 0.0, "symmetry_axis": "none",
                "horizon_level": 50.0, "horizon_tilt_deg": 0.0,
                "leading_lines": 50.0, "balance": 50.0, "overall": 50.0}

    # Write scores
    scores.sharpness         = sharp_score
    scores.exposure          = exp_score
    scores.histogram         = hist_score
    scores.is_blurry         = is_blurry
    scores.is_overexposed    = is_over
    scores.is_underexposed   = is_under
    scores.phash             = phash
    scores.rule_of_thirds    = comp["rule_of_thirds"]
    scores.symmetry          = comp["symmetry"]
    scores.symmetry_axis     = comp["symmetry_axis"]
    scores.horizon_level     = comp["horizon_level"]
    scores.horizon_tilt_deg  = comp["horizon_tilt_deg"]
    scores.leading_lines     = comp["leading_lines"]
    scores.balance           = comp["balance"]
    scores.composition_overall = comp["overall"]

    scores.composite = round(
        sharpness_weight    * sharp_score
        + exposure_weight   * exp_score
        + histogram_weight  * hist_score
        + composition_weight * comp["overall"],
        2,
    )


# ---------------------------------------------------------------------------
# pHash grouping (unchanged)
# ---------------------------------------------------------------------------

def _hamming_distance(hash1: str, hash2: str) -> int:
    try:
        return bin(int(hash1, 16) ^ int(hash2, 16)).count("1")
    except (ValueError, TypeError):
        return 999


def _group_duplicates(records: list, max_distance: int = 8) -> None:
    from pipeline import BracketRole
    hashed = [(i, r) for i, r in enumerate(records)
              if r.local_scores.phash and not r.was_skipped
              and r.bracket_role == BracketRole.NONE]

    parent = list(range(len(hashed)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i in range(len(hashed)):
        for j in range(i + 1, len(hashed)):
            if _hamming_distance(hashed[i][1].local_scores.phash,
                                 hashed[j][1].local_scores.phash) <= max_distance:
                union(i, j)

    from collections import defaultdict
    groups: dict = defaultdict(list)
    for idx, (orig_idx, _) in enumerate(hashed):
        groups[find(idx)].append(orig_idx)

    group_id = 0
    for members in groups.values():
        if len(members) > 1:
            for orig_idx in members:
                records[orig_idx].local_scores.phash_group = group_id
            group_id += 1


def _mark_duplicate_rejects(records: list) -> None:
    from collections import defaultdict
    from pipeline import BracketRole
    groups: dict = defaultdict(list)
    for record in records:
        if (record.local_scores.phash_group is not None
                and record.bracket_role == BracketRole.NONE):
            groups[record.local_scores.phash_group].append(record)
    for group_records in groups.values():
        best = max(group_records, key=lambda r: r.local_scores.composite)
        for r in group_records:
            if r is not best:
                r.is_duplicate = True


# ---------------------------------------------------------------------------
# Label assignment (unchanged)
# ---------------------------------------------------------------------------

def _assign_labels(records: list, top_percentile: float = 20.0, keep_threshold: float = 35.0) -> None:
    eligible = [r for r in records if not r.was_skipped and not r.is_duplicate]
    if not eligible:
        return

    # Exclude blurry photos from the percentile pool — they're hard-rejected anyway
    scoreable = [r for r in eligible if not r.local_scores.is_blurry]
    scores = sorted([r.local_scores.composite for r in scoreable], reverse=True)
    cutoff_idx = max(1, int(len(scores) * top_percentile / 100))
    top_threshold = scores[cutoff_idx - 1] if len(scores) > 1 else 0.0

    blurry_rejected = 0
    for record in records:
        if record.was_skipped:
            continue
        if record.is_duplicate:
            record.label = Label.REJECT
            continue
        # ── Hard-reject blurry photos regardless of composite score ──────────
        # is_blurry is set when Laplacian variance < 100 (motion blur, missed focus).
        # Without this, a blurry photo with good composition could still get KEEP.
        if record.local_scores.is_blurry:
            record.label = Label.REJECT
            blurry_rejected += 1
            continue
        score = record.local_scores.composite
        if score >= top_threshold and score >= keep_threshold:
            record.label = Label.TOP
        elif score >= keep_threshold:
            record.label = Label.KEEP
        else:
            record.label = Label.REJECT

    if blurry_rejected:
        console.print(f"[dim]  🌫 {blurry_rejected} verschwommene Foto(s) direkt abgelehnt.[/dim]")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def cull(
    records: list,
    top_percentile: float = 20.0,
    keep_threshold: float = 35.0,
    phash_distance: int = 8,
    sharpness_weight: float = 0.40,
    exposure_weight: float = 0.25,
    histogram_weight: float = 0.15,
    composition_weight: float = 0.20,
    max_workers: int = 4,
) -> list:
    """
    Score and label all PhotoRecords. Modifies records in-place.
    Each RAW file is loaded exactly once; analysis runs in parallel threads.
    Returns the same list for chaining.
    """
    importable = [r for r in records if not r.was_skipped]
    console.print(f"[bold]🔍 Culling:[/bold] {len(importable)} Fotos werden analysiert "
                  f"[dim]({max_workers} parallele Threads)[/dim]…")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Analysiere…", total=len(importable))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _analyze_record, record,
                    sharpness_weight, exposure_weight,
                    histogram_weight, composition_weight,
                ): record
                for record in importable
            }
            for future in as_completed(futures):
                record = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    console.print(f"[yellow]⚠ {record.filename}: {exc}[/yellow]")
                progress.update(task, advance=1,
                                description=f"[cyan]{record.filename}[/cyan]")

    _group_duplicates(records, max_distance=phash_distance)
    _mark_duplicate_rejects(records)
    _assign_labels(records, top_percentile=top_percentile, keep_threshold=keep_threshold)

    top    = sum(1 for r in records if r.label == Label.TOP)
    keep   = sum(1 for r in records if r.label == Label.KEEP)
    reject = sum(1 for r in records if r.label == Label.REJECT)
    dups   = sum(1 for r in records if r.is_duplicate)

    console.print(
        f"[bold]⭐ TOP:[/bold] {top}  "
        f"[green]✅ KEEP:[/green] {keep}  "
        f"[red]🗑 REJECT:[/red] {reject}  "
        f"[yellow](davon {dups} pHash-Duplikate)[/yellow]"
    )
    return records
