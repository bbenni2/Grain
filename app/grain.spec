# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Grain — Shoot more. Sort less.

block_cipher = None

a = Analysis(
    ['app_qt.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.yaml', '.'),
        ('main.py', '.'),
        ('pipeline', 'pipeline'),
        ('presets', 'presets'),
        ('assets/fonts', 'assets/fonts'),  # Fraunces + IBM Plex Mono
    ],
    hiddenimports=[
        # PyQt6
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',

        'PyQt6.sip',
        # Image processing
        'numpy',
        'cv2',
        'rawpy',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFont',
        'imagehash',
        # Config & CLI
        'yaml',
        'click',
        'rich',
        'rich.console',
        'rich.progress',
        # Pipeline internals
        'pipeline',
        'pipeline.cull',
        'pipeline.ingest',
        'pipeline.bracket',
        'pipeline.ai_analyze',
        'pipeline.export',
        'pipeline.report',
        'pipeline.compose',
        'pipeline.history',
        'pipeline.presets',
        'pipeline.critique',
        'pipeline.session_report',
        # Misc
        'piexif',
        'watchdog',
        'watchdog.observers',
        'watchdog.events',
        'anthropic',
        'threading',
        'subprocess',
        'hashlib',
        'json',
        'pathlib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['rthook_macos26_qt.py'],
    excludes=['tkinter', 'matplotlib', 'scipy'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Grain',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,       # UPX off — causes issues on macOS
    console=False,   # no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file='entitlements.plist',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='Grain',
)

app = BUNDLE(
    coll,
    name='Grain.app',
    icon='Grain.app/Contents/Resources/AppIcon.icns',
    bundle_identifier='com.grain-app.local',
    info_plist={
        'CFBundleName': 'Grain',
        'CFBundleDisplayName': 'Grain',
        'CFBundleVersion': '1.5.0',
        'CFBundleShortVersionString': '1.5.0',
        'CFBundleExecutable': 'Grain',
        'NSHighResolutionCapable': True,
        'LSMinimumSystemVersion': '12.0',
        'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
        # macOS 26: required for CFBundleGetMainBundle() to work in /Applications
        'NSPrincipalClass': 'NSApplication',
        'LSApplicationCategoryType': 'public.app-category.photography',
        'NSRequiresAquaSystemAppearance': False,
    },
)
