"""AI Foto-Pipeline — pipeline package."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
import time

PIPELINE_VERSION = "1.6.0"


class Label(str, Enum):
    TOP = "TOP"
    KEEP = "KEEP"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


LABEL_EMOJI = {
    Label.TOP: "⭐",
    Label.KEEP: "✅",
    Label.REJECT: "🗑",
    Label.UNKNOWN: "❓",
}


class BracketRole(str, Enum):
    NONE = "NONE"           # Not part of a bracket sequence
    BASE = "BASE"           # 0 EV / best-exposed frame — used as representative
    UNDER = "UNDER"         # Underexposed frame (negative EV bias)
    OVER = "OVER"           # Overexposed frame (positive EV bias)
    MEMBER = "MEMBER"       # Part of bracket but EV undetermined


BRACKET_ROLE_EMOJI = {
    BracketRole.NONE:   "",
    BracketRole.BASE:   "0️⃣",
    BracketRole.UNDER:  "➖",
    BracketRole.OVER:   "➕",
    BracketRole.MEMBER: "🔲",
}


@dataclass
class BracketGroup:
    group_id: int = 0
    frame_count: int = 0          # total frames in this bracket
    ev_range: float = 0.0         # total EV spread (e.g. 4.0 for ±2EV)
    base_filename: str = ""       # filename of the 0-EV / best-exposed frame
    subfolder: Optional[Path] = None  # path to Bracket_NN subfolder (None if quality failed)
    shake_detected: bool = False  # True = Kamera hat sich zu viel bewegt
    max_shift_px: float = 0.0     # maximale Verschiebung zwischen Frames in Pixeln


@dataclass
class LocalScores:
    sharpness: float = 0.0       # 0–100
    exposure: float = 0.0        # 0–100
    histogram: float = 0.0       # 0–100
    composite: float = 0.0       # weighted composite 0–100
    is_blurry: bool = False
    is_overexposed: bool = False
    is_underexposed: bool = False
    phash: Optional[str] = None
    phash_group: Optional[int] = None  # duplicate group id
    # Kompositions-Scores (pipeline/compose.py)
    rule_of_thirds: float = 0.0
    symmetry: float = 0.0
    symmetry_axis: str = "none"
    horizon_level: float = 0.0
    horizon_tilt_deg: float = 0.0
    leading_lines: float = 0.0
    balance: float = 0.0
    composition_overall: float = 0.0


@dataclass
class AIScores:
    # ── Kern-Scores (0–100) ─────────────────────────────────────
    composition: float = 0.0      # Bildaufbau, Drittel-Regel, Linien
    technical: float = 0.0        # KI-Einschätzung Schärfe/Rauschen/Belichtung
    subject_interest: float = 0.0 # Gibt es ein klares, interessantes Hauptmotiv?
    decisive_moment: float = 0.0  # Wirkt das Bild lebendig/spontan oder gestellt?
    light_mood: float = 0.0       # Lichtqualität + Farbpalette als kombinierter Score
    story_memory: float = 0.0     # Erzählwert + Erinnerungswert kombiniert

    # ── Kategorische Felder ─────────────────────────────────────
    light_quality: str = ""       # golden_hour | blue_hour | soft_overcast |
                                  # harsh_midday | backlight_ok | backlight_problem
    color_palette: str = ""       # warm | cool | harmonious | complementary | monochrome
    weather_mood: str = ""        # dramatic | idyllic | melancholic | clear
    motion_type: str = ""         # intentional | unintentional | none
    mood: str = ""                # Kurzphrase (z.B. "golden hour warmth")

    # ── Gesichter ────────────────────────────────────────────────
    has_faces: bool = False
    expression_score: float = 0.0 # 0 wenn kein Gesicht; sonst Qualität des Ausdrucks
    eyes_open: str = "na"         # yes | no | na

    # ── Qualitäts-Details ────────────────────────────────────────
    noise_level: str = "clean"            # clean | moderate | heavy
    background_distraction: float = 30.0  # 0 = ablenkungsfrei, 100 = sehr ablenkendes BG

    # ── Sonstiges ────────────────────────────────────────────────
    keywords: list = field(default_factory=list)
    notes: str = ""
    is_cover_shot: bool = False    # Repräsentiert die Session am besten

    # ── Final Score ──────────────────────────────────────────────
    final_score: float = 0.0      # Gewichtetes Ergebnis aller Dimensionen


@dataclass
class ExportPaths:
    web: Optional[Path] = None
    social: Optional[Path] = None
    archive: Optional[Path] = None
    print: Optional[Path] = None
    hdr: Optional[Path] = None


@dataclass
class PhotoRecord:
    # Identity
    original_path: Path = field(default_factory=Path)
    archive_path: Optional[Path] = None
    filename: str = ""
    extension: str = ""
    md5: str = ""

    # EXIF metadata
    datetime_original: Optional[str] = None
    camera_make: str = ""
    camera_model: str = ""
    lens: str = ""
    focal_length: str = ""
    aperture: str = ""
    shutter_speed: str = ""
    iso: str = ""
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None

    # Pipeline state
    label: Label = Label.UNKNOWN
    local_scores: LocalScores = field(default_factory=LocalScores)
    ai_scores: Optional[AIScores] = None
    export_paths: ExportPaths = field(default_factory=ExportPaths)

    # Bracketing
    bracket_group_id: Optional[int] = None   # None = not part of a bracket
    bracket_role: BracketRole = BracketRole.NONE
    ev_bias: Optional[float] = None          # ExposureBiasValue from EXIF (e.g. -2.0, 0.0, +2.0)

    # Flags
    is_duplicate: bool = False
    was_skipped: bool = False
    skip_reason: str = ""
    xmp_written: bool = False

    def display_name(self, max_len: int = 25) -> str:
        n = self.filename
        return n if len(n) <= max_len else n[:max_len - 1] + "…"

    @property
    def best_score(self) -> float:
        if self.ai_scores:
            return self.ai_scores.final_score
        return self.local_scores.composite


@dataclass
class SessionReport:
    session_id: str = ""
    event_name: str = ""
    pipeline_version: str = PIPELINE_VERSION
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    source_path: str = ""

    photos: list = field(default_factory=list)  # list[PhotoRecord]

    total_found: int = 0
    total_imported: int = 0
    total_skipped_duplicate: int = 0
    total_skipped_error: int = 0

    top_count: int = 0
    keep_count: int = 0
    reject_count: int = 0

    ai_calls: int = 0
    ai_input_tokens: int = 0
    ai_output_tokens: int = 0
    ai_cost_usd: float = 0.0

    export_count: int = 0
    export_style: str = ""

    # Bracketing stats
    bracket_groups: int = 0        # number of detected bracket sequences
    bracket_frames: int = 0        # total frames that are part of brackets
    bracket_sorted: int = 0        # sequences moved into Bracket_NN subfolders

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at
