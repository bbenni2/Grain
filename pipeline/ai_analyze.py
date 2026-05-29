"""
AI analysis module — Claude Vision oder lokales Ollama-Modell.

Provider-Auswahl via config.yaml:
  ai.provider: "local"   → Ollama (llama3.2-vision o.ä.), kostenlos, läuft lokal
  ai.provider: "claude"  → Claude API (Sonnet/Opus), beste Qualität

Gemeinsame Prinzipien:
- Nur TOP-Fotos werden analysiert (≤ 20% der Session)
- Bilder werden in-memory auf max_image_short_side verkleinert (kein Temp-File)
- Ergebnis wird in PhotoRecord.ai_scores gemergt
"""

import base64
import io
import json
import os
import re
import urllib.request
import warnings
from pathlib import Path
from typing import Optional

from rich.console import Console

from pipeline import AIScores, Label, PhotoRecord, SessionReport

console = Console()

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

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
# Bild vorbereiten (shared)
# ---------------------------------------------------------------------------

def _prepare_image_b64(path: Path, max_short_side: int = 800, quality: int = 75) -> Optional[str]:
    """Bild laden, verkleinern, als base64-JPEG zurückgeben. Kein Temp-File."""
    ext = path.suffix.lower()
    img_pil = None

    if ext in RAW_EXTENSIONS and _RAWPY_AVAILABLE:
        try:
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    img_pil = Image.open(io.BytesIO(thumb.data))
                elif thumb.format == rawpy.ThumbFormat.BITMAP:
                    img_pil = Image.fromarray(thumb.data)
                else:
                    rgb = raw.postprocess(half_size=True, use_camera_wb=True, output_bps=8)
                    img_pil = Image.fromarray(rgb)
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
    short = min(w, h)
    if short > max_short_side:
        scale = max_short_side / short
        img_pil = img_pil.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    if img_pil.mode != "RGB":
        img_pil = img_pil.convert("RGB")

    buf = io.BytesIO()
    img_pil.save(buf, format="JPEG", quality=quality, optimize=True)
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode("utf-8")


# ---------------------------------------------------------------------------
# Prompt (für beide Provider identisch)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a strict, experienced photo editor and art director. Your job is to ruthlessly
separate the truly good photos from the mediocre ones. Most holiday/event photos are mediocre — be honest.

CRITICAL RULES:
1. Score distribution must be realistic: 55–65 average. Most photos score 40–65.
2. 80–89: Photos a viewer genuinely stops to look at. Rare — maybe 5–15% of a session.
3. 90–100: Print-worthy, portfolio-worthy. Exceptional only — maybe 1–3% of a session.
4. Technical perfection on a boring subject is still boring. Penalise it.
5. Lighting quality is THE single most transformative factor in photography:
   - Golden hour / blue hour: transformative, soft directional light — big bonus
   - Harsh midday sun: unflattering, flat shadows — significant penalty
   - Backlight_problem: subject silhouetted without artistic intent — penalise hard
6. Portrait rules (when faces visible):
   - Eyes closed = expression_score max 15. Eyes must be open and sharp.
   - Awkward expression / mid-blink = max 25.
   - Genuine emotion, natural smile, eye contact = 70–95.
   - Blur on the eyes in a portrait = technical score max 30.
7. Motion:
   - "unintentional" (camera shake, missed focus) = technical score max 25.
   - "intentional" (silky water, light trails done well) = acceptable, score normally.
8. Noise/grain: heavy high-ISO noise destroys image quality — penalise hard in technical.
9. "notes" must be ONE specific, honest sentence naming the single biggest strength OR flaw.

SCORING GUIDE:
  90–100 = Exceptional / portfolio / print-worthy
  75–89  = Clearly good — worth sharing publicly
  60–74  = Decent — keeper for personal archive
  40–59  = Mediocre — technical problems or boring subject
  20–39  = Poor — consider deleting
  0–19   = Delete-worthy — blurry, eyes closed, severely under/overexposed

Always respond with ONLY valid JSON — no markdown, no text outside the JSON."""

_USER_TEMPLATE = """Analyse this photo strictly and return a single JSON object (not an array).

Photo filename: {filename}

Evaluate across ALL six dimensions:

1. SUBJECT & MOMENT
   - subject_interest (0–100): Clear, compelling subject? Random snapshot/empty background = 10–30.
   - decisive_moment (0–100): Alive/spontaneous vs. static/accidental? Peak action = 80+.
   - has_faces (true/false): Are human faces clearly visible?
   - eyes_open: "yes" (both eyes clearly open and sharp) | "no" (one or both closed/blinking) | "na" (no faces)
   - expression_score (0–100): ONLY if has_faces=true.
     YES eyes open + genuine smile/emotion = 70–95.
     YES eyes open + neutral/stiff = 30–55.
     NO eyes open (closed/blinking) = MAX 15 regardless of other qualities.
     If no faces: always 0.
   - motion_type: "intentional" | "unintentional" | "none"

2. COMPOSITION
   - composition (0–100): framing, rule of thirds, leading lines, balance, negative space
   - background_distraction (0–100): How distracting/cluttered is the background?
     Clean bokeh / plain sky / simple = 0–20. Busy/messy/distracting = 60–100.

3. TECHNICAL
   - technical (0–100): sharpness, exposure, noise. Be strict:
     Camera shake / missed focus = max 25. Heavy noise (high ISO) = max 40.
     Blur on eyes in portrait = max 30.
   - noise_level: "clean" | "moderate" | "heavy"

4. LIGHT & MOOD
   - light_quality: ONE of: golden_hour | blue_hour | soft_overcast | harsh_midday |
     backlight_ok | backlight_problem | artificial | mixed
   - light_mood (0–100): Quality of light. Be strict:
     Golden hour = 80–95. Blue hour = 75–90. Soft overcast = 60–75.
     Harsh midday (hard shadows, squinting) = 25–45. Backlight problem = 20–40.
     Flat/artificial = 40–60.
   - color_palette: ONE of: warm | cool | harmonious | complementary | monochrome | mixed
   - weather_mood: ONE of: dramatic | idyllic | melancholic | clear | stormy | foggy | neutral

5. STORY & MEMORY
   - story_memory (0–100): Will this matter in 10 years?
     Random snapshot = 15–35. Clear moment / emotion = 65–85.
   - is_cover_shot (true/false): Best single photo to represent the whole session?

6. OVERALL IMPRESSION
   - mood: short evocative phrase (e.g. "golden hour warmth", "quiet anticipation")
   - keywords: 3–5 search tags
   - notes: ONE specific honest sentence — biggest strength OR single biggest flaw

Return EXACTLY this JSON (no array, single object):
{{
  "filename": "{filename}",
  "subject_interest": 65,
  "decisive_moment": 60,
  "has_faces": false,
  "eyes_open": "na",
  "expression_score": 0,
  "motion_type": "none",
  "composition": 70,
  "background_distraction": 30,
  "technical": 75,
  "noise_level": "clean",
  "light_quality": "soft_overcast",
  "light_mood": 65,
  "color_palette": "warm",
  "weather_mood": "idyllic",
  "story_memory": 55,
  "is_cover_shot": false,
  "mood": "quiet afternoon light",
  "keywords": ["landscape", "nature", "peaceful"],
  "notes": "Solid composition but flat light drains the energy from the scene."
}}"""

# Claude-Batch-Template: mehrere Fotos in einem Call, gibt JSON-ARRAY zurück
_CLAUDE_BATCH_TEMPLATE = """Analyse the {n} photos below and return a JSON ARRAY — one object per photo, in the same order.

Photos:
{photo_list}

For EACH photo evaluate across ALL six dimensions and return exactly these fields:

1. SUBJECT & MOMENT
   - subject_interest (0–100): Clear, compelling subject? Random snapshot = 10–30.
   - decisive_moment (0–100): Alive/spontaneous vs. static/accidental?
   - has_faces (true/false): Human faces clearly visible?
   - eyes_open: "yes" (both eyes open & sharp) | "no" (closed/blinking) | "na" (no faces)
   - expression_score (0–100): ONLY if has_faces=true.
     YES eyes open + genuine smile/emotion = 70–95. NO eyes open = MAX 15. No faces = 0.
   - motion_type: "intentional" | "unintentional" | "none"

2. COMPOSITION
   - composition (0–100): framing, rule of thirds, leading lines, balance, negative space
   - background_distraction (0–100): 0 = clean/bokeh. 60–100 = distracting/cluttered.

3. TECHNICAL
   - technical (0–100): sharpness, exposure, noise. Camera shake = max 25. Heavy noise = max 40.
   - noise_level: "clean" | "moderate" | "heavy"

4. LIGHT & MOOD
   - light_quality: golden_hour | blue_hour | soft_overcast | harsh_midday | backlight_ok | backlight_problem | artificial | mixed
   - light_mood (0–100): Golden hour=80–95, blue hour=75–90, soft overcast=60–75, harsh midday=25–45, backlight_problem=20–40.
   - color_palette: warm | cool | harmonious | complementary | monochrome | mixed
   - weather_mood: dramatic | idyllic | melancholic | clear | stormy | foggy | neutral

5. STORY & MEMORY
   - story_memory (0–100): Random snapshot=15–35. Clear moment/emotion=65–85.
   - is_cover_shot (true/false): Best single photo to represent the whole session?

6. OVERALL
   - mood: short evocative phrase
   - keywords: 3–5 search tags
   - notes: ONE specific honest sentence — biggest strength OR flaw
   - filename: exact filename from the list above

Return EXACTLY a JSON array (no markdown, no extra text):
[
  {{
    "filename": "IMG_0001.RW2",
    "subject_interest": 65,
    "decisive_moment": 60,
    "has_faces": false,
    "eyes_open": "na",
    "expression_score": 0,
    "motion_type": "none",
    "composition": 70,
    "background_distraction": 30,
    "technical": 75,
    "noise_level": "clean",
    "light_quality": "soft_overcast",
    "light_mood": 65,
    "color_palette": "warm",
    "weather_mood": "idyllic",
    "story_memory": 55,
    "is_cover_shot": false,
    "mood": "quiet afternoon light",
    "keywords": ["landscape", "nature", "peaceful"],
    "notes": "Solid composition but flat light drains the energy from the scene."
  }}
]"""


def _parse_response(text: str) -> list[dict]:
    """JSON-Array aus Modellantwort extrahieren (robust gegen Markdown-Fences)."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        text = match.group(1).strip()
    start = text.find("[")
    end = text.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"Kein JSON-Array in Antwort: {text[:200]}")
    return json.loads(text[start:end])


def _merge_scores(record: PhotoRecord, ai_data: dict) -> None:
    """AI-Scores in PhotoRecord mergen, Final-Score nach dem 6-Dimensionen-Modell berechnen.

    Gewichtungsmodell (v2.0):
      18% Technik        — lokale Schärfe/Belichtung (Realitäts-Check)
      18% Komposition    — KI-Bildaufbau-Score (inkl. Background-Penalty)
      22% Motiv/Moment   — subject_interest + decisive_moment
      22% Licht          — light_mood + Qualitäts-Bonus/Penalty
      10% Ausdruck       — expression_score (nur wenn Gesichter, sonst neutral 50)
      10% Story/Memory   — Erzählwert + Erinnerungswert

    Hard-Penalties (nach gewichteter Summe):
      - Unintentional motion: -15
      - Augen zu (Portrait): -20
      - Starkes Rauschen: -12, Moderat: -4
      - Sehr ablenkendes Hintergrund (>75): -8
    """
    # Numerische Scores
    comp          = float(ai_data.get("composition",      50))
    technical_ai  = float(ai_data.get("technical",        50))
    subject       = float(ai_data.get("subject_interest", 50))
    decisive      = float(ai_data.get("decisive_moment",  50))
    light_mood    = float(ai_data.get("light_mood",       60))
    story_mem     = float(ai_data.get("story_memory",     50))
    expr          = float(ai_data.get("expression_score",  0))
    bg_dist       = float(ai_data.get("background_distraction", 30))

    # Kategorische Felder
    light_quality = str(ai_data.get("light_quality", ""))
    color_palette = str(ai_data.get("color_palette", ""))
    weather_mood  = str(ai_data.get("weather_mood",  ""))
    motion_type   = str(ai_data.get("motion_type",   "none"))
    noise_level   = str(ai_data.get("noise_level",   "clean"))
    eyes_open     = str(ai_data.get("eyes_open",     "na"))
    mood          = str(ai_data.get("mood",           ""))
    notes         = str(ai_data.get("notes",          ""))
    keywords      = ai_data.get("keywords", [])
    has_faces     = bool(ai_data.get("has_faces", False))
    is_cover      = bool(ai_data.get("is_cover_shot", False))

    # Lokaler technischer Score (Schärfe + Belichtung als Realitäts-Anker)
    local_tech = (record.local_scores.sharpness * 0.6
                  + record.local_scores.exposure * 0.4)

    # Motiv+Moment: decisive_moment stärker gewichtet
    subj_moment = 0.55 * decisive + 0.45 * subject

    # Komposition mit Background-Penalty
    bg_penalty_comp = max(0.0, (bg_dist - 50) / 50 * 15)  # bis -15 Punkte
    comp_adj = max(0.0, comp - bg_penalty_comp)

    # Ausdruck-Dimension: bei Gesichtern volle Dimension, sonst neutral
    expr_dim = expr if has_faces else 50.0

    # Licht-Kategorie-Bonus/Penalty (zusätzlich zur numerischen light_mood)
    light_bonus = {
        "golden_hour":       +6.0,
        "blue_hour":         +5.0,
        "backlight_ok":      +3.0,
        "soft_overcast":     +2.0,
        "mixed":              0.0,
        "artificial":         0.0,
        "harsh_midday":      -8.0,
        "backlight_problem": -12.0,
    }.get(light_quality, 0.0)

    # Finale gewichtete Summe
    final = (
        0.18 * local_tech      # echte Technik (lokal gemessen)
        + 0.18 * comp_adj      # KI-Komposition (mit BG-Penalty)
        + 0.22 * subj_moment   # Motiv + Entscheidender Moment
        + 0.22 * light_mood    # Licht-Score
        + 0.10 * expr_dim      # Ausdruck/Portrait-Qualität
        + 0.10 * story_mem     # Story + Erinnerungswert
        + light_bonus          # Licht-Kategorie-Bonus/-Penalty
    )

    # Hard-Penalties
    if motion_type == "unintentional":
        final -= 15.0   # Kameraverwacklung / Fokus verfehlt
    if has_faces and eyes_open == "no":
        final -= 20.0   # Augen zu — Portrait in der Regel unbrauchbar
    if noise_level == "heavy":
        final -= 12.0   # Starkes Rauschen
    elif noise_level == "moderate":
        final -= 4.0
    if bg_dist > 75:
        final -= 8.0    # Extrem ablenkender Hintergrund

    final = round(max(0.0, min(100.0, final)), 2)

    record.ai_scores = AIScores(
        composition=comp,
        technical=technical_ai,
        subject_interest=subject,
        decisive_moment=decisive,
        light_mood=light_mood,
        story_memory=story_mem,
        light_quality=light_quality,
        color_palette=color_palette,
        weather_mood=weather_mood,
        motion_type=motion_type,
        mood=mood,
        has_faces=has_faces,
        expression_score=expr,
        eyes_open=eyes_open,
        noise_level=noise_level,
        background_distraction=bg_dist,
        keywords=keywords if isinstance(keywords, list) else [keywords],
        notes=notes,
        is_cover_shot=is_cover,
        final_score=final,
    )


# ---------------------------------------------------------------------------
# Provider: Ollama (lokal)
# ---------------------------------------------------------------------------

def _ollama_available(base_url: str) -> bool:
    """Prüft ob Ollama läuft."""
    try:
        urllib.request.urlopen(f"{base_url}/api/tags", timeout=3)
        return True
    except Exception:
        return False


def _analyze_local(
    records: list,
    report: SessionReport,
    model: str = "llama3.2-vision",
    base_url: str = "http://localhost:11434",
    max_short_side: int = 800,
    jpeg_quality: int = 75,
    max_workers: int = 2,
) -> list:
    """
    Analyse via Ollama — 1 Foto pro Call, aber bis zu max_workers parallel.
    Gibt Liste der analysierten Records zurück.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    if not _ollama_available(base_url):
        console.print(
            f"[red]❌ Ollama nicht erreichbar unter {base_url}.[/red]\n"
            "[dim]Starte Ollama mit: ollama serve[/dim]"
        )
        return []

    console.print(
        f"[bold]🤖 AI-Analyse (lokal):[/bold] {len(records)} Fotos → "
        f"[cyan]{model}[/cyan] [dim]({max_workers} parallel)[/dim]"
    )

    results_lock = __import__('threading').Lock()
    analyzed = []
    counter = [0]   # mutable counter for thread-safe progress

    def _analyze_one(record: PhotoRecord) -> tuple:
        """Analyze a single record. Returns (record, ai_data_or_None)."""
        path = record.archive_path or record.original_path
        b64 = _prepare_image_b64(path, max_short_side, jpeg_quality)
        if b64 is None:
            return record, None

        prompt = _USER_TEMPLATE.format(filename=record.filename)
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "system": _SYSTEM_PROMPT,
            "images": [b64],
            "stream": False,
        }).encode()

        try:
            req = urllib.request.Request(
                f"{base_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read())
            raw_text = result.get("response", "").strip()

            # Modell gibt einzelnes JSON-Objekt zurück (kein Array mehr)
            item = None
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, dict):
                    item = parsed
                elif isinstance(parsed, list) and parsed:
                    item = parsed[0]
            except json.JSONDecodeError:
                # Fallback: extrahiere erstes { ... } Block
                start = raw_text.find("{")
                end   = raw_text.rfind("}") + 1
                if start >= 0 and end > start:
                    try:
                        item = json.loads(raw_text[start:end])
                    except json.JSONDecodeError:
                        pass

            if item:
                item["filename"] = record.filename
            return record, item
        except Exception:
            return record, None

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_analyze_one, r): r for r in records}
        for future in _as_completed(futures):
            record, item = future.result()
            counter[0] += 1
            idx = counter[0]
            if item:
                _merge_scores(record, item)
                analyzed.append(record)
                score = int(record.ai_scores.final_score)
                console.print(f"  [{idx}/{len(records)}] {record.filename} … [green]✓ Score {score}[/green]")
            else:
                console.print(f"  [{idx}/{len(records)}] {record.filename} … [yellow]kein Ergebnis[/yellow]")

    report.ai_calls += len(analyzed)
    report.ai_cost_usd = 0.0
    console.print(f"[green]✅ {len(analyzed)}/{len(records)} Fotos lokal analysiert.[/green]")
    return analyzed


# ---------------------------------------------------------------------------
# Provider: Claude API
# ---------------------------------------------------------------------------

def _build_claude_content(records: list, max_short_side: int, jpeg_quality: int) -> tuple[list, list]:
    """Content-Blocks für einen Claude-Batch-Call bauen."""
    content: list = []
    included: list = []
    failed: list = []
    photo_list_lines = []
    image_blocks = []

    for i, record in enumerate(records, 1):
        path = record.archive_path or record.original_path
        b64 = _prepare_image_b64(path, max_short_side, jpeg_quality)
        if b64 is None:
            failed.append(record.filename)
            continue
        photo_list_lines.append(f"{i}. {record.filename}")
        image_blocks.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
        included.append(record)

    if failed:
        console.print(f"[yellow]⚠ {len(failed)} Fotos nicht ladbar: "
                      f"{', '.join(failed[:3])}{'…' if len(failed) > 3 else ''}[/yellow]")

    for block in image_blocks:
        content.append(block)
    content.append({
        "type": "text",
        "text": _CLAUDE_BATCH_TEMPLATE.format(
            n=len(included),
            photo_list="\n".join(photo_list_lines),
        ),
    })
    return content, included


def _analyze_claude(
    records: list,
    report: SessionReport,
    model: str = "claude-sonnet-4-5",
    max_short_side: int = 800,
    jpeg_quality: int = 75,
    max_tokens: int = 1500,
    cost_input_per_mtok: float = 3.0,
    cost_output_per_mtok: float = 15.0,
    api_key: Optional[str] = None,
) -> list:
    """Analyse via Claude API — ein Batch-Call für alle TOP-Fotos."""
    if not _ANTHROPIC_AVAILABLE:
        console.print("[red]❌ anthropic-Paket nicht installiert: pip install anthropic[/red]")
        return []

    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        console.print(
            "[red]❌ Kein API-Key. Setze api_key in config.yaml "
            "oder ANTHROPIC_API_KEY als Umgebungsvariable.[/red]"
        )
        return []

    console.print(
        f"[bold]🤖 AI-Analyse (Claude):[/bold] {len(records)} Fotos → "
        f"[cyan]{model}[/cyan] (1 API-Call)"
    )

    content, included = _build_claude_content(records, max_short_side, jpeg_quality)
    if not included:
        return []

    client = anthropic.Anthropic(api_key=key)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as e:
        console.print(f"[red]❌ Claude API Fehler: {e}[/red]")
        return []

    usage = response.usage
    cost = (usage.input_tokens / 1_000_000 * cost_input_per_mtok
            + usage.output_tokens / 1_000_000 * cost_output_per_mtok)
    report.ai_calls += 1
    report.ai_input_tokens += usage.input_tokens
    report.ai_output_tokens += usage.output_tokens
    report.ai_cost_usd += round(cost, 4)
    console.print(f"[dim]Tokens: {usage.input_tokens} in / {usage.output_tokens} out — ~${cost:.4f}[/dim]")

    try:
        results = _parse_response(response.content[0].text)
    except (IndexError, ValueError, json.JSONDecodeError) as e:
        console.print(f"[red]❌ Antwort nicht parsbar: {e}[/red]")
        return []

    result_map: dict[str, dict] = {}
    for item in results:
        fname = item.get("filename", "")
        result_map[fname] = item
        result_map[Path(fname).stem] = item

    matched = 0
    for record in included:
        data = result_map.get(record.filename) or result_map.get(record.filename.rsplit(".", 1)[0])
        if data:
            _merge_scores(record, data)
            matched += 1
        else:
            console.print(f"[yellow]⚠ Kein Ergebnis für {record.filename}[/yellow]")

    console.print(f"[green]✅ {matched}/{len(included)} Fotos analysiert.[/green]")
    return included


# ---------------------------------------------------------------------------
# Haupt-Einstiegspunkt
# ---------------------------------------------------------------------------

def ai_analyze(
    records: list,
    report: SessionReport,
    provider: str = "local",
    # Ollama
    local_model: str = "llama3.2-vision",
    local_url: str = "http://localhost:11434",
    # Claude
    model: str = "claude-sonnet-4-5",
    max_tokens: int = 1500,
    cost_input_per_mtok: float = 3.0,
    cost_output_per_mtok: float = 15.0,
    api_key: Optional[str] = None,
    # Shared
    max_short_side: int = 512,
    jpeg_quality: int = 65,
    max_workers: int = 2,
) -> list:
    """
    Analysiert TOP-Fotos via lokalem Ollama-Modell oder Claude API.
    Provider wird via config.yaml gesteuert (ai.provider: local | claude).
    """
    if not _PIL_AVAILABLE:
        console.print("[yellow]⚠ Pillow nicht installiert — AI-Analyse übersprungen.[/yellow]")
        return []

    top_records = [r for r in records if r.label == Label.TOP and not r.was_skipped]
    if not top_records:
        console.print("[dim]Keine TOP-Fotos für AI-Analyse.[/dim]")
        return []

    if provider == "local":
        return _analyze_local(
            records=top_records,
            report=report,
            model=local_model,
            base_url=local_url,
            max_short_side=max_short_side,
            jpeg_quality=jpeg_quality,
            max_workers=max_workers,
        )
    elif provider == "claude":
        return _analyze_claude(
            records=top_records,
            report=report,
            model=model,
            max_short_side=max_short_side,
            jpeg_quality=jpeg_quality,
            max_tokens=max_tokens,
            cost_input_per_mtok=cost_input_per_mtok,
            cost_output_per_mtok=cost_output_per_mtok,
            api_key=api_key,
        )
    else:
        console.print(f"[red]❌ Unbekannter AI-Provider: '{provider}'. Nutze 'local' oder 'claude'.[/red]")
        return []
