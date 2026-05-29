"""
Presets module — load YAML presets and apply them via Pillow.

Preset YAML format:
  brightness: 1.1        # multiplier: 1.0 = unchanged
  contrast: 1.2
  saturation: 1.15
  sharpness: 1.0
  color_grade:           # per-channel shadows/mids/highlights (RGB offsets -128..128)
    shadows:   [r, g, b]
    mids:      [r, g, b]
    highlights:[r, g, b]
  vignette: 0.0          # 0.0 = none, 1.0 = strong vignette
  grain: 0               # 0 = none, 1–10 = intensity

darktable-cli integration:
  If darktable-cli is available and a .xmp style sidecar exists, it is
  used for RAW development. Otherwise: rawpy → numpy → Pillow.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
import yaml
from PIL import Image, ImageEnhance, ImageFilter

from rich.console import Console

console = Console()


# ---------------------------------------------------------------------------
# Preset loading
# ---------------------------------------------------------------------------

DEFAULT_PRESET: dict = {
    "brightness": 1.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "sharpness": 1.0,
    "color_grade": {
        "shadows": [0, 0, 0],
        "mids": [0, 0, 0],
        "highlights": [0, 0, 0],
    },
    "vignette": 0.0,
    "grain": 0,
}


def load_preset(preset_path: Path) -> dict:
    """Load a YAML preset file, merging with defaults for missing keys."""
    try:
        raw = yaml.safe_load(preset_path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("Preset must be a YAML mapping")
        # Deep-merge with defaults
        merged = dict(DEFAULT_PRESET)
        merged.update({k: v for k, v in raw.items() if k != "color_grade"})
        if "color_grade" in raw:
            cg = dict(DEFAULT_PRESET["color_grade"])
            cg.update(raw["color_grade"])
            merged["color_grade"] = cg
        return merged
    except Exception as e:
        console.print(f"[yellow]⚠ Preset '{preset_path}' konnte nicht geladen werden: {e}. "
                      "Verwende Standard-Preset.[/yellow]")
        return dict(DEFAULT_PRESET)


def load_preset_by_name(name: str, presets_dir: Path) -> dict:
    """Load preset by name (with or without .yaml extension)."""
    candidates = [
        presets_dir / f"{name}.yaml",
        presets_dir / f"{name}.yml",
        presets_dir / name,
    ]
    for p in candidates:
        if p.exists():
            return load_preset(p)
    console.print(f"[yellow]⚠ Preset '{name}' nicht gefunden in {presets_dir}. "
                  "Verwende Standard-Preset.[/yellow]")
    return dict(DEFAULT_PRESET)


# ---------------------------------------------------------------------------
# Colour grading via LUT (lookup table)
# ---------------------------------------------------------------------------

def _build_channel_lut(shadows: int, mids: int, highlights: int) -> list[int]:
    """
    Build a 256-point LUT for one colour channel.
    Shadows affect 0–85, mids 86–170, highlights 171–255.
    Values are offsets: +10 = brighter, -10 = darker.
    """
    lut = []
    for i in range(256):
        t = i / 255.0
        # Blend weights: smooth transitions between zones
        shadow_w = max(0.0, 1.0 - t * 3.0)         # 1→0 over 0.0–0.33
        highlight_w = max(0.0, (t - 0.67) * 3.0)   # 0→1 over 0.67–1.0
        mid_w = 1.0 - shadow_w - highlight_w

        offset = shadow_w * shadows + mid_w * mids + highlight_w * highlights
        val = int(i + offset)
        lut.append(max(0, min(255, val)))
    return lut


def _apply_color_grade(img: Image.Image, color_grade: dict) -> Image.Image:
    """Apply shadow/mid/highlight colour grading per channel."""
    shadows = color_grade.get("shadows", [0, 0, 0])
    mids = color_grade.get("mids", [0, 0, 0])
    highlights = color_grade.get("highlights", [0, 0, 0])

    if all(v == 0 for v in shadows + mids + highlights):
        return img  # No-op

    if img.mode != "RGB":
        img = img.convert("RGB")

    lut_r = _build_channel_lut(shadows[0], mids[0], highlights[0])
    lut_g = _build_channel_lut(shadows[1], mids[1], highlights[1])
    lut_b = _build_channel_lut(shadows[2], mids[2], highlights[2])

    full_lut = lut_r + lut_g + lut_b
    return img.point(full_lut)


def _apply_vignette(img: Image.Image, strength: float) -> Image.Image:
    """Apply radial vignette. strength 0.0 = none, 1.0 = heavy."""
    if strength <= 0:
        return img
    if img.mode != "RGB":
        img = img.convert("RGB")

    arr = np.array(img, dtype=np.float32)
    h, w = arr.shape[:2]
    cx, cy = w / 2, h / 2

    Y, X = np.ogrid[:h, :w]
    dist = np.sqrt(((X - cx) / cx) ** 2 + ((Y - cy) / cy) ** 2)
    # Vignette mask: 1 at centre, 0 at edge
    mask = 1.0 - np.clip(dist * strength, 0, 1)
    mask = mask[:, :, np.newaxis]

    arr = arr * mask
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


def _apply_grain(img: Image.Image, intensity: int) -> Image.Image:
    """Add subtle film grain. intensity 1–10."""
    if intensity <= 0:
        return img
    if img.mode != "RGB":
        img = img.convert("RGB")

    arr = np.array(img, dtype=np.int16)
    sigma = intensity * 2.5
    noise = np.random.normal(0, sigma, arr.shape).astype(np.int16)
    arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


# ---------------------------------------------------------------------------
# Main preset application
# ---------------------------------------------------------------------------

def apply_preset(img: Image.Image, preset: dict) -> Image.Image:
    """
    Apply all preset adjustments to a Pillow Image.
    Returns a new Image object (original unmodified).
    """
    if img.mode not in ("RGB",):
        img = img.convert("RGB")

    # Basic adjustments via ImageEnhance
    if preset.get("brightness", 1.0) != 1.0:
        img = ImageEnhance.Brightness(img).enhance(preset["brightness"])

    if preset.get("contrast", 1.0) != 1.0:
        img = ImageEnhance.Contrast(img).enhance(preset["contrast"])

    if preset.get("saturation", 1.0) != 1.0:
        img = ImageEnhance.Color(img).enhance(preset["saturation"])

    if preset.get("sharpness", 1.0) != 1.0:
        img = ImageEnhance.Sharpness(img).enhance(preset["sharpness"])

    # Colour grading
    cg = preset.get("color_grade", {})
    if cg:
        img = _apply_color_grade(img, cg)

    # Vignette
    v = float(preset.get("vignette", 0.0))
    if v > 0:
        img = _apply_vignette(img, v)

    # Grain
    g = int(preset.get("grain", 0))
    if g > 0:
        img = _apply_grain(img, g)

    return img


# ---------------------------------------------------------------------------
# RAW development
# ---------------------------------------------------------------------------

def _find_darktable_cli() -> Optional[str]:
    """Find darktable-cli in PATH or common macOS locations."""
    for candidate in [
        shutil.which("darktable-cli"),
        "/usr/local/bin/darktable-cli",
        "/opt/homebrew/bin/darktable-cli",
        "/Applications/darktable.app/Contents/MacOS/darktable-cli",
    ]:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def develop_raw_darktable(
    raw_path: Path,
    output_path: Path,
    style: str = "",
    cli_path: str = "",
    timeout: int = 120,
) -> bool:
    """
    Develop a RAW file using darktable-cli.
    Returns True on success.
    """
    exe = cli_path or _find_darktable_cli()
    if not exe:
        return False

    cmd = [exe, str(raw_path), str(raw_path), str(output_path)]
    if style:
        cmd += ["--style", style]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def develop_raw_rawpy(raw_path: Path) -> Optional[Image.Image]:
    """
    Develop RAW via rawpy (fallback). Returns Pillow Image or None.
    """
    try:
        import rawpy
        with rawpy.imread(str(raw_path)) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                output_bps=8,
                no_auto_bright=False,
                bright=1.0,
            )
        return Image.fromarray(rgb)
    except Exception as e:
        console.print(f"[red]rawpy Fehler für {raw_path.name}: {e}[/red]")
        return None
