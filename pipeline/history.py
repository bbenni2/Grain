"""
Fortschritts-Tracking — speichert Session-Statistiken in ~/.foto-pipeline/history.json

Nach jeder Pipeline-Session wird ein kompakter Datensatz gespeichert.
`foto --progress` zeigt dann Entwicklung und Muster über Zeit.
"""

import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from pipeline import Label, PhotoRecord, SessionReport

console = Console()

HISTORY_DIR  = Path.home() / ".foto-pipeline"
HISTORY_FILE = HISTORY_DIR / "history.json"

# Dimensionen für Chart + Vergleich
DIMENSIONS = [
    ("technique",    "Technik"),
    ("composition",  "Komposit."),
    ("subject_moment","Motiv/Mom."),
    ("light_mood",   "Licht/Stim."),
    ("story_memory", "Story/Mem."),
]


# ─────────────────────────────────────────────────────────────────────────────
# Daten aus einer Session extrahieren
# ─────────────────────────────────────────────────────────────────────────────

def _session_entry(report: SessionReport, records: list) -> dict:
    """Erstellt einen kompakten History-Eintrag aus einem abgeschlossenen Pipeline-Lauf."""

    eligible = [r for r in records if not r.was_skipped and r.archive_path]
    ai_scored = [r for r in eligible if r.ai_scores]

    # ── Durchschnittliche Scores ────────────────────────────────────────────
    def avg(seq):
        return round(statistics.mean(seq), 1) if seq else 0.0

    # Technik: lokal gemessen (unabhängig von AI)
    tech_local = avg([
        r.local_scores.sharpness * 0.6 + r.local_scores.exposure * 0.4
        for r in eligible
    ])

    # Komposition: lokal (kein AI nötig)
    comp_local = avg([r.local_scores.composition_overall for r in eligible])

    # AI-Scores (nur wenn AI gelaufen ist)
    if ai_scored:
        subject_moment = avg([
            0.55 * r.ai_scores.decisive_moment + 0.45 * r.ai_scores.subject_interest
            for r in ai_scored
        ])
        light_mood_avg  = avg([r.ai_scores.light_mood    for r in ai_scored])
        story_mem_avg   = avg([r.ai_scores.story_memory  for r in ai_scored])
        comp_ai         = avg([r.ai_scores.composition   for r in ai_scored])
        # Komposition: Mischung aus lokal + AI
        comp_final = round(0.5 * comp_local + 0.5 * comp_ai, 1)
    else:
        subject_moment = 0.0
        light_mood_avg = 0.0
        story_mem_avg  = 0.0
        comp_final     = comp_local

    # ── Bestes Foto ─────────────────────────────────────────────────────────
    scored = [r for r in eligible if r.label == Label.TOP]
    best_photo = {}
    if scored:
        best = max(scored, key=lambda r:
                   r.ai_scores.final_score if r.ai_scores else r.local_scores.composite)
        best_score = best.ai_scores.final_score if best.ai_scores else best.local_scores.composite
        best_photo = {
            "filename": best.filename,
            "path":     str(best.archive_path),
            "score":    round(best_score, 1),
            "notes":    best.ai_scores.notes if best.ai_scores else "",
            "mood":     best.ai_scores.mood  if best.ai_scores else "",
        }

    # ── Kamera-Settings der TOP-Fotos ────────────────────────────────────────
    top_records = [r for r in eligible if r.label == Label.TOP]
    iso_vals  = [r.iso         for r in top_records if r.iso]
    apt_vals  = [r.aperture    for r in top_records if r.aperture]
    ss_vals   = [r.shutter_speed for r in top_records if r.shutter_speed]

    def _most_common(lst):
        return Counter(lst).most_common(1)[0][0] if lst else ""

    camera_settings = {
        "iso":     _most_common(iso_vals),
        "aperture": _most_common(apt_vals),
        "shutter":  _most_common(ss_vals),
    }

    # ── Goldene Stunde ──────────────────────────────────────────────────────
    golden = sum(1 for r in ai_scored
                 if r.ai_scores and r.ai_scores.light_quality == "golden_hour")
    golden_ratio = round(golden / len(ai_scored), 2) if ai_scored else 0.0

    # ── Burst-Analyse: ist das erste oder letzte Bild besser? ───────────────
    # Vereinfachte Heuristik: vergleiche score[0] vs score[-1] in Burst-Gruppen
    from pipeline import BracketRole
    burst_groups: dict = {}
    for r in eligible:
        if r.bracket_group_id is not None:
            burst_groups.setdefault(r.bracket_group_id, []).append(r)

    first_better = 0
    last_better  = 0
    for frames in burst_groups.values():
        frames.sort(key=lambda r: r.filename)
        if len(frames) >= 2:
            s_first = frames[0].ai_scores.final_score if frames[0].ai_scores else frames[0].local_scores.composite
            s_last  = frames[-1].ai_scores.final_score if frames[-1].ai_scores else frames[-1].local_scores.composite
            if s_first >= s_last:
                first_better += 1
            else:
                last_better += 1

    total_bursts = len(burst_groups)

    # ── Stärken und Schwächen aus AI-Kritik ─────────────────────────────────
    strengths  = []
    weaknesses = []
    if ai_scored:
        # Light quality distribution
        lq_counts = Counter(r.ai_scores.light_quality for r in ai_scored
                            if r.ai_scores.light_quality)
        if lq_counts.get("golden_hour", 0) / max(len(ai_scored), 1) > 0.3:
            strengths.append("goldene Stunde")
        if lq_counts.get("harsh_midday", 0) / max(len(ai_scored), 1) > 0.4:
            weaknesses.append("hartes Mittagslicht")

        # Motion blur
        unintentional = sum(1 for r in ai_scored
                            if r.ai_scores.motion_type == "unintentional")
        if unintentional / max(len(ai_scored), 1) > 0.2:
            weaknesses.append("ungewollte Bewegungsunschärfe")

        # Composition
        if comp_final >= 70:
            strengths.append("Bildaufbau")
        elif comp_final < 55:
            weaknesses.append("Bildaufbau")

        # Decisive moment
        if ai_scored:
            dm_avg = avg([r.ai_scores.decisive_moment for r in ai_scored])
            if dm_avg >= 70:
                strengths.append("decisive moment")
            elif dm_avg < 50:
                weaknesses.append("gestellte Wirkung")

    return {
        "date":        datetime.now().strftime("%Y-%m-%d"),
        "time":        datetime.now().strftime("%H:%M"),
        "event":       report.event_name,
        "total":       report.total_imported,
        "top":         report.top_count,
        "keep":        report.keep_count,
        "reject":      report.reject_count,
        "scores": {
            "technique":      tech_local,
            "composition":    comp_final,
            "subject_moment": subject_moment,
            "light_mood":     light_mood_avg,
            "story_memory":   story_mem_avg,
        },
        "best_photo":       best_photo,
        "camera_settings":  camera_settings,
        "golden_ratio":     golden_ratio,
        "burst_analysis": {
            "total_bursts":  total_bursts,
            "first_better":  first_better,
            "last_better":   last_better,
        },
        "strengths":  strengths,
        "weaknesses": weaknesses,
        "pipeline_version": report.pipeline_version,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Laden & Speichern
# ─────────────────────────────────────────────────────────────────────────────

def load_history(history_file: Path = HISTORY_FILE) -> list:
    """Lädt History-Daten. Gibt leere Liste zurück wenn Datei nicht existiert."""
    if not history_file.exists():
        return []
    try:
        data = json.loads(history_file.read_text())
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_session(
    report: SessionReport,
    records: list,
    history_file: Path = HISTORY_FILE,
) -> None:
    """Fügt eine neue Session zur History-Datei hinzu."""
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history = load_history(history_file)
        entry = _session_entry(report, records)
        history.append(entry)
        history_file.write_text(json.dumps(history, indent=2, ensure_ascii=False))
        console.print(f"[dim]📈 Fortschritt gespeichert ({len(history)} Sessions).[/dim]")
    except Exception as e:
        console.print(f"[yellow]⚠ History konnte nicht gespeichert werden: {e}[/yellow]")


def last_session_scores(history_file: Path = HISTORY_FILE) -> Optional[dict]:
    """Gibt die Scores der letzten Session zurück (für Vergleich in Session Review)."""
    history = load_history(history_file)
    if len(history) >= 2:
        return history[-2].get("scores", {})  # vorletzter Eintrag = letzte abgeschl. Session
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Fortschritts-Anzeige (foto --progress)
# ─────────────────────────────────────────────────────────────────────────────

def _bar(score: float, width: int = 10) -> str:
    """Einfacher ASCII-Balken."""
    filled = max(0, min(width, round(score / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _trend(values: list) -> str:
    """Trend-Pfeil aus Liste von Werten."""
    if len(values) < 2:
        return "  "
    diff = values[-1] - values[-2]
    if diff > 3:
        return f"[green]↑+{diff:.0f}[/green]"
    elif diff < -3:
        return f"[red]↓{diff:.0f}[/red]"
    return "[dim]→[/dim]"


def show_progress(n_sessions: int = 10, history_file: Path = HISTORY_FILE) -> None:
    """Zeigt Fortschritts-Chart der letzten n Sessions."""
    history = load_history(history_file)

    if not history:
        console.print(Panel(
            "[yellow]Noch keine Sessions gespeichert.[/yellow]\n\n"
            "Starte die Pipeline mindestens einmal:\n"
            "[cyan]python main.py --source /Volumes/SD --event 'Test'[/cyan]",
            title="📈 Fortschritt",
            border_style="yellow",
        ))
        return

    sessions = history[-n_sessions:]  # nur die letzten N

    console.print(Rule("[bold]📈 Dein Fortschritt[/bold]"))
    console.print(f"  [dim]{len(sessions)} von {len(history)} Sessions angezeigt[/dim]\n")

    # ── Score-Tabelle ───────────────────────────────────────────────────────
    table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
    table.add_column("Session",         width=22)
    table.add_column("Fotos",   justify="right", width=6)
    table.add_column("Technik",          width=14)
    table.add_column("Komposition",      width=14)
    table.add_column("Motiv/Mom.",       width=14)
    table.add_column("Licht/Stim.",      width=14)
    table.add_column("Story",            width=12)

    for s in sessions:
        sc    = s.get("scores", {})
        date  = s.get("date", "?")
        event = s.get("event", "?")[:14]
        label = f"{date[-5:]} {event}"  # "05-17 Ausflug"
        total = str(s.get("total", "?"))

        def _cell(key):
            val = sc.get(key, 0)
            return f"{_bar(val, 8)} {val:.0f}"

        table.add_row(
            label, total,
            _cell("technique"),
            _cell("composition"),
            _cell("subject_moment"),
            _cell("light_mood"),
            _cell("story_memory"),
        )

    console.print(table)

    # ── Trends ──────────────────────────────────────────────────────────────
    console.print()
    console.print("  [bold]Entwicklung:[/bold]")

    for key, label in DIMENSIONS:
        vals = [s.get("scores", {}).get(key, 0) for s in sessions]
        if not any(vals):
            continue
        trend = _trend(vals)
        # Sparkline aus den letzten 5 Werten
        recent = vals[-5:]
        spark  = " → ".join(f"{v:.0f}" for v in recent)
        console.print(f"  {label:<12} {spark}  {trend}")

    # ── Erkannte Muster ─────────────────────────────────────────────────────
    if len(sessions) >= 3:
        console.print()
        console.print("  [bold]Muster & Erkenntnisse:[/bold]")

        all_strengths  = [s for sess in sessions for s in sess.get("strengths", [])]
        all_weaknesses = [s for sess in sessions for s in sess.get("weaknesses", [])]

        if all_strengths:
            top_str = Counter(all_strengths).most_common(2)
            for s, c in top_str:
                console.print(f"  [green]✅[/green] '{s}' — in {c}/{len(sessions)} Sessions stark")

        if all_weaknesses:
            top_wk = Counter(all_weaknesses).most_common(2)
            for w, c in top_wk:
                console.print(f"  [yellow]⚠[/yellow]  '{w}' — taucht in {c}/{len(sessions)} Sessions auf")

        # Tageszeit-Muster (aus goldener Stunde Ratio)
        golden_sessions = [s for s in sessions if s.get("golden_ratio", 0) > 0.3]
        if golden_sessions:
            console.print(
                f"  [dim]💡 In {len(golden_sessions)}/{len(sessions)} Sessions "
                f"entstanden >30% deiner Fotos in der goldenen Stunde.[/dim]"
            )

        # Burst-Muster
        first_better_total = sum(s.get("burst_analysis", {}).get("first_better", 0) for s in sessions)
        last_better_total  = sum(s.get("burst_analysis", {}).get("last_better",  0) for s in sessions)
        total_bursts = first_better_total + last_better_total
        if total_bursts >= 3:
            ratio = first_better_total / total_bursts
            if ratio > 0.65:
                console.print(f"  [dim]💡 Burst-Muster: dein erstes Bild ist in {ratio*100:.0f}% der Fälle das beste.[/dim]")
            elif ratio < 0.35:
                console.print(f"  [dim]💡 Burst-Muster: du wirst bei Bursts besser — das letzte Bild ist oft schärfer.[/dim]")

    # ── Bestes Foto aller Zeiten ─────────────────────────────────────────────
    all_bests = [(s.get("best_photo", {}), s.get("date", "")) for s in history
                 if s.get("best_photo", {}).get("score", 0) > 0]
    if all_bests:
        all_bests.sort(key=lambda x: x[0].get("score", 0), reverse=True)
        best_ever, best_date = all_bests[0]
        console.print()
        console.print(f"  [bold]🏆 Bestes Foto aller Zeiten:[/bold] "
                      f"[cyan]{best_ever.get('filename', '?')}[/cyan] "
                      f"({best_ever.get('score', 0):.0f}/100 · {best_date})")
        if best_ever.get("notes"):
            console.print(f"  [dim]   \"{best_ever['notes']}\"[/dim]")

    console.print()
