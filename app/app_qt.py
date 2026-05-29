#!/usr/bin/env python3
"""Grain — Shoot more. Sort less. (Native macOS App, PyQt6)"""

import atexit
import hashlib
import json
import os
import re
import socket as _socket
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import yaml
from PyQt6.QtCore import (Qt, QProcess, QTimer, QProcessEnvironment, pyqtSignal,
                          QPropertyAnimation, QEasingCurve, QRect, QThread, QSize)
from PyQt6.QtGui import (QFont, QColor, QTextCharFormat, QAction, QFontDatabase,
                         QPixmap, QIcon, QImage)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QTextEdit, QTextBrowser, QProgressBar,
    QFileDialog, QFrame, QSizePolicy, QMenu, QMenuBar, QMessageBox,
    QSplitter, QGraphicsOpacityEffect, QTabWidget, QScrollArea,
    QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox, QGroupBox, QFormLayout,
    QListWidget, QListWidgetItem, QStackedWidget,
)

PROJECT_DIR = Path(__file__).parent
APP_NAME = "Grain"

# Config liegt in Application Support — überlebt App-Updates
SUPPORT_DIR   = Path.home() / "Library" / "Application Support" / APP_NAME
CONFIG_PATH   = SUPPORT_DIR / "config.yaml"
_BUNDLED_CFG  = PROJECT_DIR / "config.yaml"   # Vorlage im Bundle

def _ensure_config():
    """Kopiert config.yaml beim ersten Start in Application Support."""
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists() and _BUNDLED_CFG.exists():
        import shutil
        shutil.copy(_BUNDLED_CFG, CONFIG_PATH)

_ensure_config()
SLOGAN = "Shoot more. Sort less."
VERSION = "1.6.0"
AMBER        = "#FFB400"
AMBER_LIGHT  = "#FFC933"
AMBER_BORDER = "#FFC933"
PURPLE       = "#7C5CBF"

PHOTO_EXTENSIONS = {
    ".rw2", ".jpg", ".jpeg", ".png", ".tif", ".tiff",
    ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".raf",
}

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# ── Single-Instance ───────────────────────────────────────────────────────────

# ── License / Monetization ────────────────────────────────────────────────────
MAX_FREE_PHOTOS = 500   # Free-Tier Limit (noch nicht erzwungen — kostenlose Phase)

# ── SD-Karten Hersteller/Kamera-Datenbank ─────────────────────────────────────
SD_VOLUME_HINTS: set[str] = {
    # Generische / Formatiert
    "NO NAME", "UNTITLED", "SDCARD", "SD CARD", "SD", "MEMORY CARD",
    "REMOVABLE", "USB DRIVE", "CARD", "FLASH",
    # SanDisk
    "SANDISK", "CRUZER", "ULTRA", "EXTREME", "EXTREME PRO",
    # Samsung
    "SAMSUNG", "EVO", "EVO PLUS", "PRO", "PRO PLUS", "PRO ENDURANCE",
    # Lexar
    "LEXAR", "LEXAR MEDIA", "PROFESSIONAL",
    # Kingston
    "KINGSTON", "CANVAS",
    # Transcend
    "TRANSCEND",
    # Sony
    "SONY", "SF-M", "SF-E", "SF-G", "TOUGH",
    # Fujifilm
    "FUJIFILM", "FUJI",
    # Canon (Kameranamen als Volume)
    "EOS_DIGITAL", "EOS", "CANON", "EOS R", "EOS M",
    # Nikon
    "NIKON", "NIKON Z", "COOLPIX", "D800", "D850", "D750", "D700",
    "D610", "D600", "D500", "D300", "Z9", "Z8", "Z7", "Z6", "Z5", "Z50",
    # Sony Alpha
    "ILCE", "ILCA", "DSC", "ZV", "FX",
    # Panasonic Lumix
    "LUMIX", "PANASONIC", "DC-G", "DC-S", "DC-GH",
    # Olympus / OM System
    "OLYMPUS", "OMDS", "OM-D", "E-M",
    # Leica
    "LEICA", "LEICA Q", "LEICA M", "LEICA SL",
    # Hasselblad
    "HASSELBLAD",
    # Phase One
    "PHASE ONE", "IQ",
    # Ricoh / Pentax
    "RICOH", "PENTAX", "GR",
    # GoPro
    "GOPRO", "GP", "HERO",
    # DJI Drohnen
    "DJI", "DJIM", "MINI", "AIR", "AVATA", "OSMO",
    # Insta360
    "INSTA360",
    # Sigma
    "SIGMA",
    # Nikon (weitere)
    "CF", "XQD",
}


def is_pro() -> bool:
    """True wenn Grain Pro Lizenz aktiv.
    Aktuell immer True (kostenlose Phase).
    Später: Lizenzschlüssel gegen Server oder kryptografisch prüfen.
    """
    cfg = load_config()
    key = cfg.get("license", {}).get("key", "").strip()
    if key:
        # TODO: kryptografische Validierung einbauen wenn Monetarisierung startet
        return True
    return True   # kostenlose Phase: alle Features freigeschaltet


def _license_key() -> str:
    cfg = load_config()
    return cfg.get("license", {}).get("key", "").strip()


def load_config() -> dict:
    """Load config.yaml or return defaults."""
    try:
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        pass
    return {}


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _inline(text: str) -> str:
    """Apply inline markdown formatting (bold, italic, code, backtick) to a string."""
    import re as _re, html as _html
    s = _html.escape(text)
    s = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
    s = _re.sub(r'\*(.+?)\*',     r'<i>\1</i>', s)
    s = _re.sub(r'`(.+?)`',       r'<code>\1</code>', s)
    return s


def _is_installer_volume(vol: Path) -> bool:
    """True wenn das Volume wahrscheinlich ein App-Installer-DMG ist (keine SD-Karte)."""
    try:
        top_entries = list(vol.iterdir())
    except (PermissionError, OSError):
        return False
    # Wenn das Root nur .app-Bundles, Symlinks auf /Applications und Textdateien enthält
    # → Installer-DMG, keine echte Kamera/SD-Karte
    has_app = any(e.name.endswith(".app") for e in top_entries)
    has_apps_link = any(
        e.is_symlink() and "Applications" in e.name
        for e in top_entries
    )
    if has_app and has_apps_link:
        return True
    # Wenn ausschließlich .app Bundles (kein DCIM, keine echten Fotos auf Root-Ebene)
    non_app = [e for e in top_entries if not e.name.endswith(".app")
               and not e.name.startswith(".") and e.name not in ("Applications", "Lies mich.txt")]
    if has_app and len(non_app) == 0:
        return True
    return False


def find_sd_cards() -> list[dict]:
    """
    Gibt eine sortierte Liste aller erkannten SD-Karten/Kameras zurück.
    Jedes Element: {"path": str, "name": str, "hint": str}

    Erkennung basiert auf:
    1. Bekannte Hersteller-/Kameravolumenamen
    2. DCIM-Ordner (Standard für Kameras nach DCF-Spezifikation)
    3. Fotodateien NUR auf Root-Ebene (verhindert False Positives durch eingebettete Pakete)
    """
    results = []
    try:
        volumes = Path("/Volumes")
        if not volumes.exists():
            return []

        skip_names = {
            "Macintosh HD", "Macintosh HD - Data",
            ".timemachinebackup", "com.apple.TimeMachine.localsnapshots",
            "Time Machine", "Time Machine Backups",
            APP_NAME,                           # Grain selbst nicht erkennen
            f"{APP_NAME} {VERSION}",            # "Grain 1.6.0" DMG-Volume
        }

        for vol in sorted(volumes.iterdir()):
            if not vol.is_dir() or vol.name.startswith("."):
                continue
            if vol.name in skip_names:
                continue
            # Vermeide System-Volumes / interne Festplatten
            if (vol / "System").exists() and (vol / "Library").exists():
                continue
            # Vermeide App-Installer-DMGs (haben .app + Applications-Symlink)
            if _is_installer_volume(vol):
                continue

            vol_upper = vol.name.upper()

            # Heuristik 1: Bekannter Herstellername
            name_match = any(hint in vol_upper for hint in SD_VOLUME_HINTS)

            # Heuristik 2: DCIM-Ordner vorhanden (DCF-Standard für Kameras)
            has_dcim = (vol / "DCIM").is_dir()

            # Heuristik 3: Fotos NUR auf Root-Ebene oder in DCIM/ prüfen
            # (KEIN rglob! Verhindert False Positives durch Pakete wie PIL, rawpy etc.)
            has_photos = False
            if not has_dcim and not name_match:
                # Nur direkte Kinder und DCIM-Unterordner scannen
                scan_dirs = [vol]
                try:
                    for scan_dir in scan_dirs:
                        for entry in scan_dir.iterdir():
                            if entry.is_file() and entry.suffix.lower() in PHOTO_EXTENSIONS:
                                has_photos = True
                                break
                        if has_photos:
                            break
                except (PermissionError, OSError):
                    pass

            if name_match or has_dcim or has_photos:
                if has_dcim:
                    hint = "Camera"
                elif name_match:
                    hint = "SD Card"
                else:
                    hint = "External Drive"

                results.append({
                    "path": str(vol),
                    "name": vol.name,
                    "hint": hint,
                })
    except Exception:
        pass

    return results


def find_sd_card() -> str | None:
    """Return the first detected SD card path (backwards-compatibility wrapper)."""
    cards = find_sd_cards()
    return cards[0]["path"] if cards else None


# Modell-Zeitschätzung pro Bild (Sekunden, typische M1/M2-Werte)
MODEL_SECONDS_PER_IMAGE: dict[str, float] = {
    "moondream":       5.0,
    "llava":          12.0,
    "llava:13b":      22.0,
    "qwen2.5-vl":     20.0,
    "llama3.2-vision": 25.0,
    "llama3.2-vision:11b": 35.0,
}
BASELINE_SECONDS_PER_IMAGE = 0.8   # Ingestion + Culling ohne AI


def _find_python() -> str:
    """Findet das beste Python (arm64 bevorzugt)."""
    candidates = [
        str(PROJECT_DIR / ".venv-arm64" / "bin" / "python3"),
        str(PROJECT_DIR / ".venv" / "bin" / "python3"),
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return sys.executable


class _ThumbLoader(QThread):
    """Loads gallery thumbnails off the GUI thread.

    Emits (item_index, jpeg_bytes) for each successfully decoded photo. QPixmap
    cannot be created off the main thread, so we hand back raw JPEG bytes and let
    the GUI slot build the pixmap. Decoding RAW previews is the slow part and it
    happens here, keeping the UI responsive on 400+ photo sessions.
    """
    thumb_ready = pyqtSignal(int, bytes)

    def __init__(self, items: list[tuple[int, str]], max_px: int = 300):
        super().__init__()
        self._items = items
        self._max_px = max_px
        self._stop = False

    def run(self):
        try:
            from pipeline.thumbs import load_thumbnail_bytes
        except Exception:
            return
        from pathlib import Path as _P
        for idx, path_str in self._items:
            if self._stop:
                return
            data = load_thumbnail_bytes(_P(path_str), max_px=self._max_px)
            if data:
                self.thumb_ready.emit(idx, data)

    def stop(self):
        self._stop = True


class MainWindow(QMainWindow):
    # Thread-sicheres Signal für Schätzungs-Updates aus Background-Thread
    _estimate_ready = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self._process: QProcess | None = None
        self._caffeinate_proc: subprocess.Popen | None = None
        self._last_archive: str = ""
        self._last_session_dir: str = ""
        self._thumb_loader: _ThumbLoader | None = None
        self._running = False
        self._estimate_timer = QTimer()
        self._estimate_timer.setSingleShot(True)
        self._estimate_timer.setInterval(400)   # 400 ms debounce
        self._estimate_timer.timeout.connect(self._update_estimate)

        # ── Animations ────────────────────────────────────────────────────────
        self._glow_timer = QTimer()
        self._glow_timer.setInterval(700)
        self._glow_timer.timeout.connect(self._pulse_progress_bar)
        self._glow_phase = 0
        self._pipeline_start_time: float | None = None
        self._result_banner: QFrame | None = None
        self._result_stats = {"total": 0, "top": 0, "kept": 0, "rejected": 0}
        self._toast_anim = None
        self._toast_hide_anim = None

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(680, 580)
        self.resize(720, 680)

        self._setup_ui()
        self._setup_menu()
        # Signal verbinden NACH _setup_ui (estimate_label existiert erst dann)
        self._estimate_ready.connect(self.estimate_label.setText)

        # SD detection
        QTimer.singleShot(100, self._auto_detect_source)

    # ──────────────────────────────────────────────────────────────────────────
    # UI Setup
    # ──────────────────────────────────────────────────────────────────────────

    # ── Common widget styles ───────────────────────────────────────────────────
    _BTN_SECONDARY = """
        QPushButton { background: #1A1714; border: 1px solid #252017; border-radius: 7px;
                      color: #8F7F66; font-size: 12px; padding: 6px 14px; }
        QPushButton:hover { border-color: #FFB400; color: #F5F0E8; }
        QPushButton:disabled { color: #5A4D3E; border-color: #1C1916; }
    """
    _BTN_PICK = """
        QPushButton { background: #1A1714; border: 1px solid #252017; border-radius: 7px;
                      color: #8F7F66; font-size: 12px; padding: 0 12px; }
        QPushButton:hover { border-color: #FFB400; color: #F5F0E8; }
    """
    _COMBO_STYLE = """
        QComboBox { background: #141210; color: #F5F0E8; border: 1px solid #252017;
                    border-radius: 6px; padding: 5px 10px; font-size: 12px; min-height: 28px; }
        QComboBox:hover { border-color: #FFB400; }
        QComboBox::drop-down { border: none; width: 20px; }
        QComboBox QAbstractItemView { background: #1A1714; color: #F5F0E8;
                                      selection-background-color: #FFB400;
                                      selection-color: #141210; border: 1px solid #252017; }
    """
    _CHECK_STYLE = """
        QCheckBox { color: #C4B49A; font-size: 12px; spacing: 6px; }
        QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #252017;
                               border-radius: 4px; background: #141210; }
        QCheckBox::indicator:checked { background: #FFB400; border-color: #FFB400;
                                       image: none; }
        QCheckBox::indicator:hover { border-color: #FFB400; }
    """
    _GROUP_STYLE = """
        QGroupBox { color: #8F7F66; font-size: 11px; font-weight: 700;
                    letter-spacing: 0.06em; text-transform: uppercase;
                    border: 1px solid #1C1916; border-radius: 8px;
                    margin-top: 10px; padding-top: 10px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px;
                           padding: 0 6px; color: #5A4D3E; }
    """
    _SPIN_STYLE = """
        QDoubleSpinBox, QSpinBox {
            background: #141210; color: #F5F0E8; border: 1px solid #252017;
            border-radius: 6px; padding: 4px 8px; font-size: 12px; }
        QDoubleSpinBox:hover, QSpinBox:hover { border-color: #FFB400; }
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
        QSpinBox::up-button,       QSpinBox::down-button {
            background: #1C1916; border: none; width: 16px; }
    """
    _SEG_STYLE = """
        QPushButton { background: transparent; color: #8F7F66; border: none;
                      font-size: 12px; font-weight: 600; padding: 5px 10px;
                      border-bottom: 2px solid transparent; }
        QPushButton:hover { color: #C4B49A; }
        QPushButton:checked { color: #F5F0E8; border-bottom: 2px solid #FFB400; }
    """
    _CHIP_STYLE = """
        QPushButton { background: #1A1714; color: #8F7F66; border: 1px solid #252017;
                      border-radius: 100px; font-size: 11px; padding: 4px 12px; }
        QPushButton:hover { border-color: #FFB400; color: #C4B49A; }
        QPushButton:checked { background: rgba(255,180,0,0.12); color: #FFB400;
                              border-color: rgba(255,180,0,0.4); }
    """
    _GALLERY_STYLE = """
        QListWidget { background: #100E0C; border: 1px solid #1C1916; border-radius: 10px;
                      padding: 6px; color: #C4B49A; font-size: 10px;
                      outline: none; }
        QListWidget::item { color: #8F7F66; border-radius: 8px; padding: 4px; }
        QListWidget::item:selected { background: rgba(255,180,0,0.12);
                                     color: #FFB400; }
        QListWidget::item:hover { background: #1A1714; }
    """

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(0)

        # ── Amber accent stripe ───────────────────────────────────────────────
        accent_stripe = QFrame()
        accent_stripe.setFixedHeight(3)
        accent_stripe.setStyleSheet(
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            "stop:0 #FFB400, stop:0.6 #FFB400, stop:1 #7C5CBF); border: none;")
        root.addWidget(accent_stripe)
        root.addSpacing(14)

        # ── Header (outside tabs — persistent branding) ───────────────────────
        header_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        header_label = QLabel(APP_NAME)
        header_label.setStyleSheet(
            "color: #F5F0E8; font-family: 'Fraunces', Georgia, serif; "
            "font-size: 24px; font-weight: 300; letter-spacing: -0.5px;")
        slogan_label = QLabel(SLOGAN)
        slogan_label.setStyleSheet(
            "color: #8F7F66; font-family: 'IBM Plex Mono', monospace; font-size: 11px;"
            "letter-spacing: 0.06em;")
        title_col.addWidget(header_label)
        title_col.addWidget(slogan_label)
        version_label = QLabel(f"v{VERSION}")
        version_label.setStyleSheet("color: #5A4D3E; font-size: 11px;")
        version_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_row.addLayout(title_col)
        header_row.addStretch()
        header_row.addWidget(version_label)
        root.addLayout(header_row)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background: #1C1916; border: none; max-height: 1px;")
        root.addWidget(line)
        root.addSpacing(10)

        # ── Tab Widget ────────────────────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: none; background: transparent; }
            QTabBar::tab {
                background: transparent; color: #5A4D3E;
                padding: 7px 18px; font-size: 12px; font-weight: 600;
                border-bottom: 2px solid transparent; margin-right: 2px;
            }
            QTabBar::tab:selected { color: #F5F0E8; border-bottom: 2px solid #FFB400; }
            QTabBar::tab:hover:!selected { color: #8F7F66; }
            QTabBar { background: transparent; }
        """)
        self.tabs.addTab(self._build_pipeline_tab(), "  Pipeline  ")
        self.tabs.addTab(self._build_results_tab(),  "  Ergebnisse  ")
        self.tabs.addTab(self._build_settings_tab(), "  Einstellungen  ")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # Tab: Pipeline
    # ──────────────────────────────────────────────────────────────────────────

    def _build_pipeline_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 12, 4, 8)
        lay.setSpacing(0)

        # ── Source ───────────────────────────────────────────────────────────
        lay.addWidget(self._field_label("Source (SD Card / Ordner)"))
        src_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("/Volumes/NO NAME")
        self.source_edit.textChanged.connect(self._update_start_button)
        self.source_edit.textChanged.connect(self._schedule_estimate)
        src_row.addWidget(self.source_edit)
        src_btn = QPushButton("Wählen")
        src_btn.setFixedWidth(96)
        src_btn.setStyleSheet(self._BTN_PICK)
        src_btn.clicked.connect(self._pick_source)
        src_row.addWidget(src_btn)
        lay.addLayout(src_row)

        self.sd_cards_row = QHBoxLayout()
        self.sd_cards_row.setSpacing(6)
        self.sd_label_prefix = QLabel("Erkannt:")
        self.sd_label_prefix.setStyleSheet("color: #8F7F66; font-size: 11px;")
        self.sd_label_prefix.hide()
        self.sd_cards_row.addWidget(self.sd_label_prefix)
        self.sd_cards_row.addStretch()
        lay.addLayout(self.sd_cards_row)
        lay.addSpacing(2)

        self.estimate_label = QLabel("")
        self.estimate_label.setStyleSheet("color: #8F7F66; font-size: 12px; background: transparent;")
        lay.addWidget(self.estimate_label)
        lay.addSpacing(8)

        # ── Archive ───────────────────────────────────────────────────────────
        lay.addWidget(self._field_label("Archiv"))
        arc_row = QHBoxLayout()
        self.archive_edit = QLineEdit()
        default_archive = self.config.get("paths", {}).get("archive_root", "~/Pictures/Archive")
        self.archive_edit.setText(os.path.expanduser(default_archive))
        arc_row.addWidget(self.archive_edit)
        arc_btn = QPushButton("Wählen")
        arc_btn.setFixedWidth(96)
        arc_btn.setStyleSheet(self._BTN_PICK)
        arc_btn.clicked.connect(self._pick_archive)
        arc_row.addWidget(arc_btn)
        lay.addLayout(arc_row)
        lay.addSpacing(8)

        # ── Event Name ────────────────────────────────────────────────────────
        lay.addWidget(self._field_label("Event-Name (optional)"))
        self.event_edit = QLineEdit()
        self.event_edit.setText(datetime.now().strftime("%d.%m.%Y"))
        lay.addWidget(self.event_edit)
        lay.addSpacing(12)

        # ── Options Card ──────────────────────────────────────────────────────
        opt_card = QFrame()
        opt_card.setStyleSheet("""
            QFrame { background: #141210; border: 1px solid #1C1916; border-radius: 8px; }
            QLabel { background: transparent; }
        """)
        opt_lay = QVBoxLayout(opt_card)
        opt_lay.setContentsMargins(14, 10, 14, 10)
        opt_lay.setSpacing(8)

        opt_title = QLabel("OPTIONEN")
        opt_title.setStyleSheet(
            "color: #5A4D3E; font-size: 10px; font-weight: 700; letter-spacing: 0.12em;")
        opt_lay.addWidget(opt_title)

        # Row 1 — Belichtungsreihen
        r1 = QHBoxLayout()
        r1.setSpacing(10)
        r1.addWidget(QLabel(
            "<span style='color:#8F7F66;font-size:12px;'>Belichtungsreihen:</span>"))
        self.bracket_combo = QComboBox()
        self.bracket_combo.setStyleSheet(self._COMBO_STYLE)
        self.bracket_combo.setFixedWidth(210)
        for label, data in [
            ("Aus",               "off"),
            ("Auto-Erkennung",    "auto"),
            ("3 Fotos / Sequenz", "3"),
            ("5 Fotos / Sequenz", "5"),
            ("7 Fotos / Sequenz", "7"),
        ]:
            self.bracket_combo.addItem(label, data)
        # Set from config
        b_cfg = self.config.get("bracketing", {})
        if not b_cfg.get("enabled", True):
            self.bracket_combo.setCurrentIndex(0)
        else:
            fps = int(b_cfg.get("frames_per_sequence", 0))
            idx = {0: 1, 3: 2, 5: 3, 7: 4}.get(fps, 1)
            self.bracket_combo.setCurrentIndex(idx)
        self.bracket_combo.currentIndexChanged.connect(self._on_bracket_changed)
        r1.addWidget(self.bracket_combo)
        r1.addStretch()
        opt_lay.addLayout(r1)

        # Row 2 — Checkboxes
        r2 = QHBoxLayout()
        r2.setSpacing(18)
        self.ai_check = QCheckBox("AI-Analyse")
        self.ai_check.setStyleSheet(self._CHECK_STYLE)
        self.ai_check.setChecked(self.config.get("ai", {}).get("enabled", True))
        self.ai_check.setToolTip(
            "AI bewertet Motive, Licht und Story.\n"
            "Deaktivieren = schneller, aber kein Fortschritts-Tracking.")
        r2.addWidget(self.ai_check)

        self.jpeg_check = QCheckBox("JPEG ablehnen")
        self.jpeg_check.setStyleSheet(self._CHECK_STYLE)
        self.jpeg_check.setChecked(False)
        self.jpeg_check.setToolTip(
            "Kamera-JPEGs (Begleit-Dateien zu RAW) automatisch als Reject markieren.")
        r2.addWidget(self.jpeg_check)

        self.video_check = QCheckBox("Videos verarbeiten")
        self.video_check.setStyleSheet(self._CHECK_STYLE)
        self.video_check.setChecked(self.config.get("videos", {}).get("enabled", False))
        self.video_check.setToolTip(
            "Videos von der SD-Karte importieren und bewerten.\n"
            "Grain prüft Auflösung, Länge und Audioqualität.\n"
            "Ergebnis erscheint im Ergebnisse-Tab.")
        r2.addWidget(self.video_check)

        r2.addStretch()
        opt_lay.addLayout(r2)
        lay.addWidget(opt_card)
        lay.addSpacing(14)

        # ── Start / Stop buttons ──────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setFixedHeight(44)
        self.start_btn.setMinimumWidth(200)
        self.start_btn.setStyleSheet("""
            QPushButton { background: #FFB400; color: #141210; border: none; border-radius: 9px;
                          font-size: 15px; font-weight: 800; padding: 0 24px; }
            QPushButton:hover { background: #FFC933; }
            QPushButton:disabled { background: #252017; color: #5A4D3E; }
        """)
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._start_pipeline)

        self.stop_btn = QPushButton("Stopp")
        self.stop_btn.setFixedHeight(44)
        self.stop_btn.setMinimumWidth(90)
        self.stop_btn.setStyleSheet("""
            QPushButton { background: transparent; color: #8F7F66; border: 1px solid #252017;
                          border-radius: 9px; font-size: 13px; padding: 0 18px; }
            QPushButton:hover { border-color: #FFB400; color: #F5F0E8; }
            QPushButton:disabled { color: #5A4D3E; border-color: #1C1916; }
        """)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_pipeline)

        btn_row.addWidget(self.start_btn)
        btn_row.addSpacing(10)
        btn_row.addWidget(self.stop_btn)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        lay.addSpacing(12)

        # ── Progress + Log ────────────────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        prog_w = QWidget()
        prog_lay = QVBoxLayout(prog_w)
        prog_lay.setContentsMargins(0, 0, 0, 0)
        prog_lay.setSpacing(4)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar { background: #1C1916; border: none; border-radius: 3px; }
            QProgressBar::chunk { background: #FFB400; border-radius: 3px; }
        """)
        prog_lay.addWidget(self.progress_bar)
        self.status_label = QLabel("Bereit")
        self.status_label.setStyleSheet("color: #5A4D3E; font-size: 12px; background: transparent;")
        prog_lay.addWidget(self.status_label)
        splitter.addWidget(prog_w)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        mono_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono_font.setPointSize(12)
        self.log_edit.setFont(mono_font)
        self.log_edit.setMinimumHeight(100)
        splitter.addWidget(self.log_edit)
        splitter.setSizes([50, 180])
        lay.addWidget(splitter, 1)
        lay.addSpacing(8)

        # ── Bottom buttons ────────────────────────────────────────────────────
        bot = QHBoxLayout()
        self.progress_btn = QPushButton("Verlauf")
        self.progress_btn.setStyleSheet(self._BTN_SECONDARY)
        self.progress_btn.clicked.connect(self._show_progress)
        bot.addWidget(self.progress_btn)

        self.refresh_sd_btn = QPushButton("SD Karten")
        self.refresh_sd_btn.setStyleSheet(self._BTN_SECONDARY)
        self.refresh_sd_btn.clicked.connect(self._auto_detect_source)
        bot.addWidget(self.refresh_sd_btn)

        bot.addStretch()
        lay.addLayout(bot)
        return w

    # ──────────────────────────────────────────────────────────────────────────
    # Tab: Ergebnisse
    # ──────────────────────────────────────────────────────────────────────────

    def _build_results_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 12, 4, 8)
        lay.setSpacing(8)

        # ── Stats row (hidden until first run) ────────────────────────────────
        self.result_stats_frame = QFrame()
        self.result_stats_frame.setStyleSheet("""
            QFrame { background: #141210; border: 1px solid #1C1916; border-radius: 8px; }
            QLabel { background: transparent; }
        """)
        stats_lay = QHBoxLayout(self.result_stats_frame)
        stats_lay.setContentsMargins(16, 10, 16, 10)
        stats_lay.setSpacing(0)

        def _stat(val: str, lbl: str, color: str = "#F5F0E8") -> QVBoxLayout:
            c = QVBoxLayout()
            c.setSpacing(2)
            v = QLabel(val)
            v.setObjectName("stat_val")
            v.setStyleSheet(
                f"color:{color};font-family:'Fraunces',Georgia,serif;"
                f"font-size:26px;font-weight:300;"
            )
            v.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l = QLabel(lbl)
            l.setStyleSheet(
                "color:#8F7F66;font-family:'IBM Plex Mono','Courier New',monospace;"
                "font-size:9px;letter-spacing:0.14em;text-transform:uppercase;"
            )
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            c.addWidget(v)
            c.addWidget(l)
            return c

        self._rs_total = _stat("—", "FOTOS")
        self._rs_top   = _stat("—", "TOP", "#FFB400")
        self._rs_keep  = _stat("—", "KEEP", "#6EE7B7")
        self._rs_rej   = _stat("—", "REJECT", "#FF6B63")
        self._rs_time  = _stat("—", "DAUER")
        for col in (self._rs_total, self._rs_top, self._rs_keep, self._rs_rej, self._rs_time):
            stats_lay.addLayout(col)
            stats_lay.addStretch()
        stats_lay.takeAt(stats_lay.count() - 1)  # remove last stretch

        self.result_stats_frame.hide()
        lay.addWidget(self.result_stats_frame)

        # ── Video stats row (hidden until videos processed) ───────────────────
        self.video_stats_frame = QFrame()
        self.video_stats_frame.setStyleSheet("""
            QFrame { background: #141210; border: 1px solid #1C1916; border-radius: 8px; }
            QLabel { background: transparent; }
        """)
        vstats_lay = QHBoxLayout(self.video_stats_frame)
        vstats_lay.setContentsMargins(16, 8, 16, 8)
        vstats_lay.setSpacing(0)

        vid_icon = QLabel("🎬  Videos:")
        vid_icon.setStyleSheet("color:#8F7F66;font-size:12px;font-weight:600;")
        vstats_lay.addWidget(vid_icon)
        vstats_lay.addSpacing(12)

        self._vid_keep_lbl = QLabel("—")
        self._vid_keep_lbl.setStyleSheet("color:#6EE7B7;font-size:14px;font-weight:800;")
        vstats_lay.addWidget(self._vid_keep_lbl)
        k_sub = QLabel(" KEEP/REVIEW")
        k_sub.setStyleSheet("color:#5A4D3E;font-size:11px;")
        vstats_lay.addWidget(k_sub)

        vstats_lay.addSpacing(20)

        self._vid_rej_lbl = QLabel("—")
        self._vid_rej_lbl.setStyleSheet("color:#FF6B63;font-size:14px;font-weight:800;")
        vstats_lay.addWidget(self._vid_rej_lbl)
        r_sub = QLabel(" REJECT")
        r_sub.setStyleSheet("color:#5A4D3E;font-size:11px;")
        vstats_lay.addWidget(r_sub)

        vstats_lay.addStretch()

        vid_hint = QLabel("→ session_dir/Videos/")
        vid_hint.setStyleSheet("color:#252017;font-size:11px;font-style:italic;")
        vstats_lay.addWidget(vid_hint)

        self.video_stats_frame.hide()
        lay.addWidget(self.video_stats_frame)

        # ── Session Review Browser ────────────────────────────────────────────
        self.review_browser = QTextBrowser()
        self.review_browser.setOpenExternalLinks(False)
        self.review_browser.setStyleSheet("""
            QTextBrowser {
                background: #141210; color: #C4B49A;
                border: 1px solid #1C1916; border-radius: 8px;
                font-size: 13px; padding: 12px;
                selection-background-color: #FFB400; selection-color: #141210;
            }
        """)
        # Warm-themed document style injected via HTML
        self.review_browser.document().setDefaultStyleSheet("""
            body   { font-family: -apple-system, sans-serif; color: #C4B49A; line-height: 1.65; }
            h1     { color: #F5F0E8; font-size: 18px; border-bottom: 1px solid #1C1916;
                     padding-bottom: 6px; margin-top: 0; }
            h2     { color: #FFB400; font-size: 14px; margin-top: 20px; margin-bottom: 4px; }
            table  { border-collapse: collapse; width: 100%; margin: 8px 0; }
            td, th { border: 1px solid #1C1916; padding: 5px 10px; }
            th     { background: #1A1714; color: #8F7F66; font-size: 11px; }
            tr:nth-child(even) { background: #100E0C; }
            code   { background: #1A1714; color: #FFB400; padding: 1px 5px;
                     border-radius: 3px; font-size: 12px; }
            blockquote { border-left: 3px solid #FFB400; margin: 6px 0;
                         padding-left: 12px; color: #8F7F66; font-style: italic; }
            hr     { border: none; border-top: 1px solid #1C1916; margin: 14px 0; }
            b, strong { color: #F5F0E8; }
            li     { margin-bottom: 6px; }
        """)
        self.review_browser.setPlaceholderText(
            "Hier erscheint dein Session Review nach dem nächsten Durchlauf.\n\n"
            "Grain analysiert Schärfe, Belichtung, Komposition und — mit aktivierter "
            "AI — auch Motiv, Licht und Story. Du bekommst konkrete Tipps für deine "
            "nächste Session."
        )
        # ── View switch (Galerie / Review) + filter chips ─────────────────────
        seg_row = QHBoxLayout()
        seg_row.setSpacing(6)
        self.seg_gallery_btn = QPushButton("Galerie")
        self.seg_review_btn  = QPushButton("Review")
        for b in (self.seg_gallery_btn, self.seg_review_btn):
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(self._SEG_STYLE)
        self.seg_gallery_btn.setChecked(True)
        self.seg_gallery_btn.clicked.connect(lambda: self._switch_results_view(0))
        self.seg_review_btn.clicked.connect(lambda: self._switch_results_view(1))
        seg_row.addWidget(self.seg_gallery_btn)
        seg_row.addWidget(self.seg_review_btn)
        seg_row.addSpacing(18)

        # Filter chips (apply to the gallery)
        self._filter_btns: dict[str, QPushButton] = {}
        self._gallery_filter = "ALL"
        for key, text in [("ALL", "Alle"), ("TOP", "⭐ TOP"),
                          ("KEEP", "✅ KEEP"), ("REJECT", "🗑 REJECT")]:
            chip = QPushButton(text)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(self._CHIP_STYLE)
            chip.clicked.connect(lambda _checked, k=key: self._set_gallery_filter(k))
            self._filter_btns[key] = chip
            seg_row.addWidget(chip)
        self._filter_btns["ALL"].setChecked(True)
        seg_row.addStretch()
        lay.addLayout(seg_row)

        # ── Stacked content: gallery (0) / review (1) ─────────────────────────
        self.results_stack = QStackedWidget()

        self.gallery = QListWidget()
        self.gallery.setViewMode(QListWidget.ViewMode.IconMode)
        self.gallery.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.gallery.setMovement(QListWidget.Movement.Static)
        self.gallery.setIconSize(QSize(160, 160))
        self.gallery.setGridSize(QSize(184, 200))
        self.gallery.setSpacing(6)
        self.gallery.setWordWrap(True)
        self.gallery.setStyleSheet(self._GALLERY_STYLE)
        self.gallery.itemDoubleClicked.connect(self._open_gallery_item)
        self._gallery_items: list[dict] = []   # metadata per row (label, path, score)

        self.results_stack.addWidget(self.gallery)          # index 0
        self.results_stack.addWidget(self.review_browser)   # index 1
        lay.addWidget(self.results_stack, 1)

        # ── Bottom: open results ──────────────────────────────────────────────
        bot = QHBoxLayout()
        self.open_btn = QPushButton("📁  Fotos öffnen")
        self.open_btn.setStyleSheet(self._BTN_SECONDARY)
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open_result)
        bot.addWidget(self.open_btn)
        bot.addStretch()
        lay.addLayout(bot)
        return w

    # ──────────────────────────────────────────────────────────────────────────
    # Tab: Einstellungen
    # ──────────────────────────────────────────────────────────────────────────

    def _build_settings_tab(self) -> QWidget:
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(4, 8, 4, 8)
        outer_lay.setSpacing(0)

        # Scrollable content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(content)
        lay.setContentsMargins(4, 4, 4, 16)
        lay.setSpacing(14)
        scroll.setWidget(content)
        outer_lay.addWidget(scroll, 1)

        cfg = self.config

        # ── 📂 Archiv ─────────────────────────────────────────────────────────
        grp_arch = QGroupBox("📂  Archiv")
        grp_arch.setStyleSheet(self._GROUP_STYLE)
        fl_arch = QFormLayout(grp_arch)
        fl_arch.setSpacing(8)
        fl_arch.setContentsMargins(12, 16, 12, 12)

        self.cfg_folder_combo = QComboBox()
        self.cfg_folder_combo.setStyleSheet(self._COMBO_STYLE)
        self.cfg_folder_combo.addItem("Nach Datum sortiert  (YYYY / MM / YYYY-MM-DD_Event)", "date")
        self.cfg_folder_combo.addItem("Alles in einem Ordner  (event_name/)", "flat")
        flat = cfg.get("paths", {}).get("flat_archive", False)
        self.cfg_folder_combo.setCurrentIndex(1 if flat else 0)
        fl_arch.addRow("Ordner-Struktur:", self.cfg_folder_combo)

        self.cfg_archive_edit = QLineEdit()
        self.cfg_archive_edit.setText(
            os.path.expanduser(cfg.get("paths", {}).get("archive_root", "~/Pictures/Archive")))
        fl_arch.addRow("Archiv-Pfad:", self.cfg_archive_edit)
        lay.addWidget(grp_arch)

        # Culling spinboxes — created here, placed inside Erweitert below
        cull_cfg = cfg.get("culling", {})
        def _dspin(val, lo=0.0, hi=1.0, step=0.05, decimals=2):
            s = QDoubleSpinBox()
            s.setStyleSheet(self._SPIN_STYLE)
            s.setRange(lo, hi)
            s.setSingleStep(step)
            s.setDecimals(decimals)
            s.setValue(float(val))
            return s

        self.cfg_sharp_w  = _dspin(cull_cfg.get("sharpness_weight",    0.40))
        self.cfg_exp_w    = _dspin(cull_cfg.get("exposure_weight",     0.25))
        self.cfg_hist_w   = _dspin(cull_cfg.get("histogram_weight",    0.15))
        self.cfg_comp_w   = _dspin(cull_cfg.get("composition_weight",  0.20))
        self.cfg_top_pct  = _dspin(cull_cfg.get("top_percentile",      20),    5,  50, 1, 0)
        self.cfg_keep_thr = _dspin(cull_cfg.get("keep_threshold",      35),   10,  80, 1, 0)

        # ── 🔲 Belichtungsreihen ──────────────────────────────────────────────
        grp_brk = QGroupBox("🔲  Belichtungsreihen")
        grp_brk.setStyleSheet(self._GROUP_STYLE)
        fl_brk = QFormLayout(grp_brk)
        fl_brk.setSpacing(8)
        fl_brk.setContentsMargins(12, 16, 12, 12)

        b_cfg2 = cfg.get("bracketing", {})
        self.cfg_brk_enabled = QCheckBox("Bracket-Erkennung aktiviert")
        self.cfg_brk_enabled.setStyleSheet(self._CHECK_STYLE)
        self.cfg_brk_enabled.setChecked(b_cfg2.get("enabled", True))
        fl_brk.addRow(self.cfg_brk_enabled)

        self.cfg_brk_fps = QComboBox()
        self.cfg_brk_fps.setStyleSheet(self._COMBO_STYLE)
        for lbl, d in [("Auto — Shutter-Ratio",  0), ("3 Fotos", 3), ("5 Fotos", 5), ("7 Fotos", 7)]:
            self.cfg_brk_fps.addItem(lbl, d)
        fps2 = int(b_cfg2.get("frames_per_sequence", 0))
        self.cfg_brk_fps.setCurrentIndex({0: 0, 3: 1, 5: 2, 7: 3}.get(fps2, 0))
        fl_brk.addRow("Fotos pro Sequenz:", self.cfg_brk_fps)

        note_brk = QLabel("Auch direkt im Pipeline-Tab einstellbar")
        note_brk.setStyleSheet("color:#5A4D3E;font-size:11px;")
        fl_brk.addRow("", note_brk)
        lay.addWidget(grp_brk)

        # ── 🤖 AI-Analyse ─────────────────────────────────────────────────────
        grp_ai = QGroupBox("🤖  AI-Analyse")
        grp_ai.setStyleSheet(self._GROUP_STYLE)
        fl_ai = QFormLayout(grp_ai)
        fl_ai.setSpacing(8)
        fl_ai.setContentsMargins(12, 16, 12, 12)

        ai_cfg2 = cfg.get("ai", {})
        self.cfg_ai_enabled = QCheckBox("AI-Analyse aktiviert")
        self.cfg_ai_enabled.setStyleSheet(self._CHECK_STYLE)
        self.cfg_ai_enabled.setChecked(ai_cfg2.get("enabled", True))
        fl_ai.addRow(self.cfg_ai_enabled)

        self.cfg_ai_provider = QComboBox()
        self.cfg_ai_provider.setStyleSheet(self._COMBO_STYLE)
        self.cfg_ai_provider.addItem("Lokal (Ollama) — kostenlos, privat", "local")
        self.cfg_ai_provider.addItem("Claude API — höhere Qualität", "claude")
        self.cfg_ai_provider.setCurrentIndex(0 if ai_cfg2.get("provider", "local") == "local" else 1)
        fl_ai.addRow("Provider:", self.cfg_ai_provider)

        self.cfg_ai_model = QComboBox()
        self.cfg_ai_model.setStyleSheet(self._COMBO_STYLE)
        for lbl, d in [
            ("moondream  (schnell, ~5s/Foto)", "moondream"),
            ("llava  (besser, ~12s/Foto)", "llava"),
            ("qwen2.5-vl  (beste Qualität, ~20s/Foto)", "qwen2.5-vl"),
        ]:
            self.cfg_ai_model.addItem(lbl, d)
        cur_model = ai_cfg2.get("local_model", "moondream")
        for i in range(self.cfg_ai_model.count()):
            if self.cfg_ai_model.itemData(i) == cur_model:
                self.cfg_ai_model.setCurrentIndex(i)
        fl_ai.addRow("Lokales Modell:", self.cfg_ai_model)

        note_ai = QLabel(
            "AI erkennt Motive, Licht, Stimmung und gibt dir Tipps.\n"
            "Ohne AI: nur technische Scores (Schärfe, Belichtung).")
        note_ai.setStyleSheet("color:#5A4D3E;font-size:11px;")
        note_ai.setWordWrap(True)
        fl_ai.addRow(note_ai)
        lay.addWidget(grp_ai)

        # ── 🎬 Videos ─────────────────────────────────────────────────────────
        grp_vid = QGroupBox("🎬  Videos")
        grp_vid.setStyleSheet(self._GROUP_STYLE)
        fl_vid = QFormLayout(grp_vid)
        fl_vid.setSpacing(8)
        fl_vid.setContentsMargins(12, 16, 12, 12)

        vid_cfg2 = cfg.get("videos", {})
        self.cfg_vid_enabled = QCheckBox("Videos verarbeiten")
        self.cfg_vid_enabled.setStyleSheet(self._CHECK_STYLE)
        self.cfg_vid_enabled.setChecked(vid_cfg2.get("enabled", False))
        fl_vid.addRow(self.cfg_vid_enabled)

        self.cfg_vid_min_dur = QDoubleSpinBox()
        self.cfg_vid_min_dur.setStyleSheet(self._SPIN_STYLE)
        self.cfg_vid_min_dur.setRange(0.5, 30.0)
        self.cfg_vid_min_dur.setSingleStep(0.5)
        self.cfg_vid_min_dur.setDecimals(1)
        self.cfg_vid_min_dur.setSuffix(" s")
        self.cfg_vid_min_dur.setValue(float(vid_cfg2.get("min_duration", 3.0)))
        fl_vid.addRow("Mindestlänge (REJECT):", self.cfg_vid_min_dur)

        note_vid = QLabel(
            "Clips kürzer als die Mindestlänge werden automatisch als REJECT markiert.\n"
            "Grain bewertet Auflösung, Audioqualtät und Länge (KEEP / REVIEW / REJECT).")
        note_vid.setStyleSheet("color:#5A4D3E;font-size:11px;")
        note_vid.setWordWrap(True)
        fl_vid.addRow(note_vid)
        lay.addWidget(grp_vid)

        # ── ⚙ Erweitert (collapsible) ─────────────────────────────────────────
        adv_toggle = QPushButton("▶  Erweitert")
        adv_toggle.setCheckable(True)
        adv_toggle.setChecked(False)
        adv_toggle.setStyleSheet("""
            QPushButton {
                background: transparent; border: none;
                color: #5A4D3E; font-size: 11px; font-weight: 700;
                letter-spacing: 0.06em; text-align: left; padding: 4px 2px;
            }
            QPushButton:hover { color: #8F7F66; }
            QPushButton:checked { color: #8F7F66; }
        """)
        lay.addWidget(adv_toggle)

        adv_body = QWidget()
        adv_body.setVisible(False)
        adv_layout = QVBoxLayout(adv_body)
        adv_layout.setContentsMargins(0, 0, 0, 0)
        adv_layout.setSpacing(14)

        def _toggle_adv(checked: bool) -> None:
            adv_toggle.setText(("▼  Erweitert" if checked else "▶  Erweitert"))
            adv_body.setVisible(checked)
        adv_toggle.toggled.connect(_toggle_adv)

        # Culling group (inside Erweitert)
        grp_cull2 = QGroupBox("🔍  Culling-Gewichtungen")
        grp_cull2.setStyleSheet(self._GROUP_STYLE)
        fl_cull2 = QFormLayout(grp_cull2)
        fl_cull2.setSpacing(8)
        fl_cull2.setContentsMargins(12, 16, 12, 12)
        fl_cull2.addRow("Schärfe-Gewicht:",      self.cfg_sharp_w)
        fl_cull2.addRow("Belichtungs-Gewicht:",  self.cfg_exp_w)
        fl_cull2.addRow("Histogramm-Gewicht:",   self.cfg_hist_w)
        fl_cull2.addRow("Kompositions-Gewicht:", self.cfg_comp_w)
        note_sum = QLabel("↑ Summe sollte 1.0 ergeben")
        note_sum.setStyleSheet("color:#5A4D3E;font-size:11px;")
        fl_cull2.addRow("", note_sum)
        fl_cull2.addRow("Top-Prozent (%):",  self.cfg_top_pct)
        fl_cull2.addRow("Keep-Schwellenwert:", self.cfg_keep_thr)
        adv_layout.addWidget(grp_cull2)

        # Performance group (inside Erweitert)
        grp_perf = QGroupBox("⚡  Performance")
        grp_perf.setStyleSheet(self._GROUP_STYLE)
        fl_perf = QFormLayout(grp_perf)
        fl_perf.setSpacing(8)
        fl_perf.setContentsMargins(12, 16, 12, 12)

        pipe_cfg2 = cfg.get("pipeline", {})
        def _ispin(val, lo=1, hi=16):
            s = QSpinBox()
            s.setStyleSheet(self._SPIN_STYLE)
            s.setRange(lo, hi)
            s.setValue(int(val))
            return s
        self.cfg_cull_workers = _ispin(pipe_cfg2.get("culling_workers", 2), 1, 8)
        self.cfg_ai_workers   = _ispin(pipe_cfg2.get("ai_workers", 2),      1, 4)
        self.cfg_nice         = _ispin(pipe_cfg2.get("nice_level", 10),      0, 19)
        fl_perf.addRow("Culling-Threads:", self.cfg_cull_workers)
        fl_perf.addRow("AI-Threads:",      self.cfg_ai_workers)
        fl_perf.addRow("Priorität:",       self.cfg_nice)
        note_nice = QLabel("Prozess-Priorität: 0=normal, 10=Hintergrund, 19=minimal")
        note_nice.setStyleSheet("color:#5A4D3E;font-size:11px;")
        note_nice.setWordWrap(True)
        fl_perf.addRow("", note_nice)
        adv_layout.addWidget(grp_perf)

        lay.addWidget(adv_body)
        lay.addStretch()

        # ── Save button ───────────────────────────────────────────────────────
        save_btn = QPushButton("Einstellungen speichern")
        save_btn.setFixedHeight(38)
        save_btn.setStyleSheet("""
            QPushButton { background: #FFB400; color: #141210; border: none;
                          border-radius: 8px; font-size: 13px; font-weight: 800;
                          padding: 0 24px; }
            QPushButton:hover { background: #FFC933; }
        """)
        save_btn.clicked.connect(self._save_settings)
        outer_lay.addSpacing(8)
        outer_lay.addWidget(save_btn)
        return outer

    def _field_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-weight: 600; font-size: 11px; color: #8F7F66; "
            "letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 4px;")
        return lbl

    def _on_bracket_changed(self, _index: int) -> None:
        """Persist bracket setting immediately when the Pipeline-tab combo changes."""
        val = self.bracket_combo.currentData()
        self._write_config_values({
            "bracketing.enabled":           "false" if val == "off" else "true",
            "bracketing.frames_per_sequence": "0" if val in ("off", "auto") else val,
        })
        # Keep settings tab in sync
        if hasattr(self, "cfg_brk_enabled"):
            self.cfg_brk_enabled.setChecked(val != "off")
            fps_map = {"off": 0, "auto": 0, "3": 3, "5": 5, "7": 7}
            idx = {0: 0, 3: 1, 5: 2, 7: 3}.get(fps_map.get(val, 0), 0)
            self.cfg_brk_fps.setCurrentIndex(idx)

    def _write_config_values(self, updates: dict) -> None:
        """Update dotted config keys (e.g. 'bracketing.enabled') in config.yaml.

        Keys such as 'enabled' exist in several sections (ai, videos, bracketing).
        A naive global regex replace would clobber every section at once, so when
        the key path carries a section prefix we scope the replacement to that
        section only. Keys without a prefix (assumed unique) fall back to a
        global replace.
        """
        import re as _re
        try:
            text = CONFIG_PATH.read_text()
            for key_path, value in updates.items():
                if "." in key_path:
                    section, key = key_path.rsplit(".", 1)
                    text = self._replace_in_section(text, section, key, str(value))
                else:
                    text = _re.sub(
                        rf'^(\s*{_re.escape(key_path)}:\s*)\S.*$',
                        rf'\g<1>{value}',
                        text,
                        flags=_re.MULTILINE,
                    )
            CONFIG_PATH.write_text(text)
            self.config = load_config()
        except Exception as exc:
            console.print(f"[yellow]⚠ config write: {exc}[/yellow]")

    @staticmethod
    def _replace_in_section(text: str, section_name: str, key: str, value: str) -> str:
        """Replace `key: value` only inside the given top-level YAML section.

        This is the section-aware counterpart used for ambiguous keys like
        'enabled'. If the key is missing inside the section it is inserted
        directly after the section header so the setting still takes effect.
        """
        import re as _re
        m = _re.search(rf'^{_re.escape(section_name)}:\s*$', text, _re.MULTILINE)
        if not m:
            return text
        pos = m.end()
        nxt = _re.search(r'\n\S', text[pos:])
        end = pos + nxt.start() + 1 if nxt else len(text)
        section = text[pos:end]
        new_section, n = _re.subn(
            rf'^(\s+{_re.escape(key)}:\s*)\S.*$',
            rf'\g<1>{value}',
            section,
            flags=_re.MULTILINE,
        )
        if n == 0:  # key not present yet → insert just after the header line
            new_section = f"  {key}: {value}\n" + section
        return text[:pos] + new_section + text[end:]

    def _save_settings(self) -> None:
        """Write all Einstellungen-tab values to config.yaml."""
        import re as _re
        try:
            text = CONFIG_PATH.read_text()

            def _set(key: str, value: str) -> None:
                """Replace key globally — only use for keys that appear once in config."""
                nonlocal text
                text = _re.sub(
                    rf'^(\s*{_re.escape(key)}:\s*)\S.*$',
                    rf'\g<1>{value}',
                    text,
                    flags=_re.MULTILINE,
                )

            def _set_in(section_name: str, key: str, value: str) -> None:
                """Replace key within a specific top-level YAML section only.
                Required for keys like 'enabled' that appear in multiple sections."""
                nonlocal text
                m = _re.search(rf'^{_re.escape(section_name)}:\s*$', text, _re.MULTILINE)
                if not m:
                    return
                pos = m.end()
                # Find where this section ends (next top-level key or EOF)
                nxt = _re.search(r'\n\S', text[pos:])
                end = pos + nxt.start() + 1 if nxt else len(text)
                section = text[pos:end]
                section = _re.sub(
                    rf'^(\s+{_re.escape(key)}:\s*)\S.*$',
                    rf'\g<1>{value}',
                    section,
                    flags=_re.MULTILINE,
                )
                text = text[:pos] + section + text[end:]

            # Archive
            flat_val = "true" if self.cfg_folder_combo.currentData() == "flat" else "false"
            _set("flat_archive", flat_val)
            archive_path = self.cfg_archive_edit.text().strip() or "~/Pictures/Archive"
            # archive_root has spaces/~ — inline replace (unique key)
            text = _re.sub(
                r'^(\s*archive_root:\s*).*$',
                rf'\g<1>{archive_path}',
                text, flags=_re.MULTILINE,
            )

            # Culling (all unique keys)
            _set("sharpness_weight",   str(round(self.cfg_sharp_w.value(), 2)))
            _set("exposure_weight",    str(round(self.cfg_exp_w.value(), 2)))
            _set("histogram_weight",   str(round(self.cfg_hist_w.value(), 2)))
            _set("composition_weight", str(round(self.cfg_comp_w.value(), 2)))
            _set("top_percentile",     str(int(self.cfg_top_pct.value())))
            _set("keep_threshold",     str(int(self.cfg_keep_thr.value())))

            # Bracketing — 'enabled' is ambiguous, use section-aware set
            _set_in("bracketing", "enabled",
                    "true" if self.cfg_brk_enabled.isChecked() else "false")
            _set("frames_per_sequence",  str(self.cfg_brk_fps.currentData()))

            # AI — 'enabled' is ambiguous, use section-aware set
            _set_in("ai", "enabled",
                    "true" if self.cfg_ai_enabled.isChecked() else "false")
            _set("provider",    self.cfg_ai_provider.currentData())
            _set("local_model", self.cfg_ai_model.currentData())

            # Videos — 'enabled' is ambiguous, use section-aware set
            _set_in("videos", "enabled",
                    "true" if self.cfg_vid_enabled.isChecked() else "false")
            _set("min_duration", str(round(self.cfg_vid_min_dur.value(), 1)))

            # Performance
            _set("culling_workers", str(self.cfg_cull_workers.value()))
            _set("ai_workers",      str(self.cfg_ai_workers.value()))
            _set("nice_level",      str(self.cfg_nice.value()))

            CONFIG_PATH.write_text(text)
            self.config = load_config()

            # Sync pipeline tab combo
            fps_saved = self.cfg_brk_fps.currentData()
            enabled   = self.cfg_brk_enabled.isChecked()
            if not enabled:
                self.bracket_combo.setCurrentIndex(0)
            else:
                idx = {0: 1, 3: 2, 5: 3, 7: 4}.get(fps_saved, 1)
                self.bracket_combo.setCurrentIndex(idx)

            # Sync archive field in pipeline tab
            self.archive_edit.setText(archive_path)

            # Sync video checkbox in pipeline tab
            self.video_check.setChecked(self.cfg_vid_enabled.isChecked())

            # Sync AI checkbox in pipeline tab
            self.ai_check.setChecked(self.cfg_ai_enabled.isChecked())

            self._show_toast("✅  Einstellungen gespeichert")
        except Exception as exc:
            QMessageBox.warning(self, "Fehler", f"Konnte Einstellungen nicht speichern:\n{exc}")

    def _show_toast(self, msg: str, ms: int = 2200) -> None:
        """Temporary amber toast at the bottom of the window."""
        cw = self.centralWidget()
        tw, th = 300, 44
        toast = QFrame(cw)
        toast.setStyleSheet("""
            QFrame { background: #FFB400; border-radius: 8px; }
            QLabel { color: #141210; font-size: 12px; font-weight: 700;
                     background: transparent; }
        """)
        lbl = QLabel(msg, toast)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setGeometry(0, 0, tw, th)
        x = (cw.width() - tw) // 2
        toast.setGeometry(x, cw.height() - th - 12, tw, th)
        toast.show()
        toast.raise_()
        QTimer.singleShot(ms, toast.deleteLater)

    def _load_session_review(self, session_dir: str) -> None:
        """Load SESSION_REVIEW.md from session_dir and render it in the Ergebnisse tab."""
        from pathlib import Path as _P
        review_path = _P(session_dir) / "SESSION_REVIEW.md"
        if not review_path.exists():
            return
        md = review_path.read_text(encoding="utf-8")
        # Qt's setMarkdown handles basic GFM — tables need a small pre-pass
        # because Qt doesn't render pipe tables; convert them to HTML manually
        html = self._md_to_html(md)
        self.review_browser.setHtml(html)
        # Switch to results tab
        self.tabs.setCurrentIndex(1)

    # ── Gallery (Ergebnisse tab) ──────────────────────────────────────────────
    def _on_tab_changed(self, index: int) -> None:
        """Lazy-load the latest session into the gallery when Ergebnisse opens empty."""
        if index != 1 or self._running:
            return
        if self.gallery.count() > 0:
            return
        root = self.archive_edit.text().strip() or os.path.expanduser("~/Pictures/Archive")
        self._load_gallery(root)

    def _switch_results_view(self, index: int) -> None:
        """Toggle between gallery (0) and review (1)."""
        self.results_stack.setCurrentIndex(index)
        self.seg_gallery_btn.setChecked(index == 0)
        self.seg_review_btn.setChecked(index == 1)
        for chip in self._filter_btns.values():
            chip.setVisible(index == 0)

    def _set_gallery_filter(self, key: str) -> None:
        """Show only thumbnails whose label matches (ALL shows everything)."""
        self._gallery_filter = key
        for k, chip in self._filter_btns.items():
            chip.setChecked(k == key)
        for row in range(self.gallery.count()):
            item = self.gallery.item(row)
            label = self._gallery_items[row].get("label", "") if row < len(self._gallery_items) else ""
            item.setHidden(key != "ALL" and label != key)

    def _open_gallery_item(self, item: QListWidgetItem) -> None:
        """Open the double-clicked photo in the system viewer."""
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and os.path.exists(path):
            subprocess.run(["open", path], check=False)

    def _load_gallery(self, session_dir: str) -> None:
        """Read .pipeline_report.json and populate the thumbnail gallery."""
        from pathlib import Path as _P
        try:
            from pipeline.report import load_report, find_latest_report
        except Exception:
            return
        sd = _P(session_dir)
        report_path = sd / ".pipeline_report.json"
        if not report_path.exists():
            found = find_latest_report(sd) if sd.exists() else None
            if not found:
                return
            report_path = found
        data = load_report(report_path)
        if not data:
            return
        photos = data.get("photos", [])

        # Stop any in-flight loader before rebuilding
        if self._thumb_loader is not None:
            self._thumb_loader.stop()
            self._thumb_loader.wait(100)
            self._thumb_loader = None

        self.gallery.clear()
        self._gallery_items = []
        load_list: list[tuple[int, str]] = []
        label_emoji = {"TOP": "⭐", "KEEP": "✅", "REJECT": "🗑", "UNKNOWN": "·"}
        # Best shots first so the most relevant thumbnails decode first.
        label_priority = {"TOP": 0, "KEEP": 1, "UNKNOWN": 2, "REJECT": 3}
        MAX_TILES = 800   # safety cap for huge archive-wide reports

        # Only show photos we can actually display (file still on disk).
        visible = []
        for p in photos:
            if p.get("was_skipped"):
                continue
            path = p.get("archive_path") or p.get("original_path")
            if not path or not os.path.exists(path):
                continue
            visible.append((p, path))
        visible.sort(key=lambda t: label_priority.get(t[0].get("label", "UNKNOWN"), 2))
        visible = visible[:MAX_TILES]

        for idx, (p, path) in enumerate(visible):
            label = p.get("label", "UNKNOWN")
            ai = p.get("ai_scores") or {}
            ls = p.get("local_scores") or {}
            score = ai.get("final_score") or ls.get("composite", 0.0)
            fname = p.get("filename") or _P(path).name
            caption = f"{label_emoji.get(label, '·')} {fname}\n{int(score)}"

            item = QListWidgetItem(caption)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            self.gallery.addItem(item)
            self._gallery_items.append({"label": label, "path": path, "score": score})
            load_list.append((idx, path))

        if not load_list:
            return

        self._set_gallery_filter(self._gallery_filter)

        self._thumb_loader = _ThumbLoader(load_list, max_px=300)
        self._thumb_loader.thumb_ready.connect(self._on_thumb_ready)
        self._thumb_loader.start()

    def _on_thumb_ready(self, index: int, data: bytes) -> None:
        """Build a QPixmap from the worker's JPEG bytes and set the item icon."""
        if index >= self.gallery.count():
            return
        img = QImage.fromData(data, "JPEG")
        if img.isNull():
            return
        item = self.gallery.item(index)
        if item:
            item.setIcon(QIcon(QPixmap.fromImage(img)))

    @staticmethod
    def _md_to_html(md: str) -> str:
        """Minimal Markdown→HTML converter focused on the session review format."""
        import re as _re, html as _html

        lines = md.split("\n")
        out: list[str] = []
        in_table = False
        in_code  = False

        for raw in lines:
            ln = raw

            # Code blocks
            if ln.strip().startswith("```"):
                if in_code:
                    out.append("</pre>")
                    in_code = False
                else:
                    out.append('<pre style="background:#1A1714;color:#FFB400;'
                               'padding:10px;border-radius:6px;font-size:11px;'
                               'overflow-x:auto;">')
                    in_code = True
                continue
            if in_code:
                out.append(_html.escape(ln))
                continue

            # Tables
            if ln.startswith("|"):
                cells = [c.strip() for c in ln.strip("|").split("|")]
                # Separator row (---|---)
                if all(_re.match(r"^[-: ]+$", c) for c in cells if c):
                    if not in_table:
                        out.append('<table>')
                        in_table = True
                    # promote previous <tr> to <thead>
                    if out and out[-1].startswith('<tr>'):
                        out[-1] = '<thead>' + out[-1].replace('<td>', '<th>').replace('</td>', '</th>') + '</thead><tbody>'
                    continue
                if not in_table:
                    out.append('<table><tbody>')
                    in_table = True
                row = '<tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>'
                out.append(row)
                continue
            elif in_table:
                out.append('</tbody></table>')
                in_table = False

            # Headers
            m = _re.match(r'^(#{1,3})\s+(.*)', ln)
            if m:
                lvl = len(m.group(1))
                txt = _inline(m.group(2))
                out.append(f'<h{lvl}>{txt}</h{lvl}>')
                continue

            # Blockquote
            if ln.startswith("> "):
                out.append(f'<blockquote>{_inline(ln[2:])}</blockquote>')
                continue

            # Horizontal rule
            if _re.match(r'^---+$', ln.strip()):
                out.append('<hr>')
                continue

            # Numbered list
            m2 = _re.match(r'^(\d+)\.\s+(.*)', ln)
            if m2:
                out.append(f'<p style="margin:4px 0;">'
                            f'<b style="color:#FFB400;">{m2.group(1)}.</b> '
                            f'{_inline(m2.group(2))}</p>')
                continue

            # Normal paragraph
            if ln.strip():
                out.append(f'<p style="margin:4px 0;">{_inline(ln)}</p>')
            else:
                out.append('<br>')

        if in_table:
            out.append('</tbody></table>')

        body = '\n'.join(out)
        return (
            '<html><body style="font-family:-apple-system,sans-serif;'
            'font-size:13px;color:#C4B49A;background:#141210;padding:4px;">'
            + body + '</body></html>'
        )

    def _update_result_stats(self, total: int, top: int, keep: int,
                              reject: int, elapsed_s: int) -> None:
        """Populate the stats bar in the Ergebnisse tab."""
        mins, secs = divmod(elapsed_s, 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        for col, val in zip(
            (self._rs_total, self._rs_top, self._rs_keep, self._rs_rej, self._rs_time),
            (str(total), str(top), str(keep), str(reject), time_str),
        ):
            # Each col is a QVBoxLayout; item(0) is the value label
            item = col.itemAt(0)
            if item and item.widget():
                item.widget().setText(val)
        self.result_stats_frame.show()

    # ──────────────────────────────────────────────────────────────────────────
    # Menu
    # ──────────────────────────────────────────────────────────────────────────

    def _setup_menu(self):
        menubar = self.menuBar()

        app_menu = menubar.addMenu(APP_NAME)

        settings_action = QAction("Einstellungen…", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._show_settings_dialog)
        app_menu.addAction(settings_action)

        app_menu.addSeparator()
        quit_action = QAction("Beenden", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(QApplication.quit)
        app_menu.addAction(quit_action)

        help_menu = menubar.addMenu("Hilfe")
        about_action = QAction(f"About {APP_NAME}", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

        license_action = QAction("Enter License Key…", self)
        license_action.triggered.connect(self._show_license_dialog)
        help_menu.addSeparator()
        help_menu.addAction(license_action)

    # ──────────────────────────────────────────────────────────────────────────
    # Source detection
    # ──────────────────────────────────────────────────────────────────────────

    def _auto_detect_source(self):
        """Erkennt alle SD-Karten und zeigt sie als auswählbare Buttons."""
        cards = find_sd_cards()

        # Alte Buttons entfernen (alles zwischen prefix label [0] und stretch [last])
        while self.sd_cards_row.count() > 2:  # prefix label + stretch
            item = self.sd_cards_row.takeAt(1)
            if item.widget():
                item.widget().deleteLater()

        if not cards:
            self.sd_label_prefix.hide()
            return

        self.sd_label_prefix.show()

        for card in cards:
            btn = QPushButton(card['name'])
            btn.setFixedHeight(24)
            btn.setStyleSheet("""
                QPushButton { background: #1A1714; border: 1px solid #252017; border-radius: 11px;
                              font-size: 11px; padding: 0 12px; color: #8F7F66; }
                QPushButton:hover { background: #1C1916; border-color: #FFB400; color: #F5F0E8; }
            """)
            path = card["path"]
            btn.clicked.connect(lambda checked, p=path: self._select_sd_card(p))
            # Füge vor dem Stretch ein
            self.sd_cards_row.insertWidget(self.sd_cards_row.count() - 1, btn)

        # Erste Karte automatisch auswählen wenn Source noch leer
        if not self.source_edit.text() and cards:
            self._select_sd_card(cards[0]["path"])
            QTimer.singleShot(2400, lambda: self._show_card_toast(cards[0]["name"]))

    def _select_sd_card(self, path: str):
        self.source_edit.setText(path)
        self._append_log(f"Card selected: {path}")

    # ──────────────────────────────────────────────────────────────────────────
    # File pickers
    # ──────────────────────────────────────────────────────────────────────────

    def _pick_source(self):
        start = self.source_edit.text() or "/Volumes"
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Source Folder", start,
            QFileDialog.Option.ShowDirsOnly,
        )
        if folder:
            self.source_edit.setText(folder)

    def _pick_archive(self):
        start = self.archive_edit.text() or str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Archive Folder", start,
            QFileDialog.Option.ShowDirsOnly,
        )
        if folder:
            self.archive_edit.setText(folder)

    # ──────────────────────────────────────────────────────────────────────────
    # Start / Stop
    # ──────────────────────────────────────────────────────────────────────────

    def _update_start_button(self):
        has_source = bool(self.source_edit.text().strip())
        self.start_btn.setEnabled(has_source and not self._running)

    # ── Zeitschätzung ─────────────────────────────────────────────────────────

    def _schedule_estimate(self):
        """Debounced estimate update — läuft 400 ms nach letzter Eingabe."""
        self._estimate_timer.start()

    def _load_known_hashes(self) -> set[str]:
        """Lädt bereits verarbeitete MD5-Hashes aus dem State-File."""
        state_path_str = self.config.get("paths", {}).get(
            "state_file", "~/.photo_pipeline_state.json"
        )
        state_path = Path(os.path.expanduser(state_path_str))
        try:
            if state_path.exists():
                data = json.loads(state_path.read_text())
                return set(data.get("processed_md5s", []))
        except Exception:
            pass
        return set()

    def _count_new_photos(self, source: str) -> tuple[int, int]:
        """
        Zählt (gesamt, neu) Fotos im Quellordner.
        Auf externen Laufwerken (/Volumes/): Dateinamen-Abgleich mit Archiv (schnell).
        Auf internen Laufwerken: MD5-Abgleich mit State-File (genauer).
        Läuft im Hintergrund-Thread.
        """
        known = self._load_known_hashes()
        source_path = Path(source)
        is_external = str(source_path).startswith("/Volumes/")

        # Für externe Laufwerke (SD-Karten): Dateinamen gegen Archiv-Pfade prüfen
        # Das ist viel schneller als 64KB von jeder Datei zu lesen
        if is_external:
            archive_root_str = self.config.get("paths", {}).get(
                "archive_root", "~/Pictures/Archive"
            )
            archive_root = Path(os.path.expanduser(archive_root_str))
            # Archivierte Dateinamen einmalig einlesen
            archived_names: set[str] = set()
            if archive_root.exists():
                try:
                    for f in archive_root.rglob("*"):
                        if f.is_file() and f.suffix.lower() in PHOTO_EXTENSIONS:
                            archived_names.add(f.name)
                except Exception:
                    pass

            total = 0
            new = 0
            try:
                for entry in source_path.rglob("*"):
                    if entry.is_file() and entry.suffix.lower() in PHOTO_EXTENSIONS:
                        total += 1
                        if total > 2000:
                            break
                        if not archived_names or entry.name not in archived_names:
                            new += 1
            except Exception:
                pass
            return total, new

        # Intern: MD5-basierter Abgleich mit State-File
        chunk_kb   = self.config.get("ingestion", {}).get("md5_chunk_kb", 64)
        chunk_size = chunk_kb * 1024
        total = 0
        new   = 0
        try:
            for entry in source_path.rglob("*"):
                if not entry.is_file():
                    continue
                if entry.suffix.lower() not in PHOTO_EXTENSIONS:
                    continue
                total += 1
                if total > 2000:
                    break
                if not known:
                    new += 1
                    continue
                try:
                    h = hashlib.md5()
                    with open(entry, "rb") as f:
                        h.update(f.read(chunk_size))
                    if h.hexdigest() not in known:
                        new += 1
                except OSError:
                    new += 1
        except Exception:
            pass

        return total, new

    def _get_estimate(self, total: int, new: int) -> str:
        """Gibt Zeitschätzung als lesbaren String zurück."""
        if total == 0:
            return ""

        ai_cfg     = self.config.get("ai", {})
        ai_enabled = ai_cfg.get("enabled", True)
        model      = ai_cfg.get("local_model", "moondream").lower()

        # AI-Zeit pro Bild ermitteln
        ai_secs = 0.0
        if ai_enabled:
            for key in sorted(MODEL_SECONDS_PER_IMAGE, key=len, reverse=True):
                if key in model:
                    ai_secs = MODEL_SECONDS_PER_IMAGE[key]
                    break
            if ai_secs == 0:
                ai_secs = 10.0   # Unbekanntes Modell: mittlerer Default

        model_hint = f"AI: {model}" if ai_enabled else "no AI"

        # Alle bereits archiviert → schneller Duplikat-Check, keine AI
        if new == 0 and total > 0:
            return f"📷 {total} photos  ·  all already archived  ·  ⏱ < 1 min"

        # Schätzung basiert auf neuen Fotos
        total_secs = new * (BASELINE_SECONDS_PER_IMAGE + ai_secs)
        lo = max(1, int(total_secs * 0.75 / 60))
        hi = int(total_secs * 1.25 / 60) + 1

        if hi < 2:
            time_str = "< 1 Min"
        elif lo == hi:
            time_str = f"~{lo} Min"
        else:
            time_str = f"~{lo}–{hi} Min"

        already_str = f"  ·  {total - new} already archived" if new < total else ""
        new_str     = f"{new} neue" if new < total else f"{total} Fotos"

        return f"📷 {new_str}  ·  {model_hint}  ·  ⏱ {time_str}{already_str}"

    def _update_estimate(self):
        """Startet Hintergrund-Thread zum Zählen + Schätzen."""
        source = self.source_edit.text().strip()
        if not source or not Path(source).is_dir():
            self.estimate_label.setText("")
            return

        self.estimate_label.setText("⏳  Zähle neue Fotos…")

        def _worker():
            total, new = self._count_new_photos(source)
            text = self._get_estimate(total, new)
            # Signal emittiert thread-safe zurück zum Haupt-Thread
            self._estimate_ready.emit(text)

        threading.Thread(target=_worker, daemon=True).start()

    # ── Mac wach halten ───────────────────────────────────────────────────────

    def _start_caffeinate(self):
        """Startet caffeinate — verhindert Schlafmodus während Pipeline läuft."""
        try:
            self._caffeinate_proc = subprocess.Popen(
                ["caffeinate", "-i"],   # -i = prevent idle sleep
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._caffeinate_proc = None

    def _stop_caffeinate(self):
        """Beendet caffeinate — erlaubt Schlafmodus wieder."""
        if self._caffeinate_proc:
            try:
                self._caffeinate_proc.terminate()
            except Exception:
                pass
            self._caffeinate_proc = None

    # ── Pipeline starten / stoppen ────────────────────────────────────────────

    def _show_start_hints(self) -> bool:
        """Zeigt Hinweis-Dialog vor dem ersten Start.
        Gibt True zurück wenn der User fortfahren will, False wenn abgebrochen.
        Wird übersprungen wenn 'Nicht mehr anzeigen' gesetzt ist.
        """
        cfg = load_config()
        if cfg.get("ui", {}).get("skip_start_hints", False):
            return True

        from PyQt6.QtWidgets import QDialog, QCheckBox, QDialogButtonBox

        dlg = QDialog(self)
        dlg.setWindowTitle("Vor dem Start")
        dlg.setFixedWidth(400)
        dlg.setStyleSheet("""
            QDialog { background: #1A1714; }
            QLabel  { background: transparent; }
            QCheckBox { color: #8F7F66; font-size: 12px; background: transparent; spacing: 8px; }
            QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
                                   border: 1px solid #252017; background: #141210; }
            QCheckBox::indicator:checked { background: #FFB400; border-color: #FFB400; }
        """)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(0)

        # Titel
        title = QLabel("Kurz zur Erinnerung")
        title.setStyleSheet("font-size: 16px; font-weight: 800; color: #F5F0E8; margin-bottom: 16px;")
        layout.addWidget(title)

        # Hinweis-Items
        hints = [
            ("Originale bleiben sicher",   "Fotos werden kopiert, nie verschoben oder gelöscht."),
            ("AI may make mistakes", "Briefly review suggestions before deleting rejects."),
            ("CPU usage will spike", "Best started when you take a short break."),
        ]
        for h_title, h_sub in hints:
            row = QFrame()
            row.setStyleSheet("QFrame { background: #1C1916; border-radius: 8px; margin-bottom: 8px; }")
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(14, 10, 14, 10)
            row_layout.setSpacing(2)
            t = QLabel(h_title)
            t.setStyleSheet("font-weight: 700; font-size: 13px; color: #F5F0E8;")
            s = QLabel(h_sub)
            s.setStyleSheet("font-size: 12px; color: #8F7F66;")
            s.setWordWrap(True)
            row_layout.addWidget(t)
            row_layout.addWidget(s)
            layout.addWidget(row)

        layout.addSpacing(16)

        # Checkbox
        skip_cb = QCheckBox("Nicht mehr anzeigen")
        layout.addWidget(skip_cb)
        layout.addSpacing(16)

        # Buttons
        btn_box = QDialogButtonBox()
        cancel_btn = btn_box.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        start_btn  = btn_box.addButton("Start",   QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_btn.setStyleSheet("""
            QPushButton { background: transparent; color: #8F7F66; border: 1px solid #252017;
                          border-radius: 8px; padding: 9px 20px; font-size: 13px; }
            QPushButton:hover { border-color: #FFB400; color: #F5F0E8; }
        """)
        start_btn.setStyleSheet("""
            QPushButton { background: #FFB400; color: #141210; border: none;
                          border-radius: 8px; padding: 9px 24px; font-size: 13px; font-weight: 800; }
            QPushButton:hover { background: #FFC933; }
        """)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        layout.addWidget(btn_box)

        result = dlg.exec()

        if result == QDialog.DialogCode.Accepted and skip_cb.isChecked():
            # "Nicht mehr anzeigen" in config.yaml persistieren
            try:
                cfg_text = CONFIG_PATH.read_text()
                if "ui:" in cfg_text:
                    import re as _re
                    if "skip_start_hints" in cfg_text:
                        cfg_text = _re.sub(r"skip_start_hints:.*", "skip_start_hints: true", cfg_text)
                    else:
                        cfg_text = _re.sub(r"(ui:\s*\n)", r"\1  skip_start_hints: true\n", cfg_text)
                else:
                    cfg_text += "\nui:\n  skip_start_hints: true\n"
                CONFIG_PATH.write_text(cfg_text)
                self.config = load_config()
            except Exception:
                pass

        return result == QDialog.DialogCode.Accepted

    def _start_pipeline(self):
        source = self.source_edit.text().strip()
        if not source:
            QMessageBox.warning(self, "Error", "Bitte einen Quell-Ordner wählen.")
            return

        archive = self.archive_edit.text().strip()
        event   = self.event_edit.text().strip()

        self.log_edit.clear()
        self.progress_bar.setValue(0)
        self.open_btn.setEnabled(False)
        self._last_archive = archive
        self._last_session_dir = ""  # reset for review loading

        estimate_txt = self.estimate_label.text()

        # Mac wach halten
        self._start_caffeinate()
        awake_note = "  ·  ☕ keeping Mac awake" if self._caffeinate_proc else ""
        self.status_label.setText(f"Running…{awake_note}")

        python_bin = _find_python()

        args = [str(PROJECT_DIR / "main.py"), "--source", source]
        if archive:
            args += ["--archive-root", archive]
        if event:
            args += ["--event", event]

        # ── Quick-option flags ────────────────────────────────────────────────
        if not self.ai_check.isChecked():
            args.append("--no-ai")
        if self.jpeg_check.isChecked():
            args.append("--jpeg-reject")
        if not self.video_check.isChecked():
            args.append("--no-videos")

        self._process = QProcess(self)
        self._process.setWorkingDirectory(str(PROJECT_DIR))
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        self._process.setProcessEnvironment(env)

        self._process.readyReadStandardOutput.connect(self._on_output)
        self._process.finished.connect(self._on_finished)

        self._running = True
        self.start_btn.setText("Running…")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._result_stats = {"total": 0, "top": 0, "kept": 0, "rejected": 0,
                              "video_keep": -1, "video_reject": -1}
        self.video_stats_frame.hide()
        import time
        self._pipeline_start_time = time.time()
        self._glow_timer.start()

        if estimate_txt:
            self._append_log(f"ℹ  {estimate_txt}")
        self._append_log("☕ Keeping Mac awake during processing.")
        self._process.start(python_bin, args)

        if not self._process.waitForStarted(3000):
            self._append_log("❌ Error: Could not start process.", force_red=True)
            self._stop_caffeinate()
            self._reset_ui()

    def _stop_pipeline(self):
        if self._process and self._running:
            self._process.terminate()
            self._stop_caffeinate()
            self._append_log("⚠  Stopped.")
            self._reset_ui()

    def _on_output(self):
        if not self._process:
            return
        raw = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in raw.splitlines():
            line = strip_ansi(line)
            if not line.strip():
                continue
            # Exakter Session-Ordner aus Pipeline-Marker auslesen
            if line.startswith("GRAIN_SESSION_DIR:"):
                session_path = line.split("GRAIN_SESSION_DIR:", 1)[1].strip()
                if session_path:
                    self._last_archive = session_path
                    self._last_session_dir = session_path
                continue          # nicht ins Log schreiben
            if line.startswith("GRAIN_VIDEO_KEEP:"):
                val = line.split("GRAIN_VIDEO_KEEP:", 1)[1].strip()
                self._result_stats["video_keep"] = int(val) if val.isdigit() else 0
                continue
            if line.startswith("GRAIN_VIDEO_REJECT:"):
                val = line.split("GRAIN_VIDEO_REJECT:", 1)[1].strip()
                self._result_stats["video_reject"] = int(val) if val.isdigit() else 0
                continue
            self._append_log(line)
            pct = self._parse_progress(line)
            if pct is not None:
                self.progress_bar.setValue(pct)
            # Live-Zähler in Status-Label
            m = re.search(r"\b(\d+)\s*/\s*(\d+)\b", line)
            if m and self._running:
                cur, tot = int(m.group(1)), int(m.group(2))
                self.status_label.setText(f"📸  {cur} / {tot} Fotos verarbeitet …")
            # Stats für Ergebnis-Banner sammeln
            tm = re.search(r"Importiert[:\s]+(\d+)", line)
            if tm:
                self._result_stats["total"] = int(tm.group(1))
            tt = re.search(r"TOP[:\s]+(\d+)", line, re.IGNORECASE)
            if tt:
                self._result_stats["top"] = int(tt.group(1))
            tk = re.search(r"KEEP[:\s]+(\d+)", line, re.IGNORECASE)
            if tk:
                self._result_stats["kept"] = int(tk.group(1))
            tr = re.search(r"REJECT[:\s]+(\d+)", line, re.IGNORECASE)
            if tr:
                self._result_stats["rejected"] = int(tr.group(1))

    def _on_finished(self, exit_code: int, exit_status):
        import time as _time
        self._running = False
        self._stop_caffeinate()
        if exit_code == 0:
            self.progress_bar.setValue(100)
            self.status_label.setText("✅ Fertig — Mac kann wieder schlafen.")
            self.open_btn.setEnabled(bool(self._last_archive))
            self._append_log("✅ Done.")

            # Populate Ergebnisse tab stats
            elapsed = int(_time.time() - self._pipeline_start_time) if self._pipeline_start_time else 0
            s = self._result_stats
            self._update_result_stats(
                s.get("total", 0), s.get("top", 0),
                s.get("kept", 0),  s.get("rejected", 0), elapsed,
            )

            # Video stats — show if videos were processed
            v_keep   = self._result_stats.get("video_keep", -1)
            v_reject = self._result_stats.get("video_reject", -1)
            if v_keep >= 0:
                self._vid_keep_lbl.setText(str(v_keep))
                self._vid_rej_lbl.setText(str(v_reject if v_reject >= 0 else 0))
                self.video_stats_frame.show()
            else:
                self.video_stats_frame.hide()

            # Load Session Review into browser (uses _last_session_dir set in _on_output)
            session_dir = getattr(self, "_last_session_dir", "") or self._last_archive
            if session_dir:
                QTimer.singleShot(400, lambda: self._load_session_review(session_dir))
                QTimer.singleShot(450, lambda: self._load_gallery(session_dir))

            QTimer.singleShot(300, self._show_result_banner)
        else:
            self.status_label.setText(f"Exited with code {exit_code}")
            self._append_log(f"❌ Exited with code {exit_code}.")
        self._reset_ui()

    def _reset_ui(self):
        self._running = False
        self._glow_timer.stop()
        self.progress_bar.setStyleSheet("""
            QProgressBar { background: #1C1916; border: none; border-radius: 3px; }
            QProgressBar::chunk { background: #FFB400; border-radius: 3px; }
        """)
        self.start_btn.setText("Start")
        self._update_start_button()
        self.stop_btn.setEnabled(False)

    # ──────────────────────────────────────────────────────────────────────────
    # Animations
    # ──────────────────────────────────────────────────────────────────────────

    def _pulse_progress_bar(self):
        """Alterniert den Amber-Glow der Progressbar während der Verarbeitung."""
        self._glow_phase = 1 - self._glow_phase
        if self._glow_phase:
            self.progress_bar.setStyleSheet("""
                QProgressBar { background: #1C1916; border: none; border-radius: 3px; }
                QProgressBar::chunk {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 #FFB400, stop:0.5 #FFC933, stop:1 #FFB400);
                    border-radius: 3px;
                }
            """)
        else:
            self.progress_bar.setStyleSheet("""
                QProgressBar { background: #1C1916; border: none; border-radius: 3px; }
                QProgressBar::chunk { background: #FFB400; border-radius: 3px; }
            """)

    def _show_result_banner(self):
        """Zeigt ein animiertes Ergebnis-Banner das von unten reingleitet."""
        import time
        elapsed = int(time.time() - self._pipeline_start_time) if self._pipeline_start_time else 0
        mins, secs = divmod(elapsed, 60)
        time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

        stats = self._result_stats
        total = stats["total"]
        top   = stats["top"]

        # Altes Banner entfernen
        if self._result_banner:
            self._result_banner.deleteLater()

        banner = QFrame(self.centralWidget())
        banner.setObjectName("resultBanner")
        banner.setStyleSheet("""
            QFrame#resultBanner {
                background: #1A1714;
                border: 1px solid #FFB400;
                border-radius: 12px;
            }
        """)

        layout = QHBoxLayout(banner)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(0)

        def stat_col(value_str, label_str, color="#F5F0E8"):
            col = QVBoxLayout()
            col.setSpacing(2)
            v = QLabel(value_str)
            v.setStyleSheet(
                f"color: {color}; font-family: 'Fraunces', Georgia, serif; "
                f"font-size: 28px; font-weight: 300; background: transparent;")
            v.setAlignment(Qt.AlignmentFlag.AlignCenter)
            l = QLabel(label_str)
            l.setStyleSheet(
                "color: #8F7F66; font-family: 'IBM Plex Mono', monospace; "
                "font-size: 9px; background: transparent; letter-spacing: 0.14em;")
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(v)
            col.addWidget(l)
            return col

        layout.addLayout(stat_col(str(total), "FOTOS"))
        layout.addStretch()
        layout.addLayout(stat_col(str(top), "TOP SHOTS", "#FFB400"))
        layout.addStretch()
        layout.addLayout(stat_col(time_str, "DAUER"))

        # Position: am unteren Rand des Fensters
        cw = self.centralWidget()
        bw, bh = cw.width() - 40, 80
        start_y = cw.height()           # startet unterhalb sichtbar
        end_y   = cw.height() - bh - 16

        banner.setGeometry(20, start_y, bw, bh)
        banner.show()
        banner.raise_()
        self._result_banner = banner

        # Slide-in Animation
        anim = QPropertyAnimation(banner, b"geometry", self)
        anim.setDuration(500)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.setStartValue(QRect(20, start_y, bw, bh))
        anim.setEndValue(QRect(20, end_y, bw, bh))
        anim.start()
        self._slide_anim = anim  # Referenz halten damit GC es nicht löscht

        # Zähler-Animation für TOP-Shots
        self._counter_target = top
        self._counter_label = banner.findChild(QLabel)  # erste QLabel = total
        # Alle Labels sammeln
        self._banner_labels = banner.findChildren(QLabel)
        self._animate_counters(total, top, time_str)

    def _animate_counters(self, total: int, top: int, time_str: str):
        """Zählt die Statistik-Zahlen von 0 auf den Endwert hoch."""
        steps = 20
        step_ms = 30

        def tick(i):
            if i > steps:
                return
            p = i / steps
            ease = p * p * (3 - 2 * p)
            labels = self._result_banner.findChildren(QLabel) if self._result_banner else []
            # Labels sind: total_val, total_lbl, top_val, top_lbl, time_val, time_lbl
            val_labels = [l for l in labels if l.styleSheet() and "Fraunces" in l.styleSheet()]
            if len(val_labels) >= 2:
                val_labels[0].setText(str(int(ease * total)))
                val_labels[1].setText(str(int(ease * top)))
            QTimer.singleShot(step_ms, lambda: tick(i + 1))

        tick(0)

    def _show_welcome(self):
        """Kurze Welcome-Animation beim App-Start — blendet nach 2.2s aus."""
        cw = self.centralWidget()
        overlay = QFrame(cw)
        overlay.setObjectName("welcomeOverlay")
        overlay.setStyleSheet("""
            QFrame#welcomeOverlay {
                background: #100E0C;
                border-radius: 0px;
            }
        """)
        overlay.setGeometry(0, 0, cw.width(), cw.height())
        overlay.show()
        overlay.raise_()

        layout = QVBoxLayout(overlay)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)

        title = QLabel("Grain")
        title.setStyleSheet("""
            color: #F5F0E8;
            font-family: 'Fraunces', Georgia, serif;
            font-size: 80px;
            font-weight: 300;
            letter-spacing: -2px;
            background: transparent;
        """)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        line = QFrame()
        line.setFixedSize(80, 3)
        line.setStyleSheet(
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #FFB400, stop:1 #7C5CBF); border: none;")

        slogan = QLabel("Shoot more. Sort less.")
        slogan.setStyleSheet("""
            color: #8F7F66;
            font-family: 'IBM Plex Mono', monospace;
            font-size: 13px;
            font-weight: 400;
            letter-spacing: 0.1em;
            background: transparent;
        """)
        slogan.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(title)
        layout.addWidget(line, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(4)
        layout.addWidget(slogan)

        # Fade-in via opacity effect
        effect = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(effect)

        fade_in = QPropertyAnimation(effect, b"opacity", self)
        fade_in.setDuration(600)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade_in.start()
        self._welcome_fade_in = fade_in

        # Nach 1.8s wieder ausblenden
        def start_fade_out():
            fade_out = QPropertyAnimation(effect, b"opacity", self)
            fade_out.setDuration(700)
            fade_out.setStartValue(1.0)
            fade_out.setEndValue(0.0)
            fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
            fade_out.finished.connect(overlay.deleteLater)
            fade_out.start()
            self._welcome_fade_out = fade_out

        QTimer.singleShot(1800, start_fade_out)

    def _show_card_toast(self, card_name: str):
        """Zeigt einen 'Card detected' Toast von oben rechts."""
        cw = self.centralWidget()
        tw, th = 260, 62
        toast = QFrame(cw)
        toast.setObjectName("cardToast")
        toast.setStyleSheet("""
            QFrame#cardToast {
                background: #1A1714;
                border: 1px solid #FFB400;
                border-radius: 10px;
            }
        """)

        inner = QVBoxLayout(toast)
        inner.setContentsMargins(14, 10, 14, 10)
        inner.setSpacing(2)

        top_row = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet("color: #FFB400; font-size: 8px; background: transparent;")
        lbl1 = QLabel("Card detected")
        lbl1.setStyleSheet("color: #FFB400; font-size: 11px; font-weight: 700; letter-spacing: 0.05em; background: transparent;")
        top_row.addWidget(dot)
        top_row.addSpacing(4)
        top_row.addWidget(lbl1)
        top_row.addStretch()

        lbl2 = QLabel(card_name)
        lbl2.setStyleSheet("color: #8F7F66; font-size: 12px; background: transparent;")

        inner.addLayout(top_row)
        inner.addWidget(lbl2)

        # Start position: rechts oben, etwas oberhalb sichtbar
        start_x = cw.width() - tw - 16
        start_y = -th - 10
        end_y   = 16

        toast.setGeometry(start_x, start_y, tw, th)
        toast.show()
        toast.raise_()
        self._card_toast = toast

        # Slide-in von oben
        slide_in = QPropertyAnimation(toast, b"geometry", self)
        slide_in.setDuration(450)
        slide_in.setEasingCurve(QEasingCurve.Type.OutBack)
        slide_in.setStartValue(QRect(start_x, start_y, tw, th))
        slide_in.setEndValue(QRect(start_x, end_y, tw, th))
        slide_in.start()
        self._toast_anim = slide_in

        # Nach 3s wieder rausschieben
        def hide_toast():
            slide_out = QPropertyAnimation(toast, b"geometry", self)
            slide_out.setDuration(350)
            slide_out.setEasingCurve(QEasingCurve.Type.InCubic)
            slide_out.setStartValue(QRect(start_x, end_y, tw, th))
            slide_out.setEndValue(QRect(start_x, -th - 10, tw, th))
            slide_out.finished.connect(toast.deleteLater)
            slide_out.start()
            self._toast_hide_anim = slide_out

        QTimer.singleShot(3000, hide_toast)

    # ──────────────────────────────────────────────────────────────────────────
    # Colored log
    # ──────────────────────────────────────────────────────────────────────────

    def _append_log(self, line: str, *, force_red: bool = False):
        cursor = self.log_edit.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)

        fmt = QTextCharFormat()

        if force_red:
            fmt.setForeground(QColor("#FF6B63"))
        elif re.search(r"✅|TOP|KEEP", line):
            fmt.setForeground(QColor("#6EE7B7"))
        elif re.search(r"⚠|WARN", line):
            fmt.setForeground(QColor("#FFB400"))
        elif re.search(r"❌|ERROR", line):
            fmt.setForeground(QColor("#FF6B63"))
        elif re.search(r"──|━━|\d+/\d+", line):
            fmt.setForeground(QColor("#60A5FA"))
            font = fmt.font()
            font.setBold(True)
            fmt.setFont(font)
        else:
            fmt.setForeground(QColor("#8F7F66"))

        cursor.insertText(line + "\n", fmt)
        self.log_edit.setTextCursor(cursor)
        self.log_edit.ensureCursorVisible()

    # ──────────────────────────────────────────────────────────────────────────
    # Progress parsing
    # ──────────────────────────────────────────────────────────────────────────

    def _parse_progress(self, line: str) -> int | None:
        # Match "67%" or " 67 %"
        m = re.search(r"\b(\d{1,3})\s*%", line)
        if m:
            v = int(m.group(1))
            if 0 <= v <= 100:
                return v

        # Match "Bild 34/50" or "34/50" or "Step 2/6"
        m = re.search(r"\b(\d+)\s*/\s*(\d+)\b", line)
        if m:
            num, denom = int(m.group(1)), int(m.group(2))
            if denom > 0:
                return min(100, int(num * 100 / denom))

        return None

    # ──────────────────────────────────────────────────────────────────────────
    # Bottom buttons
    # ──────────────────────────────────────────────────────────────────────────

    def _open_result(self):
        path = self._last_archive or self.archive_edit.text().strip()
        if path:
            subprocess.run(["open", os.path.expanduser(path)], check=False)

    def _show_progress(self):
        python_bin = str(PROJECT_DIR / ".venv" / "bin" / "python3")
        if not Path(python_bin).exists():
            python_bin = sys.executable

        try:
            result = subprocess.run(
                [python_bin, str(PROJECT_DIR / "main.py"), "--progress"],
                capture_output=True, text=True, timeout=15,
                cwd=str(PROJECT_DIR),
            )
            output = result.stdout + result.stderr
            self.log_edit.clear()
            for line in output.splitlines():
                self._append_log(strip_ansi(line))
        except Exception as exc:
            self._append_log(f"❌ Fehler: {exc}", force_red=True)

    # ──────────────────────────────────────────────────────────────────────────
    # Settings Dialog
    # ──────────────────────────────────────────────────────────────────────────

    def _show_settings_dialog(self):
        """Switch to Einstellungen tab (Cmd+, shortcut handler)."""
        self.tabs.setCurrentIndex(2)
        return
        # --- legacy dialog code below (unreachable, kept for reference) ---
        from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
                                     QComboBox2, QCheckBox, QGroupBox, QVBoxLayout)
        import re as _re

        dlg = QDialog(self)
        dlg.setWindowTitle("Einstellungen")
        dlg.setMinimumWidth(400)
        dlg.setStyleSheet("""
            QDialog { background: #1A1714; color: #F5F0E8; }
            QLabel { color: #C4B49A; font-size: 12px; }
            QGroupBox { color: #8F7F66; font-size: 11px; font-weight: 600;
                        border: 1px solid #252017; border-radius: 8px;
                        margin-top: 8px; padding-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px;
                               padding: 0 4px; color: #8F7F66; }
            QComboBox { background: #141210; color: #F5F0E8; border: 1px solid #252017;
                        border-radius: 6px; padding: 5px 10px; font-size: 12px; }
            QComboBox:hover { border-color: #FFB400; }
            QComboBox QAbstractItemView { background: #1A1714; color: #F5F0E8;
                                          selection-background-color: #FFB400;
                                          selection-color: #141210; }
            QCheckBox { color: #C4B49A; font-size: 12px; }
            QCheckBox::indicator { width: 16px; height: 16px;
                                   border: 1px solid #252017; border-radius: 4px;
                                   background: #141210; }
            QCheckBox::indicator:checked { background: #FFB400; border-color: #FFB400; }
            QPushButton { background: #FFB400; color: #141210; border: none;
                          border-radius: 8px; padding: 8px 20px;
                          font-size: 12px; font-weight: 800; }
            QPushButton:hover { background: #FFC933; }
            QPushButton[flat="true"] { background: transparent; color: #8F7F66;
                                       border: 1px solid #252017; }
            QPushButton[flat="true"]:hover { border-color: #FFB400; color: #F5F0E8; }
        """)

        # Read current values from config
        cfg = load_config()
        bracket_cfg = cfg.get("bracketing", {})
        fps_current = int(bracket_cfg.get("frames_per_sequence", 0))
        bracket_enabled = bool(bracket_cfg.get("enabled", True))

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(14)

        # ── Bracketing Group ────────────────────────────────────────────────
        bracket_group = QGroupBox("Belichtungsreihen (Bracketing)")
        bracket_layout = QFormLayout(bracket_group)
        bracket_layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        bracket_layout.setSpacing(10)

        enabled_cb = QCheckBox("Bracket-Erkennung aktiviert")
        enabled_cb.setChecked(bracket_enabled)
        bracket_layout.addRow(enabled_cb)

        fps_combo = QComboBox()
        fps_options = [
            ("Auto — Belichtungsabstand erkennen", 0),
            ("3 Fotos pro Sequenz  (±1 EV)", 3),
            ("5 Fotos pro Sequenz  (±2 EV)", 5),
            ("7 Fotos pro Sequenz  (±3 EV)", 7),
        ]
        for label, val in fps_options:
            fps_combo.addItem(label, val)
        # Select current value
        for i, (_, val) in enumerate(fps_options):
            if val == fps_current:
                fps_combo.setCurrentIndex(i)
                break
        fps_combo.setEnabled(bracket_enabled)
        enabled_cb.toggled.connect(fps_combo.setEnabled)

        bracket_layout.addRow("Fotos pro Sequenz:", fps_combo)
        outer.addWidget(bracket_group)

        # ── Hint text ───────────────────────────────────────────────────────
        hint = QLabel(
            "Auto: Grain erkennt Belichtungsreihen anhand der Verschlusszeit.\n"
            "Fest: Ideal wenn du die AEB-Einstellung deiner Kamera kennst."
        )
        hint.setStyleSheet("color: #5A4D3E; font-size: 11px;")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        outer.addStretch()

        # ── Button box ──────────────────────────────────────────────────────
        btn_box = QDialogButtonBox()
        cancel_btn = btn_box.addButton("Abbrechen", QDialogButtonBox.ButtonRole.RejectRole)
        save_btn   = btn_box.addButton("Speichern", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel_btn.setProperty("flat", True)
        btn_box.accepted.connect(dlg.accept)
        btn_box.rejected.connect(dlg.reject)
        outer.addWidget(btn_box)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_fps     = fps_combo.currentData()
        new_enabled = enabled_cb.isChecked()

        # Persist to config.yaml via regex (safe, preserves comments)
        try:
            cfg_text = CONFIG_PATH.read_text()

            def _set_bracket_key(text: str, key: str, value: str) -> str:
                pattern = rf'(bracketing:.*?\n(?:(?!^\S).*\n)*?  {key}:\s*).*'
                replacement = rf'\g<1>{value}'
                result = _re.sub(pattern, replacement, text, flags=_re.MULTILINE)
                if result == text:
                    # Key doesn't exist — append after "bracketing:" block header
                    result = _re.sub(
                        r'(bracketing:\s*\n)',
                        rf'\1  {key}: {value}\n',
                        text,
                    )
                return result

            cfg_text = _set_bracket_key(cfg_text, "frames_per_sequence", str(new_fps))
            cfg_text = _set_bracket_key(cfg_text, "enabled", "true" if new_enabled else "false")
            CONFIG_PATH.write_text(cfg_text)
            self.config = load_config()
        except Exception as e:
            QMessageBox.warning(self, "Fehler", f"Konnte Einstellungen nicht speichern:\n{e}")

    # ──────────────────────────────────────────────────────────────────────────
    # About
    # ──────────────────────────────────────────────────────────────────────────

    def _show_about(self):
        pro_status = "✅ Grain Pro" if is_pro() else f"🆓 Free (max {MAX_FREE_PHOTOS} Fotos)"
        key = _license_key()
        key_info = f"<br><small>Key: {key[:8]}…</small>" if key else ""
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> v{VERSION}<br>"
            f"<i>{SLOGAN}</i><br><br>"
            "KI-gestützte Foto-Pipeline für macOS.<br>"
            "Lokal · Privat · Keine Cloud.<br><br>"
            f"<b>Status:</b> {pro_status}{key_info}<br><br>"
            "<small>© 2026 — Grain. Alle Rechte vorbehalten.</small>",
        )

    def _show_license_dialog(self):
        from PyQt6.QtWidgets import QInputDialog
        current = _license_key()
        key, ok = QInputDialog.getText(
            self,
            "Grain Pro — License Key",
            "Enter your license key:",
            text=current,
        )
        if not ok:
            return
        key = key.strip()
        # Schlüssel in config.yaml speichern
        try:
            cfg_text = CONFIG_PATH.read_text()
            if "license:" in cfg_text:
                import re as _re
                cfg_text = _re.sub(r'(license:\s*\n\s*key:\s*).*', f'\\1"{key}"', cfg_text)
            else:
                cfg_text += f"\nlicense:\n  key: \"{key}\"\n"
            CONFIG_PATH.write_text(cfg_text)
            self.config = load_config()
            if key:
                QMessageBox.information(self, "Grain Pro", "✅ License key saved. Grain Pro is now active!")
            else:
                QMessageBox.information(self, "Grain", "Lizenzschlüssel entfernt.")
        except Exception as e:
            QMessageBox.warning(self, "Fehler", f"Konnte Schlüssel nicht speichern:\n{e}")

    def closeEvent(self, event):
        if self._running and self._process:
            reply = QMessageBox.question(
                self,
                f"{APP_NAME} — Still Running",
                "Processing is still running. Quit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
            self._process.terminate()
        if self._thumb_loader is not None:
            self._thumb_loader.stop()
            self._thumb_loader.wait(200)
        self._stop_caffeinate()
        event.accept()


# ──────────────────────────────────────────────────────────────────────────────
# Single-Instance Helpers  (Python stdlib socket — kein PyQt6.QtNetwork nötig)
# ──────────────────────────────────────────────────────────────────────────────

INSTANCE_PORT = 47_891          # localhost TCP-Port für Single-Instance-Guard
_raise_flag   = threading.Event()   # gesetzt wenn zweite Instanz aufgemacht wird


def _is_already_running() -> bool:
    """Versucht sich mit laufender Instanz zu verbinden.
    Gibt True zurück wenn eine andere Instanz läuft (und signalisiert ihr RAISE).
    """
    try:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.settimeout(0.3)
        if sock.connect_ex(("127.0.0.1", INSTANCE_PORT)) == 0:
            # Sende RAISE-Signal und beende diese Instanz
            try:
                sock.sendall(b"RAISE")
            except OSError:
                pass
            sock.close()
            return True
        sock.close()
    except OSError:
        pass
    return False


def _start_instance_server() -> None:
    """Startet einen Hintergrund-Thread der auf Port INSTANCE_PORT lauscht.
    Wenn eine zweite Grain-Instanz startet, setzt sie _raise_flag.
    """
    try:
        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", INSTANCE_PORT))
        srv.listen(1)
        srv.settimeout(1.0)
    except OSError:
        return   # Port bereits belegt oder nicht verfügbar → kein Fatal-Fehler

    _alive = [True]

    def _serve():
        while _alive[0]:
            try:
                conn, _ = srv.accept()
                conn.settimeout(0.3)
                try:
                    conn.recv(16)
                except OSError:
                    pass
                conn.close()
                _raise_flag.set()   # Haupt-Thread wird Fenster in den Vordergrund bringen
            except _socket.timeout:
                pass
            except OSError:
                break
        srv.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    def _stop():
        _alive[0] = False

    atexit.register(_stop)


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    # Single-Instance-Schutz BEVOR Qt initialisiert wird
    if _is_already_running():
        sys.exit(0)

    _start_instance_server()

    app = QApplication(sys.argv)

    # ── Load custom fonts ────────────────────────────────────────────────────
    _fonts_dir = PROJECT_DIR / "assets" / "fonts"
    _font_files = [
        "Fraunces-Variable.ttf",
        "IBMPlexMono-Regular.ttf",
        "IBMPlexMono-Medium.ttf",
        "IBMPlexMono-Italic.ttf",
    ]
    for _ff in _font_files:
        _fp = _fonts_dir / _ff
        if _fp.exists():
            QFontDatabase.addApplicationFont(str(_fp))

    # IBM Plex Mono as default app font (falls back to system monospace if not found)
    _families = QFontDatabase.families()
    if "IBM Plex Mono" in _families:
        _default_font = QFont("IBM Plex Mono", 12)
        app.setFont(_default_font)

    app.setStyleSheet("""
QWidget { background: #141210; color: #F5F0E8; font-family: "IBM Plex Mono", "Courier New", monospace; font-size: 13px; }
QMainWindow { background: #141210; }
QLineEdit { background: #1A1714; border: 1px solid #252017; border-radius: 8px; padding: 8px 12px; color: #F5F0E8; selection-background-color: #FFB400; }
QLineEdit:focus { border: 1px solid #FFB400; }
QTextEdit { background: #100E0C; border: 1px solid #1C1916; border-radius: 10px; color: #8F7F66; padding: 8px; }
QScrollBar:vertical { background: #141210; width: 8px; border-radius: 4px; }
QScrollBar::handle:vertical { background: #252017; border-radius: 4px; min-height: 20px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QMenuBar { background: #141210; color: #8F7F66; }
QMenuBar::item:selected { background: #1A1714; color: #F5F0E8; }
QMenu { background: #1A1714; border: 1px solid #252017; color: #F5F0E8; }
QMenu::item:selected { background: #FFB400; color: #141210; }
QMessageBox { background: #1A1714; color: #F5F0E8; }
QMessageBox QLabel { color: #F5F0E8; background: transparent; }
QMessageBox QPushButton { background: #FFB400; color: #141210; border: none; border-radius: 7px; padding: 8px 18px; font-weight: bold; min-width: 80px; }
QInputDialog { background: #1A1714; color: #F5F0E8; }
QInputDialog QLabel { color: #F5F0E8; background: transparent; }
QInputDialog QLineEdit { background: #141210; border: 1px solid #252017; color: #F5F0E8; border-radius: 6px; padding: 6px 10px; }
QInputDialog QPushButton { background: #FFB400; color: #141210; border: none; border-radius: 6px; padding: 8px 16px; font-weight: bold; }
QSplitter::handle { background: #1C1916; }
QSplitter::handle:vertical { height: 1px; }
""")
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)

    window = MainWindow()
    window.show()

    # QTimer prüft alle 500 ms ob eine zweite Instanz die App öffnen will
    def _check_raise():
        if _raise_flag.is_set():
            _raise_flag.clear()
            window.raise_()
            window.activateWindow()

    _raise_timer = QTimer()
    _raise_timer.timeout.connect(_check_raise)
    _raise_timer.start(500)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
