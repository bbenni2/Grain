"""
Bracket module — detection, grouping, and subfolder sorting for exposure bracketing.

Detection strategy (Lumix G9 / AEB):
  1. Parse ExposureBiasValue from EXIF for every photo
  2. Group photos that were taken within a short time window (< 4 seconds)
     AND share very similar pHash (same scene, different exposure)
  3. Within a time-window group, confirm bracketing if EV values differ
     by a consistent step (e.g. 0, -2, +2 or -1, 0, +1 EV)
  4. Assign BracketRole: BASE (0 EV or closest to 0), UNDER, OVER, MEMBER

Subfolder sorting (replaces HDR merge):
  - Quality check: shake_detected == False AND base frame composite score >= min_composite_score
  - Pass: RAW files are moved into session_dir/Brackets/Bracket_NN/ and archive_path updated
  - Fail (quality): frames stay in session_dir; non-base frames labelled KEEP
  - Fail (shake): all frames labelled REJECT (existing behaviour, unchanged)

Important interactions with the rest of the pipeline:
  - Bracket members must NOT be marked as pHash duplicates (they look alike!)
  - Only the BASE frame is forwarded to AI analysis (avoids 3× token waste)
  - Non-base bracket frames get label KEEP (not TOP/REJECT) unless the base is REJECT
"""

import io
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from pipeline import BracketGroup, BracketRole, Label, PhotoRecord, SessionReport

console = Console()

try:
    import cv2
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

try:
    import rawpy
    _RAWPY_AVAILABLE = True
except ImportError:
    _RAWPY_AVAILABLE = False

RAW_EXTENSIONS = {".rw2", ".cr2", ".cr3", ".nef", ".arw"}

# Maximum seconds between two consecutive frames within ONE bracket burst
# Ein einzelner 7-Frame AEB-Burst bei 9fps dauert <0.8s — 1.5s ist großzügig genug
BRACKET_TIME_WINDOW = 1.5

# Maximaler Abstand zwischen zwei AUFEINANDERFOLGENDEN Frames innerhalb eines Bursts.
# Längere Pause = neuer Burst beginnt (anderes Motiv oder neue AEB-Sequenz)
MAX_INTER_FRAME_GAP = 1.5

# pHash Hamming distance threshold for "same scene" detection.
# Brackets haben durch Belichtungsunterschied mehr pHash-Abstand als normale Duplikate
# → deutlich großzügigere Schwelle als beim Duplikat-Check (8)
# Belichtungsunterschied von ±2EV kann pHash-Distanz von 20+ verursachen
BRACKET_PHASH_DISTANCE = 28

# Minimum EV difference between frames to confirm it's a bracket (not burst)
MIN_EV_STEP = 0.5

# Typical AEB frame counts
VALID_BRACKET_SIZES = {2, 3, 5, 7}

# Minimum shutter ratio to confirm exposure bracketing (1.6 catches 2/3-stop AEB steps)
# 1-stop = ratio 2.0, 2/3-stop = ratio ~1.59, 1/2-stop = ratio ~1.41
MIN_SHUTTER_RATIO = 1.6


# ---------------------------------------------------------------------------
# EV bias parsing
# ---------------------------------------------------------------------------

def _parse_ev_bias(record: PhotoRecord) -> Optional[float]:
    """
    Parse ExposureBiasValue from EXIF via exiftool.
    Returns float (e.g. -2.0, 0.0, 2.0) or None.
    """
    path = record.archive_path or record.original_path
    try:
        result = subprocess.run(
            ["exiftool", "-j", "-ExposureCompensation", "-ExposureBiasValue", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            if data:
                d = data[0]
                for key in ("ExposureCompensation", "ExposureBiasValue",
                        "ExposureBracketValue", "AEBBracketValue"):
                    val = d.get(key)
                    if val is not None:
                        # exiftool may return "0" or "+2" or "-1.33" etc.
                        try:
                            return float(str(val).replace("+", ""))
                        except ValueError:
                            pass
    except Exception:
        pass
    return None


def _parse_timestamp(record: PhotoRecord) -> Optional[float]:
    """Return datetime_original as POSIX timestamp."""
    if not record.datetime_original:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(record.datetime_original, fmt).timestamp()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Bracket detection
# ---------------------------------------------------------------------------

def _hamming(h1: str, h2: str) -> int:
    try:
        return bin(int(h1, 16) ^ int(h2, 16)).count("1")
    except Exception:
        return 999


def _split_consecutive_bursts(frames: list, max_gap: float = MAX_INTER_FRAME_GAP) -> list[list]:
    """
    Teilt eine nach Zeit sortierte Frame-Liste an natürlichen Lücken auf.
    Lücke > max_gap → neue Gruppe (neuer AEB-Burst oder neues Motiv).

    Auto-Modus: max_gap = 1.5s  (Standard für normale Burst-Serien)
    Fixed-Modus: max_gap = 15s  (HDR-Brackets mit langen Belichtungen)
    """
    if not frames:
        return []
    bursts = [[frames[0]]]
    for i in range(1, len(frames)):
        ts_prev = _parse_timestamp(frames[i - 1]) or 0.0
        ts_curr = _parse_timestamp(frames[i]) or 0.0
        if ts_curr - ts_prev > max_gap:
            bursts.append([frames[i]])
        else:
            bursts[-1].append(frames[i])
    return bursts


def _split_burst_into_brackets(burst: list) -> list[list]:
    """
    Split a time-grouped burst into individual bracket sequences.

    Problem: If you shoot two 3-frame AEB sequences back-to-back with < 1.5s
    between them, _split_consecutive_bursts puts all 6 frames in one burst.
    This function tries to sub-divide that burst into proper bracket groups.

    Strategy:
    1. If burst size is already in VALID_BRACKET_SIZES → one group, done.
    2. Try to split evenly into sub-groups of each valid size (smallest first).
       Verify each sub-group still has sufficient shutter variation.
    3. Fallback: return the burst as a single group.
    """
    n = len(burst)
    if n in VALID_BRACKET_SIZES:
        return [burst]

    # Try splitting into equal chunks of each valid bracket size
    for sub_size in sorted(VALID_BRACKET_SIZES):
        if n % sub_size != 0:
            continue
        chunks = [burst[i:i + sub_size] for i in range(0, n, sub_size)]
        # Verify each chunk has enough shutter variation to be a real bracket
        all_valid = True
        for chunk in chunks:
            s_vals = [getattr(r, '_shutter', 0.0) for r in chunk]
            s_valid = [s for s in s_vals if s > 0]
            if len(s_valid) >= 2:
                ratio = max(s_valid) / min(s_valid)
                if ratio < MIN_SHUTTER_RATIO:
                    all_valid = False
                    break
        if all_valid:
            return chunks

    # No clean split found — return as-is (one big group, best effort)
    return [burst]


def _detect_bracket_groups(records: list) -> list[list[PhotoRecord]]:
    """
    Identify groups of photos that form exposure bracket sequences.

    Strategie:
    1. Alle Fotos nach Zeitstempel sortieren
    2. An natürlichen Zeit-Lücken (>MAX_INTER_FRAME_GAP) in Bursts aufteilen
    3. Jeden Burst auf Belichtungsvariation (Shutter-Ratio oder EV-Spread) prüfen
    4. Valide Bursts = separate Bracket-Gruppen (je Burst ein Unterordner)

    Returns list of groups (each group is a list of PhotoRecords).
    """
    eligible = [r for r in records if not r.was_skipped]
    if not eligible:
        return []

    def _sort_key(r):
        ts = _parse_timestamp(r) or 0.0
        return (ts, r.filename)

    eligible.sort(key=_sort_key)

    # Alle Frames in natürliche Bursts aufteilen
    all_bursts = _split_consecutive_bursts(eligible)

    groups = []
    debug_rejected = 0

    for burst in all_bursts:
        if len(burst) < 2:
            continue

        # Shutter-Variation prüfen (Lumix AEB ändert Shutter, nicht EV-Bias)
        shutters = [getattr(r, '_shutter', 0.0) for r in burst]
        shutters_valid = [s for s in shutters if s > 0]

        if len(shutters_valid) >= 2:
            shutter_ratio = max(shutters_valid) / min(shutters_valid)
            if shutter_ratio < MIN_SHUTTER_RATIO:
                # Zu wenig Variation → Burst-Serie, kein Bracket
                debug_rejected += 1
                continue
        else:
            # Kein Shutter-Daten: EV-Spread prüfen
            ev_values = []
            for r in burst:
                ev = r.ev_bias
                if ev is None:
                    ev = _parse_ev_bias(r)
                    r.ev_bias = ev
                ev_values.append(ev)
            non_none_evs = [v for v in ev_values if v is not None]
            if len(non_none_evs) < 2:
                debug_rejected += 1
                continue
            ev_spread = max(non_none_evs) - min(non_none_evs)
            if ev_spread < MIN_EV_STEP:
                debug_rejected += 1
                continue

        # ── Sub-split bursts that contain multiple AEB sequences ──────────────
        # E.g. 6 frames = two 3-frame AEB sequences shot back-to-back (<1.5s gap)
        sub_groups = _split_burst_into_brackets(burst)
        for sg in sub_groups:
            groups.append(sg)
            console.print(
                f"[dim]  ✓ Bracket erkannt: {sg[0].filename}…{sg[-1].filename} "
                f"({len(sg)} Frames)[/dim]"
            )

    if debug_rejected:
        console.print(f"[dim]  {debug_rejected} Burst(s) nicht als Bracket erkannt (kein EV/Shutter-Unterschied).[/dim]")

    return groups


def _assign_bracket_roles(group: list[PhotoRecord], group_id: int) -> BracketGroup:
    """
    Assign BracketRole to each frame and return a BracketGroup descriptor.
    BASE = frame with EV bias closest to 0 (or best composite score if EV unknown).
    """
    ev_non_none = [r.ev_bias for r in group if r.ev_bias is not None]
    ev_range = (max(ev_non_none) - min(ev_non_none)) if ev_non_none else 0.0
    # EV bias is only trustworthy when it actually spreads across the sequence.
    # Many cameras (e.g. Lumix AEB) leave ExposureBiasValue at 0 for every frame
    # and vary the shutter speed instead — in that case ev_range stays ~0 and we
    # must fall back to shutter timing rather than picking an arbitrary frame.
    has_usable_ev = len(ev_non_none) >= 2 and ev_range >= MIN_EV_STEP

    if has_usable_ev:
        # Assign roles by EV bias — BASE is the frame closest to 0 EV.
        base_record = min(
            group,
            key=lambda r: abs(r.ev_bias) if r.ev_bias is not None else 999
        )
        for r in group:
            r.bracket_group_id = group_id
            ev = r.ev_bias
            if r is base_record:
                r.bracket_role = BracketRole.BASE
            elif ev is not None and ev < -MIN_EV_STEP / 2:
                r.bracket_role = BracketRole.UNDER
            elif ev is not None and ev > MIN_EV_STEP / 2:
                r.bracket_role = BracketRole.OVER
            else:
                r.bracket_role = BracketRole.MEMBER
    else:
        shutters = [getattr(r, "_shutter", 0.0) for r in group]
        if all(s > 0 for s in shutters) and max(shutters) > min(shutters):
            # Shutter-based AEB: the median exposure is the natural "0 EV" BASE,
            # longer shutter = brighter = OVER, shorter shutter = darker = UNDER.
            ordered = sorted(group, key=lambda r: getattr(r, "_shutter", 0.0))
            base_record = ordered[len(ordered) // 2]
            base_shutter = getattr(base_record, "_shutter", 0.0)
            for r in group:
                r.bracket_group_id = group_id
                s = getattr(r, "_shutter", 0.0)
                if r is base_record:
                    r.bracket_role = BracketRole.BASE
                elif s > base_shutter:
                    r.bracket_role = BracketRole.OVER
                elif s < base_shutter:
                    r.bracket_role = BracketRole.UNDER
                else:
                    r.bracket_role = BracketRole.MEMBER
        else:
            # No EV and no shutter info — fall back to best composite as BASE.
            base_record = max(group, key=lambda r: r.local_scores.composite)
            for r in group:
                r.bracket_group_id = group_id
                r.bracket_role = (
                    BracketRole.BASE if r is base_record else BracketRole.MEMBER
                )

    return BracketGroup(
        group_id=group_id,
        frame_count=len(group),
        ev_range=round(ev_range, 2),
        base_filename=base_record.filename,
    )


# ---------------------------------------------------------------------------
# Shake-Erkennung via Phase Correlation
# ---------------------------------------------------------------------------

# Maximale Verschiebung in Pixeln (bei ANALYZE_SIZE ~600px) bis Bracket als "shaky" gilt
MAX_SHIFT_PX = 12.0


def _load_small_gray(record: PhotoRecord, size: int = 512) -> Optional[np.ndarray]:
    """Kleines Graustufen-Array für Shake-Analyse laden."""
    path = record.archive_path or record.original_path
    ext = path.suffix.lower()
    img_pil = None

    if ext in {".rw2", ".cr2", ".cr3", ".nef", ".arw"} and _RAWPY_AVAILABLE:
        try:
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img_pil = Image.open(io.BytesIO(thumb.data))
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    img_pil = Image.fromarray(thumb.data)
        except Exception:
            return None
    elif _PIL_AVAILABLE:
        try:
            img_pil = Image.open(path)
            img_pil.load()
        except Exception:
            return None

    if img_pil is None:
        return None

    w, h = img_pil.size
    scale = size / max(w, h)
    img_pil = img_pil.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return np.array(img_pil.convert("L"), dtype=np.float32)


def detect_bracket_shake(group: list, max_shift_px: float = MAX_SHIFT_PX) -> tuple[bool, float]:
    """
    Erkennt Kamera-Wackeln zwischen Bracket-Frames via Phase Correlation.

    Vergleicht den BASE-Frame mit allen anderen Frames.
    Gibt (shake_detected, max_shift_px) zurück.

    Phase Correlation ist schnell und belichtungsunabhängig —
    funktioniert also auch bei stark unterschiedlich belichteten Frames.
    """
    if not _CV2_AVAILABLE:
        return False, 0.0

    base = next((r for r in group if r.bracket_role == BracketRole.BASE), group[0])
    base_gray = _load_small_gray(base)
    if base_gray is None:
        return False, 0.0

    max_shift = 0.0

    for r in group:
        if r is base:
            continue
        other_gray = _load_small_gray(r)
        if other_gray is None:
            continue

        # Beide Arrays auf gleiche Größe bringen
        h = min(base_gray.shape[0], other_gray.shape[0])
        w = min(base_gray.shape[1], other_gray.shape[1])
        a = base_gray[:h, :w]
        b = other_gray[:h, :w]

        try:
            # Phase Correlation liefert (dx, dy) Verschiebungsvektor
            (dx, dy), _ = cv2.phaseCorrelate(a, b)
            shift = np.sqrt(dx**2 + dy**2)
            max_shift = max(max_shift, shift)
        except cv2.error:
            pass

    shake = max_shift > max_shift_px
    return shake, round(max_shift, 1)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _detect_fixed_bracket_groups(records: list, frames_per_sequence: int) -> list[list[PhotoRecord]]:
    """
    Fixed-count bracket grouping — no EXIF guessing needed.

    When the user explicitly sets frames_per_sequence (e.g. 3), we split every
    consecutive burst of frames into groups of exactly that size.  This is far
    more reliable than auto-detection because it doesn't depend on Shutter EXIF
    or EV bias metadata being present.

    A "burst" is still defined by MAX_INTER_FRAME_GAP: a gap > 1.5 s starts a
    new sequence.  Within each burst, frames are chunked into groups of
    frames_per_sequence.  Any trailing frames that don't fill a full group are
    left ungrouped (treated as single shots).
    """
    eligible = [r for r in records if not r.was_skipped]
    if not eligible:
        return []

    eligible.sort(key=lambda r: (_parse_timestamp(r) or 0.0, r.filename))
    # Use a generous 15s gap: HDR brackets with +3EV can have frames up to ~8s apart.
    # 15s still separates brackets from different scenes without false merges.
    bursts = _split_consecutive_bursts(eligible, max_gap=15.0)

    groups = []
    for burst in bursts:
        if len(burst) < frames_per_sequence:
            console.print(
                f"[dim yellow]  ⚠ Burst zu klein: {len(burst)} Frames "
                f"(brauche {frames_per_sequence}) — {burst[0].filename}…{burst[-1].filename}[/dim yellow]"
            )
            continue
        # Chunk into fixed-size groups; discard incomplete trailing chunk
        for i in range(0, len(burst) - frames_per_sequence + 1, frames_per_sequence):
            chunk = burst[i: i + frames_per_sequence]
            if len(chunk) == frames_per_sequence:
                groups.append(chunk)
                console.print(
                    f"[dim]  ✓ Bracket (fix {frames_per_sequence}): "
                    f"{chunk[0].filename}…{chunk[-1].filename}[/dim]"
                )
        remainder = len(burst) % frames_per_sequence
        if remainder:
            console.print(
                f"[dim yellow]  ⚠ {remainder} Rest-Frame(s) am Ende von Burst "
                f"({burst[-remainder].filename}…) nicht gruppiert[/dim yellow]"
            )
    return groups


def process_brackets(
    records: list,
    report: SessionReport,
    session_dir: Path,
    min_composite_score: float = 20.0,
    frames_per_sequence: int = 0,
) -> list[BracketGroup]:
    """
    Detect bracket sequences in records, assign roles, and sort into subfolders.

    For each bracket group, two quality checks are applied:
      1. shake_detected == False
      2. Base frame composite score >= min_composite_score

    If both pass: RAW files are moved into
      session_dir / "Brackets" / "Bracket_NN"
    and r.archive_path is updated for every frame in the group.

    If quality fails (but no shake): frames stay in session_dir; non-base
    frames receive label KEEP.

    If shake is detected: all frames receive label REJECT (unchanged behaviour).

    Side effects on records:
    - Sets bracket_group_id, bracket_role, ev_bias on each bracket member
    - Non-base frames: label set to KEEP when not rejected
    - archive_path updated in-place when files are moved to a subfolder

    Returns list of BracketGroup descriptors.
    """
    importable = [r for r in records if not r.was_skipped]

    if not importable:
        return []

    if frames_per_sequence > 0:
        mode_label = f"fix {frames_per_sequence} Frames/Reihe"
    else:
        mode_label = "auto"
    console.print(
        f"[bold]🔲 Bracket-Erkennung:[/bold] {len(importable)} Fotos "
        f"[dim]({mode_label})[/dim]…"
    )

    # Step 1: Read shutter/EV EXIF for all photos in one batch call
    _batch_read_ev_bias(importable)

    # Step 2: Detect groups — fixed count or auto
    if frames_per_sequence > 0:
        groups = _detect_fixed_bracket_groups(importable, frames_per_sequence)
    else:
        groups = _detect_bracket_groups(importable)

    if not groups:
        console.print("[dim]Keine Belichtungsreihen gefunden.[/dim]")
        return []

    bracket_descriptors: list[BracketGroup] = []

    console.print(
        f"[bold cyan]📷 {len(groups)} Belichtungsreihe(n) gefunden[/bold cyan] "
        f"({sum(len(g) for g in groups)} Frames total)"
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Verarbeite Brackets…", total=len(groups))

        for group_id, group in enumerate(groups):
            progress.update(task, advance=1,
                            description=f"Gruppe {group_id + 1}: {group[0].filename}…")

            # Assign roles
            descriptor = _assign_bracket_roles(group, group_id)

            # Shake-Erkennung
            shake_detected, max_shift = detect_bracket_shake(group)
            descriptor.shake_detected = shake_detected
            descriptor.max_shift_px = max_shift

            if shake_detected:
                # Schlechtes Bracket — alle Frames auf REJECT setzen
                for r in group:
                    r.label = Label.REJECT
                    r.is_duplicate = False
                    r.skip_reason = f"Bracket verwackelt ({max_shift:.0f}px Versatz)"
                bracket_descriptors.append(descriptor)
                continue

            # Non-base frames: immer KEEP setzen
            for r in group:
                if r.bracket_role != BracketRole.BASE:
                    r.label = Label.KEEP
                r.is_duplicate = False

            # Quality check: base frame composite score.
            # In FIXED mode the user explicitly declared "these are N-frame
            # brackets", so we always sort them into a subfolder — a low composite
            # on a deliberately dark/bright frame must not block foldering.
            # AUTO mode keeps the quality floor as a guard against false positives.
            base_record = next(
                (r for r in group if r.bracket_role == BracketRole.BASE), group[0]
            )
            force_sort = frames_per_sequence > 0
            quality_ok = (
                force_sort
                or base_record.local_scores.composite >= min_composite_score
            )

            if quality_ok:
                # Move RAW files into Brackets/Bracket_NN subfolder
                subfolder = session_dir / "Brackets" / f"Bracketing_{group_id + 1:02d}"
                subfolder.mkdir(parents=True, exist_ok=True)

                for r in group:
                    src = r.archive_path
                    if src and src.exists():
                        dst = subfolder / src.name
                        try:
                            shutil.move(str(src), str(dst))
                            r.archive_path = dst
                        except OSError as exc:
                            console.print(
                                f"[yellow]⚠ Konnte {src.name} nicht verschieben: {exc}[/yellow]"
                            )

                descriptor.subfolder = subfolder
                report.bracket_sorted += 1
            # If quality fails, frames stay in place; KEEP labels already set above

            bracket_descriptors.append(descriptor)

    # Update report stats
    report.bracket_groups = len(bracket_descriptors)
    report.bracket_frames = sum(len(g) for g in groups)

    # Summary
    sorted_count = sum(1 for d in bracket_descriptors if d.subfolder)
    console.print(
        f"[green]✅ {len(bracket_descriptors)} Reihe(n)[/green] — "
        f"{report.bracket_frames} Frames — "
        f"[cyan]{sorted_count} in Unterordner sortiert[/cyan]"
    )

    return bracket_descriptors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_shutter(val) -> float:
    """Parse ExposureTime value — handles decimal (0.004) and fraction strings ('1/250')."""
    if val is None:
        return 0.0
    try:
        s = str(val).strip()
        if '/' in s:
            num, den = s.split('/', 1)
            return float(num) / float(den)
        return float(s)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def _batch_read_ev_bias(records: list) -> None:
    """
    Read ExposureCompensation + ExposureTime for all records in a single exiftool call.
    Paths werden via Temp-File übergeben (kein Kommandozeilen-Limit bei 400+ Dateien).
    Uses -n for reliable numeric output.
    """
    if not shutil.which("exiftool"):
        return

    paths = [str(r.archive_path or r.original_path) for r in records]

    # Pfade via Temp-File übergeben (-@ filelist) — vermeidet "Argument list too long"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                         delete=False, encoding="utf-8") as f:
            f.write("\n".join(paths) + "\n")
            tmp_path = f.name

        result = subprocess.run(
            ["exiftool", "-j", "-n",
             "-FileName",
             "-ExposureCompensation", "-ExposureBiasValue",
             "-ExposureBracketValue", "-AEBBracketValue",
             "-ExposureTime",
             "-@", tmp_path],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0 or not result.stdout.strip():
            console.print(f"[dim yellow]⚠ exiftool batch-EV fehlgeschlagen (rc={result.returncode})[/dim yellow]")
            return

        data = json.loads(result.stdout)
        # Build lookup by filename
        ev_by_name: dict = {}
        shutter_by_name: dict = {}
        for item in data:
            name = Path(item.get("SourceFile", item.get("FileName", ""))).name
            ev = None
            for key in ("ExposureCompensation", "ExposureBiasValue",
                        "ExposureBracketValue", "AEBBracketValue"):
                val = item.get(key)
                if val is not None:
                    try:
                        ev = float(str(val).replace("+", ""))
                        break
                    except ValueError:
                        pass
            shutter = _parse_shutter(item.get("ExposureTime"))
            if name:
                ev_by_name[name] = ev
                shutter_by_name[name] = shutter

        for r in records:
            if r.ev_bias is None:
                r.ev_bias = ev_by_name.get(r.filename)
            r._shutter = shutter_by_name.get(r.filename, 0.0)

        # Diagnose: zeige wie viele Photos nützliche Shutter-Daten haben
        with_shutter = sum(1 for r in records if getattr(r, '_shutter', 0.0) > 0)
        nonzero_ev = sum(1 for r in records if r.ev_bias is not None and r.ev_bias != 0.0)
        console.print(
            f"[dim]  EXIF-Batch: {with_shutter}/{len(records)} mit Shutter-Zeit, "
            f"{nonzero_ev} mit EV≠0[/dim]"
        )

    except Exception as exc:
        console.print(f"[dim yellow]⚠ _batch_read_ev_bias Fehler: {exc}[/dim yellow]")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def pre_mark_brackets(records: list, frames_per_sequence: int = 0) -> int:
    """
    Schnelle Bracket-Vormarkierung VOR dem Culling.

    Schützt Bracket-Frames vor fälschlicher pHash-Duplikat-Markierung.
    Setzt bracket_role = MEMBER für erkannte Kandidaten.

    frames_per_sequence > 0  → fixe Gruppengröße, kein EXIF-Raten nötig
    frames_per_sequence == 0 → auto-Erkennung via Shutter-Ratio
    """
    eligible = [r for r in records if not r.was_skipped]
    if not eligible:
        return 0

    _batch_read_ev_bias(eligible)
    eligible_sorted = sorted(eligible, key=lambda r: (_parse_timestamp(r) or 0.0, r.filename))
    # Fixed mode needs a larger gap (15s) because long-exposure HDR frames can be >1.5s apart
    gap = 15.0 if frames_per_sequence > 0 else MAX_INTER_FRAME_GAP
    all_bursts = _split_consecutive_bursts(eligible_sorted, max_gap=gap)

    marked = 0
    for burst in all_bursts:
        if len(burst) < 2:
            continue

        if frames_per_sequence > 0:
            # Fixed mode: mark every frame in bursts that are multiples of frame count
            if len(burst) >= frames_per_sequence:
                for r in burst:
                    if r.bracket_role == BracketRole.NONE:
                        r.bracket_role = BracketRole.MEMBER
                        marked += 1
        else:
            # Auto mode: check shutter ratio
            shutters = [getattr(r, '_shutter', 0.0) for r in burst]
            shutters_valid = [s for s in shutters if s > 0]
            if len(shutters_valid) < 2:
                continue
            if max(shutters_valid) / min(shutters_valid) < MIN_SHUTTER_RATIO:
                continue
            for r in burst:
                if r.bracket_role == BracketRole.NONE:
                    r.bracket_role = BracketRole.MEMBER
                    marked += 1

    if marked:
        console.print(f"[dim]🔲 Bracket-Vormarkierung: {marked} Frames geschützt (pHash-Dedup übersprungen).[/dim]")
    return marked


