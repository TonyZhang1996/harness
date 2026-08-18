"""Single-file Windows GUI build for AI Harness."""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent

analysis = Analysis(
    [str(ROOT / "packaging" / "windows_gui_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=[
        (str(ROOT / "assets"), "assets"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch",
        "matplotlib",
        "pygame",
        "tensorboard",
        "tensorflow",
        "pandas",
        "scipy",
        "sklearn",
        "sympy",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="AI-Harness-0.6.0",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "ai-harness-rabbit.ico"),
)
