"""
Artistic critique module — Ollama-gestützte Session-Analyse.

Kernprinzip: Künstlerische Freiheit hat Vorrang.
Ein gutes Foto ist nicht zwingend eines das alle Regeln befolgt.
Ein schlechtes Foto ist nicht automatisch eines das Regeln bricht.

Was bewertet wird:
- Ob erkennbare stilistische Entscheidungen getroffen wurden (Absicht vs. Zufall)
- Stärken der Session als Ganzes (nicht nur Einzelfotos)
- Technische Fehler die klar ungewollt sind (verwackelt, verpasster Fokus)
- Muster: Was wird oft gut gemacht? Was wiederholt sich als Schwäche?
- Konkrete, umsetzbare Hinweise für die nächste Session

Was NICHT bewertet wird:
- Ob Regeln eingehalten wurden (Rule of Thirds ist Werkzeug, kein Gesetz)
- Ob ein Motiv "fotogen" ist
- Ob der Stil "modern" oder "klassisch" ist

Output: strukturiertes dict das in SESSION_REVIEW.md landet
"""

import base64
import io
import json
import re
import urllib.request
from pathlib import Path
from typing import Optional

from rich.console import Console

from pipeline import Label, PhotoRecord, SessionReport

console = Console()

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


# ---------------------------------------------------------------------------
# System-Prompt — das Herzstück
# ---------------------------------------------------------------------------

CRITIQUE_SYSTEM_PROMPT = """You are an experienced photo editor and visual arts coach reviewing
a photographer's session. Your role is to give honest, nuanced, and encouraging feedback.

IMPORTANT PRINCIPLES:
1. Rules are tools, not laws. A deliberately broken rule (Dutch angle, centered subject,
   blown highlights for mood) is a CREATIVE CHOICE, not a mistake.
2. Judge intent: Does this look accidental or deliberate? Context matters.
3. Look at the body of work as a whole — patterns across multiple images reveal style and habits.
4. Technical errors (motion blur from shake, missed focus, accidental overexposure) ARE worth
   noting — but only when clearly unintentional.
5. Give specific, actionable advice. Not "improve composition" but "try moving 2 steps left to
   separate the subject from the background."
6. Acknowledge what's working FIRST. Strengths before growth areas.
7. Never say "this breaks rule X" — say "this creates tension/energy" or "this feels unbalanced
   in a way that may not serve the image."

Your response must be valid JSON only — no markdown, no explanation outside the JSON."""

CRITIQUE_USER_PROMPT = """Review this photography session and provide artistic feedback.

SESSION STATISTICS:
- Total photos: {total}
- Labeled TOP: {top_count}
- Labeled KEEP: {keep_count}
- Labeled REJECT: {reject_count}
- Bracket sequences: {brackets}
- Shaky brackets (unusable): {shaky_brackets}
- Event: {event_name}

TOP PHOTOS (best {n_samples} shown — analyze these as a body of work):
{photo_summaries}

COMPOSITION PATTERNS ACROSS SESSION:
{composition_patterns}

TECHNICAL ERROR PATTERNS:
{error_patterns}

Provide feedback in this exact JSON structure:
{{
  "session_style": "2-3 word description of the photographer's apparent style/approach",
  "strengths": [
    "specific strength 1 with example filename if relevant",
    "specific strength 2",
    "specific strength 3"
  ],
  "growth_areas": [
    {{
      "area": "short name",
      "observation": "what you noticed (non-judgmental)",
      "suggestion": "one concrete, specific thing to try next time"
    }}
  ],
  "technical_issues": [
    "specific technical issue if clearly unintentional (skip if none)"
  ],
  "best_moment": "filename of the single most interesting/successful image and why (1 sentence)",
  "next_session_focus": "one specific thing to experiment with or pay attention to next time",
  "overall_impression": "2-3 sentences — honest, warm, specific to THIS session"
}}"""


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def _load_thumb_b64(record: PhotoRecord, max_side: int = 400, quality: int = 60) -> Optional[str]:
    """Thumbnail als base64 JPEG für Ollama laden."""
    if not _PIL_AVAILABLE:
        return None
    path = record.archive_path or record.original_path
    ext = path.suffix.lower()

    try:
        if ext in RAW_EXTENSIONS and _RAWPY_AVAILABLE:
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img = Image.open(io.BytesIO(thumb.data))
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    img = Image.fromarray(thumb.data)
                else:
                    return None
        else:
            img = Image.open(path)
            img.load()

        w, h = img.size
        scale = max_side / max(w, h)
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        buf.seek(0)
        return base64.standard_b64encode(buf.read()).decode()
    except Exception:
        return None


def _summarize_record(record: PhotoRecord) -> str:
    """Kompakte Text-Zusammenfassung eines Fotos für den Prompt."""
    s = record.local_scores
    lines = [f"FILE: {record.filename}"]
    lines.append(f"  Scores: sharpness={s.sharpness:.0f}, exposure={s.exposure:.0f}, "
                 f"composition={s.composition_overall:.0f}")
    lines.append(f"  Rule-of-thirds={s.rule_of_thirds:.0f}, "
                 f"horizon_level={s.horizon_level:.0f} (tilt={s.horizon_tilt_deg:.1f}°), "
                 f"leading_lines={s.leading_lines:.0f}")
    if s.symmetry > 40:
        lines.append(f"  Symmetry detected ({s.symmetry_axis}): {s.symmetry:.0f}/100")
    if record.ai_scores:
        ai = record.ai_scores
        lines.append(f"  AI: composition={ai.composition:.0f}, mood='{ai.mood}', "
                     f"keywords={ai.keywords[:3]}")
        if ai.notes:
            lines.append(f"  AI note: {ai.notes}")
    if s.is_blurry:
        lines.append("  ⚠ Unscharf")
    if s.is_overexposed:
        lines.append("  ⚠ Überbelichtet")
    if s.is_underexposed:
        lines.append("  ⚠ Unterbelichtet")
    return "\n".join(lines)


def _composition_patterns(records: list) -> str:
    """Aggregierte Kompositions-Muster über die ganze Session."""
    eligible = [r for r in records if not r.was_skipped and r.local_scores.composition_overall > 0]
    if not eligible:
        return "Keine Kompositionsdaten verfügbar."

    rot_avg = sum(r.local_scores.rule_of_thirds for r in eligible) / len(eligible)
    horiz_avg = sum(r.local_scores.horizon_level for r in eligible) / len(eligible)
    lines_avg = sum(r.local_scores.leading_lines for r in eligible) / len(eligible)
    sym_avg = sum(r.local_scores.symmetry for r in eligible) / len(eligible)
    tilt_avg = sum(abs(r.local_scores.horizon_tilt_deg) for r in eligible) / len(eligible)

    # Häufig starke/schwache Bereiche
    lines = [
        f"Rule of thirds alignment (avg): {rot_avg:.0f}/100",
        f"Horizon level (avg): {horiz_avg:.0f}/100, average tilt: {tilt_avg:.1f}°",
        f"Leading lines usage (avg): {lines_avg:.0f}/100",
        f"Symmetry score (avg): {sym_avg:.0f}/100",
    ]

    # Extremwerte
    most_tilted = max(eligible, key=lambda r: abs(r.local_scores.horizon_tilt_deg))
    if abs(most_tilted.local_scores.horizon_tilt_deg) > 2.0:
        lines.append(f"Most tilted: {most_tilted.filename} "
                     f"({most_tilted.local_scores.horizon_tilt_deg:.1f}°)")

    high_sym = [r for r in eligible if r.local_scores.symmetry > 65]
    if high_sym:
        lines.append(f"Strong symmetry in {len(high_sym)} photos: "
                     f"{', '.join(r.filename for r in high_sym[:3])}")

    return "\n".join(lines)


def _error_patterns(records: list) -> str:
    """Technische Fehler-Muster zusammenfassen."""
    blurry = [r for r in records if r.local_scores.is_blurry]
    over   = [r for r in records if r.local_scores.is_overexposed]
    under  = [r for r in records if r.local_scores.is_underexposed]
    shaky  = [r for r in records if "verwackelt" in r.skip_reason]

    total = len([r for r in records if not r.was_skipped])
    if total == 0:
        return "Keine Daten."

    lines = []
    if blurry:
        lines.append(f"Motion blur / unsharp: {len(blurry)}/{total} photos "
                     f"({len(blurry)/total*100:.0f}%)")
    if over:
        lines.append(f"Overexposure: {len(over)}/{total} photos")
    if under:
        lines.append(f"Underexposure: {len(under)}/{total} photos")
    if shaky:
        lines.append(f"Shaky brackets (unusable): {len(shaky)} frames")

    return "\n".join(lines) if lines else "No significant technical issues detected."


# ---------------------------------------------------------------------------
# Ollama API Call
# ---------------------------------------------------------------------------

def _ollama_critique(
    prompt: str,
    images_b64: list[str],
    model: str,
    base_url: str,
    timeout: int = 180,
) -> Optional[str]:
    """Sendet Critique-Prompt an Ollama, gibt Raw-Text zurück."""
    # llama3.2-vision unterstützt nur 1 Bild pro Request → immer auf 1 begrenzen
    # Payload-Größe prüfen: bei >4MB das Bild weglassen
    payload_size = sum(len(b) for b in images_b64) * 3 // 4  # base64 → bytes
    if payload_size < 4_000_000 and images_b64:
        send_images = images_b64[:1]   # max. 1 Bild (Modell-Limit)
    else:
        send_images = []
    if not send_images and images_b64:
        console.print("[dim]⚠ Bild zu groß für Kritik-Prompt — nur Text wird gesendet.[/dim]")

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "system": CRITIQUE_SYSTEM_PROMPT,
        "images": send_images,
        "stream": False,
        "options": {"temperature": 0.7},
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        return result.get("response", "")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        console.print(f"[red]Ollama HTTP {e.code}: {body}[/red]")
        return None
    except urllib.error.URLError as e:
        console.print(f"[yellow]⚠ Ollama nicht erreichbar: {e.reason}[/yellow]")
        return None
    except Exception as e:
        console.print(f"[red]Ollama Critique Fehler: {e}[/red]")
        return None


def _parse_critique(text: str) -> Optional[dict]:
    """JSON aus Ollama-Antwort extrahieren."""
    if not text:
        return None
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        text = match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Haupt-Einstiegspunkt
# ---------------------------------------------------------------------------

def generate_critique(
    records: list,
    report: SessionReport,
    model: str = "llama3.2-vision",
    base_url: str = "http://localhost:11434",
    max_sample_photos: int = 4,
) -> Optional[dict]:
    """
    Generiert eine künstlerische Session-Kritik via Ollama.
    Analysiert die TOP-Fotos als Gesamtwerk.

    Gibt strukturiertes dict zurück (oder None bei Fehler).
    """
    # Prüfe Ollama
    try:
        urllib.request.urlopen(f"{base_url}/api/tags", timeout=3)
    except Exception:
        console.print("[yellow]⚠ Ollama nicht erreichbar — Kritik übersprungen.[/yellow]")
        return None

    console.print(f"[bold]🎨 Künstlerische Kritik:[/bold] analysiere Session via [cyan]{model}[/cyan]…")

    # Sample: beste TOP-Fotos auswählen
    top_photos = sorted(
        [r for r in records if r.label == Label.TOP and not r.was_skipped],
        key=lambda r: r.local_scores.composite,
        reverse=True,
    )[:max_sample_photos]

    if not top_photos:
        top_photos = sorted(
            [r for r in records if not r.was_skipped],
            key=lambda r: r.local_scores.composite,
            reverse=True,
        )[:max_sample_photos]

    if not top_photos:
        console.print("[yellow]Keine Fotos für Kritik verfügbar.[/yellow]")
        return None

    # Bilder vorbereiten
    images_b64 = []
    used_records = []
    for record in top_photos:
        b64 = _load_thumb_b64(record)
        if b64:
            images_b64.append(b64)
            used_records.append(record)

    if not images_b64:
        console.print("[yellow]Keine Bilder ladbar — Kritik ohne Bilder.[/yellow]")

    # Shaky brackets zählen
    shaky_count = sum(1 for r in records if "verwackelt" in (r.skip_reason or ""))

    # Prompt befüllen
    photo_summaries = "\n\n".join(_summarize_record(r) for r in used_records)
    comp_patterns = _composition_patterns(records)
    err_patterns = _error_patterns(records)

    prompt = CRITIQUE_USER_PROMPT.format(
        total=report.total_imported,
        top_count=report.top_count,
        keep_count=report.keep_count,
        reject_count=report.reject_count,
        brackets=report.bracket_groups,
        shaky_brackets=shaky_count,
        event_name=report.event_name,
        n_samples=len(used_records),
        photo_summaries=photo_summaries,
        composition_patterns=comp_patterns,
        error_patterns=err_patterns,
    )

    # API Call
    raw = _ollama_critique(prompt, images_b64, model, base_url)
    critique = _parse_critique(raw)

    if critique:
        console.print(f"[green]✅ Kritik generiert — Stil: '{critique.get('session_style', '?')}'[/green]")
    else:
        console.print("[yellow]⚠ Kritik konnte nicht geparst werden.[/yellow]")

    return critique
