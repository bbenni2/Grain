"""
Composition analysis module — vollständig lokal, kein API-Call.

Analysiert stilistische Qualitätsmerkmale:
  - Rule of Thirds: liegt das Hauptmotiv auf einem Drittel-Schnittpunkt?
  - Symmetrie: horizontale oder vertikale Bildspiegelung
  - Horizont-Level: ist die Kamera gerade gehalten?
  - Leading Lines: führende Linien (Diagonalen, Konvergenz)
  - Framing-Balance: Gewichtsverteilung im Bild

Alle Scores 0–100. Rückgabe als ComposeScores-Dict.
"""

from pathlib import Path
from typing import Optional

import numpy as np

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

# Analyse-Auflösung — klein genug für Geschwindigkeit, groß genug für Präzision
ANALYZE_SIZE = 600


# ---------------------------------------------------------------------------
# Bild laden
# ---------------------------------------------------------------------------

def _load_gray(path: Path) -> Optional[np.ndarray]:
    """Graustufen-Array für Kompositionsanalyse laden."""
    if not _PIL_AVAILABLE:
        return None
    try:
        ext = path.suffix.lower()
        if ext in {".rw2", ".cr2", ".cr3", ".nef", ".arw"}:
            try:
                import rawpy, io
                with rawpy.imread(str(path)) as raw:
                    thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        img = Image.open(io.BytesIO(thumb.data))
                    else:
                        img = Image.fromarray(thumb.data)
            except Exception:
                return None
        else:
            img = Image.open(path)
            img.load()

        # Auf Analyse-Größe skalieren
        w, h = img.size
        scale = ANALYZE_SIZE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        return np.array(img.convert("L"), dtype=np.uint8)
    except Exception:
        return None


def _load_color(path: Path) -> Optional[np.ndarray]:
    """RGB-Array laden (für Symmetrie-Analyse)."""
    if not _PIL_AVAILABLE:
        return None
    try:
        ext = path.suffix.lower()
        if ext in {".rw2", ".cr2", ".cr3", ".nef", ".arw"}:
            try:
                import rawpy, io
                with rawpy.imread(str(path)) as raw:
                    thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        img = Image.open(io.BytesIO(thumb.data))
                    else:
                        img = Image.fromarray(thumb.data)
            except Exception:
                return None
        else:
            img = Image.open(path)
            img.load()

        w, h = img.size
        scale = ANALYZE_SIZE / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        return np.array(img.convert("RGB"), dtype=np.uint8)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. Rule of Thirds
# ---------------------------------------------------------------------------

def score_rule_of_thirds(gray: np.ndarray) -> float:
    """
    Bewertet ob das Hauptmotiv auf einem Drittel-Schnittpunkt liegt.

    Methode:
    - Saliency-Map via Laplacian + Gradient-Magnitude
    - Schwerpunkt der salienten Region berechnen
    - Distanz zu den 4 Drittel-Schnittpunkten messen
    - Nähe = hoher Score

    Score 0–100.
    """
    if not _CV2_AVAILABLE:
        return 50.0

    h, w = gray.shape

    # Saliency-Approximation: Laplacian-Betrag + Gradient
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    lap_abs = np.abs(lap)

    gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    grad = np.sqrt(gx**2 + gy**2)

    saliency = lap_abs * 0.5 + grad * 0.5
    saliency = cv2.GaussianBlur(saliency, (21, 21), 0)

    # Schwellwert: obere 30% der Saliency
    thresh = np.percentile(saliency, 70)
    mask = saliency > thresh

    if mask.sum() == 0:
        return 40.0

    ys, xs = np.where(mask)
    cx = xs.mean() / w   # normiert 0–1
    cy = ys.mean() / h

    # 4 Drittel-Schnittpunkte (normiert)
    thirds = [(1/3, 1/3), (2/3, 1/3), (1/3, 2/3), (2/3, 2/3)]

    min_dist = min(
        np.sqrt((cx - tx)**2 + (cy - ty)**2)
        for tx, ty in thirds
    )

    # Max mögliche Distanz vom Mittelpunkt zu einem Schnittpunkt ≈ 0.47
    score = max(0.0, 100.0 - (min_dist / 0.47) * 100.0)
    return round(score, 2)


# ---------------------------------------------------------------------------
# 2. Symmetrie
# ---------------------------------------------------------------------------

def score_symmetry(gray: np.ndarray) -> tuple[float, str]:
    """
    Bewertet horizontale und vertikale Symmetrie.
    Gibt (score 0–100, axis: 'horizontal'|'vertical'|'none') zurück.

    Methode: strukturelle Ähnlichkeit (vereinfacht via normierte Kreuzkorrelation)
    zwischen linker/rechter bzw. oberer/unterer Bildhälfte.
    """
    h, w = gray.shape
    gray_f = gray.astype(np.float32)

    # Horizontale Symmetrie (links vs. rechts gespiegelt)
    left  = gray_f[:, :w//2]
    right = np.fliplr(gray_f[:, w - w//2:])
    min_w = min(left.shape[1], right.shape[1])
    h_sim = _ncc(left[:, :min_w], right[:, :min_w])

    # Vertikale Symmetrie (oben vs. unten gespiegelt)
    top    = gray_f[:h//2, :]
    bottom = np.flipud(gray_f[h - h//2:, :])
    min_h  = min(top.shape[0], bottom.shape[0])
    v_sim  = _ncc(top[:min_h, :], bottom[:min_h, :])

    best = max(h_sim, v_sim)
    axis = "horizontal" if h_sim >= v_sim else "vertical"

    # Nur echte Symmetrie werten (> 0.75 NCC)
    if best < 0.6:
        return round(best * 60, 2), "none"

    score = round((best - 0.6) / 0.4 * 100, 2)   # 0.6–1.0 → 0–100
    return min(100.0, score), axis


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Normierte Kreuzkorrelation zweier Arrays (0–1)."""
    a = a - a.mean()
    b = b - b.mean()
    denom = (np.std(a) * np.std(b) * a.size)
    if denom < 1e-6:
        return 0.0
    return float(np.sum(a * b) / denom)


# ---------------------------------------------------------------------------
# 3. Horizont-Level
# ---------------------------------------------------------------------------

def score_horizon_level(gray: np.ndarray) -> tuple[float, float]:
    """
    Erkennt dominante horizontale Linien und berechnet Neigung.
    Gibt (score 0–100, tilt_degrees) zurück.
    Score 100 = perfekt gerade, Score 0 = stark geneigt (>5°).
    """
    if not _CV2_AVAILABLE:
        return 50.0, 0.0

    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=80)

    if lines is None:
        return 50.0, 0.0

    # Linien nahe horizontal (±20°) oder vertikal (±20°) filtern
    angles = []
    for line in lines:
        rho, theta = line[0]
        angle_deg = np.degrees(theta) - 90   # 0° = horizontal
        if abs(angle_deg) <= 20:
            angles.append(angle_deg)

    if not angles:
        return 50.0, 0.0

    # Median-Winkel (robust gegen Ausreißer)
    median_angle = float(np.median(angles))
    tilt = abs(median_angle)

    # Score: 0° → 100, ≥5° → 0
    score = max(0.0, 100.0 - (tilt / 5.0) * 100.0)
    return round(score, 2), round(median_angle, 2)


# ---------------------------------------------------------------------------
# 4. Leading Lines
# ---------------------------------------------------------------------------

def score_leading_lines(gray: np.ndarray) -> float:
    """
    Erkennt führende Linien (Diagonalen, Konvergenz).
    Score 0–100: mehr starke diagonale Linien = höherer Score.
    """
    if not _CV2_AVAILABLE:
        return 50.0

    h, w = gray.shape
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180,
        threshold=60,
        minLineLength=int(min(w, h) * 0.2),
        maxLineGap=15,
    )

    if lines is None:
        return 30.0

    diagonal_count = 0
    strong_count = 0

    for line in lines:
        x1, y1, x2, y2 = line[0]
        length = np.sqrt((x2 - x1)**2 + (y2 - y1)**2)
        if length < min(w, h) * 0.15:
            continue
        strong_count += 1

        angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        # Diagonal = 20°–70° oder 110°–160°
        if (20 <= angle <= 70) or (110 <= angle <= 160):
            diagonal_count += 1

    if strong_count == 0:
        return 30.0

    diagonal_ratio = diagonal_count / strong_count
    # Mehr Diagonalen und mehr starke Linien = besserer Score
    score = diagonal_ratio * 70 + min(strong_count / 10, 1.0) * 30
    return round(min(100.0, score), 2)


# ---------------------------------------------------------------------------
# 5. Framing Balance (Gewichtsverteilung)
# ---------------------------------------------------------------------------

def score_balance(gray: np.ndarray) -> float:
    """
    Bewertet die visuelle Gewichtsverteilung im Bild.
    Gleichmäßig verteilte Helligkeit/Details = ausgewogenes Bild.
    Stark asymmetrisch = niedrigerer Score (kann aber auch Stil sein).
    Score 0–100.
    """
    h, w = gray.shape
    gray_f = gray.astype(np.float32)

    # 4 Quadranten
    quads = [
        gray_f[:h//2, :w//2].mean(),
        gray_f[:h//2, w//2:].mean(),
        gray_f[h//2:, :w//2].mean(),
        gray_f[h//2:, w//2:].mean(),
    ]
    std = np.std(quads)
    # Std von 0 = perfekt ausgewogen, Std > 60 = sehr ungleich
    score = max(0.0, 100.0 - (std / 60.0) * 60.0)
    return round(score, 2)


# ---------------------------------------------------------------------------
# Haupt-Funktion
# ---------------------------------------------------------------------------

def analyze_composition(path: Path) -> dict:
    """
    Führt alle Kompositions-Checks durch.
    Gibt Dict mit Einzel-Scores und Gesamt-Score zurück.
    Fällt bei fehlendem OpenCV auf Defaults zurück.
    """
    gray = _load_gray(path)

    if gray is None:
        return {
            "rule_of_thirds": 50.0,
            "symmetry": 0.0,
            "symmetry_axis": "none",
            "horizon_level": 50.0,
            "horizon_tilt_deg": 0.0,
            "leading_lines": 50.0,
            "balance": 50.0,
            "overall": 50.0,
        }

    rot     = score_rule_of_thirds(gray)
    sym, ax = score_symmetry(gray)
    horiz, tilt = score_horizon_level(gray)
    lines   = score_leading_lines(gray)
    balance = score_balance(gray)

    # Gewichteter Gesamt-Score
    # Rule of thirds hat höchstes Gewicht — wichtigstes Kompositionsprinzip
    # Symmetrie ist Stil, nicht immer erwünscht → niedrigeres Gewicht
    overall = round(
        0.35 * rot
        + 0.10 * sym
        + 0.25 * horiz
        + 0.20 * lines
        + 0.10 * balance,
        2
    )

    return {
        "rule_of_thirds": rot,
        "symmetry": sym,
        "symmetry_axis": ax,
        "horizon_level": horiz,
        "horizon_tilt_deg": tilt,
        "leading_lines": lines,
        "balance": balance,
        "overall": overall,
    }


def analyze_composition_from_array(gray: np.ndarray) -> dict:
    """Kompositionsanalyse aus bereits geladenem Gray-Array (kein Disk-Zugriff)."""
    if gray is None:
        return {
            "rule_of_thirds": 50.0, "symmetry": 0.0, "symmetry_axis": "none",
            "horizon_level": 50.0, "horizon_tilt_deg": 0.0,
            "leading_lines": 50.0, "balance": 50.0, "overall": 50.0,
        }
    rot     = score_rule_of_thirds(gray)
    sym, ax = score_symmetry(gray)
    horiz, tilt = score_horizon_level(gray)
    lines   = score_leading_lines(gray)
    balance = score_balance(gray)
    overall = round(0.35*rot + 0.10*sym + 0.25*horiz + 0.20*lines + 0.10*balance, 2)
    return {
        "rule_of_thirds": rot, "symmetry": sym, "symmetry_axis": ax,
        "horizon_level": horiz, "horizon_tilt_deg": tilt,
        "leading_lines": lines, "balance": balance, "overall": overall,
    }
