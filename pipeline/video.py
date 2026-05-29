"""
Video module — import, analyze and label video clips.

Requires: ffprobe + ffmpeg  (brew install ffmpeg)

Scoring logic:
  - Duration < min_duration → REJECT  (versehentlicher Auslöser)
  - Resolution < 720p       → REJECT  (zu gering)
  - Audio clipped (peak > −1 dBFS) → −25 Punkte
  - Audio silent (mean < −50 dB)   → −15 Punkte
  - No audio stream at all         → −5  Punkte

Labels:
  ≥ 60  → KEEP
  35–59 → REVIEW  (zeigen, manuell entscheiden)
  < 35  → REJECT
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

console = Console()

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mts", ".m2ts", ".avi", ".mkv", ".m4v", ".3gp"}


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VideoRecord:
    original_path: Path
    filename: str

    # Archive destination (set after copying)
    archive_path: Optional[Path] = None

    # Identity
    md5: Optional[str] = None
    was_skipped: bool = False   # True if already in archive (duplicate)

    # Video metadata
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    video_bitrate_kbps: int = 0

    # Audio metadata
    has_audio: bool = False
    audio_codec: str = ""
    audio_channels: int = 0
    audio_sample_rate: int = 0

    # Audio quality (from ffmpeg volumedetect)
    audio_mean_db: Optional[float] = None
    audio_peak_db: Optional[float] = None
    # "good" | "low" | "silent" | "clipped" | "none" | "unknown"
    audio_quality: str = "unknown"

    # Scoring
    quality_score: float = 0.0      # 0–100
    label: str = "UNKNOWN"          # "KEEP" | "REVIEW" | "REJECT"
    reject_reason: Optional[str] = None

    @property
    def resolution_label(self) -> str:
        if self.width == 0:
            return "?"
        h = self.height
        if h >= 2160:
            return "4K"
        if h >= 1080:
            return "1080p"
        if h >= 720:
            return "720p"
        return f"{h}p"

    @property
    def duration_str(self) -> str:
        s = int(self.duration_s)
        m, sec = divmod(s, 60)
        return f"{m}:{sec:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# ffprobe / ffmpeg helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _run_ffprobe(path: Path) -> Optional[dict]:
    """Return parsed ffprobe JSON for all streams + format, or None on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def _volumedetect(path: Path) -> tuple[Optional[float], Optional[float]]:
    """
    Run ffmpeg volumedetect pass on the audio track.
    Returns (mean_volume_dB, max_volume_dB) or (None, None) on failure.
    Fast: audio-only pass, no video decoding.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(path),
             "-af", "volumedetect",
             "-vn", "-sn", "-dn",
             "-f", "null", "/dev/null"],
            capture_output=True, text=True, timeout=120,
        )
        stderr = result.stderr
        mean_db = peak_db = None
        for line in stderr.splitlines():
            if "mean_volume:" in line:
                try:
                    mean_db = float(line.split("mean_volume:")[1].split("dB")[0].strip())
                except Exception:
                    pass
            if "max_volume:" in line:
                try:
                    peak_db = float(line.split("max_volume:")[1].split("dB")[0].strip())
                except Exception:
                    pass
        return mean_db, peak_db
    except Exception:
        return None, None


def _fast_md5(path: Path, chunk_kb: int = 128) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read(chunk_kb * 1024))
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Metadata extraction
# ─────────────────────────────────────────────────────────────────────────────

def _fill_metadata(record: VideoRecord) -> None:
    """Populate all VideoRecord fields from ffprobe output. Modifies in-place."""
    path = record.archive_path or record.original_path
    info = _run_ffprobe(path)
    if not info:
        record.label = "REVIEW"
        record.reject_reason = "ffprobe nicht verfügbar — manuell prüfen"
        return

    fmt = info.get("format", {})

    # Duration
    try:
        record.duration_s = float(fmt.get("duration", 0))
    except Exception:
        pass

    # Streams
    for stream in info.get("streams", []):
        kind = stream.get("codec_type")

        if kind == "video" and record.width == 0:
            record.width       = int(stream.get("width", 0))
            record.height      = int(stream.get("height", 0))
            record.video_codec = stream.get("codec_name", "")
            fps_str = stream.get("r_frame_rate", "0/1")
            try:
                num, den = fps_str.split("/")
                record.fps = round(float(num) / max(float(den), 1), 2)
            except Exception:
                pass
            try:
                record.video_bitrate_kbps = int(
                    stream.get("bit_rate") or fmt.get("bit_rate") or 0
                ) // 1000
            except Exception:
                pass

        elif kind == "audio" and not record.has_audio:
            record.has_audio         = True
            record.audio_codec       = stream.get("codec_name", "")
            record.audio_channels    = int(stream.get("channels", 0))
            record.audio_sample_rate = int(stream.get("sample_rate", 0))

    # Audio levels (only if audio track present and ffmpeg available)
    if record.has_audio and _ffmpeg_available():
        mean_db, peak_db = _volumedetect(path)
        record.audio_mean_db = mean_db
        record.audio_peak_db = peak_db

        if peak_db is not None and mean_db is not None:
            if peak_db >= -1.0:
                record.audio_quality = "clipped"   # Übersteuerung
            elif mean_db < -50.0:
                record.audio_quality = "silent"    # Praktisch stumm
            elif mean_db < -35.0:
                record.audio_quality = "low"       # Sehr leise
            else:
                record.audio_quality = "good"
        else:
            record.audio_quality = "unknown"
    elif not record.has_audio:
        record.audio_quality = "none"


# ─────────────────────────────────────────────────────────────────────────────
# Scoring & labeling
# ─────────────────────────────────────────────────────────────────────────────

def _score_and_label(record: VideoRecord, min_duration: float = 3.0) -> None:
    """Assign quality_score and label. Modifies record in-place."""

    # Hard reject: too short
    if record.duration_s < min_duration and record.duration_s > 0:
        record.label        = "REJECT"
        record.quality_score = 5.0
        record.reject_reason = (
            f"Zu kurz ({record.duration_s:.1f}s — wahrscheinlich versehentlich)"
        )
        return

    score   = 100.0
    reasons = []

    # Resolution
    if record.height == 0:
        score -= 15
        reasons.append("Auflösung unbekannt")
    elif record.height < 480:
        record.label        = "REJECT"
        record.quality_score = 10.0
        record.reject_reason = f"Zu niedrige Auflösung ({record.height}p)"
        return
    elif record.height < 720:
        score -= 30
        reasons.append(f"Niedrige Auflösung ({record.height}p)")

    # Audio quality
    aq = record.audio_quality
    if aq == "clipped":
        score -= 25
        reasons.append("Audio übersteuert (Pegelspitzen > −1 dBFS)")
    elif aq == "silent":
        score -= 15
        reasons.append("Kein Ton (Spur fast stumm)")
    elif aq == "low":
        score -= 10
        reasons.append("Sehr leiser Ton (< −35 dB)")
    elif aq == "none":
        score -= 5   # No audio track — might be intentional (B-Roll)

    # Duration bonus: longer clips tend to be more valuable
    if record.duration_s >= 60:
        score = min(100.0, score + 8)
    elif record.duration_s >= 10:
        score = min(100.0, score + 3)

    record.quality_score = round(max(0.0, score), 1)

    if record.quality_score >= 60:
        record.label = "KEEP"
    elif record.quality_score >= 35:
        record.label = "REVIEW"
    else:
        record.label        = "REJECT"
        record.reject_reason = " · ".join(reasons) if reasons else "Niedrige Qualität"


# ─────────────────────────────────────────────────────────────────────────────
# Archive path builder
# ─────────────────────────────────────────────────────────────────────────────

def _video_archive_path(session_dir: Path, filename: str) -> Path:
    """Videos go into session_dir/Videos/filename."""
    return session_dir / "Videos" / filename


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def process_videos(
    source_path: Path,
    session_dir: Path,
    min_duration: float = 3.0,
    dry_run: bool = False,
) -> list[VideoRecord]:
    """
    Scan source_path for video files, copy to session_dir/Videos/,
    analyze quality, assign labels.

    Returns list of VideoRecord (all found clips, including rejects).
    """
    source_path = Path(source_path)
    session_dir = Path(session_dir)

    if not _ffprobe_available():
        console.print(
            "[yellow]⚠ ffprobe nicht gefunden — Video-Analyse deaktiviert.\n"
            "  Installiere: brew install ffmpeg[/yellow]"
        )
        return []

    # ── 1. Scan ───────────────────────────────────────────────────────────────
    candidates: list[Path] = []
    for ext in VIDEO_EXTENSIONS:
        candidates += list(source_path.rglob(f"*{ext}"))
        candidates += list(source_path.rglob(f"*{ext.upper()}"))
    candidates = sorted(set(c for c in candidates if not c.is_symlink()))

    if not candidates:
        console.print("[dim]Keine Videodateien gefunden.[/dim]")
        return []

    console.print(
        f"[bold]🎬 Video-Modus:[/bold] {len(candidates)} Clip(s) gefunden …"
    )

    records: list[VideoRecord] = []

    # ── 2. Dedup (MD5-Prefix) + Copy ─────────────────────────────────────────
    existing_md5s: set[str] = set()
    videos_dir = session_dir / "Videos"
    if not dry_run:
        videos_dir.mkdir(parents=True, exist_ok=True)
        # Collect MD5s of already-archived videos to skip re-imports
        for existing in videos_dir.glob("*"):
            if existing.suffix.lower() in VIDEO_EXTENSIONS:
                try:
                    existing_md5s.add(_fast_md5(existing))
                except Exception:
                    pass

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Importiere Videos…", total=len(candidates))

        for path in candidates:
            progress.update(task, advance=1, description=f"[cyan]{path.name}[/cyan]")

            rec = VideoRecord(original_path=path, filename=path.name)

            # Dedup
            try:
                rec.md5 = _fast_md5(path)
            except Exception:
                pass

            if rec.md5 and rec.md5 in existing_md5s:
                rec.was_skipped = True
                records.append(rec)
                continue

            # Copy
            dst = videos_dir / path.name
            if not dry_run:
                try:
                    # Collision: append counter
                    if dst.exists():
                        stem, suf = path.stem, path.suffix
                        i = 1
                        while dst.exists():
                            dst = videos_dir / f"{stem}_{i}{suf}"
                            i += 1
                    shutil.copy2(str(path), str(dst))
                    rec.archive_path = dst
                    if rec.md5:
                        existing_md5s.add(rec.md5)
                except OSError as exc:
                    console.print(f"[yellow]⚠ {path.name}: {exc}[/yellow]")
                    records.append(rec)
                    continue
            else:
                rec.archive_path = dst  # dry-run: pretend

            records.append(rec)

    # ── 3. Analyze (only newly copied clips) ─────────────────────────────────
    to_analyze = [r for r in records if not r.was_skipped and r.archive_path]

    if to_analyze:
        console.print(
            f"[bold]🔬 Analysiere {len(to_analyze)} Clip(s)[/bold] "
            f"[dim](Metadaten + Audio-Level)[/dim]…"
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Analysiere…", total=len(to_analyze))
            for rec in to_analyze:
                progress.update(task, advance=1,
                                description=f"[cyan]{rec.filename}[/cyan]")
                _fill_metadata(rec)
                _score_and_label(rec, min_duration=min_duration)

    # ── 4. Summary ───────────────────────────────────────────────────────────
    _print_summary(records)

    return records


def _print_summary(records: list[VideoRecord]) -> None:
    """Print a rich table summary of processed videos."""
    if not records:
        return

    new     = [r for r in records if not r.was_skipped]
    keep    = [r for r in new if r.label == "KEEP"]
    review  = [r for r in new if r.label == "REVIEW"]
    reject  = [r for r in new if r.label == "REJECT"]
    skipped = [r for r in records if r.was_skipped]

    console.print(
        f"[bold]🎬 Videos:[/bold] "
        f"[green]✅ {len(keep)} KEEP[/green]  "
        f"[yellow]👁 {len(review)} REVIEW[/yellow]  "
        f"[red]🗑 {len(reject)} REJECT[/red]  "
        f"[dim]{len(skipped)} bereits importiert[/dim]"
    )

    if not new:
        return

    tbl = Table(show_header=True, header_style="dim", box=None,
                padding=(0, 1), min_width=60)
    tbl.add_column("Datei",       style="cyan",    max_width=28)
    tbl.add_column("Länge",       style="white",   justify="right", width=7)
    tbl.add_column("Auflösung",   style="white",   width=7)
    tbl.add_column("Audio",       style="white",   width=10)
    tbl.add_column("Score",       style="white",   justify="right", width=6)
    tbl.add_column("Label",       style="bold",    width=8)

    label_style = {"KEEP": "green", "REVIEW": "yellow", "REJECT": "red"}
    audio_labels = {
        "good":    "✅ gut",
        "low":     "⚠ leise",
        "silent":  "🔇 stumm",
        "clipped": "📢 übersteuert",
        "none":    "— kein Ton",
        "unknown": "?",
    }

    for r in sorted(new, key=lambda x: x.quality_score, reverse=True):
        style = label_style.get(r.label, "white")
        reject_note = f"  [dim]({r.reject_reason})[/dim]" if r.reject_reason else ""
        tbl.add_row(
            r.filename,
            r.duration_str,
            r.resolution_label,
            audio_labels.get(r.audio_quality, r.audio_quality),
            f"{r.quality_score:.0f}",
            f"[{style}]{r.label}[/{style}]{reject_note}",
        )

    console.print(tbl)
