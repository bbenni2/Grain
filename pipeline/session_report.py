"""
Session Review Report — Foto-Coach Format (v1.5)

Erstellt SESSION_REVIEW.md als persönlichen Coaching-Bericht:
klare Sprache, konkrete Tipps, Fortschritts-Vergleich.
"""

import json
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Optional

from pipeline import BracketRole, Label, PhotoRecord, SessionReport


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _pct(n: int, total: int) -> str:
    return f"{n/total*100:.0f}%" if total else "0%"


def _fmt_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def _score_bar(score: float, width: int = 10) -> str:
    filled = max(0, min(width, round(score / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _trend_arrow(now: float, prev: Optional[float]) -> str:
    if prev is None:
        return "—"
    diff = now - prev
    if diff > 3:
        return f"↑ +{diff:.0f}"
    elif diff < -3:
        return f"↓ {diff:.0f}"
    return "→ ±0"


def _avg(records: list, attr: str, sub: str = "") -> float:
    vals = []
    for r in records:
        try:
            obj = getattr(r, attr)
            v = float(getattr(obj, sub) if sub else obj)
            vals.append(v)
        except Exception:
            pass
    return round(mean(vals), 1) if vals else 0.0


def _ai_avg(records: list, attr: str) -> float:
    vals = [getattr(r.ai_scores, attr) for r in records
            if r.ai_scores and hasattr(r.ai_scores, attr)]
    return round(mean(vals), 1) if vals else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Abschnitte
# ─────────────────────────────────────────────────────────────────────────────

def _header(report: SessionReport) -> str:
    elapsed = (report.finished_at - report.started_at) if report.finished_at else 0
    m, s = divmod(int(elapsed), 60)
    cost  = f"~${report.ai_cost_usd:.4f}" if report.ai_cost_usd else "kostenlos (lokal)"

    return f"""# 📷 Session Review – {report.event_name} – {_fmt_dt(report.started_at)}

## Deine Session auf einen Blick

| | |
|---|---|
| **Fotos gesamt** | {report.total_imported} |
| **⭐ TOP** | {report.top_count} ({_pct(report.top_count, report.total_imported)}) |
| **✅ KEEP** | {report.keep_count} ({_pct(report.keep_count, report.total_imported)}) |
| **🗑 REJECT** | {report.reject_count} ({_pct(report.reject_count, report.total_imported)}) |
| **Analyse-Dauer** | {m}m {s:02d}s |
| **KI-Kosten** | {cost} |
"""


def _best_photo(records: list) -> str:
    top = [r for r in records if r.label == Label.TOP and not r.was_skipped]
    if not top:
        return ""

    best = max(top, key=lambda r:
               r.ai_scores.final_score if r.ai_scores else r.local_scores.composite)
    score = best.ai_scores.final_score if best.ai_scores else best.local_scores.composite

    why_parts = []
    if best.ai_scores:
        ai = best.ai_scores
        if ai.light_quality == "golden_hour":
            why_parts.append("goldenes Stundenlicht")
        elif ai.light_quality == "blue_hour":
            why_parts.append("blaue Stunde")
        if ai.decisive_moment >= 75:
            why_parts.append("starker entscheidender Moment")
        if ai.has_faces and ai.expression_score >= 70:
            why_parts.append("ausdrucksstarkes Gesicht")
        if ai.composition >= 75:
            why_parts.append("gute Komposition")
        if ai.notes:
            why_parts.append(ai.notes)
        mood = f" · *{ai.mood}*" if ai.mood else ""
    else:
        why_parts.append(f"Schärfe {best.local_scores.sharpness:.0f}/100, "
                         f"Belichtung {best.local_scores.exposure:.0f}/100")
        mood = ""

    why = why_parts[0].capitalize() if why_parts else "Bestes technisches Ergebnis"

    return f"""
## 🏆 Dein bestes Foto

**`{best.filename}`**{mood}

> {why}

Score: **{score:.0f}/100**
"""


def _scores_table(records: list, prev_scores: Optional[dict]) -> str:
    eligible = [r for r in records if not r.was_skipped]
    ai_rec   = [r for r in eligible if r.ai_scores]

    # Scores berechnen (gleiche Logik wie history.py)
    tech = round(mean([
        r.local_scores.sharpness * 0.6 + r.local_scores.exposure * 0.4
        for r in eligible
    ]), 1) if eligible else 0.0

    comp_local = _avg(eligible, "local_scores", "composition_overall")
    comp_ai    = _ai_avg(ai_rec, "composition")
    comp = round(0.5 * comp_local + 0.5 * comp_ai, 1) if ai_rec else comp_local

    subj_moment = round(mean([
        0.55 * r.ai_scores.decisive_moment + 0.45 * r.ai_scores.subject_interest
        for r in ai_rec
    ]), 1) if ai_rec else 0.0

    light_mood  = _ai_avg(ai_rec, "light_mood")
    story_mem   = _ai_avg(ai_rec, "story_memory")

    p = prev_scores or {}
    rows = [
        ("Technik",        tech,        p.get("technique"),     "Schärfe, Belichtung, Rauschen"),
        ("Komposition",    comp,        p.get("composition"),   "Bildaufbau, Linien, Balance"),
        ("Motiv & Moment", subj_moment, p.get("subject_moment"),"Decisiver Moment, Ausdruck, Fokus"),
        ("Licht & Stimmung",light_mood, p.get("light_mood"),    "Lichtqualität, Farbe, Atmosphäre"),
        ("Story & Erinnerung",story_mem,p.get("story_memory"),  "Erzählwert, Erinnerungswert"),
    ]

    has_ai   = bool(ai_rec)
    has_prev = bool(p)

    lines = ["\n## 📊 Deine Scores heute\n"]

    if has_prev:
        lines.append("| Kategorie | Score | Balken | vs. letzte Session |")
        lines.append("|---|---|---|---|")
        for label, score, prev, _ in rows:
            bar = _score_bar(score)
            trend = _trend_arrow(score, prev)
            na = " *(kein AI)*" if not has_ai and label != "Technik" else ""
            lines.append(f"| {label}{na} | **{score:.0f}/100** | `{bar}` | {trend} |")
    else:
        lines.append("| Kategorie | Score | Balken | |")
        lines.append("|---|---|---|---|")
        for label, score, _, desc in rows:
            bar = _score_bar(score)
            na = " *(kein AI)*" if not has_ai and label != "Technik" else ""
            lines.append(f"| {label}{na} | **{score:.0f}/100** | `{bar}` | {desc} |")

    if not has_ai:
        lines.append("\n> *AI-Scores (Motiv, Licht, Story) wurden nicht berechnet "
                     "— starte mit aktivierter AI für vollständige Analyse.*")
    if not has_prev:
        lines.append("\n> *Vergleich zur letzten Session erscheint ab der zweiten Session.*")

    return "\n".join(lines) + "\n"


def _generate_tips(records: list, report: SessionReport) -> str:
    """Generiert 3 konkrete, datenbasierte Tipps für die nächste Session."""
    eligible = [r for r in records if not r.was_skipped]
    ai_rec   = [r for r in eligible if r.ai_scores]

    tips = []

    # Tipp 1: Technische Schwäche
    blurry = [r for r in eligible if r.local_scores.is_blurry]
    overexp = [r for r in eligible if r.local_scores.is_overexposed]
    underexp = [r for r in eligible if r.local_scores.is_underexposed]

    total = max(len(eligible), 1)
    if len(blurry) / total > 0.20:
        pct = _pct(len(blurry), total)
        tips.append(
            f"**Schärfe verbessern** — {pct} deiner Fotos ({len(blurry)} Bilder) sind unscharf. "
            f"Probiere eine kürzere Belichtungszeit (Faustregel: mind. 1/{100}s bei Handhalten) "
            f"oder aktiviere den optischen Bildstabilisator."
        )
    elif len(overexp) / total > 0.15:
        tips.append(
            f"**Belichtung anpassen** — {_pct(len(overexp), total)} deiner Fotos sind überbelichtet. "
            f"Nutze die Belichtungskorrektur (−1 EV als Ausgangspunkt) oder Spot-Messung."
        )
    elif len(underexp) / total > 0.15:
        tips.append(
            f"**Mehr Licht einfangen** — {_pct(len(underexp), total)} Fotos sind unterbelichtet. "
            f"Erhöhe ISO (Lumix G9: bis ISO 1600 ist sehr sauber) oder öffne die Blende weiter."
        )

    if ai_rec:
        # Tipp 2: Motiv oder Moment
        dm_avg = mean(r.ai_scores.decisive_moment for r in ai_rec)
        si_avg = mean(r.ai_scores.subject_interest for r in ai_rec)
        unintentional = sum(1 for r in ai_rec
                            if r.ai_scores.motion_type == "unintentional")

        if unintentional / max(len(ai_rec), 1) > 0.15:
            tips.append(
                f"**Bewegungsunschärfe kontrollieren** — bei {unintentional} Fotos wirkt die "
                f"Unschärfe ungewollt. Entweder schnellere Verschlusszeit wählen — oder bewusst "
                f"mit langsamerer Zeit und Stativ für Seideneffekt bei Wasser/Licht arbeiten."
            )
        elif dm_avg < 58:
            tips.append(
                f"**Entscheidenden Moment abwarten** — viele Fotos wirken gestellt "
                f"(Ø Decisive Moment: {dm_avg:.0f}/100). Fotografiere mehr aus dem Moment heraus: "
                f"warte auf natürliche Bewegung, echte Emotionen, oder den Gipfel einer Handlung."
            )
        elif si_avg < 55:
            tips.append(
                f"**Klareres Hauptmotiv suchen** — das durchschnittliche Motiv-Interesse ist "
                f"{si_avg:.0f}/100. Stelle dir vor dem Drücken die Frage: 'Was genau fotografiere ich?' "
                f"Näher ran, oder einen Rahmen durch Vordergrund schaffen."
            )

        # Tipp 3: Licht
        lq_counts = {}
        for r in ai_rec:
            lq = r.ai_scores.light_quality
            lq_counts[lq] = lq_counts.get(lq, 0) + 1

        golden_ratio = lq_counts.get("golden_hour", 0) / max(len(ai_rec), 1)
        harsh_ratio  = lq_counts.get("harsh_midday", 0) / max(len(ai_rec), 1)
        light_avg    = mean(r.ai_scores.light_mood for r in ai_rec)

        if harsh_ratio > 0.35:
            tips.append(
                f"**Mittagslicht meiden** — {_pct(lq_counts.get('harsh_midday', 0), len(ai_rec))} "
                f"deiner Fotos entstanden bei hartem Mittagslicht (niedrigster Licht-Score). "
                f"Plane deine Sessions für goldene Stunde (1h nach Sonnenaufgang / 1h vor Sonnenuntergang) "
                f"oder nutze Schatten/Bewölkung als natürlichen Diffusor."
            )
        elif golden_ratio > 0.4:
            tips.append(
                f"**Goldene Stunde weiter nutzen** — bereits {_pct(lq_counts.get('golden_hour', 0), len(ai_rec))} "
                f"deiner Fotos entstanden bei idealen Lichtverhältnissen. "
                f"Experimentiere zusätzlich mit Gegenlicht-Situationen für Silhouetten und Lens Flares."
            )
        elif light_avg < 55:
            tips.append(
                f"**Licht bewusster einsetzen** — der durchschnittliche Licht-Score ist {light_avg:.0f}/100. "
                f"Achte auf die Lichtrichtung (Seitenlicht modelliert Gesichter), "
                f"nutze Schatten als Gestaltungselement, und suche weiche Lichtsituationen (Bewölkung, Schatten)."
            )

    # Fülle auf 3 Tipps wenn zu wenige
    if len(tips) < 3:
        tips.append(
            "**Session-Nachbereitung** — sortiere direkt nach dem Fotografieren in "
            "deinen _TOP/-Ordner. Schau was du dreimal fotografiert hast — "
            "das zeigt was dich wirklich interessiert und wo du besser werden willst."
        )
    if len(tips) < 3:
        tips.append(
            "**Vor dem Drücken fragen** — 'Warum drücke ich jetzt ab?' "
            "Dieser eine Moment der Reflektion verbessert die Trefferquote deutlich "
            "und reduziert die Menge der Rejects."
        )

    lines = ["\n## 💡 Deine 3 wichtigsten Tipps für nächstes Mal\n"]
    for i, tip in enumerate(tips[:3], 1):
        lines.append(f"{i}. {tip}\n")

    return "\n".join(lines)


def _progress_section(prev_scores: Optional[dict], records: list) -> str:
    eligible = [r for r in records if not r.was_skipped]
    ai_rec   = [r for r in eligible if r.ai_scores]

    lines = ["\n## 📈 Dein Fortschritt\n"]

    if not ai_rec:
        lines.append("*Aktiviere die AI-Analyse für Fortschritts-Tracking.*")
        return "\n".join(lines)

    # Stärken und Wachstumsbereiche aus den Daten
    strengths = []
    growth = []

    comp_local = mean(r.local_scores.composition_overall for r in eligible) if eligible else 0
    if comp_local >= 68:
        strengths.append("Bildaufbau — du platzierst dein Hauptmotiv gut im Bild")

    dm_avg = mean(r.ai_scores.decisive_moment for r in ai_rec)
    if dm_avg >= 68:
        strengths.append("Timing — du erwischst lebendige, natürliche Momente")
    else:
        growth.append("Decisive Moment — öfter warten, öfter auslösen")

    lm_avg = mean(r.ai_scores.light_mood for r in ai_rec)
    if lm_avg >= 72:
        strengths.append("Lichtsinn — du erkennst und nutzt gutes Licht")
    else:
        growth.append("Lichtführung — Lichtsituationen bewusster auswählen")

    sm_avg = mean(r.ai_scores.story_memory for r in ai_rec)
    if sm_avg >= 65:
        strengths.append("Storytelling — deine Fotos erzählen etwas")
    elif sm_avg < 50:
        growth.append("Story — suche Fotos die auch in 10 Jahren noch bedeutsam sind")

    if strengths:
        lines.append(f"**Stärken:** {' · '.join(strengths)}")
    if growth:
        lines.append(f"**Wachstumsbereiche:** {' · '.join(growth)}")

    # Muster (nur mit AI und mindestens 10 Fotos)
    if len(ai_rec) >= 10:
        lq_counts = {}
        for r in ai_rec:
            lq = r.ai_scores.light_quality or "mixed"
            lq_counts[lq] = lq_counts.get(lq, 0) + 1
        top_lq, top_lq_n = max(lq_counts.items(), key=lambda x: x[1])
        lq_labels = {
            "golden_hour": "goldene Stunde", "blue_hour": "blaue Stunde",
            "soft_overcast": "weiches Licht", "harsh_midday": "Mittagslicht",
        }
        if top_lq in lq_labels:
            lines.append(
                f"\n**Muster:** In dieser Session entstanden "
                f"{_pct(top_lq_n, len(ai_rec))} deiner Fotos bei "
                f"*{lq_labels[top_lq]}* — das ist dein dominantes Lichtverhältnis."
            )

    return "\n".join(lines) + "\n"


def _highlights(records: list, n: int = 5) -> str:
    top = sorted(
        [r for r in records if r.label == Label.TOP and not r.was_skipped],
        key=lambda r: r.ai_scores.final_score if r.ai_scores else r.local_scores.composite,
        reverse=True,
    )[:n]

    if not top:
        return ""

    lines = [f"\n## 🌅 Session Highlights\n",
             f"*Die {len(top)} besten Fotos dieser Session:*\n"]

    for i, r in enumerate(top, 1):
        score = r.ai_scores.final_score if r.ai_scores else r.local_scores.composite
        if r.ai_scores and r.ai_scores.notes:
            why = r.ai_scores.notes
        elif r.ai_scores and r.ai_scores.mood:
            why = f"Stimmung: {r.ai_scores.mood}"
        else:
            why = f"Komposition: {r.local_scores.composition_overall:.0f}/100, Schärfe: {r.local_scores.sharpness:.0f}/100"

        cover = " 🎯 *(Cover Shot)*" if r.ai_scores and r.ai_scores.is_cover_shot else ""
        lines.append(f"{i}. **`{r.filename}`** — {score:.0f}/100{cover}  ")
        lines.append(f"   {why}\n")

    return "\n".join(lines)


def _camera_insights(records: list) -> str:
    top = [r for r in records if r.label == Label.TOP and not r.was_skipped]
    if not top:
        return ""

    from collections import Counter
    iso_vals  = [r.iso          for r in top if r.iso]
    apt_vals  = [r.aperture     for r in top if r.aperture]
    ss_vals   = [r.shutter_speed for r in top if r.shutter_speed]

    def mc(lst): return Counter(lst).most_common(1)[0][0] if lst else "?"

    lines = ["\n## 📷 Kamera-Insights\n",
             f"*Analyse der {len(top)} TOP-Fotos:*\n",
             f"| Einstellung | Häufigster Wert bei TOP-Fotos |",
             f"|---|---|",
             f"| ISO | {mc(iso_vals)} |",
             f"| Blende | {mc(apt_vals)} |",
             f"| Verschlusszeit | {mc(ss_vals)} |",
             ]

    if records[0].ai_scores if records else None:
        ai_rec = [r for r in top if r.ai_scores]
        if ai_rec:
            golden = sum(1 for r in ai_rec if r.ai_scores.light_quality == "golden_hour")
            lines.append(f"\n**Goldene Stunde:** {_pct(golden, len(ai_rec))} der TOP-Fotos entstanden bei "
                         f"optimalem Licht.")

            # Brackets
            bracket_top = [r for r in top if r.bracket_role == BracketRole.BASE]
            if bracket_top:
                lines.append(f"\n**Bracket-Reihen:** {len(bracket_top)} TOP-Fotos sind Basis-Frames "
                             f"von Belichtungsreihen — bereit für HDR-Merge in Photoshop.")

    return "\n".join(lines) + "\n"


def _raw_data_block(report: SessionReport, records: list) -> str:
    """Maschinenlesbarer Anhang für KI-Weiterverarbeitung."""
    eligible = [r for r in records if not r.was_skipped]
    ai_rec   = [r for r in eligible if r.ai_scores]

    def ai_avg(attr):
        vals = [getattr(r.ai_scores, attr) for r in ai_rec if hasattr(r.ai_scores, attr)]
        return round(mean(vals), 1) if vals else 0.0

    raw = {
        "session_id":    report.session_id,
        "event":         report.event_name,
        "date":          _fmt_dt(report.started_at),
        "pipeline_version": report.pipeline_version,
        "stats": {
            "total": report.total_imported,
            "top":   report.top_count,
            "keep":  report.keep_count,
            "reject": report.reject_count,
        },
        "avg_scores": {
            "technique":      round(mean([
                r.local_scores.sharpness * 0.6 + r.local_scores.exposure * 0.4
                for r in eligible
            ]), 1) if eligible else 0,
            "composition":    round(mean([r.local_scores.composition_overall for r in eligible]), 1) if eligible else 0,
            "subject_moment": round(mean([0.55 * r.ai_scores.decisive_moment + 0.45 * r.ai_scores.subject_interest for r in ai_rec]), 1) if ai_rec else 0,
            "light_mood":     ai_avg("light_mood"),
            "story_memory":   ai_avg("story_memory"),
        },
        "ai_analyzed": len(ai_rec),
    }

    return (
        "\n---\n\n## 🤖 Maschinenlesbarer Anhang\n\n"
        "```json\n"
        + json.dumps(raw, indent=2, ensure_ascii=False)
        + "\n```\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Haupt-Einstiegspunkt
# ─────────────────────────────────────────────────────────────────────────────

def generate_session_review(
    records: list,
    report: SessionReport,
    output_path: Path,
    critique: Optional[dict] = None,
    prev_scores: Optional[dict] = None,
) -> Path:
    """
    Erstellt SESSION_REVIEW.md im Foto-Coach-Format.
    prev_scores: Scores der letzten Session (aus history.py) für Vergleich.
    """
    sections = [
        _header(report),
        _best_photo(records),
        _scores_table(records, prev_scores),
        _generate_tips(records, report),
        _progress_section(prev_scores, records),
        _highlights(records),
        _camera_insights(records),
    ]

    # Artistikritik aus Ollama (wenn vorhanden)
    if critique:
        crit_lines = ["\n## 🎭 KI-Künstlerkritik\n",
                      f"> *Generiert von Ollama — fokussiert auf Wachstum, nicht Regelkonformität.*\n",
                      f"**Session-Stil:** {critique.get('session_style', '—')}\n"]
        for s in critique.get("strengths", []):
            crit_lines.append(f"✅ {s}")
        for g in critique.get("growth_areas", []):
            crit_lines.append(f"\n**{g.get('area', '?')}** — {g.get('observation', '')}")
            crit_lines.append(f"→ *{g.get('suggestion', '')}*")
        if critique.get("best_moment"):
            crit_lines.append(f"\n**Bester Moment:** {critique['best_moment']}")
        if critique.get("next_session_focus"):
            crit_lines.append(f"\n**Fokus für nächste Session:** {critique['next_session_focus']}")
        sections.append("\n".join(crit_lines) + "\n")

    sections.append(_raw_data_block(report, records))

    content = "\n".join(s for s in sections if s)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    return output_path
