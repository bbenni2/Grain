"""
Report module — JSON persistence + Dex/WhatsApp formatter.

JSON schema: session metadata + per-photo records + API usage stats.
DexFormatter: produces ≤10-line WhatsApp-readable summaries with emojis.
No ANSI colour codes in Dex output (WhatsApp strips them).
"""

import dataclasses
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from pipeline import Label, LABEL_EMOJI, PhotoRecord, SessionReport


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------

class _PipelineEncoder(json.JSONEncoder):
    """Handles dataclasses, Path, Enum, numpy types in JSON serialisation."""

    def default(self, obj):
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, Label):
            return obj.value
        # numpy scalar types (bool_, int_, float_, etc.)
        try:
            import numpy as np
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
        except ImportError:
            pass
        return super().default(obj)


def save_report(report: SessionReport, session_dir: Path) -> Path:
    """Write .pipeline_report.json into session_dir. Returns the path."""
    session_dir.mkdir(parents=True, exist_ok=True)
    out = session_dir / ".pipeline_report.json"
    out.write_text(json.dumps(dataclasses.asdict(report), cls=_PipelineEncoder, indent=2, ensure_ascii=False))
    return out


def load_report(report_path: Path) -> Optional[dict]:
    """Load a JSON report. Returns raw dict (not dataclass)."""
    try:
        return json.loads(report_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def find_latest_report(search_root: Path) -> Optional[Path]:
    """
    Recursively find the most recently modified .pipeline_report.json
    under search_root (typically archive_root or export_root).
    """
    candidates = list(search_root.rglob(".pipeline_report.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# SessionReport population helpers
# ---------------------------------------------------------------------------

def populate_counts(report: SessionReport) -> None:
    """Update TOP/KEEP/REJECT counts from photos list."""
    report.top_count = sum(1 for p in report.photos if p.label == Label.TOP)
    report.keep_count = sum(1 for p in report.photos if p.label == Label.KEEP)
    report.reject_count = sum(1 for p in report.photos if p.label == Label.REJECT)
    report.total_imported = sum(1 for p in report.photos if not p.was_skipped)
    report.total_skipped_duplicate = sum(1 for p in report.photos if p.is_duplicate)
    report.total_skipped_error = sum(
        1 for p in report.photos if p.was_skipped and not p.is_duplicate
    )


# ---------------------------------------------------------------------------
# Dex formatter
# ---------------------------------------------------------------------------

class DexFormatter:
    """
    Formats a SessionReport (or raw report dict) into compact,
    WhatsApp-readable summaries. No ANSI, emojis only, ≤10 lines.
    """

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        cost_cfg = self.config.get("ai", {})
        self._cost_in = float(cost_cfg.get("cost_input_per_mtok", 15.0))
        self._cost_out = float(cost_cfg.get("cost_output_per_mtok", 75.0))

    def _fmt_elapsed(self, seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s:02d}s"

    def _best_shot(self, photos) -> Optional[str]:
        """Return display name of the highest-scoring non-rejected photo."""
        candidates = [p for p in photos if not p.was_skipped]
        if not candidates:
            return None
        best = max(candidates, key=lambda p: (
            p.ai_scores.final_score if p.ai_scores else p.local_scores.composite
        ))
        score = int(best.ai_scores.final_score if best.ai_scores else best.local_scores.composite)
        name = best.filename if len(best.filename) <= 22 else best.filename[:21] + "…"
        return f"{name} — Score {score}/100"

    def session_summary(self, report: SessionReport) -> str:
        """Full 8–10 line session summary."""
        dt = datetime.fromtimestamp(report.started_at).strftime("%Y-%m-%d")
        lines = [
            f"📸 Session: {dt} {report.event_name}",
            f"📁 {report.total_imported} Fotos importiert"
            + (f" ({report.total_skipped_duplicate} Duplikate übersprungen)"
               if report.total_skipped_duplicate else ""),
        ]

        if report.total_skipped_error:
            lines.append(f"⚠️  {report.total_skipped_error} Fehler beim Import")

        lines.append(
            f"⭐ {report.top_count} TOP  ✅ {report.keep_count} KEEP  🗑 {report.reject_count} REJECT"
        )

        # Bracket info (only if any were detected)
        if report.bracket_groups > 0:
            lines.append(
                f"🔲 {report.bracket_groups} Belichtungsreihe(n) — {report.bracket_frames} Frames"
            )

        if report.ai_calls > 0:
            cost_str = ""
            if report.ai_cost_usd > 0:
                cost_str = f" (~${report.ai_cost_usd:.3f})"
            lines.append(
                f"🤖 Claude hat {report.top_count} Fotos analysiert "
                f"({report.ai_calls} API-Call{cost_str})"
            )

        best = self._best_shot(report.photos)
        if best:
            lines.append(f"✨ Best Shot: {best}")

        if report.export_count > 0 and report.export_style:
            # Find export dir from first exported photo
            export_dir = ""
            for p in report.photos:
                ep = p.export_paths
                target = getattr(ep, report.export_style, None)
                if target:
                    export_dir = str(Path(str(target)).parent)
                    break
            lines.append(
                f"🗂 Export: {report.export_count} {report.export_style.capitalize()}-JPEGs"
                + (f" → {export_dir}" if export_dir else "")
            )

        lines.append(f"⏱ Laufzeit: {self._fmt_elapsed(report.elapsed_seconds)}")
        return "\n".join(lines)

    def status_summary(self, report_dict: dict) -> str:
        """Compact status from raw report dict (--status command)."""
        ev = report_dict.get("event_name", "?")
        started = report_dict.get("started_at", 0)
        dt = datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M") if started else "?"
        top = report_dict.get("top_count", 0)
        keep = report_dict.get("keep_count", 0)
        rej = report_dict.get("reject_count", 0)
        imp = report_dict.get("total_imported", 0)
        dups = report_dict.get("total_skipped_duplicate", 0)
        ai_calls = report_dict.get("ai_calls", 0)
        cost = report_dict.get("ai_cost_usd", 0.0)
        elapsed = report_dict.get("finished_at", 0) - started if report_dict.get("finished_at") else 0

        lines = [
            f"📊 Letzter Report: {dt} — {ev}",
            f"📁 {imp} importiert" + (f", {dups} Duplikate" if dups else ""),
            f"⭐ {top} TOP  ✅ {keep} KEEP  🗑 {rej} REJECT",
        ]
        bracket_groups = report_dict.get("bracket_groups", 0)
        if bracket_groups:
            lines.append(f"🔲 {bracket_groups} Reihe(n)")

        if ai_calls:
            lines.append(f"🤖 {ai_calls} API-Call(s), Kosten: ${cost:.3f}")
        if elapsed > 0:
            lines.append(f"⏱ Laufzeit: {self._fmt_elapsed(elapsed)}")
        return "\n".join(lines)

    def top_photos(self, photos, n: int = 10) -> str:
        """List top N photos by score."""
        candidates = sorted(
            [p for p in photos if not p.was_skipped and p.label == Label.TOP],
            key=lambda p: (p.ai_scores.final_score if p.ai_scores else p.local_scores.composite),
            reverse=True,
        )[:n]

        if not candidates:
            return "⭐ Keine TOP-Fotos in dieser Session."

        lines = [f"⭐ TOP {len(candidates)} Fotos:"]
        for i, p in enumerate(candidates, 1):
            score = int(p.ai_scores.final_score if p.ai_scores else p.local_scores.composite)
            name = p.filename if len(p.filename) <= 24 else p.filename[:23] + "…"
            mood = ""
            if p.ai_scores and p.ai_scores.mood:
                mood = f" — {p.ai_scores.mood}"
            lines.append(f"  {i}. {name}  ({score}/100){mood}")
        return "\n".join(lines)

    def error_summary(self, error: str) -> str:
        return f"❌ Pipeline-Fehler:\n{error}"

    def dry_run_notice(self) -> str:
        return "🔍 DRY-RUN Modus — keine Dateien wurden verändert."

    def help_text(self) -> str:
        return (
            "📸 Foto-Pipeline — Befehle:\n"
            "  --run [pfad]        Pipeline starten\n"
            "  --status            Letzten Report anzeigen\n"
            "  --top [n]           Top-Fotos listen (Standard: 10)\n"
            "  --export [style]    Export (web/social/print/archive)\n"
            "  --watch [pfad]      Ordner überwachen\n"
            "  --watch stop        Watcher stoppen\n"
            "  --event [name]      Event-Name setzen\n"
            "  --dry-run           Simulation ohne Datei-I/O\n"
            "  --no-ai             Ohne Claude-Analyse\n"
        )
