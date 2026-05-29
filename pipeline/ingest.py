"""Ingest module — scan, dedup, copy, EXIF-based folder structure."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from pipeline import PhotoRecord

console = Console()

SUPPORTED_EXTENSIONS = {".rw2", ".jpg", ".jpeg", ".png", ".tif", ".tiff",
                         ".cr2", ".cr3", ".nef", ".arw"}

MONTH_NAMES = {
    1: "Januar", 2: "Februar", 3: "März", 4: "April",
    5: "Mai", 6: "Juni", 7: "Juli", 8: "August",
    9: "September", 10: "Oktober", 11: "November", 12: "Dezember",
}


def _md5_fast(path: Path, chunk_kb: int = 64) -> str:
    """MD5 of first chunk_kb kilobytes — fast dedup for large RAW files."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(chunk_kb * 1024))
    return h.hexdigest()


def _exiftool_metadata(path: Path) -> dict:
    """Extract EXIF metadata via exiftool subprocess."""
    try:
        result = subprocess.run(
            ["exiftool", "-j", "-DateTimeOriginal", "-Make", "-Model",
             "-LensModel", "-FocalLength", "-FNumber", "-ExposureTime",
             "-ISO", "-GPSLatitude", "-GPSLongitude", "-GPSLatitudeRef",
             "-GPSLongitudeRef", str(path)],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return data[0] if data else {}
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
        pass
    return {}


def _parse_gps(meta: dict) -> tuple[Optional[float], Optional[float]]:
    """Parse GPS coordinates from exiftool metadata."""
    try:
        lat = float(str(meta.get("GPSLatitude", "")).split()[0])
        lon = float(str(meta.get("GPSLongitude", "")).split()[0])
        if meta.get("GPSLatitudeRef", "N") == "S":
            lat = -lat
        if meta.get("GPSLongitudeRef", "E") == "W":
            lon = -lon
        return lat, lon
    except (ValueError, AttributeError, IndexError):
        return None, None


def _parse_datetime(meta: dict, path: Path) -> Optional[datetime]:
    """Parse DateTimeOriginal from EXIF, fall back to file mtime."""
    raw = meta.get("DateTimeOriginal", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(raw), fmt)
        except (ValueError, TypeError):
            pass
    # Fallback: file modification time
    return datetime.fromtimestamp(path.stat().st_mtime)


def _build_target_path(
    archive_root: Path, dt: datetime, event_name: str, filename: str,
    flat: bool = False,
) -> Path:
    """
    Build archive target path.
    flat=False (default): archive_root/YYYY/MM-MonthName/YYYY-MM-DD_EventName/filename
    flat=True:            archive_root/EventName/filename  (all in one folder)
    """
    safe_event = re.sub(r"[^\w\s-]", "", event_name).strip().replace(" ", "_") or "Unbekannt"
    if flat:
        return archive_root / safe_event / filename
    month_folder = f"{dt.month:02d}-{MONTH_NAMES[dt.month]}"
    day_folder = f"{dt.strftime('%Y-%m-%d')}_{safe_event}"
    return archive_root / str(dt.year) / month_folder / day_folder / filename


def _resolve_collision(target: Path, src_md5: str) -> Path:
    """Wenn target bereits existiert und einen anderen Inhalt hat, Datei umbenennen.
    Gibt den endgültigen (freien) Zielpfad zurück.
    Beispiel: IMG_0001.JPG → IMG_0001_1.JPG → IMG_0001_2.JPG …
    """
    if not target.exists():
        return target
    # Gleicher Inhalt → kein echter Konflikt, Pfad trotzdem zurückgeben
    try:
        with open(target, "rb") as f:
            existing_md5 = hashlib.md5(f.read()).hexdigest()
        if existing_md5 == src_md5:
            return target
    except Exception:
        pass
    # Echter Namenskonflikt → freien Zählpfad finden
    stem   = target.stem
    suffix = target.suffix
    parent = target.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _fill_record_from_meta(record: PhotoRecord, meta: dict, path: Path) -> None:
    """Populate PhotoRecord fields from exiftool metadata dict."""
    dt = _parse_datetime(meta, path)
    record.datetime_original = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None

    record.camera_make = str(meta.get("Make", "")).strip()
    record.camera_model = str(meta.get("Model", "")).strip()
    record.lens = str(meta.get("LensModel", "")).strip()
    record.focal_length = str(meta.get("FocalLength", "")).strip()
    record.aperture = str(meta.get("FNumber", "")).strip()
    record.shutter_speed = str(meta.get("ExposureTime", "")).strip()
    record.iso = str(meta.get("ISO", "")).strip()

    lat, lon = _parse_gps(meta)
    record.gps_lat = lat
    record.gps_lon = lon


def _load_state(state_file: Path) -> tuple[set, dict]:
    """Load processed MD5s and MD5→archive_path mapping from state file."""
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            md5s  = set(data.get("processed_md5s", []))
            paths = data.get("archive_paths", {})   # MD5 → archive path string
            return md5s, paths
        except (json.JSONDecodeError, KeyError):
            pass
    return set(), {}


def _save_state(state_file: Path, new_md5s: set, new_paths: dict) -> None:
    """Persist processed MD5s and archive paths to state file."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    existing_md5s, existing_paths = _load_state(state_file)
    merged_md5s  = existing_md5s  | new_md5s
    merged_paths = {**existing_paths, **new_paths}
    state_file.write_text(json.dumps({
        "processed_md5s":  list(merged_md5s),
        "archive_paths":   merged_paths,
    }, indent=2))


def ingest(
    source_path: Path,
    archive_root: Path,
    event_name: str = "Unbekannt",
    state_file: Optional[Path] = None,
    md5_chunk_kb: int = 64,
    dry_run: bool = False,
    supported_extensions: Optional[set] = None,
    flat_archive: bool = False,
) -> list:
    """
    Scan source_path, dedup, copy to archive_root, return list[PhotoRecord].

    Idempotent: already-processed MD5s (from state_file) are skipped.
    Never moves or deletes originals.
    """
    if supported_extensions is None:
        supported_extensions = SUPPORTED_EXTENSIONS

    archive_root = Path(os.path.expanduser(str(archive_root)))
    source_path = Path(os.path.expanduser(str(source_path)))

    # Load cross-session state (MD5s + archive paths)
    processed_md5s: set = set()
    archived_paths: dict = {}   # MD5 → archive path string
    if state_file:
        processed_md5s, archived_paths = _load_state(Path(os.path.expanduser(str(state_file))))

    # Discover all candidate files — Symlinks explizit ausschließen (Sicherheit)
    candidates = [
        p for p in sorted(source_path.rglob("*"))
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in supported_extensions
    ]

    # Smart JPEG dedup: JPEG überspringen wenn gleichnamiges RAW vorhanden.
    # Beispiel: IMG_0001.JPG wird ignoriert wenn IMG_0001.RW2 existiert.
    # Nur-JPEG-Fotos (kein passendes RAW) werden normal verarbeitet.
    _RAW_EXTS  = {".rw2", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf",
                  ".dng", ".pef", ".srw", ".x3f"}
    _JPEG_EXTS = {".jpg", ".jpeg"}
    raw_stems = {p.stem.lower() for p in candidates if p.suffix.lower() in _RAW_EXTS}
    jpeg_skip_stems = raw_stems  # JPEGs mit diesem Stem werden übersprungen

    # Build archive filename lookup — O(1) per lookup, O(N) einmalig.
    # Ermöglicht Pfad-Rekonstruktion für Duplikate ohne gespeicherten Pfad.
    archive_name_map: dict[str, Path] = {}
    if archived_paths:
        pass  # State hat Pfade → kein Scan nötig
    else:
        # Alter State ohne Pfade → Archiv einmalig scannen
        console.print("[dim]🔍 Archiv-Scan für Pfad-Rekonstruktion (einmalig)…[/dim]")
        for ap in archive_root.rglob("*"):
            if ap.is_file() and ap.suffix.lower() in supported_extensions:
                archive_name_map[ap.name] = ap

    records: list[PhotoRecord] = []
    session_md5s: dict[str, Path] = {}  # md5 → first path seen this session

    console.print(f"[bold]📂 Gefunden:[/bold] {len(candidates)} Dateien in [cyan]{source_path}[/cyan]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Importiere…", total=len(candidates))

        for path in candidates:
            progress.update(task, advance=1, description=f"[cyan]{path.name}[/cyan]")

            record = PhotoRecord(
                original_path=path,
                filename=path.name,
                extension=path.suffix.lower(),
            )

            # Smart JPEG skip: JPEG ignorieren wenn gleichnamiges RAW vorhanden
            if (path.suffix.lower() in _JPEG_EXTS
                    and path.stem.lower() in jpeg_skip_stems):
                record.was_skipped = True
                record.skip_reason = "JPEG übersprungen (RAW vorhanden)"
                records.append(record)
                continue

            # Fast MD5
            try:
                md5 = _md5_fast(path, md5_chunk_kb)
                record.md5 = md5
            except OSError as e:
                record.was_skipped = True
                record.skip_reason = f"Lesefehler: {e}"
                records.append(record)
                continue

            # Cross-session dedup
            if md5 in processed_md5s:
                record.is_duplicate = True
                # Archivpfad aus State wiederherstellen — Pfad muss innerhalb archive_root liegen
                _safe_root = archive_root.resolve()
                stored = archived_paths.get(md5)
                if stored:
                    p = Path(stored)
                    try:
                        # Sicherheitscheck: nur Pfade innerhalb des Archivs akzeptieren
                        if p.exists() and p.resolve().is_relative_to(_safe_root):
                            record.archive_path = p
                    except (ValueError, OSError):
                        pass  # ungültiger Pfad im State → ignorieren
                if record.archive_path is None and archive_name_map:
                    # Fallback: Dateiname im vorher gescannten Archiv nachschlagen
                    found = archive_name_map.get(path.name)
                    if found and found.exists():
                        try:
                            if found.resolve().is_relative_to(_safe_root):
                                record.archive_path = found
                        except (ValueError, OSError):
                            pass

                if record.archive_path is not None:
                    # Datei existiert noch im Archiv → überspringen
                    record.was_skipped = True
                    record.skip_reason = "Bereits importiert (state)"
                    records.append(record)
                    continue
                # Archivdatei wurde gelöscht → neu kopieren (is_duplicate bleibt True als Hinweis,
                # aber was_skipped bleibt False damit die Datei normal verarbeitet wird)
                record.is_duplicate = False

            # Within-session dedup
            if md5 in session_md5s:
                record.was_skipped = True
                record.skip_reason = f"Duplikat von {session_md5s[md5].name}"
                record.is_duplicate = True
                records.append(record)
                continue

            session_md5s[md5] = path

            # EXIF metadata
            meta = _exiftool_metadata(path)
            _fill_record_from_meta(record, meta, path)

            # Determine target path
            dt_str = record.datetime_original or ""
            try:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                dt = datetime.fromtimestamp(path.stat().st_mtime)

            target = _build_target_path(archive_root, dt, event_name, path.name, flat=flat_archive)

            # Copy file
            if not dry_run:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Namenskonflikt: andere Datei mit gleichem Namen → umbenennen
                    target = _resolve_collision(target, record.md5 or "")
                    if not target.exists():
                        shutil.copy2(path, target)
                except OSError as e:
                    record.was_skipped = True
                    record.skip_reason = f"Kopierfehler: {e}"
                    records.append(record)
                    continue

            record.archive_path = target

            records.append(record)

    # Persist state — MD5s + archive paths für künftige Re-Processing-Läufe
    if state_file and not dry_run:
        new_md5s  = {r.md5 for r in records if not r.was_skipped and r.md5}
        new_paths = {r.md5: str(r.archive_path)
                     for r in records
                     if not r.was_skipped and r.md5 and r.archive_path}
        _save_state(Path(os.path.expanduser(str(state_file))), new_md5s, new_paths)

    imported = [r for r in records if not r.was_skipped]
    duplicates = [r for r in records if r.is_duplicate]
    errors = [r for r in records if r.was_skipped and not r.is_duplicate]

    console.print(
        f"[green]✅ Importiert:[/green] {len(imported)}  "
        f"[yellow]⚠ Duplikate:[/yellow] {len(duplicates)}  "
        f"[red]✗ Fehler:[/red] {len(errors)}"
    )

    return records
