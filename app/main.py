#!/usr/bin/env python3
"""
AI Foto-Pipeline — Main Orchestrator

Usage:
  python main.py --source /Volumes/SD --event "Ponza 2025"
  python main.py --source ~/Downloads/photos --no-ai --dry-run
  python main.py --watch /Volumes/ --event "Shooting"
  python main.py --export web --session-dir ~/Pictures/Archive/2025/...
"""

import os
import signal
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule

from pipeline import SessionReport
from pipeline.ai_analyze import ai_analyze
from pipeline.cull import cull
from pipeline.export import export
from pipeline.ingest import ingest
from pipeline.report import DexFormatter, find_latest_report, populate_counts, save_report

console = Console()


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> dict:
    """Load config.yaml; return defaults if not found."""
    if config_path.exists():
        try:
            return yaml.safe_load(config_path.read_text()) or {}
        except yaml.YAMLError as e:
            console.print(f"[yellow]⚠ config.yaml Fehler: {e}. Verwende Defaults.[/yellow]")
    return {}


def _expand(path_str: str) -> Path:
    return Path(os.path.expanduser(str(path_str)))


# ---------------------------------------------------------------------------
# GPS reverse geocode (best-effort, no hard dependency)
# ---------------------------------------------------------------------------

def _city_from_gps(lat: float, lon: float) -> Optional[str]:
    """Attempt reverse geocode via nominatim. Returns city name or None."""
    try:
        import urllib.request
        import json as _json
        url = (
            f"https://nominatim.openstreetmap.org/reverse"
            f"?lat={lat}&lon={lon}&format=json&zoom=10"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "foto-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
            addr = data.get("address", {})
            return (addr.get("city") or addr.get("town") or
                    addr.get("village") or addr.get("county"))
    except Exception:
        return None


def _suggest_event_name(records: list, fallback: str = "Unbekannt") -> str:
    """Try to derive event name from GPS data of first geotagged photo."""
    for r in records:
        if r.gps_lat and r.gps_lon:
            city = _city_from_gps(r.gps_lat, r.gps_lon)
            if city:
                return city
    return fallback


# ---------------------------------------------------------------------------
# Finder Color Labels (macOS)
# ---------------------------------------------------------------------------

def _set_finder_labels(records: list, verbose: bool) -> None:
    """
    Setzt macOS Finder Farb-Labels via xattr auf archivierte Fotos.
      🟣 Lila  = TOP mit Score ≥ 90
      🔴 Rot   = TOP mit Score < 90
      🟢 Grün  = KEEP
      (REJECT / UNKNOWN → kein Label)
    """
    import subprocess
    from pipeline import Label

    # Finder-Label-Byte (Byte 9 von com.apple.FinderInfo, Bits 1–3):
    # label_number * 2 → 6=Lila(3), 12=Rot(6), 4=Grün(2)
    COLOR_PURPLE = 6
    COLOR_RED    = 12
    COLOR_GREEN  = 4

    labeled = 0
    for r in records:
        if r.was_skipped or not r.archive_path or not r.archive_path.exists():
            continue
        score = r.ai_scores.final_score if r.ai_scores else r.local_scores.composite
        if r.label == Label.TOP and score >= 90:
            color_byte = COLOR_PURPLE
        elif r.label == Label.TOP:
            color_byte = COLOR_RED
        elif r.label == Label.KEEP:
            color_byte = COLOR_GREEN
        else:
            continue

        try:
            result = subprocess.run(
                ["xattr", "-px", "com.apple.FinderInfo", str(r.archive_path)],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    info = bytearray.fromhex(result.stdout.strip().replace(" ", ""))
                except ValueError:
                    info = bytearray(32)
                if len(info) < 32:
                    info += bytearray(32 - len(info))
            else:
                info = bytearray(32)

            info[9] = (info[9] & 0xF1) | color_byte
            hex_val = " ".join(f"{b:02X}" for b in info)
            subprocess.run(
                ["xattr", "-wx", "com.apple.FinderInfo", hex_val, str(r.archive_path)],
                capture_output=True,
            )
            labeled += 1
        except Exception:
            pass

    if verbose:
        if labeled:
            console.print(
                f"[dim]🏷  {labeled} Finder-Labels gesetzt "
                f"(🟣 Score≥90, 🔴 TOP, 🟢 KEEP).[/dim]"
            )
        else:
            eligible = [r for r in records if not r.was_skipped
                        and r.archive_path and r.archive_path.exists()
                        and r.label in (__import__('pipeline').Label.TOP,
                                        __import__('pipeline').Label.KEEP)]
            console.print(
                f"[dim yellow]🏷  0 Finder-Labels gesetzt "
                f"({len(eligible)} TOP/KEEP mit gültigem Archiv-Pfad).[/dim yellow]"
            )


# ---------------------------------------------------------------------------
# _TOP/ Ordner & Finder öffnen
# ---------------------------------------------------------------------------

def _create_top_folder(records: list, session_dir: Path, verbose: bool) -> Optional[Path]:
    """
    Erstellt session_dir/_TOP/ mit Symlinks zu allen TOP-Fotos.
    Bestehenden _TOP/ Ordner wird vorher geleert (sauberer Neustart).
    Gibt den Pfad zurück, oder None wenn keine TOP-Fotos vorhanden.
    """
    import shutil
    from pipeline import Label, BracketRole

    top_photos = [
        r for r in records
        if r.label == Label.TOP
        and not r.was_skipped
        and r.archive_path
        and r.archive_path.exists()
        and r.bracket_role in (BracketRole.NONE, BracketRole.BASE)  # nur BASE aus Brackets
    ]

    if not top_photos:
        return None

    top_dir = session_dir / "_TOP"
    if top_dir.exists():
        shutil.rmtree(top_dir)
    top_dir.mkdir()

    for r in top_photos:
        link = top_dir / r.archive_path.name
        try:
            link.symlink_to(r.archive_path)
        except Exception:
            pass

    if verbose:
        console.print(f"[dim]📁 _TOP/ Ordner erstellt — {len(top_photos)} Fotos verlinkt.[/dim]")

    return top_dir


def _open_in_finder(path: Path) -> None:
    """Öffnet den Ordner in macOS Finder."""
    import subprocess
    try:
        subprocess.Popen(["open", str(path)])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Reject-Archivierung
# ---------------------------------------------------------------------------

def _delete_rejects(records: list, archive_root: Path, verbose: bool) -> int:
    """
    Löscht alle REJECT-Fotos (+ XMP-Sidecars) direkt aus dem Archiv.
    Originale bleiben auf der SD-Karte — kein ZIP nötig.
    Sicherheits-Check: löscht nur Dateien innerhalb von archive_root.
    Gibt die Anzahl gelöschter Dateien zurück.
    """
    from pipeline import Label

    safe_root = archive_root.resolve()
    deleted = 0
    for r in records:
        if (r.label == Label.REJECT
                and not r.was_skipped
                and r.archive_path
                and r.archive_path.exists()):
            try:
                # Sicherheits-Check: nur Dateien innerhalb archive_root löschen
                if not r.archive_path.resolve().is_relative_to(safe_root):
                    console.print(
                        f"[yellow]⚠ Sicherheit: {r.archive_path} liegt außerhalb des Archivs — übersprungen.[/yellow]"
                    )
                    continue
                r.archive_path.unlink()
                xmp = r.archive_path.with_suffix(".xmp")
                if xmp.exists() and xmp.resolve().is_relative_to(safe_root):
                    xmp.unlink()
                deleted += 1
            except Exception:
                pass

    if verbose and deleted:
        console.print(f"[dim]🗑  {deleted} Rejects aus Archiv entfernt.[/dim]")

    return deleted


# ---------------------------------------------------------------------------
# Full pipeline run
# ---------------------------------------------------------------------------

def run_pipeline(
    source_path: Path,
    config: dict,
    event_name: str = "",
    run_ai: bool = True,
    dry_run: bool = False,
    jpeg_reject: bool = False,
    run_videos: bool = True,
    export_style: Optional[str] = None,
    export_preset: Optional[str] = None,
    verbose: bool = True,
    archive_root_override: Optional[str] = None,
) -> SessionReport:
    """Execute the full pipeline and return a SessionReport."""

    start_time = time.time()
    session_id = str(uuid.uuid4())[:8]

    # Prozess-Priorität senken → Mac bleibt responsiv während Pipeline läuft
    pipeline_cfg = config.get("pipeline", {})
    nice_level = int(pipeline_cfg.get("nice_level", 10))
    try:
        import os as _os
        _os.nice(nice_level)
    except Exception:
        pass  # Windows / keine Berechtigung → ignorieren

    paths_cfg = config.get("paths", {})
    archive_root = _expand(paths_cfg.get("archive_root", "~/Pictures/Archive"))
    if archive_root_override:
        archive_root = Path(os.path.expanduser(archive_root_override))
    export_root = _expand(paths_cfg.get("export_root", "~/Pictures/Exports"))
    presets_dir = _expand(paths_cfg.get("presets_dir", "./presets"))
    state_file = _expand(paths_cfg.get("state_file", "~/.photo_pipeline_state.json"))

    culling_cfg = config.get("culling", {})
    ai_cfg      = config.get("ai", {})
    darktable_cfg = config.get("darktable", {})
    export_cfg = config.get("export", {})
    ingest_cfg = config.get("ingestion", {})
    bracket_cfg = config.get("bracketing", {})
    video_cfg  = config.get("videos", {})

    if verbose:
        console.print(Rule("[bold cyan]📸 Foto-Pipeline gestartet[/bold cyan]"))
        console.print(f"  Quelle:   [cyan]{source_path}[/cyan]")
        console.print(f"  Archiv:   [cyan]{archive_root}[/cyan]")
        if dry_run:
            console.print("[bold yellow]  DRY-RUN — keine Dateien werden verändert[/bold yellow]")

    # --- STEP 1: INGEST ---
    if verbose:
        console.print(Rule("[dim]1/6  Ingestion[/dim]"))

    exts = set(ingest_cfg.get("supported_extensions", []))
    records = ingest(
        source_path=source_path,
        archive_root=archive_root,
        event_name=event_name or ingest_cfg.get("event_name", "Unbekannt"),
        state_file=state_file,
        md5_chunk_kb=ingest_cfg.get("md5_chunk_kb", 64),
        dry_run=dry_run,
        supported_extensions=exts or None,
        flat_archive=bool(paths_cfg.get("flat_archive", False)),
    )

    if not records:
        console.print("[yellow]Keine Fotos gefunden.[/yellow]")
        return SessionReport(session_id=session_id, started_at=start_time)

    # Früher Stopp: alle Fotos bereits importiert
    new_records = [r for r in records if not r.was_skipped]
    dup_records  = [r for r in records if r.was_skipped and r.is_duplicate]
    if not new_records and dup_records:
        # Versions-Check: wurde die Session mit einer älteren Pipeline-Version verarbeitet?
        from pipeline import PIPELINE_VERSION
        from pipeline.report import load_report
        existing_report = None
        for r in dup_records:
            if r.archive_path:
                candidate = r.archive_path.parent / ".pipeline_report.json"
                if candidate.exists():
                    existing_report = load_report(candidate)
                    break

        old_version = existing_report.get("pipeline_version", "0.0.0") if existing_report else "0.0.0"

        def _ver(v: str) -> tuple:
            try:
                return tuple(int(x) for x in v.split("."))
            except Exception:
                return (0, 0, 0)

        if _ver(old_version) < _ver(PIPELINE_VERSION):
            console.print(Panel(
                f"[cyan]🔄 Neuere Pipeline-Version ({PIPELINE_VERSION})[/cyan]\n\n"
                f"Diese Session wurde mit Version [yellow]{old_version}[/yellow] verarbeitet.\n"
                f"Die Analyse wird mit der neuen Version wiederholt und überschrieben.\n",
                border_style="cyan",
            ))
            # Aktuellen Session-Ordner ermitteln
            current_session_dir = None
            for r in dup_records:
                if r.archive_path:
                    current_session_dir = r.archive_path.parent
                    break

            # Alte Duplikat-Session-Ordner mit gleichem Event-Namen löschen
            if current_session_dir and current_session_dir.parent.exists():
                # Event-Suffix aus aktuellem Ordner-Namen extrahieren (alles nach erstem "_")
                current_name = current_session_dir.name
                suffix = current_name[current_name.find("_"):] if "_" in current_name else None
                if suffix:
                    old_dirs_removed = 0
                    for d in sorted(current_session_dir.parent.iterdir()):
                        if (d.is_dir()
                                and d != current_session_dir
                                and d.name.endswith(suffix)):
                            try:
                                import shutil
                                shutil.rmtree(d)
                                old_dirs_removed += 1
                                console.print(f"[dim]🗑  Alter Session-Ordner gelöscht: [cyan]{d.name}[/cyan][/dim]")
                            except Exception as e:
                                console.print(f"[yellow]⚠ Konnte {d.name} nicht löschen: {e}[/yellow]")
                    if old_dirs_removed:
                        console.print(f"[dim]🧹 {old_dirs_removed} veraltete Session-Ordner bereinigt.[/dim]")

            # Alte XMP-Sidecars aus vorherigen Läufen entfernen
            xmp_removed = 0
            for r in dup_records:
                if r.archive_path:
                    xmp = r.archive_path.with_suffix(".xmp")
                    if xmp.exists():
                        try:
                            xmp.unlink()
                            xmp_removed += 1
                        except Exception:
                            pass
            # Auch _HDR.xmp Reste aus alten HDR-Merge-Läufen entfernen
            if current_session_dir:
                for old_xmp in current_session_dir.rglob("*_HDR.xmp"):
                    try:
                        old_xmp.unlink()
                        xmp_removed += 1
                    except Exception:
                        pass
            if xmp_removed:
                console.print(f"[dim]🧹 {xmp_removed} alte XMP-Sidecars entfernt.[/dim]")
            # Re-processing: Duplicate-Flags zurücksetzen damit Culling normal läuft
            for r in dup_records:
                r.was_skipped = False
                r.is_duplicate = False
                r.label = __import__('pipeline').Label.UNKNOWN
        else:
            console.print(Panel(
                f"[yellow]⚠ Bereits importiert[/yellow]\n\n"
                f"Alle [bold]{len(dup_records)}[/bold] Fotos auf der Karte sind schon im Archiv.\n"
                f"Nichts zu tun — Pipeline wird nicht erneut ausgeführt.\n\n"
                f"[dim]Neue Fotos aufnehmen und Karte neu einlegen.[/dim]",
                border_style="yellow",
            ))
            return SessionReport(session_id=session_id, started_at=start_time,
                                 total_found=len(records),
                                 total_skipped_duplicate=len(dup_records))

    # Auto event name from GPS if not provided
    if (not event_name and
            config.get("watcher", {}).get("auto_event_from_gps", True)):
        suggested = _suggest_event_name(
            [r for r in records if not r.was_skipped],
            fallback=event_name or "Unbekannt"
        )
        if suggested and suggested != "Unbekannt":
            event_name = suggested
            console.print(f"[dim]📍 Event-Name aus GPS: {event_name}[/dim]")

    # Session-Ordner = Ordner des zeitlich neuesten Fotos
    # (SD-Karten enthalten oft Fotos aus mehreren Sessions — wir nehmen die aktuellste)
    session_dir = archive_root
    latest_ts = -1.0
    for r in records:
        if r.archive_path:
            ts = 0.0
            if r.datetime_original:
                try:
                    from datetime import datetime as _dt
                    ts = _dt.strptime(r.datetime_original, "%Y-%m-%d %H:%M:%S").timestamp()
                except Exception:
                    ts = r.archive_path.stat().st_mtime if r.archive_path.exists() else 0.0
            if ts > latest_ts:
                latest_ts = ts
                session_dir = r.archive_path.parent

    # Build initial report
    report = SessionReport(
        session_id=session_id,
        event_name=event_name or "Unbekannt",
        started_at=start_time,
        source_path=str(source_path),
        photos=records,
        total_found=len(records),
    )

    # --- STEP 2: CULL ---
    if verbose:
        console.print(Rule("[dim]2/6  Culling[/dim]"))

    # Bracket-Frames VOR Culling markieren — verhindert dass sie als pHash-Duplikate
    # markiert und in den Rejects-Papierkorb wandern
    if bracket_cfg.get("enabled", True):
        from pipeline.bracket import pre_mark_brackets
        pre_mark_brackets(records,
                          frames_per_sequence=int(bracket_cfg.get("frames_per_sequence", 0)))

    cull(
        records=records,
        top_percentile=float(culling_cfg.get("top_percentile", 20)),
        keep_threshold=float(culling_cfg.get("keep_threshold", 40)),
        phash_distance=int(culling_cfg.get("phash_distance", 8)),
        sharpness_weight=float(culling_cfg.get("sharpness_weight", 0.40)),
        exposure_weight=float(culling_cfg.get("exposure_weight", 0.25)),
        histogram_weight=float(culling_cfg.get("histogram_weight", 0.15)),
        composition_weight=float(culling_cfg.get("composition_weight", 0.20)),
        max_workers=int(culling_cfg.get("workers", pipeline_cfg.get("culling_workers", 2))),
    )

    # JPEG-Reject: Kamera-JPEGs (Begleit-JPEGs zu RAW) automatisch aussortieren
    if jpeg_reject:
        from pipeline import Label
        jpeg_exts = {".jpg", ".jpeg"}
        jpeg_count = 0
        for r in records:
            if (r.original_path.suffix.lower() in jpeg_exts
                    and not r.was_skipped):
                r.label = Label.REJECT
                jpeg_count += 1
        if jpeg_count and verbose:
            console.print(f"[dim]📷 {jpeg_count} Kamera-JPEGs als REJECT markiert.[/dim]")

    populate_counts(report)

    # --- STEP 3: BRACKET DETECTION + SUBFOLDER SORTING ---
    bracket_enabled = bracket_cfg.get("enabled", True)
    if bracket_enabled:
        if verbose:
            console.print(Rule("[dim]3/6  Belichtungsreihen & Bracket-Sortierung[/dim]"))
        from pipeline.bracket import process_brackets
        process_brackets(
            records=records,
            report=report,
            session_dir=session_dir,
            min_composite_score=float(bracket_cfg.get("min_composite_score", 20.0)),
            frames_per_sequence=int(bracket_cfg.get("frames_per_sequence", 0)),
        )
        populate_counts(report)
    elif verbose:
        console.print(Rule("[dim]3/6  Bracket-Erkennung (übersprungen)[/dim]"))

    # --- STEP 3b: VIDEO PROCESSING (optional) ---
    video_records = []
    video_enabled = run_videos and video_cfg.get("enabled", False)
    if video_enabled:
        if verbose:
            console.print(Rule("[dim]3b/6  Video-Verarbeitung[/dim]"))
        try:
            from pipeline.video import process_videos
            video_records = process_videos(
                source_path=source_path,
                session_dir=session_dir,
                min_duration=float(video_cfg.get("min_duration", 3.0)),
                dry_run=dry_run,
            )
            if verbose and video_records:
                v_keep   = sum(1 for v in video_records if v.label in ("KEEP", "REVIEW") and not v.was_skipped)
                v_reject = sum(1 for v in video_records if v.label == "REJECT" and not v.was_skipped)
                v_skip   = sum(1 for v in video_records if v.was_skipped)
                console.print(
                    f"[green]🎬 Videos:[/green] "
                    f"{v_keep} KEEP/REVIEW  "
                    f"[red]{v_reject} REJECT[/red]  "
                    f"[dim]{v_skip} übersprungen[/dim]"
                )
                # GUI-Marker: video summary für Ergebnisse-Tab
                print(f"GRAIN_VIDEO_KEEP: {v_keep}", flush=True)
                print(f"GRAIN_VIDEO_REJECT: {v_reject}", flush=True)
        except Exception as exc:
            console.print(f"[yellow]⚠ Video-Verarbeitung fehlgeschlagen: {exc}[/yellow]")
    elif verbose:
        console.print(Rule("[dim]3b/6  Video-Verarbeitung (übersprungen — deaktiviert)[/dim]"))

    # --- STEP 4: AI ANALYSIS ---
    if run_ai and ai_cfg.get("enabled", True):
        if verbose:
            console.print(Rule("[dim]4/6  AI-Analyse (Claude Vision)[/dim]"))

        ai_analyze(
            records=records,
            report=report,
            provider=ai_cfg.get("provider", "local"),
            local_model=ai_cfg.get("local_model", "llama3.2-vision"),
            local_url=ai_cfg.get("local_url", "http://localhost:11434"),
            model=ai_cfg.get("model", "claude-sonnet-4-5"),
            max_tokens=int(ai_cfg.get("max_tokens", 1500)),
            cost_input_per_mtok=float(ai_cfg.get("cost_input_per_mtok", 3.0)),
            cost_output_per_mtok=float(ai_cfg.get("cost_output_per_mtok", 15.0)),
            api_key=ai_cfg.get("api_key") or None,
            max_short_side=int(ai_cfg.get("max_image_short_side", 512)),
            jpeg_quality=int(ai_cfg.get("jpeg_quality_for_api", 65)),
            max_workers=int(ai_cfg.get("workers", pipeline_cfg.get("ai_workers", 2))),
        )
    elif verbose:
        console.print(Rule("[dim]4/6  AI-Analyse (übersprungen)[/dim]"))

    # Finder-Labels setzen — nach AI-Analyse damit final_score verfügbar ist
    if not dry_run:
        _set_finder_labels(records, verbose)

    # XMP Sidecars deaktiviert — kein Lightroom/Capture One im Einsatz

    # --- STEP 6: EXPORT (optional) ---
    if export_style:
        if verbose:
            console.print(Rule(f"[dim]5/6  Export ({export_style})[/dim]"))
        preset_name = export_preset or export_cfg.get("default_preset", "default")
        export(
            records=records,
            report=report,
            export_root=export_root,
            style=export_style,
            preset_name=preset_name,
            presets_dir=presets_dir,
            export_cfg=export_cfg,
            darktable_cfg=darktable_cfg,
            preserve_exif_flag=export_cfg.get("preserve_exif", True),
            write_xmp=False,  # Already written above
        )
    elif verbose:
        console.print(Rule("[dim]5/6  Export (übersprungen — nutze --export)[/dim]"))

    # Finalise report
    report.finished_at = time.time()
    populate_counts(report)

    # --- STEP 7: SESSION REVIEW (Kritik + Markdown-Bericht) ---
    review_path = None
    if not dry_run and run_ai and ai_cfg.get("enabled", True):
        if verbose:
            console.print(Rule("[dim]6/6  Session Review & Künstlerische Kritik[/dim]"))

        from pipeline.critique import generate_critique
        from pipeline.session_report import generate_session_review
        from pipeline.history import save_session, last_session_scores

        # Scores der letzten Session für Vergleich laden (vor save_session!)
        prev_scores = last_session_scores()

        critique = generate_critique(
            records=records,
            report=report,
            model=ai_cfg.get("local_model", "llama3.2-vision"),
            base_url=ai_cfg.get("local_url", "http://localhost:11434"),
            max_sample_photos=8,
        )

        review_path = generate_session_review(
            records=records,
            report=report,
            output_path=session_dir / "SESSION_REVIEW.md",
            critique=critique,
            prev_scores=prev_scores,
        )

        # Session in History speichern (nach Review-Generierung)
        save_session(report, records)

        if verbose:
            console.print(f"[green]📋 Session Review gespeichert:[/green] [cyan]{review_path}[/cyan]")
    elif dry_run and verbose:
        console.print(Rule("[dim]6/6  Session Review (übersprungen — dry-run)[/dim]"))
    elif verbose:
        console.print(Rule("[dim]6/6  Session Review (übersprungen — AI deaktiviert)[/dim]"))

    # --- Rejects aus Archiv löschen (Originale bleiben auf SD-Karte) ---
    if not dry_run:
        _delete_rejects(records, archive_root, verbose)

    if not dry_run:
        report_path = save_report(report, session_dir)
        if verbose:
            console.print(f"[dim]💾 Report gespeichert: {report_path}[/dim]")

    # Print summary
    if verbose:
        console.print(Rule("[bold cyan]Zusammenfassung[/bold cyan]"))
        fmt = DexFormatter(config)
        summary = fmt.session_summary(report)
        if review_path:
            summary += f"\n[dim]📋 SESSION_REVIEW.md → {review_path.name}[/dim]"
        console.print(Panel(summary, border_style="cyan"))

    # _TOP/ Ordner erstellen + Finder öffnen
    if not dry_run:
        top_folder = _create_top_folder(records, session_dir, verbose)
        # GUI-Marker: immer session_dir → "Ergebnisse"-Button öffnet den ganzen Session-Ordner
        # (_TOP/ ist als Unterordner sichtbar, kein Suchen nötig)
        print(f"GRAIN_SESSION_DIR: {session_dir}", flush=True)
        # Finder öffnet _TOP/ automatisch wenn vorhanden (beste Fotos sofort im Blick)
        _open_in_finder(top_folder if top_folder else session_dir)

    return report


# ---------------------------------------------------------------------------
# Watchdog integration
# ---------------------------------------------------------------------------

def _start_watcher(watch_path: Path, config: dict, event_name: str, pid_file: Path) -> None:
    """Start a watchdog-based file system monitor. Forks to background."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        console.print("[red]❌ watchdog nicht installiert. Installiere: pip install watchdog[/red]")
        sys.exit(1)

    paths_cfg = config.get("paths", {})
    debounce = config.get("watcher", {}).get("debounce_seconds", 5)

    class PhotoHandler(FileSystemEventHandler):
        def __init__(self):
            self._pending: dict = {}
            self._last_trigger: float = 0

        def on_created(self, event):
            if event.is_directory:
                return
            path = Path(event.src_path)
            from pipeline.ingest import SUPPORTED_EXTENSIONS
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                self._pending[str(path)] = time.time()
                self._maybe_trigger()

        def _maybe_trigger(self):
            now = time.time()
            if now - self._last_trigger < debounce:
                return
            self._last_trigger = now
            # Find the parent directory containing new files
            dirs = set(Path(p).parent for p in self._pending)
            self._pending.clear()
            for d in dirs:
                console.print(f"\n[bold cyan]🔔 Neue Fotos erkannt in {d}[/bold cyan]")
                try:
                    run_pipeline(d, config, event_name=event_name)
                except Exception as e:
                    console.print(f"[red]Pipeline-Fehler: {e}[/red]")

    observer = Observer()
    handler = PhotoHandler()
    observer.schedule(handler, str(watch_path), recursive=True)
    observer.start()

    # Write PID
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    console.print(f"[green]👁 Watcher gestartet für [cyan]{watch_path}[/cyan][/green]")
    console.print("[dim]Strg+C zum Stoppen[/dim]")

    def _stop(sig, frame):
        observer.stop()
        if pid_file.exists():
            pid_file.unlink()
        console.print("\n[yellow]Watcher gestoppt.[/yellow]")
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        while observer.is_alive():
            time.sleep(1)
    finally:
        observer.stop()
        observer.join()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--source", "-s", type=click.Path(exists=True), help="Quellordner mit Fotos")
@click.option("--event", "-e", default="", help="Event-Name für Ordnerstruktur")
@click.option("--archive-root", default=None, help="Archiv-Ziel überschreiben")
@click.option("--export", "export_style", default=None,
              type=click.Choice(["web", "social", "archive", "print"]),
              help="Export-Style nach Pipeline")
@click.option("--export-preset", default=None, help="Preset-Name für Export")
@click.option("--watch", "-w", type=click.Path(), default=None,
              help="Ordner überwachen (Watchdog)")
@click.option("--watch-stop", is_flag=True, default=False, help="Watcher stoppen")
@click.option("--no-ai", is_flag=True, default=False, help="AI-Analyse überspringen")
@click.option("--no-videos", is_flag=True, default=False, help="Video-Verarbeitung überspringen")
@click.option("--dry-run", is_flag=True, default=False, help="Simulation — keine Datei-I/O")
@click.option("--jpeg-reject", is_flag=True, default=False,
              help="JPEGs automatisch als REJECT labeln (RAW+JPEG Workflows)")
@click.option("--config", "config_path", default="config.yaml",
              type=click.Path(), help="Pfad zur config.yaml")
@click.option("--status", is_flag=True, default=False, help="Letzten Report anzeigen")
@click.option("--progress", is_flag=True, default=False,
              help="Fortschritt über letzte Sessions als Chart anzeigen")
@click.option("--verbose/--quiet", default=True)
def cli(
    source, event, archive_root, export_style, export_preset, watch, watch_stop,
    no_ai, no_videos, dry_run, jpeg_reject, config_path, status, progress, verbose
):
    """AI-gestützte Foto-Pipeline für macOS (Lumix G9 / Apple Silicon)."""

    config = load_config(Path(config_path))
    paths_cfg = config.get("paths", {})
    pid_file = _expand(paths_cfg.get("pid_file", "~/.photo_pipeline.pid"))
    archive_root = _expand(paths_cfg.get("archive_root", "~/Pictures/Archive"))

    # --progress
    if progress:
        from pipeline.history import show_progress
        show_progress()
        return

    # --status
    if status:
        report_path = find_latest_report(archive_root)
        if not report_path:
            console.print("[yellow]Kein Report gefunden.[/yellow]")
            return
        from pipeline.report import load_report
        data = load_report(report_path)
        if data:
            fmt = DexFormatter(config)
            console.print(fmt.status_summary(data))
        return

    # --watch-stop
    if watch_stop:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                pid_file.unlink(missing_ok=True)
                console.print(f"[green]Watcher (PID {pid}) gestoppt.[/green]")
            except (ValueError, ProcessLookupError):
                pid_file.unlink(missing_ok=True)
                console.print("[yellow]Kein laufender Watcher gefunden.[/yellow]")
        else:
            console.print("[yellow]Kein aktiver Watcher.[/yellow]")
        return

    # --watch
    if watch:
        _start_watcher(Path(watch), config, event, pid_file)
        return

    # --source (normal run)
    if not source:
        console.print("[red]❌ Bitte --source angeben oder --help für Hilfe.[/red]")
        sys.exit(1)

    run_pipeline(
        source_path=Path(source),
        config=config,
        event_name=event,
        run_ai=not no_ai,
        dry_run=dry_run,
        jpeg_reject=jpeg_reject,
        run_videos=not no_videos,
        export_style=export_style,
        export_preset=export_preset,
        verbose=verbose,
        archive_root_override=archive_root,
    )


if __name__ == "__main__":
    cli()
