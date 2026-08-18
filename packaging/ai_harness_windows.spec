"""Single-file Windows GUI build for AI Harness."""

import json
import os
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent


def _playwright_browser_datas() -> list[tuple[str, str]]:
    """Collect the Chromium revisions required by the installed Playwright."""
    playwright_spec = find_spec("playwright")
    if playwright_spec is None or not playwright_spec.submodule_search_locations:
        raise SystemExit("无法定位 Playwright。请先在构建环境安装项目依赖。")
    package_root = Path(next(iter(playwright_spec.submodule_search_locations)))
    browsers_file = package_root / "driver" / "package" / "browsers.json"
    try:
        browser_manifest = json.loads(browsers_file.read_text(encoding="utf-8"))
        revisions = {
            item["name"]: str(item["revision"])
            for item in browser_manifest["browsers"]
            if item.get("name") in {"chromium", "chromium-headless-shell"}
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"无法读取 Playwright 浏览器版本清单：{exc}") from exc

    configured_root = os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    roots: list[Path] = []
    if configured_root == "0":
        roots.append(package_root / "driver" / "package" / ".local-browsers")
    elif configured_root:
        roots.append(Path(configured_root).expanduser())
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        roots.append(Path(local_app_data) / "ms-playwright")
    roots.extend(
        [
            Path.home() / "AppData" / "Local" / "ms-playwright",
            Path.home() / "Library" / "Caches" / "ms-playwright",
            Path.home() / ".cache" / "ms-playwright",
        ]
    )
    registry_root = next(
        (candidate.expanduser().resolve() for candidate in roots if candidate.is_dir()),
        None,
    )
    if registry_root is None:
        raise SystemExit(
            "未找到 Playwright Chromium。请先执行 "
            "python -m playwright install chromium，再重新构建。"
        )

    browser_paths = [
        registry_root / f"{name.replace('-', '_')}-{revision}"
        for name, revision in revisions.items()
    ]
    missing = [path.name for path in browser_paths if not path.is_dir()]
    if missing:
        raise SystemExit(
            "Playwright Chromium 文件不完整，缺少："
            + ", ".join(missing)
            + "。请先执行 python -m playwright install chromium。"
        )
    return [
        (str(path), f"playwright-browsers/{path.name}")
        for path in browser_paths
    ]


analysis = Analysis(
    [str(ROOT / "packaging" / "windows_gui_entry.py")],
    pathex=[str(ROOT / "src")],
    datas=[
        (str(ROOT / "assets"), "assets"),
        *_playwright_browser_datas(),
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
