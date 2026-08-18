"""Polished Tkinter desktop interface for AI Harness."""

from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import queue
import re
import sys
import threading
import tkinter as tk
import traceback
import uuid
import webbrowser
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from PIL import Image, ImageGrab, ImageTk

from . import __version__
from .agent import AgentPaused, AgentSession, _remove_visible_reasoning_blocks
from .config import (
    OPENCODE_GO_BASE_URL,
    OPENCODE_GO_CHAT_MODELS,
    OPENCODE_GO_DEFAULT_MODEL,
    is_opencode_go_provider,
    load_env_file,
)


COLORS = {
    "app": "#ffffff",
    "sidebar": "#f7f8fa",
    "sidebar_alt": "#ffffff",
    "panel": "#ffffff",
    "panel_hover": "#f0f1f3",
    "composer": "#ffffff",
    "border": "#e1e4e8",
    "border_bright": "#cfd4da",
    "text": "#17191c",
    "muted": "#5f6670",
    "subtle": "#8b929b",
    "accent": "#3d72e8",
    "accent_hover": "#2e5fcf",
    "accent_text": "#ffffff",
    "user_bubble": "#eef3ff",
    "assistant_bubble": "#ffffff",
    "tool": "#f7f8fa",
    "success": "#378a58",
    "warning": "#a56a00",
    "danger": "#c5474f",
    "success_bg": "#eff8f1",
    "warning_bg": "#fff7e6",
    "danger_bg": "#fff0f1",
}


PERMISSION_LABELS = {
    "ask": "请求批准",
    "auto": "帮我批准",
    "full-access": "完全访问",
}

UI_FONT = "Microsoft YaHei UI" if platform.system() == "Windows" else "TkDefaultFont"
MAX_RENDERED_HISTORY_ITEMS = 80
MOUSEWHEEL_SEQUENCES = ("<MouseWheel>", "<Button-4>", "<Button-5>")
TOUCHPAD_SCROLL_SEQUENCE = "<TouchpadScroll>"
MOUSEWHEEL_FRAME_MS = 16
MOUSEWHEEL_PIXELS_PER_UNIT = 48.0
MOUSEWHEEL_MAX_UNITS_PER_FRAME = 4.0
MODEL_CATALOG_TIMEOUT = 10.0
SIDEBAR_WIDTH = 328
CHAT_MAX_WIDTH = 980
CHAT_MIN_SIDE_PADDING = 30
COMPOSER_SIDE_PADDING = 24
PROMPT_PLACEHOLDER = "描述你想要构建的内容"
IMAGE_ATTACHMENT_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
ATTACHMENT_PREVIEW_SIZE = (96, 72)
CHAT_IMAGE_MAX_SIZE = (480, 360)
REMOTE_IMAGE_MAX_BYTES = 8 * 1024 * 1024
REMOTE_IMAGE_TIMEOUT = 15
PROCESS_TITLES = frozenset({"Think", "Search", "Pwsh"})
PROCESS_ICONS = {
    "Think": "⊗",
    "Search": "◉",
    "Pwsh": "▹",
}
PROCESS_PREVIEW_CHARS = 96
REMOTE_IMAGE_MARKDOWN_RE = re.compile(
    r"!\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^\s)]+))\s*\)"
)


class _PillBubble(tk.Canvas):
    """A canvas-backed bubble with fully rounded left and right ends."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        bg: str,
        outer_bg: str,
        radius: int = 24,
    ) -> None:
        super().__init__(
            master,
            bg=outer_bg,
            bd=0,
            highlightthickness=0,
            relief="flat",
        )
        self._pill_bg = bg
        self._radius = radius
        self._content_sync_scheduled = False
        self._geometry_syncing = False
        self._last_content_geometry: tuple[int, int] | None = None
        self._content = tk.Frame(self, bg=bg, bd=0, highlightthickness=0)
        self._content_window = self.create_window(
            0,
            0,
            window=self._content,
            anchor="nw",
        )
        self.bind("<Configure>", self._on_configure, add="+")
        self._content.bind("<Configure>", self._schedule_content_sync, add="+")
        self.after_idle(self._sync_content_geometry)

    @property
    def content(self) -> tk.Frame:
        """Return the frame that owns the bubble's normal Tk child widgets."""
        return self._content

    def _on_configure(self, event: tk.Event) -> None:
        width = max(1, int(getattr(event, "width", self.winfo_width())))
        height = max(1, int(getattr(event, "height", self.winfo_height())))
        # Do not resize the embedded window from the Canvas Configure
        # callback.  ``itemconfigure`` can itself emit another Configure
        # event on Tk/macOS, which otherwise re-enters this callback until
        # Python hits its recursion limit during GUI startup.
        self._draw_background(width, height)

    def _position_content(self, width: int, height: int) -> None:
        if getattr(self, "_geometry_syncing", False):
            return
        inset = min(self._radius, max(0, width // 2 - 1))
        # Canvas window items move with ``coords``. ``itemconfigure`` only
        # accepts window-item options such as width/height; passing x/y there
        # raises TclError on macOS and stops the GUI event drain.
        self._geometry_syncing = True
        try:
            self.coords(self._content_window, inset, 0)
            self.itemconfigure(
                self._content_window,
                width=max(1, width - 2 * inset),
                height=max(1, height),
            )
        finally:
            self._geometry_syncing = False

    def _draw_background(self, width: int, height: int) -> None:
        self.delete("pill-background")
        radius = min(self._radius, max(1, height // 2), max(1, width // 2))
        self.create_rectangle(
            radius,
            0,
            width - radius,
            height,
            fill=self._pill_bg,
            outline="",
            tags="pill-background",
        )
        self.create_rectangle(
            0,
            radius,
            width,
            height - radius,
            fill=self._pill_bg,
            outline="",
            tags="pill-background",
        )
        self.create_oval(
            0,
            0,
            2 * radius,
            2 * radius,
            fill=self._pill_bg,
            outline="",
            tags="pill-background",
        )
        self.create_oval(
            width - 2 * radius,
            0,
            width,
            2 * radius,
            fill=self._pill_bg,
            outline="",
            tags="pill-background",
        )
        self.tag_lower("pill-background")

    def _schedule_content_sync(self, _event: tk.Event | None = None) -> None:
        if self._content_sync_scheduled:
            return
        self._content_sync_scheduled = True
        try:
            self.after_idle(self._sync_content_geometry)
        except tk.TclError:
            self._content_sync_scheduled = False

    def _sync_content_geometry(self) -> None:
        self._content_sync_scheduled = False
        try:
            self._content.update_idletasks()
            width = max(1, self._content.winfo_reqwidth() + 2 * self._radius)
            height = max(1, self._content.winfo_reqheight())
            geometry = (width, height)
            if (
                geometry == self._last_content_geometry
                and self.winfo_width() == width
                and self.winfo_height() == height
            ):
                return
            # Set this before configure/itemconfigure: those operations may
            # synchronously schedule a child Configure event.
            self._last_content_geometry = geometry
            if self.winfo_width() != width or self.winfo_height() != height:
                self.configure(width=width, height=height)
            self._position_content(width, height)
            self._draw_background(width, height)
        except tk.TclError:
            pass


def _load_attachment_image(
    path: Path,
    max_size: tuple[int, int],
) -> Image.Image | None:
    """Load an attachment and scale it down without changing its aspect ratio."""
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            image.thumbnail(max_size, Image.Resampling.LANCZOS)
            return image
    except (OSError, ValueError):
        return None


def _make_attachment_preview(
    path: Path,
    size: tuple[int, int] = ATTACHMENT_PREVIEW_SIZE,
) -> Image.Image | None:
    """Load an image into a fixed dark canvas for the composer thumbnail."""
    image = _load_attachment_image(path, size)
    if image is None:
        # A selected file can have an image extension without containing a
        # readable image. The caller will render the normal filename chip.
        return None
    canvas = Image.new("RGBA", size, COLORS["app"])
    offset = (
        max(0, (size[0] - image.width) // 2),
        max(0, (size[1] - image.height) // 2),
    )
    canvas.alpha_composite(image, offset)
    return canvas.convert("RGB")


def _normalize_attachment_paths(
    attachments: Sequence[str | Path] | None,
) -> list[Path]:
    """Normalize attachment metadata while tolerating old or malformed state."""
    if isinstance(attachments, (str, Path)):
        attachments = (attachments,)
    paths: list[Path] = []
    for item in attachments or ():
        try:
            path = Path(item).expanduser().resolve()
        except (OSError, TypeError, ValueError):
            continue
        if path not in paths:
            paths.append(path)
    return paths


def _extract_remote_image_refs(body: str) -> list[tuple[str, str]]:
    """Return safe HTTP(S) Markdown image references from an assistant message."""
    references: list[tuple[str, str]] = []
    for match in REMOTE_IMAGE_MARKDOWN_RE.finditer(str(body)):
        alt = match.group(1).strip() or "搜索图片"
        url = (match.group(2) or match.group(3) or "").strip()
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        item = (alt[:120], url)
        if item not in references:
            references.append(item)
    return references


def _remove_remote_image_markdown(body: str) -> str:
    """Keep image alt text readable while moving the actual image below the text."""
    rendered = REMOTE_IMAGE_MARKDOWN_RE.sub(
        lambda match: match.group(1).strip() or "",
        str(body),
    )
    return rendered.strip()


def _compact_process_preview(body: str, max_chars: int = PROCESS_PREVIEW_CHARS) -> str:
    """Return the one-line preview shown for a collapsed process row."""
    first_line = ""
    for line in str(body).splitlines():
        compact = " ".join(line.split()).strip()
        if compact:
            first_line = compact
            break
    if not first_line:
        return "完成"
    if len(first_line) <= max_chars:
        return first_line
    return first_line[: max(1, max_chars - 1)].rstrip() + "…"


def _download_remote_image(
    url: str,
    max_size: tuple[int, int] = CHAT_IMAGE_MAX_SIZE,
) -> Image.Image:
    """Download one public image and prepare it for a Tk image widget."""
    request = Request(
        url,
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/148.0 Safari/537.36"
            ),
        },
    )
    with urlopen(request, timeout=REMOTE_IMAGE_TIMEOUT) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > REMOTE_IMAGE_MAX_BYTES:
            raise ValueError("图片超过 8 MB 限制")
        data = response.read(REMOTE_IMAGE_MAX_BYTES + 1)
    if len(data) > REMOTE_IMAGE_MAX_BYTES:
        raise ValueError("图片超过 8 MB 限制")
    with Image.open(BytesIO(data)) as source:
        image = source.convert("RGBA")
        image.thumbnail(max_size, Image.Resampling.LANCZOS)
        return image


def _model_catalog_url(api_url: str) -> str:
    """Derive a provider's model-list endpoint from its configured base URL."""
    normalized = api_url.strip().rstrip("/")
    if not normalized:
        raise ValueError("API URL 不能为空")
    if normalized.endswith("/models"):
        return normalized
    for suffix in ("/chat/completions", "/responses", "/messages"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return f"{normalized}/models"


def _parse_model_catalog(payload: Any) -> list[str]:
    """Extract unique model IDs from common OpenAI-style model responses."""
    if isinstance(payload, dict):
        items = payload.get("data", payload.get("models", []))
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError("模型列表响应格式不正确")

    model_ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            model_id = str(item.get("id", "")).strip()
        else:
            model_id = ""
        if model_id and model_id not in model_ids:
            model_ids.append(model_id)
    if not model_ids:
        raise ValueError("模型列表为空")
    return model_ids


def _fetch_model_catalog(api_url: str, api_key: str) -> list[str]:
    """Fetch model IDs without exposing credentials in errors or logs."""
    url = _model_catalog_url(api_url)
    headers = {"Accept": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=MODEL_CATALOG_TIMEOUT) as response:
            payload = json.loads(response.read(2_000_000).decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"获取模型列表失败（HTTP {exc.code}）") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("获取模型列表失败，请检查 API URL 或网络连接") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("获取模型列表失败，接口返回的不是有效 JSON") from exc
    return _parse_model_catalog(payload)


def _mousewheel_units(event: Any) -> int:
    """Normalize Tk mouse-wheel events across macOS, Windows, and X11."""
    delta = getattr(event, "delta", 0) or 0
    if delta:
        # Tk on macOS commonly reports +/-1, while Windows reports multiples
        # of 120. Always emit at least one Canvas unit for a non-zero event;
        # otherwise high-resolution Windows wheels can appear completely dead.
        if platform.system() == "Darwin":
            return -1 if delta > 0 else 1
        magnitude = max(1, abs(int(delta)) // 120)
        return -magnitude if delta > 0 else magnitude

    button = getattr(event, "num", None)
    if button == 4:
        return -1
    if button == 5:
        return 1
    return 0


def _touchpad_scroll_units(event: Any) -> float:
    """Decode Tk 9's packed TouchpadScroll Y delta into wheel-like units."""
    try:
        packed = int(getattr(event, "delta", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    low = packed & 0xFFFF
    delta_y = low if low < 0x8000 else low - 0x10000
    return -float(delta_y) / MOUSEWHEEL_PIXELS_PER_UNIT


def _app_asset_path(filename: str) -> Path | None:
    """Return a bundled application asset from the project checkout."""
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "assets" / filename)
    candidates.extend(
        [
            Path(__file__).resolve().parent / "assets" / filename,
            Path(__file__).resolve().parents[2] / "assets" / filename,
        ]
    )
    return next((path for path in candidates if path.is_file()), None)


def _set_windows_app_user_model_id() -> None:
    """Keep Windows from grouping the GUI under the Python executable icon."""
    if platform.system() != "Windows":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ZhangJie.AIHarness"
        )
    except (AttributeError, OSError):
        pass


def _force_windows_repaint(root: tk.Tk) -> None:
    """Synchronously repaint the Tk window after Windows restores it.

    Tk normally repaints asynchronously. On some Windows desktop/DPI
    combinations, restoring a busy Tk window can briefly expose the native
    window's unpainted black client regions before Tk's normal idle repaint
    runs. RedrawWindow with RDW_UPDATENOW makes that repaint happen before the
    activation callback returns, while RDW_ALLCHILDREN includes Tk's embedded
    child widgets.
    """
    if platform.system() != "Windows":
        return
    try:
        hwnd = ctypes.c_void_p(int(root.winfo_id()))
        redraw_window = ctypes.windll.user32.RedrawWindow
        redraw_window.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        redraw_window.restype = ctypes.c_bool
        redraw_window(
            hwnd,
            None,
            None,
            0x0001  # RDW_INVALIDATE
            | 0x0080  # RDW_ALLCHILDREN
            | 0x0100,  # RDW_UPDATENOW
        )
    except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
        # A destroyed/headless Tk instance must not turn a repaint hint into a
        # startup or shutdown failure.
        pass


def _enable_windows_dpi_awareness() -> None:
    """Enable crisp per-monitor rendering before Tk creates a window."""
    if platform.system() != "Windows":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def _tk_resource_pairs() -> list[tuple[Path, Path]]:
    """Return likely Tcl/Tk script directories for source and bundled runs."""
    pairs: list[tuple[Path, Path]] = []

    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        root = Path(bundle_root)
        pairs.append((root / "_tcl_data", root / "_tk_data"))

    base_prefix = Path(sys.base_prefix)
    prefixes = [base_prefix, Path(sys.prefix)]
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    prefixes.extend(
        candidate
        for candidate in base_prefix.parent.glob(f"cpython-{version}*/")
        if candidate != base_prefix
    )

    for prefix in prefixes:
        for version_name in ("9.0", "8.6"):
            pairs.append(
                (
                    prefix / "lib" / f"tcl{version_name}",
                    prefix / "lib" / f"tk{version_name}",
                )
            )

        # python.org macOS builds keep Tcl/Tk in framework resources.
        for tcl_scripts in (prefix / "Frameworks").glob(
            "Tcl.framework/Versions/*/Resources/Scripts"
        ):
            version_dir = tcl_scripts.parent.parent
            tk_scripts = (
                prefix
                / "Frameworks"
                / "Tk.framework"
                / "Versions"
                / version_dir.name
                / "Resources"
                / "Scripts"
            )
            pairs.append((tcl_scripts, tk_scripts))

    return pairs


def _configure_tk_runtime() -> None:
    """Point Tk at resources that PyInstaller or uv may keep outside the venv."""
    current_tcl = Path(os.environ["TCL_LIBRARY"]) if os.environ.get("TCL_LIBRARY") else None
    current_tk = Path(os.environ["TK_LIBRARY"]) if os.environ.get("TK_LIBRARY") else None
    if current_tcl is not None and current_tk is not None:
        if (current_tcl / "init.tcl").is_file() and (current_tk / "tk.tcl").is_file():
            return

    for tcl_dir, tk_dir in _tk_resource_pairs():
        if (tcl_dir / "init.tcl").is_file() and (tk_dir / "tk.tcl").is_file():
            os.environ["TCL_LIBRARY"] = str(tcl_dir)
            os.environ["TK_LIBRARY"] = str(tk_dir)
            return


def _set_tk_scaling(root: tk.Tk) -> None:
    """Apply DPI scaling only when Tk reports a finite screen size."""
    try:
        pixels_per_inch = float(root.winfo_fpixels("1i"))
        if math.isfinite(pixels_per_inch) and pixels_per_inch > 0:
            root.tk.call("tk", "scaling", pixels_per_inch / 72.0)
    except (TypeError, ValueError, tk.TclError):
        # A headless or partially initialized Tk must keep its default scaling.
        pass


def _write_startup_error(error_text: str) -> None:
    """Persist pre-window startup failures that Tk cannot display itself."""
    try:
        log_path = Path.home() / ".ai-harness" / "gui-startup-error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}]\n{error_text.rstrip()}\n\n")
    except OSError:
        pass


class HarnessGUI:
    """Codex-inspired desktop workbench with projects and saved sessions."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        workspace: str | Path | None = None,
        approval_mode: str = "auto",
        full_access: bool = False,
        model_name: str | None = None,
        max_turns: int = 100,
        state_path: str | Path | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        self.root = root
        self._workspace_was_explicit = workspace is not None
        self.workspace = Path(workspace or Path.cwd()).expanduser().resolve()
        if not full_access and approval_mode not in PERMISSION_LABELS:
            approval_mode = "auto"
        self.permission_mode = "full-access" if full_access else approval_mode
        self.max_turns = max_turns
        self.config_path = Path(
            config_path
            or os.getenv("AI_HARNESS_ENV_FILE")
            or Path.home() / ".ai-harness" / ".env"
        ).expanduser().resolve()
        if self.config_path.is_file():
            load_env_file(self.config_path)
        provider = os.getenv("AI_HARNESS_PROVIDER")
        go_configured = is_opencode_go_provider(provider) or (
            not provider and bool(os.getenv("OPENCODE_GO_API_KEY"))
        )
        self.api_key = (
            (os.getenv("OPENCODE_GO_API_KEY") if go_configured else None)
            or os.getenv("AI_HARNESS_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        )
        self.api_url = os.getenv("AI_HARNESS_BASE_URL") or (
            OPENCODE_GO_BASE_URL if go_configured else "https://api.deepseek.com"
        )
        self.model_name = model_name or os.getenv("AI_HARNESS_MODEL") or (
            OPENCODE_GO_DEFAULT_MODEL if go_configured else "deepseek-v4-flash"
        )
        self.state_path = Path(
            state_path or Path.home() / ".ai-harness" / "gui-state.json"
        ).expanduser().resolve()
        self.attachments_dir = self.state_path.parent / "attachments"
        self.pending_attachments: list[Path] = []
        # Tk drops image data when the PhotoImage object is garbage-collected.
        # Keep the current composer thumbnails alive for as long as their chips
        # are displayed.
        self._attachment_preview_images: list[ImageTk.PhotoImage] = []
        # Chat cards also need strong references to their PhotoImage objects;
        # Tk otherwise removes the image as soon as the local variable dies.
        self._chat_image_references: list[ImageTk.PhotoImage] = []
        # Search-result images are fetched off the Tk thread. Keep decoded
        # images cached so switching Sessions does not repeatedly download the
        # same public thumbnail, and keep the current labels by URL so the
        # worker result can update every matching card.
        self._remote_image_cache: dict[str, Image.Image] = {}
        self._remote_image_failures: dict[str, str] = {}
        self._remote_image_loading: set[str] = set()
        self._remote_image_widgets: dict[str, list[tk.Label]] = {}
        # session_id -> runtime state, so every Session can run concurrently.
        self.runtimes: dict[str, dict[str, Any]] = {}
        self.event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.approval_queue: list[dict[str, Any]] = []
        self.active_approval: dict[str, Any] | None = None
        self.closing = False
        self.projects: list[dict[str, str]] = []
        self.sessions: list[dict[str, Any]] = []
        self.session_rows: list[dict[str, Any]] = []
        self.current_session_id = ""
        self._prompt_placeholder_visible = False
        self._rendering_history = False
        self._drag_project_path = ""
        self._drag_start_y = 0
        self._drag_moved = False
        self._drop_project_path = ""
        # Message bodies use read-only Text widgets instead of Labels so the
        # user can select and copy both prompts and assistant responses.
        self._body_labels: list[tk.Text] = []
        self._card_wrappers: list[tk.Frame] = []
        self._active_tool_cards: dict[str, dict[str, Any]] = {}
        self._wrap_refresh_scheduled = False
        self._model_catalog_loading = False
        self._model_catalog_callbacks: dict[str, Callable[[list[str]], None]] = {}
        self._window_repaint_after_id: str | None = None
        self._mousewheel_pending: dict[str, float] = {}
        self._mousewheel_after_ids: dict[str, str] = {}
        self._chat_autoscroll_token = 0

        self._load_state()
        self.root.title(f"AI Harness {__version__} · 张杰")
        ico_path = _app_asset_path("ai-harness-rabbit.ico")
        if ico_path is not None:
            try:
                self.root.iconbitmap(default=str(ico_path))
            except tk.TclError:
                pass
        png_path = _app_asset_path("ai-harness-rabbit.png")
        if png_path is not None:
            try:
                self._app_icon_image = tk.PhotoImage(file=str(png_path))
                self.root.iconphoto(True, self._app_icon_image)
            except tk.TclError:
                self._app_icon_image = None
        self.root.geometry("1380x860")
        self.root.minsize(1080, 680)
        self.root.configure(bg=COLORS["app"])
        self.root.report_callback_exception = self._report_callback_exception
        _set_tk_scaling(self.root)
        self.root.option_add("*Font", (UI_FONT, 9))
        self._configure_styles()
        self._build_layout()
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_mousewheel, add="+")
        self._mousewheel_sequences = list(MOUSEWHEEL_SEQUENCES)
        try:
            # Tk 9 emits TouchpadScroll (not MouseWheel) for high-resolution
            # devices on macOS and Windows. Tk 8.6 rejects this event name.
            self.root.bind_all(
                TOUCHPAD_SCROLL_SEQUENCE,
                self._on_touchpad_scroll,
                add="+",
            )
            self._mousewheel_sequences.append(TOUCHPAD_SCROLL_SEQUENCE)
        except tk.TclError:
            pass
        self._bind_window_repaint_events()
        # On macOS Tk may deliver a wheel event to the focused widget instead
        # of the widget currently under the pointer. Bind every concrete
        # widget early, then route by the live pointer position below.
        self._bind_mousewheel_tree(self.root)
        self._refresh_project_list()
        self._refresh_session_list()
        self._render_current_session()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_events)

    def _write_gui_error(self, error_text: str) -> None:
        """Persist hidden Tk callback failures when running under pythonw.exe."""
        try:
            log_path = self.state_path.parent / "gui-errors.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{timestamp}]\n{error_text.rstrip()}\n\n")
        except OSError:
            pass

    def _report_callback_exception(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: Any,
    ) -> None:
        self._write_gui_error(
            "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        )
        try:
            self._set_status("界面发生错误", COLORS["danger"])
        except (AttributeError, tk.TclError):
            pass

    def _bind_window_repaint_events(self) -> None:
        """Repair transient restore/activation paint gaps without busy looping."""
        for sequence in ("<Map>", "<Visibility>"):
            try:
                self.root.bind(sequence, self._queue_window_repaint, add="+")
            except tk.TclError:
                pass
        try:
            self.root.bind_all("<FocusIn>", self._queue_window_repaint, add="+")
        except tk.TclError:
            pass

    def _queue_window_repaint(self, event: tk.Event[Any] | None = None) -> None:
        """Coalesce focus/map notifications into one repaint after Tk settles."""
        if self.closing:
            return
        if event is not None and getattr(event, "widget", None) is not None:
            try:
                if str(event.widget.winfo_toplevel()) != str(self.root):
                    return
            except (AttributeError, tk.TclError):
                return
        if self._window_repaint_after_id is not None:
            return
        try:
            self._window_repaint_after_id = self.root.after_idle(
                self._flush_window_repaint
            )
        except tk.TclError:
            self._window_repaint_after_id = None

    def _flush_window_repaint(self) -> None:
        """Flush Tk layout and force the native Windows client area to repaint."""
        self._window_repaint_after_id = None
        if self.closing:
            return
        try:
            self.root.update_idletasks()
        except tk.TclError:
            return
        _force_windows_repaint(self.root)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _new_session_record(self, title: str = "新任务") -> dict[str, Any]:
        now = self._now()
        return {
            "id": uuid.uuid4().hex[:12],
            "title": title,
            "workspace": str(self.workspace),
            "created_at": now,
            "updated_at": now,
            "items": [],
            "messages": [],
        }

    @staticmethod
    def _new_runtime() -> dict[str, Any]:
        return {
            "session": None,
            "worker": None,
            "busy": False,
            "paused": False,
            "stop_pending": False,
            "follow_up_queue": [],
            "direction_pending": 0,
            "active_approval": None,
            "pending_review_message": "",
            "progress": "",
            "active_process_title": "",
            "active_process_body": "",
            "active_process_base_body": "",
            "active_process_item": None,
            "rebuild_session_after_busy": False,
        }

    @staticmethod
    def _invalidate_sessions_for_connection_change(
        runtimes: Iterable[dict[str, Any]],
    ) -> None:
        """Rebuild idle clients now and defer busy-client rebuilds safely."""
        for runtime in runtimes:
            if runtime.get("busy"):
                runtime["rebuild_session_after_busy"] = True
            else:
                runtime["session"] = None
                runtime["rebuild_session_after_busy"] = False

    def _runtime(self, session_id: str) -> dict[str, Any]:
        runtime = self.runtimes.get(session_id)
        if runtime is None:
            runtime = self._new_runtime()
            self.runtimes[session_id] = runtime
        return runtime

    def _active_runtime(self) -> dict[str, Any]:
        return self._runtime(self.current_session_id)

    def _session_record(self, session_id: str) -> dict[str, Any]:
        for record in self.sessions:
            if record["id"] == session_id:
                return record
        record = self._new_session_record()
        record["id"] = session_id
        self.sessions.append(record)
        return record

    def _load_state(self) -> None:
        state: dict[str, Any] = {}
        try:
            if self.state_path.is_file():
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            state = {}

        for project in state.get("projects", []):
            if isinstance(project, dict) and project.get("path"):
                path = str(Path(project["path"]).expanduser().resolve())
                if not any(item["path"] == path for item in self.projects):
                    self.projects.append({"name": project.get("name") or Path(path).name, "path": path})

        for record in state.get("sessions", []):
            if not isinstance(record, dict) or not record.get("id") or not record.get("workspace"):
                continue
            record["workspace"] = str(Path(record["workspace"]).expanduser().resolve())
            record.setdefault("title", "未命名任务")
            record.setdefault("items", [])
            record.setdefault("messages", [])
            record.setdefault("created_at", self._now())
            record.setdefault("updated_at", record["created_at"])
            self.sessions.append(record)

        preferred_id = state.get("current_session_id", "")
        preferred = next(
            (item for item in self.sessions if item["id"] == preferred_id),
            None,
        )
        if preferred is not None and not self._workspace_was_explicit:
            self.workspace = Path(preferred["workspace"]).expanduser().resolve()

        current_path = str(self.workspace)
        if not any(project["path"] == current_path for project in self.projects):
            self.projects.insert(0, {"name": self.workspace.name or current_path, "path": current_path})

        matching = [item for item in self.sessions if item["workspace"] == current_path]
        if any(item["id"] == preferred_id for item in matching):
            self.current_session_id = preferred_id
        elif matching:
            self.current_session_id = max(matching, key=lambda item: item["updated_at"])["id"]
        else:
            record = self._new_session_record()
            self.sessions.append(record)
            self.current_session_id = record["id"]

    def _save_state(self) -> None:
        self._snapshot_agent_messages()
        payload = {
            "version": 1,
            "projects": self.projects,
            "sessions": self.sessions,
            "current_session_id": self.current_session_id,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.state_path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self.state_path)
        except OSError:
            # A read-only home directory must not prevent the GUI from working.
            pass

    def _current_record(self) -> dict[str, Any]:
        return self._session_record(self.current_session_id)

    def _snapshot_agent_messages(self) -> None:
        for session_id, runtime in self.runtimes.items():
            session = runtime["session"]
            if session is None:
                continue
            record = next(
                (item for item in self.sessions if item["id"] == session_id),
                None,
            )
            if record is not None:
                record["messages"] = session.messages[1:]

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background=COLORS["app"])
        style.configure("Sidebar.TFrame", background=COLORS["sidebar"])
        style.configure(
            "Primary.TButton",
            background=COLORS["text"],
            foreground="#ffffff",
            borderwidth=0,
            focusthickness=0,
            padding=(14, 8),
            font=(UI_FONT, 9, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#36393d"), ("disabled", "#d3d6da")],
            foreground=[("disabled", "#ffffff")],
        )
        style.configure(
            "NewTask.TButton",
            background=COLORS["sidebar"],
            foreground=COLORS["text"],
            borderwidth=1,
            bordercolor=COLORS["border"],
            focusthickness=0,
            padding=(13, 8),
            font=(UI_FONT, 10),
        )
        style.map(
            "NewTask.TButton",
            background=[("active", COLORS["panel_hover"]), ("pressed", COLORS["panel_hover"])],
        )
        style.configure(
            "Run.TButton",
            background=COLORS["text"],
            foreground="#ffffff",
            borderwidth=0,
            focusthickness=0,
            padding=(14, 8),
            font=(UI_FONT, 9, "bold"),
        )
        style.map("Run.TButton", background=[("active", "#36393d")])
        style.configure(
            "Stop.TButton",
            background=COLORS["warning_bg"],
            foreground=COLORS["warning"],
            borderwidth=1,
            bordercolor=COLORS["warning"],
            focusthickness=0,
            padding=(16, 8),
            font=(UI_FONT, 10, "bold"),
        )
        style.map(
            "Stop.TButton",
            background=[("active", COLORS["panel_hover"]), ("disabled", COLORS["sidebar_alt"])],
            foreground=[("disabled", COLORS["subtle"])],
        )
        style.configure(
            "Ghost.TButton",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            borderwidth=1,
            bordercolor=COLORS["border"],
            focusthickness=0,
            padding=(10, 6),
            font=(UI_FONT, 9),
        )
        style.map("Ghost.TButton", background=[("active", COLORS["panel_hover"])])
        style.configure(
            "Suggestion.TButton",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            borderwidth=0,
            focusthickness=0,
            anchor="w",
            padding=(10, 7),
            font=(UI_FONT, 9),
        )
        style.map(
            "Suggestion.TButton",
            background=[
                ("active", COLORS["panel_hover"]),
                ("pressed", COLORS["panel_hover"]),
            ],
            foreground=[
                ("active", COLORS["text"]),
                ("pressed", COLORS["text"]),
            ],
        )
        style.configure(
            "Icon.TButton",
            background=COLORS["sidebar"],
            foreground=COLORS["muted"],
            borderwidth=0,
            focusthickness=0,
            padding=(6, 4),
            font=(UI_FONT, 9),
        )
        style.map("Icon.TButton", background=[("active", COLORS["panel_hover"])], foreground=[("active", COLORS["text"])])
        style.configure(
            "Context.TButton",
            background=COLORS["composer"],
            foreground=COLORS["muted"],
            borderwidth=0,
            focusthickness=0,
            padding=(0, 2),
            font=(UI_FONT, 8),
        )
        style.map(
            "Context.TButton",
            background=[("active", COLORS["panel_hover"]), ("pressed", COLORS["panel_hover"])],
            foreground=[("active", COLORS["text"]), ("pressed", COLORS["text"])],
        )
        style.configure(
            "Dark.TCombobox",
            fieldbackground=COLORS["panel"],
            background=COLORS["panel"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["border"],
            borderwidth=0,
            relief="flat",
            padding=2,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", COLORS["panel"])],
            background=[("readonly", COLORS["panel"])],
            foreground=[("readonly", COLORS["text"])],
            selectbackground=[("readonly", COLORS["panel"])],
            selectforeground=[("readonly", COLORS["text"])],
        )
        style.configure(
            "Sidebar.TCombobox",
            fieldbackground=COLORS["sidebar"],
            background=COLORS["sidebar"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["subtle"],
            bordercolor=COLORS["sidebar"],
            borderwidth=0,
            relief="flat",
            padding=0,
        )
        style.map(
            "Sidebar.TCombobox",
            fieldbackground=[("readonly", COLORS["sidebar"])],
            background=[("readonly", COLORS["sidebar"])],
            foreground=[("readonly", COLORS["text"])],
            selectbackground=[("readonly", COLORS["sidebar"])],
            selectforeground=[("readonly", COLORS["text"])],
        )
        style.configure(
            "Composer.TCombobox",
            fieldbackground=COLORS["composer"],
            background=COLORS["composer"],
            foreground=COLORS["muted"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["composer"],
            borderwidth=0,
            relief="flat",
            padding=0,
            font=(UI_FONT, 8),
        )
        style.map(
            "Composer.TCombobox",
            fieldbackground=[("readonly", COLORS["composer"]), ("focus", COLORS["composer"])],
            background=[("readonly", COLORS["composer"]), ("focus", COLORS["composer"])],
            foreground=[("readonly", COLORS["muted"]), ("focus", COLORS["text"])],
            selectbackground=[("readonly", COLORS["composer"])],
            selectforeground=[("readonly", COLORS["text"])],
        )
        self.root.option_add("*TCombobox*Listbox.background", COLORS["panel"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["user_bubble"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", COLORS["text"])
        style.configure(
            "Project.Treeview",
            background=COLORS["sidebar"],
            fieldbackground=COLORS["sidebar"],
            foreground=COLORS["muted"],
            borderwidth=0,
            relief="flat",
            rowheight=30,
            indent=14,
            font=(UI_FONT, 9),
        )
        style.map(
            "Project.Treeview",
            background=[("selected", "#eceef1")],
            foreground=[("selected", COLORS["text"])],
        )
        style.configure(
            "Dark.Vertical.TScrollbar",
            background="#d4d7db",
            troughcolor=COLORS["sidebar"],
            bordercolor=COLORS["sidebar"],
            arrowcolor="#8a9097",
            width=8,
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", "#b7bcc3"), ("pressed", "#9299a2")],
        )

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame")
        outer.pack(fill="both", expand=True)

        accent_rail = tk.Frame(outer, width=1, bg=COLORS["border"])
        accent_rail.pack(side="left", fill="y")
        self.sidebar = ttk.Frame(outer, width=SIDEBAR_WIDTH, style="Sidebar.TFrame")
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        divider = tk.Frame(outer, width=1, bg=COLORS["border"])
        divider.pack(side="left", fill="y")
        content = ttk.Frame(outer, style="App.TFrame")
        content.pack(side="left", fill="both", expand=True)
        self._build_header(content)
        self._build_chat(content)
        self._build_composer(content)

    def _build_sidebar(self) -> None:
        brand = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        brand.pack(fill="x", padx=18, pady=(18, 14))
        logo = tk.Frame(brand, bg=COLORS["text"], width=34, height=34)
        logo.pack(side="left", padx=(0, 11))
        logo.pack_propagate(False)
        tk.Label(
            logo,
            text="H",
            bg=COLORS["text"],
            fg="#ffffff",
            font=(UI_FONT, 14, "bold"),
        ).pack(expand=True)
        brand_text = tk.Frame(brand, bg=COLORS["sidebar"])
        brand_text.pack(side="left", fill="x", expand=True)
        tk.Label(
            brand_text,
            text="AI Harness",
            bg=COLORS["sidebar"],
            fg=COLORS["text"],
            font=(UI_FONT, 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            brand_text,
            text="开发者：张杰",
            bg=COLORS["sidebar"],
            fg=COLORS["muted"],
            font=(UI_FONT, 8),
        ).pack(anchor="w", pady=(3, 0))
        tk.Label(
            brand,
            text=f"v{__version__}",
            bg=COLORS["sidebar"],
            fg=COLORS["subtle"],
            font=(UI_FONT, 8),
        ).pack(side="right", anchor="n")

        ttk.Button(
            self.sidebar,
            text="新会话",
            style="NewTask.TButton",
            command=self.new_conversation,
        ).pack(fill="x", padx=16, pady=(0, 15))

        project_summary = tk.Frame(
            self.sidebar,
            bg=COLORS["sidebar"],
        )
        project_summary.pack(fill="x", padx=20, pady=(0, 10))
        tk.Label(
            project_summary,
            text="工作区",
            bg=COLORS["sidebar"],
            fg=COLORS["subtle"],
            font=(UI_FONT, 10),
        ).pack(anchor="w", padx=1, pady=(4, 4))
        self.sidebar_workspace = tk.Label(
            project_summary,
            text=self.workspace.name or str(self.workspace),
            bg=COLORS["sidebar"],
            fg=COLORS["text"],
            font=(UI_FONT, 10),
            anchor="w",
        )
        self.sidebar_workspace.pack(fill="x", padx=1)
        self.sidebar_project_path = tk.Label(
            project_summary,
            text=str(self.workspace),
            bg=COLORS["sidebar"],
            fg=COLORS["muted"],
            font=(UI_FONT, 8),
            anchor="w",
            justify="left",
            wraplength=242,
        )
        self.sidebar_project_path.pack(fill="x", padx=1, pady=(2, 5))

        project_header = self._section_header("会话", "添加", self.add_project)
        ttk.Button(
            project_header,
            text="删除",
            style="Icon.TButton",
            command=self.delete_selected_tree_item,
        ).pack(side="right", padx=(2, 0))
        project_header.pack(fill="x", padx=17)
        tree_frame = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        tree_frame.pack(fill="both", expand=True, padx=16, pady=(5, 14))
        self.project_tree = ttk.Treeview(
            tree_frame,
            show="tree",
            selectmode="browse",
            style="Project.Treeview",
        )
        tree_scroll = ttk.Scrollbar(
            tree_frame,
            orient="vertical",
            command=self.project_tree.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.project_tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.project_tree.pack(side="left", fill="both", expand=True)
        self.project_tree.bind("<<TreeviewSelect>>", self._select_tree_item)
        # Tk reports a macOS secondary click as Button-2 or Button-3
        # depending on the pointing device; Control-click is the fallback.
        self.project_tree.bind("<Button-2>", self._show_tree_context_menu)
        self.project_tree.bind("<Button-3>", self._show_tree_context_menu)
        if platform.system() == "Darwin":
            self.project_tree.bind("<Control-Button-1>", self._show_tree_context_menu)
        self.project_tree.bind("<Delete>", lambda _event: self.delete_selected_tree_item())
        self.project_tree.bind("<ButtonPress-1>", self._start_project_drag)
        self.project_tree.bind("<B1-Motion>", self._drag_project)
        self.project_tree.bind("<ButtonRelease-1>", self._finish_project_drag)
        self.project_tree.tag_configure("project", foreground=COLORS["text"])
        self.project_tree.tag_configure("current", foreground=COLORS["text"], background="#eceef1")
        self.project_tree.tag_configure("busy", foreground=COLORS["warning"])
        self.project_tree.tag_configure("idle", foreground=COLORS["muted"])
        self.project_tree.tag_configure("drop-target", background=COLORS["panel_hover"])
        self.tree_context_menu = tk.Menu(
            self.root,
            tearoff=False,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            activebackground=COLORS["user_bubble"],
            activeforeground=COLORS["text"],
            bd=0,
        )

        settings = tk.Frame(
            self.sidebar,
            bg=COLORS["sidebar"],
        )
        settings.pack(fill="x", padx=20, pady=(0, 8))
        top = tk.Frame(settings, bg=COLORS["sidebar"])
        top.pack(fill="x", padx=1, pady=(4, 6))
        tk.Label(
            top,
            text="当前任务",
            bg=COLORS["sidebar"],
            fg=COLORS["subtle"],
            font=(UI_FONT, 9),
        ).pack(side="left")
        ttk.Button(
            top,
            text="连接设置",
            style="Icon.TButton",
            command=self.open_connection_settings,
        ).pack(side="right")

        controls = tk.Frame(settings, bg=COLORS["sidebar"])
        controls.pack(fill="x", padx=0, pady=(0, 5))
        self.permission_var = tk.StringVar(value=self.permission_mode)
        self.permission_display_var = tk.StringVar(value=PERMISSION_LABELS[self.permission_mode])
        permission_row = tk.Frame(controls, bg=COLORS["sidebar"])
        permission_row.pack(fill="x", pady=(0, 7))
        tk.Label(
            permission_row,
            text="权限",
            width=5,
            anchor="w",
            bg=COLORS["sidebar"],
            fg=COLORS["subtle"],
            font=(UI_FONT, 9),
        ).pack(side="left")
        self.permission_menu = ttk.Combobox(
            permission_row,
            textvariable=self.permission_display_var,
            values=list(PERMISSION_LABELS.values()),
            state="readonly",
            style="Sidebar.TCombobox",
            width=18,
        )
        self.permission_menu.pack(side="left", fill="x", expand=True)
        self.permission_menu.bind("<<ComboboxSelected>>", self.change_permission)
        self.model_var = tk.StringVar(value=self.model_name)
        model_row = tk.Frame(controls, bg=COLORS["sidebar"])
        model_row.pack(fill="x")
        tk.Label(
            model_row,
            text="模型",
            width=5,
            anchor="w",
            bg=COLORS["sidebar"],
            fg=COLORS["subtle"],
            font=(UI_FONT, 9),
        ).pack(side="left")
        initial_models = list(OPENCODE_GO_CHAT_MODELS)
        if self.model_name and self.model_name not in initial_models:
            initial_models.insert(0, self.model_name)
        self.model_entry = ttk.Combobox(
            model_row,
            textvariable=self.model_var,
            values=initial_models,
            state="normal",
            style="Sidebar.TCombobox",
            width=18,
            postcommand=self._request_model_catalog,
        )
        self.model_entry.pack(side="left", fill="x", expand=True, ipady=2)
        self.model_entry.bind(
            "<<ComboboxSelected>>",
            self.change_model,
            add="+",
        )
        self.model_entry.bind(
            "<FocusOut>",
            self.change_model,
            add="+",
        )

        footer = tk.Frame(
            self.sidebar,
            bg=COLORS["sidebar"],
        )
        footer.pack(fill="x", padx=20, pady=(0, 12))
        self.status_dot = tk.Label(
            footer,
            text="●",
            bg=COLORS["sidebar"],
            fg=COLORS["success"],
            font=(UI_FONT, 9),
        )
        self.status_dot.pack(side="left", padx=(10, 0), pady=8)
        self.sidebar_status = tk.Label(
            footer,
            text="就绪",
            bg=COLORS["sidebar"],
            fg=COLORS["muted"],
            font=(UI_FONT, 9),
        )
        self.sidebar_status.pack(side="left", padx=(6, 10), pady=8)

    def _section_header(self, title: str, action: str, command: Any) -> tk.Frame:
        frame = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        tk.Label(
            frame,
            text=title,
            bg=COLORS["sidebar"],
            fg=COLORS["muted"],
            font=(UI_FONT, 9, "bold"),
        ).pack(side="left")
        ttk.Button(frame, text=action, style="Icon.TButton", command=command).pack(side="right")
        return frame

    def _build_header(self, parent: ttk.Frame) -> None:
        # Let the header follow the real font metrics. A fixed 78 px height
        # clips the breadcrumb on Windows when DPI scaling is above 100%.
        header = tk.Frame(parent, bg=COLORS["app"])
        header.pack(fill="x")
        title_group = tk.Frame(header, bg=COLORS["app"])
        title_group.pack(side="left", padx=34, pady=(12, 11))
        self.header_title = tk.Label(
            title_group,
            text="新任务",
            bg=COLORS["app"],
            fg=COLORS["text"],
            font=(UI_FONT, 15, "bold"),
        )
        self.header_title.pack(anchor="w")
        self.header_breadcrumb = tk.Label(
            title_group,
            text="",
            bg=COLORS["app"],
            fg=COLORS["subtle"],
            font=(UI_FONT, 8),
        )
        self.header_breadcrumb.pack(anchor="w", pady=(4, 0))
        status_panel = tk.Frame(
            header,
            bg=COLORS["app"],
        )
        status_panel.pack(side="right", padx=34, pady=(18, 16))
        self.header_status_panel = status_panel
        self.header_status_dot = tk.Label(
            status_panel,
            text="●",
            bg=COLORS["app"],
            fg=COLORS["success"],
            font=(UI_FONT, 8),
        )
        self.header_status_dot.pack(side="left", padx=(10, 5), pady=6)
        self.header_status = tk.Label(
            status_panel,
            text="就绪",
            bg=COLORS["app"],
            fg=COLORS["muted"],
            font=(UI_FONT, 8),
        )
        self.header_status.pack(side="left", padx=(0, 11), pady=6)
        tk.Frame(parent, height=1, bg=COLORS["border"]).pack(fill="x")

    def _build_chat(self, parent: ttk.Frame) -> None:
        chat_area = tk.Frame(parent, bg=COLORS["app"])
        chat_area.pack(fill="both", expand=True, padx=24)
        self.chat_canvas = tk.Canvas(chat_area, bg=COLORS["app"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(
            chat_area,
            orient="vertical",
            command=self.chat_canvas.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.chat_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.chat_canvas.pack(side="left", fill="both", expand=True)
        self.chat_inner = tk.Frame(self.chat_canvas, bg=COLORS["app"])
        self.chat_window = self.chat_canvas.create_window((0, 0), window=self.chat_inner, anchor="nw")
        self.chat_inner.bind("<Configure>", lambda _event: self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all")))
        self.chat_canvas.bind("<Configure>", self._on_chat_canvas_configure)

    def _on_chat_canvas_configure(self, event: tk.Event[Any]) -> None:
        """Keep the chat content width and card wrapping in sync with resizing."""
        self.chat_canvas.itemconfigure(self.chat_window, width=event.width)
        self._schedule_card_wrap_refresh()

    def _build_composer(self, parent: ttk.Frame) -> None:
        composer_wrap = tk.Frame(parent, bg=COLORS["app"])
        composer_wrap.pack(fill="x", padx=COMPOSER_SIDE_PADDING, pady=(8, 18))
        outer_border = tk.Frame(
            composer_wrap,
            bg=COLORS["border"],
            padx=1,
            pady=1,
        )
        outer_border.pack(fill="x")
        self.composer = tk.Frame(outer_border, bg=COLORS["composer"])
        self.composer.pack(fill="x")
        self.prompt = ScrolledText(
            self.composer,
            height=3,
            wrap="word",
            bg=COLORS["composer"],
            fg=COLORS["text"],
            insertbackground=COLORS["accent"],
            selectbackground="#dfe7fb",
            selectforeground=COLORS["text"],
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=13,
            font=(UI_FONT, 10),
        )
        self.prompt.pack(fill="x", expand=True)
        self.prompt.bind("<Return>", self._send_from_event)
        self.prompt.bind("<Control-Return>", self._queue_from_event)
        self.prompt.bind("<Shift-Return>", self._insert_newline)
        self.prompt.bind("<Control-v>", self._paste_from_clipboard)
        self.prompt.bind("<FocusIn>", lambda _event: self._clear_prompt_placeholder(), add="+")
        self.prompt.bind("<FocusOut>", lambda _event: self._show_prompt_placeholder(), add="+")
        self._show_prompt_placeholder()
        self.attachment_bar = tk.Frame(self.composer, bg=COLORS["composer"])
        self.composer_controls = tk.Frame(self.composer, bg=COLORS["composer"])
        self.composer_controls.pack(fill="x", padx=12, pady=(0, 10))
        tk.Button(
            self.composer_controls,
            text="附件",
            command=self.select_attachments,
            bg=COLORS["composer"],
            fg=COLORS["muted"],
            activebackground=COLORS["panel_hover"],
            activeforeground=COLORS["text"],
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=2,
            pady=2,
            font=(UI_FONT, 8),
        ).pack(side="left", padx=(0, 16))

        self.composer_project = ttk.Button(
            self.composer_controls,
            text=self.workspace.name or str(self.workspace),
            style="Context.TButton",
            command=self.choose_workspace,
            cursor="hand2",
        )
        self.composer_project.pack(side="left", padx=(0, 18), pady=2)

        model_values = self._model_choices(
            list(self.model_entry.cget("values")),
            self.model_var.get(),
        )
        self.composer_model = ttk.Combobox(
            self.composer_controls,
            textvariable=self.model_var,
            values=model_values,
            state="readonly",
            style="Composer.TCombobox",
            width=18,
            cursor="hand2",
            postcommand=lambda: self._request_model_catalog(self.composer_model),
        )
        self.composer_model.pack(side="left", padx=(0, 18), pady=2)
        self.composer_model.bind("<<ComboboxSelected>>", self.change_model, add="+")

        self.composer_permission = ttk.Combobox(
            self.composer_controls,
            textvariable=self.permission_display_var,
            values=list(PERMISSION_LABELS.values()),
            state="readonly",
            style="Composer.TCombobox",
            width=12,
            cursor="hand2",
        )
        self.composer_permission.pack(side="left", padx=(0, 18), pady=2)
        self.composer_permission.bind("<<ComboboxSelected>>", self.change_permission, add="+")
        self.send_button = ttk.Button(
            self.composer_controls,
            text="发送",
            style="Primary.TButton",
            command=self.send_message,
        )
        self.send_button.pack(side="right")
        self.resume_button = ttk.Button(
            self.composer_controls,
            text="继续",
            style="Run.TButton",
            command=self.resume_running,
        )
        self.resume_button.pack_forget()
        self.queue_button = ttk.Button(
            self.composer_controls,
            text="加入队列",
            style="Ghost.TButton",
            command=self.queue_message,
        )
        self.steer_button = ttk.Button(
            self.composer_controls,
            text="调整方向",
            style="Run.TButton",
            command=self.adjust_direction,
        )
        # These actions are only meaningful while the active Session is
        # running.  Keep the normal composer compact when it is idle.
        self.queue_button.pack_forget()
        self.steer_button.pack_forget()

    def _show_prompt_placeholder(self) -> None:
        if not hasattr(self, "prompt"):
            return
        try:
            if self.prompt.get("1.0", "end-1c").strip():
                return
            self.prompt.configure(fg=COLORS["subtle"])
            self.prompt.insert("1.0", PROMPT_PLACEHOLDER)
            self._prompt_placeholder_visible = True
        except tk.TclError:
            pass

    def _clear_prompt_placeholder(self) -> None:
        if not hasattr(self, "prompt"):
            return
        try:
            if self._prompt_placeholder_visible:
                self.prompt.delete("1.0", "end")
                self.prompt.configure(fg=COLORS["text"])
                self._prompt_placeholder_visible = False
        except tk.TclError:
            pass

    def _refresh_composer_context(self) -> None:
        """Keep the composer context badges synchronized with the active task."""
        try:
            if hasattr(self, "composer_project"):
                self.composer_project.configure(text=self.workspace.name or str(self.workspace))
            if hasattr(self, "composer_model"):
                model = self.model_var.get().strip() or self.model_name
                self.model_var.set(model)
            if hasattr(self, "composer_permission"):
                self.permission_display_var.set(
                    PERMISSION_LABELS.get(self.permission_var.get(), self.permission_var.get())
                )
        except tk.TclError:
            pass

    def _send_from_event(self, _event: tk.Event[Any]) -> str:
        self.send_message()
        return "break"

    def _queue_from_event(self, _event: tk.Event[Any]) -> str:
        """Ctrl+Enter queues a follow-up while a Session is running."""
        if self._active_runtime()["busy"] and not self._active_runtime()["paused"]:
            self.queue_message()
        else:
            self.send_message()
        return "break"

    def _insert_newline(self, _event: tk.Event[Any]) -> str:
        self._clear_prompt_placeholder()
        self.prompt.insert("insert", "\n")
        return "break"

    def select_attachments(self) -> None:
        paths = filedialog.askopenfilenames(
            initialdir=str(self.workspace),
            title="选择图片或文件",
            filetypes=[
                ("所有支持的文件", "*.*"),
                ("图片", "*.png;*.jpg;*.jpeg;*.gif;*.webp"),
                ("文本与代码", "*.txt;*.md;*.py;*.js;*.ts;*.json;*.yaml;*.yml"),
            ],
        )
        for path in paths:
            self._add_attachment(Path(path))

    def _paste_from_clipboard(self, _event: tk.Event[Any]) -> str | None:
        """Attach clipboard images or Explorer files; leave text paste native."""
        try:
            clipboard = ImageGrab.grabclipboard()
        except OSError:
            return None
        if isinstance(clipboard, Image.Image):
            self.attachments_dir.mkdir(parents=True, exist_ok=True)
            path = self.attachments_dir / f"clipboard-{uuid.uuid4().hex[:10]}.png"
            clipboard.save(path, format="PNG")
            self._add_attachment(path)
            return "break"
        if isinstance(clipboard, list):
            added = False
            for item in clipboard:
                path = Path(item)
                if path.is_file():
                    self._add_attachment(path)
                    added = True
            return "break" if added else None
        return None

    def _add_attachment(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            return
        if resolved not in self.pending_attachments:
            self.pending_attachments.append(resolved)
            self._refresh_attachment_bar()

    def _remove_attachment(self, path: Path) -> None:
        self.pending_attachments = [item for item in self.pending_attachments if item != path]
        self._refresh_attachment_bar()

    def _refresh_attachment_bar(self) -> None:
        self._attachment_preview_images = []
        for child in self.attachment_bar.winfo_children():
            child.destroy()
        if not self.pending_attachments:
            self.attachment_bar.pack_forget()
            return
        self.attachment_bar.pack(
            fill="x",
            padx=12,
            pady=(0, 8),
            before=self.composer_controls,
        )
        for path in self.pending_attachments[:6]:
            chip = tk.Frame(
                self.attachment_bar,
                bg=COLORS["composer"],
                highlightthickness=0,
            )
            chip.pack(side="left", padx=(0, 14))
            if path.suffix.lower() in IMAGE_ATTACHMENT_SUFFIXES:
                preview = _make_attachment_preview(path)
                if preview is not None:
                    photo = ImageTk.PhotoImage(preview, master=self.root)
                    self._attachment_preview_images.append(photo)
                    preview_frame = tk.Frame(chip, bg=COLORS["composer"])
                    preview_frame.pack(padx=5, pady=(5, 2))
                    tk.Label(
                        preview_frame,
                        image=photo,
                        bg=COLORS["composer"],
                        relief="flat",
                    ).pack()
                    tk.Button(
                        preview_frame,
                        text="×",
                        command=lambda item=path: self._remove_attachment(item),
                        bg=COLORS["composer"],
                        fg=COLORS["text"],
                        activebackground=COLORS["panel_hover"],
                        activeforeground=COLORS["text"],
                        relief="flat",
                        borderwidth=0,
                        padx=2,
                        pady=0,
                        font=(UI_FONT, 9, "bold"),
                    ).place(relx=1.0, rely=0.0, anchor="ne")
                    tk.Label(
                        chip,
                        text=path.name,
                        bg=COLORS["composer"],
                        fg=COLORS["muted"],
                        font=(UI_FONT, 8),
                        justify="center",
                        wraplength=108,
                    ).pack(padx=4, pady=(0, 5))
                    continue

            tk.Label(
                chip,
                text=path.name,
                bg=COLORS["composer"],
                fg=COLORS["muted"],
                font=(UI_FONT, 8),
            ).pack(side="left", padx=(0, 4), pady=6)
            tk.Button(
                chip,
                text="×",
                command=lambda item=path: self._remove_attachment(item),
                bg=COLORS["composer"],
                fg=COLORS["subtle"],
                activebackground=COLORS["panel_hover"],
                activeforeground=COLORS["text"],
                relief="flat",
                borderwidth=0,
                font=(UI_FONT, 9),
            ).pack(side="left", padx=(0, 5), pady=2)
        if len(self.pending_attachments) > 6:
            tk.Label(
                self.attachment_bar,
                text=f"另有 {len(self.pending_attachments) - 6} 个",
                bg=COLORS["composer"],
                fg=COLORS["subtle"],
                font=(UI_FONT, 8),
            ).pack(side="left", padx=4)

    @staticmethod
    def _widget_is_inside(widget: tk.Misc, container: tk.Misc) -> bool:
        """Return whether a Tk widget is the container or one of its children."""
        widget_path = str(widget)
        container_path = str(container)
        return widget_path == container_path or widget_path.startswith(container_path + ".")

    def _bind_mousewheel(self, widget: tk.Misc) -> None:
        """Bind wheel input on the concrete widget before its class binding."""
        if getattr(widget, "_ai_harness_mousewheel_bound", False):
            return
        try:
            sequences = getattr(self, "_mousewheel_sequences", MOUSEWHEEL_SEQUENCES)
            for sequence in sequences:
                callback = (
                    self._on_touchpad_scroll
                    if sequence == TOUCHPAD_SCROLL_SEQUENCE
                    else self._on_mousewheel
                )
                widget.bind(sequence, callback, add="+")
            widget._ai_harness_mousewheel_bound = True
        except (AttributeError, tk.TclError):
            pass

    def _bind_mousewheel_tree(self, widget: tk.Misc) -> None:
        """Install the early wheel binding on a canvas and all content widgets."""
        self._bind_mousewheel(widget)
        for child in widget.winfo_children():
            self._bind_mousewheel_tree(child)

    def _mousewheel_target(self, event: tk.Event[Any]) -> tk.Misc | None:
        """Find the scrollable region under a wheel event on every Tk platform."""
        containers = (
            getattr(self, "chat_canvas", None),
            getattr(self, "project_tree", None),
        )
        candidates: list[Any] = [getattr(event, "widget", None)]
        root = getattr(self, "root", None)
        try:
            x_root = getattr(event, "x_root")
            y_root = getattr(event, "y_root")
            if root is not None:
                candidates.append(root.winfo_containing(x_root, y_root))
        except (AttributeError, tk.TclError, TypeError, ValueError):
            pass
        try:
            if root is not None:
                pointer_x = root.winfo_pointerx()
                pointer_y = root.winfo_pointery()
                candidates.append(root.winfo_containing(pointer_x, pointer_y))
        except (AttributeError, tk.TclError, TypeError, ValueError):
            pass

        for candidate in candidates:
            if candidate is None:
                continue
            for container in containers:
                if container is not None and self._widget_is_inside(candidate, container):
                    return container
        return None

    def _on_mousewheel(self, event: tk.Event[Any]) -> str | None:
        units = _mousewheel_units(event)
        if not units:
            return None
        target = self._mousewheel_target(event)
        if target is None:
            return None
        if target is getattr(self, "chat_canvas", None):
            self._cancel_chat_autoscroll()
        self._scroll_with_mousewheel(target, units)
        return "break"

    def _on_touchpad_scroll(self, event: tk.Event[Any]) -> str | None:
        units = _touchpad_scroll_units(event)
        if not units:
            return None
        target = self._mousewheel_target(event)
        if target is None:
            return None
        if target is getattr(self, "chat_canvas", None):
            self._cancel_chat_autoscroll()
        self._scroll_with_mousewheel(target, units)
        return "break"

    def _scroll_with_mousewheel(self, widget: tk.Misc, units: float) -> None:
        """Coalesce wheel bursts so complex chat content repaints once per frame."""
        key = str(widget)
        pending = getattr(self, "_mousewheel_pending", None)
        if pending is None:
            pending = self._mousewheel_pending = {}
        after_ids = getattr(self, "_mousewheel_after_ids", None)
        if after_ids is None:
            after_ids = self._mousewheel_after_ids = {}

        pending[key] = max(
            -MOUSEWHEEL_MAX_UNITS_PER_FRAME,
            min(MOUSEWHEEL_MAX_UNITS_PER_FRAME, pending.get(key, 0.0) + units),
        )
        if key in after_ids:
            return
        root = getattr(self, "root", None)
        if root is None:
            self._flush_mousewheel(widget)
            return
        try:
            after_ids[key] = root.after(
                MOUSEWHEEL_FRAME_MS,
                self._flush_mousewheel,
                widget,
            )
        except (AttributeError, tk.TclError):
            pending.pop(key, None)

    def _flush_mousewheel(self, widget: tk.Misc) -> None:
        """Apply one bounded wheel movement after merging events for a UI frame."""
        key = str(widget)
        getattr(self, "_mousewheel_after_ids", {}).pop(key, None)
        units = getattr(self, "_mousewheel_pending", {}).pop(key, 0.0)
        if not units:
            return

        # Treeview has row-based scrolling and is cheap to repaint. The chat
        # Canvas contains many embedded Tk widgets, so move it by a pixel-like
        # fraction only once per frame instead of redrawing for every event.
        if widget is getattr(self, "project_tree", None):
            try:
                widget.yview_scroll(int(round(units)), "units")
            except (AttributeError, tk.TclError):
                pass
            return
        try:
            view = tuple(float(value) for value in widget.yview())
            viewport_height = max(1, int(widget.winfo_height()))
        except (AttributeError, TypeError, ValueError, tk.TclError):
            return
        if len(view) < 2:
            return
        visible_fraction = max(0.0, min(1.0, view[1] - view[0]))
        if visible_fraction <= 0.0 or visible_fraction >= 1.0:
            return
        delta = (
            units
            * MOUSEWHEEL_PIXELS_PER_UNIT
            * visible_fraction
            / viewport_height
        )
        maximum = max(0.0, 1.0 - visible_fraction)
        target = max(0.0, min(maximum, view[0] + delta))
        if target == view[0]:
            return
        try:
            widget.yview_moveto(target)
        except (AttributeError, tk.TclError):
            pass

    def _clear_mousewheel_queue(self, widget: tk.Misc | None = None) -> None:
        """Cancel queued wheel work when changing sessions or closing the GUI."""
        pending = getattr(self, "_mousewheel_pending", {})
        after_ids = getattr(self, "_mousewheel_after_ids", {})
        keys = list(after_ids) if widget is None else [str(widget)]
        for key in keys:
            after_id = after_ids.pop(key, None)
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except (AttributeError, tk.TclError):
                    pass
            pending.pop(key, None)

    def _refresh_project_list(self) -> None:
        self._refresh_project_tree()

    def _refresh_session_list(self) -> None:
        self._refresh_project_tree()

    def _refresh_project_tree(self) -> None:
        children = self.project_tree.get_children()
        if children:
            self.project_tree.delete(*children)
        if hasattr(self, "sidebar_workspace"):
            self.sidebar_workspace.configure(text=self.workspace.name or str(self.workspace))
            self.sidebar_project_path.configure(text=str(self.workspace))
        selected_iid = ""
        for index, project in enumerate(self.projects):
            project_iid = f"project-{index}"
            self.project_tree.insert(
                "",
                "end",
                iid=project_iid,
                text=project["name"],
                tags=("project",),
                open=True,
            )
            records = sorted(
                [item for item in self.sessions if item["workspace"] == project["path"]],
                key=lambda item: item["updated_at"],
                reverse=True,
            )
            for record in records:
                session_iid = f"session-{record['id']}"
                is_current = record["id"] == self.current_session_id
                is_busy = bool(self.runtimes.get(record["id"], {}).get("busy"))
                tag = "current" if is_current else "busy" if is_busy else "idle"
                self.project_tree.insert(
                    project_iid,
                    "end",
                    iid=session_iid,
                    text=record["title"],
                    tags=(tag,),
                )
                if record["id"] == self.current_session_id:
                    selected_iid = session_iid
        if selected_iid:
            self.project_tree.selection_set(selected_iid)
            self.project_tree.see(selected_iid)

    def _project_path_from_item(self, item_id: str) -> str:
        """Resolve a project path from either a project or Session tree row."""
        if item_id.startswith("session-"):
            item_id = self.project_tree.parent(item_id)
        if not item_id.startswith("project-"):
            return ""
        try:
            return self.projects[int(item_id.removeprefix("project-"))]["path"]
        except (ValueError, IndexError):
            return ""

    def _show_tree_context_menu(self, event: tk.Event[Any]) -> str | None:
        item_id = self.project_tree.identify_row(event.y)
        if not item_id:
            return None
        self.project_tree.selection_set(item_id)
        self.project_tree.focus(item_id)
        self.tree_context_menu.delete(0, "end")
        if item_id.startswith("project-"):
            project_path = self._project_path_from_item(item_id)
            self.tree_context_menu.add_command(
                label="移除项目（不删除磁盘文件）",
                command=lambda path=project_path: self._remove_project(path),
            )
        elif item_id.startswith("session-"):
            session_id = item_id.removeprefix("session-")
            self.tree_context_menu.add_command(
                label="删除 Session",
                command=lambda record_id=session_id: self._delete_session(record_id),
            )
        else:
            return None
        self.tree_context_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def delete_selected_tree_item(self) -> None:
        """Delete the selected Session or remove a project from the sidebar."""
        selection = self.project_tree.selection()
        if not selection:
            messagebox.showinfo("删除", "请先选择一个项目或 Session。", parent=self.root)
            return
        item_id = selection[0]
        if item_id.startswith("session-"):
            self._delete_session(item_id.removeprefix("session-"))
        elif item_id.startswith("project-"):
            self._remove_project(self._project_path_from_item(item_id))

    def _delete_session(self, session_id: str) -> None:
        record = next((item for item in self.sessions if item["id"] == session_id), None)
        if record is None:
            return
        if self._runtime(session_id)["busy"]:
            messagebox.showinfo(
                "任务执行中",
                "该 Session 正在运行任务，请先停止或等待其完成后再删除。",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "删除 Session",
            f"确定删除 Session“{record['title']}”吗？\n聊天记录会从 AI Harness 中移除。",
            parent=self.root,
        ):
            return
        was_current = session_id == self.current_session_id
        self.sessions = [item for item in self.sessions if item["id"] != session_id]
        self.runtimes.pop(session_id, None)
        if was_current:
            remaining = [
                item for item in self.sessions if item["workspace"] == record["workspace"]
            ]
            if remaining:
                latest = max(remaining, key=lambda item: item["updated_at"])
                self.current_session_id = latest["id"]
            else:
                self.workspace = Path(record["workspace"]).resolve()
                replacement = self._new_session_record()
                self.sessions.append(replacement)
                self.current_session_id = replacement["id"]
            self._render_current_session()
        self._refresh_session_list()
        self._save_state()

    def _remove_project(self, project_path: str) -> None:
        project = next(
            (item for item in self.projects if item["path"] == project_path),
            None,
        )
        if project is None:
            return
        if len(self.projects) <= 1:
            messagebox.showinfo(
                "移除项目",
                "至少需要保留一个项目。你可以先添加其他项目后再移除。",
                parent=self.root,
            )
            return
        project_sessions = [
            item for item in self.sessions if item["workspace"] == project_path
        ]
        if any(self._runtime(item["id"])["busy"] for item in project_sessions):
            messagebox.showinfo(
                "任务执行中",
                "该项目下仍有 Session 正在运行，请先停止或等待其完成后再移除项目。",
                parent=self.root,
            )
            return
        session_count = len(project_sessions)
        if not messagebox.askyesno(
            "移除项目",
            (
                f"确定从 AI Harness 中移除项目“{project['name']}”吗？\n"
                f"同时移除其 {session_count} 个 Session，但不会删除磁盘上的项目文件。"
            ),
            parent=self.root,
        ):
            return

        removing_current = str(self.workspace) == project_path
        self.projects = [item for item in self.projects if item["path"] != project_path]
        self.sessions = [
            item for item in self.sessions if item["workspace"] != project_path
        ]
        for item in project_sessions:
            self.runtimes.pop(item["id"], None)
        if removing_current:
            target = self.projects[0]
            self.workspace = Path(target["path"]).resolve()
            matching = [
                item for item in self.sessions if item["workspace"] == target["path"]
            ]
            if matching:
                self.current_session_id = max(
                    matching, key=lambda item: item["updated_at"]
                )["id"]
            else:
                replacement = self._new_session_record()
                self.sessions.append(replacement)
                self.current_session_id = replacement["id"]
            self._render_current_session()
        self._refresh_project_tree()
        self._save_state()

    def _clear_project_drop_target(self) -> None:
        for item_id in self.project_tree.get_children(""):
            self.project_tree.item(item_id, tags=())
        self._drop_project_path = ""

    def _start_project_drag(self, event: tk.Event[Any]) -> str | None:
        item_id = self.project_tree.identify_row(event.y)
        if not item_id.startswith("project-"):
            self._drag_project_path = ""
            return None
        try:
            element = self.project_tree.identify("element", event.x, event.y)
        except tk.TclError:
            element = ""
        if "indicator" in element:
            return None
        self._drag_project_path = self._project_path_from_item(item_id)
        self._drag_start_y = event.y
        self._drag_moved = False
        return "break"

    def _drag_project(self, event: tk.Event[Any]) -> str | None:
        if not self._drag_project_path:
            return None
        if abs(event.y - self._drag_start_y) >= 4:
            self._drag_moved = True
        if not self._drag_moved:
            return "break"
        item_id = self.project_tree.identify_row(event.y)
        target_path = self._project_path_from_item(item_id)
        if target_path == self._drop_project_path:
            return "break"
        self._clear_project_drop_target()
        if target_path:
            project_item = item_id
            if item_id.startswith("session-"):
                project_item = self.project_tree.parent(item_id)
            self.project_tree.item(project_item, tags=("drop-target",))
            self._drop_project_path = target_path
        return "break"

    def _finish_project_drag(self, event: tk.Event[Any]) -> str | None:
        source_path = self._drag_project_path
        if not source_path:
            return None
        item_id = self.project_tree.identify_row(event.y)
        target_path = self._project_path_from_item(item_id)
        moved = self._drag_moved
        self._drag_project_path = ""
        self._drag_moved = False
        self._clear_project_drop_target()

        if moved and target_path and target_path != source_path:
            if self._move_project(source_path, target_path):
                self._refresh_project_tree()
                self._save_state()
        else:
            project_item = next(
                (
                    f"project-{index}"
                    for index, item in enumerate(self.projects)
                    if item["path"] == source_path
                ),
                "",
            )
            if project_item:
                self.project_tree.selection_set(project_item)
                self.project_tree.focus(project_item)
        return "break"

    def _move_project(self, source_path: str, target_path: str) -> bool:
        """Move one project to the target row and preserve the new list order."""
        if source_path == target_path:
            return False
        source_index = next(
            (
                index for index, item in enumerate(self.projects)
                if item["path"] == source_path
            ),
            None,
        )
        target_index = next(
            (
                index for index, item in enumerate(self.projects)
                if item["path"] == target_path
            ),
            None,
        )
        if source_index is None or target_index is None:
            return False
        project = self.projects.pop(source_index)
        self.projects.insert(target_index, project)
        return True

    def add_project(self) -> None:
        selected = filedialog.askdirectory(initialdir=str(self.workspace), title="添加项目")
        if not selected:
            return
        path = str(Path(selected).resolve())
        if not any(project["path"] == path for project in self.projects):
            self.projects.append({"name": Path(path).name or path, "path": path})
        self._switch_project(path)

    def choose_workspace(self) -> None:
        self.add_project()

    def _select_tree_item(self, _event: tk.Event[Any]) -> None:
        selection = self.project_tree.selection()
        if not selection:
            return
        item_id = selection[0]
        if item_id.startswith("session-"):
            session_id = item_id.removeprefix("session-")
            if session_id != self.current_session_id:
                self._switch_session(session_id)
            return
        if item_id.startswith("project-"):
            try:
                project = self.projects[int(item_id.removeprefix("project-"))]
            except (ValueError, IndexError):
                return
            if project["path"] != str(self.workspace):
                self._switch_project(project["path"])

    def _switch_project(self, path: str) -> None:
        self._save_state()
        self.workspace = Path(path).resolve()
        matching = [record for record in self.sessions if record["workspace"] == path]
        if matching:
            self.current_session_id = max(matching, key=lambda record: record["updated_at"])["id"]
        else:
            record = self._new_session_record()
            self.sessions.append(record)
            self.current_session_id = record["id"]
        self._refresh_project_list()
        self._refresh_session_list()
        self._render_current_session()
        self._save_state()

    def _switch_session(self, session_id: str) -> None:
        self._save_state()
        self.current_session_id = session_id
        record = self._current_record()
        self.workspace = Path(record["workspace"]).resolve()
        self._refresh_project_list()
        self._refresh_session_list()
        self._render_current_session()
        self._save_state()

    def new_conversation(self) -> None:
        self._save_state()
        record = self._new_session_record()
        self.sessions.append(record)
        self.current_session_id = record["id"]
        self._refresh_session_list()
        self._render_current_session()
        self._save_state()
        self.prompt.focus_set()

    def _history_attachment_paths(self, item: dict[str, Any]) -> list[Path]:
        """Return stored image paths and recover old clipboard-only records."""
        stored = item.get("attachments")
        paths = _normalize_attachment_paths(
            stored if isinstance(stored, (list, tuple, str, Path)) else None
        )
        if paths:
            return paths

        body = str(item.get("body", ""))
        marker = "\n\n附件："
        if marker not in body:
            return []
        names = body.rsplit(marker, 1)[1].split("、")
        recovered: list[Path] = []
        for raw_name in names:
            name = Path(raw_name.strip()).name
            if not name or Path(name).suffix.lower() not in IMAGE_ATTACHMENT_SUFFIXES:
                continue
            candidate = (self.attachments_dir / name).resolve()
            if candidate.is_file() and candidate not in recovered:
                recovered.append(candidate)
        return recovered

    def _render_current_session(self) -> None:
        record = self._current_record()
        self._clear_mousewheel_queue(self.chat_canvas)
        for child in self.chat_inner.winfo_children():
            child.destroy()
        self._body_labels.clear()
        self._card_wrappers.clear()
        self._active_tool_cards.clear()
        self._chat_image_references = []
        self._remote_image_widgets = {}
        self._rendering_history = True
        all_items = record["items"]
        items = [item for item in all_items if self._should_render_history_item(item)]
        if len(items) > MAX_RENDERED_HISTORY_ITEMS:
            self._add_card(
                "tool",
                "历史记录已折叠",
                f"当前 Session 共 {len(items)} 条可见过程记录，启动时显示最近 {MAX_RENDERED_HISTORY_ITEMS} 条。完整记录仍保存在本地会话文件中。",
                save=False,
            )
            items = items[-MAX_RENDERED_HISTORY_ITEMS:]
        if items:
            for item in items:
                role = item.get("role", "assistant")
                self._add_card(
                    role,
                    item.get("title", "AI Harness"),
                    item.get("body", ""),
                    attachments=self._history_attachment_paths(item),
                    prompt_text=self._prompt_text_from_item(item) if role == "user" else None,
                    created_at=item.get("created_at"),
                    save=False,
                )
        else:
            self._show_welcome()
        self._rendering_history = False
        runtime = self._runtime(record["id"])
        if runtime["busy"] and runtime.get("active_process_title"):
            card = self._add_card(
                "tool",
                runtime["active_process_title"],
                runtime.get("active_process_body") or runtime.get("progress", ""),
                save=False,
                session_id=record["id"],
            )
            if card is not None:
                card["process_base_body"] = runtime.get("active_process_base_body", "")
                self._active_tool_cards[record["id"]] = card
        self.header_title.configure(text=record["title"])
        self.header_breadcrumb.configure(
            text=f"{self.workspace.name}   ·   Session {record['id'][:6]}"
        )
        self._refresh_composer_context()
        self._refresh_composer_state()
        self._refresh_session_status()
        self._bind_mousewheel_tree(self.chat_inner)

    @staticmethod
    def _should_render_history_item(item: dict[str, Any]) -> bool:
        """Filter internal tool transcript rows from the user-facing timeline."""
        if item.get("visible") is False:
            return False
        if item.get("role") == "tool":
            return str(item.get("title", "")) in PROCESS_TITLES
        return True

    def _show_welcome(self) -> None:
        hero = tk.Frame(self.chat_inner, bg=COLORS["app"])
        hero.pack(fill="x", padx=self._chat_side_padding(), pady=(152, 12))
        tk.Label(
            hero,
            text="AI Harness",
            bg=COLORS["app"],
            fg=COLORS["text"],
            font=(UI_FONT, 23, "bold"),
        ).pack(anchor="center")
        tk.Label(
            hero,
            text="探索项目新可能",
            bg=COLORS["app"],
            fg=COLORS["muted"],
            font=(UI_FONT, 11),
        ).pack(anchor="center", pady=(7, 0))
        tk.Label(
            hero,
            text=f"{self.workspace.name}  ·  本地工作区",
            bg=COLORS["app"],
            fg=COLORS["subtle"],
            font=(UI_FONT, 8),
        ).pack(anchor="center", pady=(8, 0))
        self._add_suggestions()

    def _add_suggestions(self) -> None:
        row = tk.Frame(self.chat_inner, bg=COLORS["app"])
        row.pack(fill="x", padx=self._chat_side_padding(), pady=(0, 20))
        suggestions = (
            ("理解项目", "检查结构并说明关键模块"),
            ("修复问题", "定位 Bug 并给出可靠修复"),
            ("运行验证", "执行测试并总结结果"),
        )
        for title, task in suggestions:
            button = ttk.Button(
                row,
                text=title,
                command=lambda value=task: self._use_suggestion(value),
                style="Suggestion.TButton",
            )
            button.pack(side="left", padx=5)

    def _use_suggestion(self, text: str) -> None:
        self._clear_prompt_placeholder()
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", text)
        self.prompt.focus_set()

    @staticmethod
    def _prompt_text_from_item(item: dict[str, Any]) -> str:
        """Return the original user prompt, excluding the attachment summary."""
        saved_prompt = item.get("prompt")
        if saved_prompt is not None:
            return str(saved_prompt)
        body = str(item.get("body", ""))
        # Older state files stored the visible attachment names in ``body``
        # but did not have a separate raw-prompt field yet.
        return body.split("\n\n附件：", 1)[0]

    @staticmethod
    def _format_prompt_time(created_at: Any) -> str:
        """Format a stored UTC timestamp like the compact chat action row."""
        raw_value = str(created_at or "").strip()
        if not raw_value:
            return ""
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return ""
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%H:%M")

    def _copy_prompt(self, text: str) -> None:
        """Copy one previously sent prompt to the system clipboard."""
        prompt_text = str(text)
        if not prompt_text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(prompt_text)
            # Keep the clipboard selection synchronized before showing the
            # status message, especially on Windows and X11.
            self.root.update()
        except tk.TclError:
            self._set_status("复制提示词失败", COLORS["danger"])
            return
        self._set_status("提示词已复制", COLORS["success"])

    def _edit_prompt(self, text: str) -> None:
        """Load a previous prompt into the composer without changing history."""
        if self._active_runtime()["busy"]:
            self._set_status("当前任务正在运行，请完成后再编辑", COLORS["warning"])
            return
        try:
            self._clear_prompt_placeholder()
            self.prompt.configure(state="normal")
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", str(text))
            self.prompt.mark_set("insert", "end-1c")
            self.prompt.focus_set()
        except tk.TclError:
            self._set_status("载入提示词失败", COLORS["danger"])
            return
        self._set_status("提示词已载入，可修改后发送", COLORS["accent"])

    def _bind_prompt_actions_hover(self, widget: tk.Misc, card: dict[str, Any]) -> None:
        """Keep a user card's copy/edit row visible while the pointer is over it."""
        widget.bind(
            "<Enter>",
            lambda _event, target=card: self._show_prompt_actions(target),
            add="+",
        )
        widget.bind(
            "<Leave>",
            lambda _event, target=card: self._schedule_hide_prompt_actions(target),
            add="+",
        )
        for child in widget.winfo_children():
            self._bind_prompt_actions_hover(child, card)

    def _cancel_prompt_actions_hide(self, card: dict[str, Any]) -> None:
        after_id = card.get("prompt_actions_hide_after")
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except tk.TclError:
            pass
        card["prompt_actions_hide_after"] = None

    def _show_prompt_actions(self, card: dict[str, Any]) -> None:
        actions = card.get("prompt_actions")
        if not isinstance(actions, tk.Misc):
            return
        self._cancel_prompt_actions_hide(card)
        try:
            if not actions.winfo_ismapped():
                actions.pack(fill="x", padx=12, pady=(0, 8))
            self._schedule_card_wrap_refresh()
        except tk.TclError:
            return

    def _schedule_hide_prompt_actions(self, card: dict[str, Any]) -> None:
        self._cancel_prompt_actions_hide(card)
        try:
            card["prompt_actions_hide_after"] = self.root.after(
                90,
                self._hide_prompt_actions,
                card,
            )
        except tk.TclError:
            card["prompt_actions_hide_after"] = None

    def _hide_prompt_actions(self, card: dict[str, Any]) -> None:
        card["prompt_actions_hide_after"] = None
        bubble = card.get("prompt_bubble")
        actions = card.get("prompt_actions")
        if not isinstance(bubble, tk.Misc) or not isinstance(actions, tk.Misc):
            return
        try:
            pointer = self.root.winfo_containing(
                self.root.winfo_pointerx(),
                self.root.winfo_pointery(),
            )
            if pointer is not None and self._widget_is_inside(pointer, bubble):
                return
            if actions.winfo_ismapped():
                actions.pack_forget()
                self._schedule_card_wrap_refresh()
        except tk.TclError:
            return

    def _chat_side_padding(self) -> int:
        """Center the conversation in a stable reading column as the window resizes."""
        canvas_width = self.chat_canvas.winfo_width()
        if canvas_width <= 1:
            return CHAT_MIN_SIDE_PADDING
        return max(CHAT_MIN_SIDE_PADDING, (canvas_width - CHAT_MAX_WIDTH) // 2)

    def _initial_card_wraplength(self, role: str) -> int:
        """Choose a safe first wrap width before Tk has laid out the card."""
        canvas_width = self.chat_canvas.winfo_width()
        if canvas_width <= 1:
            return 420
        available = max(240, min(CHAT_MAX_WIDTH - 36, canvas_width - 2 * self._chat_side_padding() - 36))
        if role == "user":
            return max(220, min(680, available - 70))
        return max(260, available)

    def _selectable_body_width(self, role: str, body: str) -> int:
        """Return a reasonable initial Text width for a chat card.

        Assistant cards fill the reading column, while user bubbles keep
        their compact, content-sized appearance. Text widgets express their
        requested width in average characters, so only the latter needs an
        explicit width before geometry has been calculated.
        """
        if role != "user":
            return 1
        max_chars = max(20, self._initial_card_wraplength(role) // 10)
        longest_line = max((len(line) for line in body.splitlines()), default=1)
        return min(max_chars, max(20, longest_line))

    @staticmethod
    def _set_selectable_body_text(widget: tk.Text, text: str) -> None:
        """Replace the contents of a read-only chat body safely."""
        current_state = str(widget.cget("state"))
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", str(text))
        widget.configure(state=current_state)

    @staticmethod
    def _selectable_body_line_count(widget: tk.Text) -> int:
        """Count wrapped display lines so a Text body never gets an inner scrollbar."""
        # tkinter.Text.count adds the leading dash itself. Passing
        # ``-displaylines`` produces the invalid Tcl option
        # ``--displaylines`` and leaves every body at its initial one-line
        # height. ``update`` also forces wrapped-line metrics to be current.
        count = widget.count("1.0", "end-1c", "update", "displaylines")
        if isinstance(count, (tuple, list)):
            count = count[0] if count else 0
        try:
            return max(1, int(count or 0))
        except (TypeError, ValueError):
            return 1

    def _schedule_card_wrap_refresh(self) -> None:
        if self._wrap_refresh_scheduled:
            return
        self._wrap_refresh_scheduled = True
        self.root.after_idle(self._refresh_card_wraps)

    def _refresh_card_wraps(self) -> None:
        self._wrap_refresh_scheduled = False
        try:
            self.chat_inner.update_idletasks()
            for body_widget in self._body_labels:
                if not body_widget.winfo_exists():
                    continue
                body_widget.configure(
                    height=self._selectable_body_line_count(body_widget)
                )
                content = body_widget.master
                bubble = getattr(content, "master", None)
                if isinstance(bubble, _PillBubble):
                    bubble._schedule_content_sync()
            side_padding = self._chat_side_padding()
            for wrapper in self._card_wrappers:
                if wrapper.winfo_exists():
                    wrapper.pack_configure(padx=side_padding)
        except tk.TclError:
            pass

    def _start_remote_image_load(self, url: str) -> None:
        """Fetch one public search image without blocking the Tk event loop."""
        if (
            url in self._remote_image_cache
            or url in self._remote_image_failures
            or url in self._remote_image_loading
        ):
            return
        self._remote_image_loading.add(url)

        def worker() -> None:
            image: Image.Image | None = None
            error = ""
            try:
                image = _download_remote_image(url)
            except (HTTPError, URLError, OSError, ValueError, TypeError) as exc:
                error = str(exc) or exc.__class__.__name__
            except Exception as exc:  # pragma: no cover - defensive CDN/parser guard
                error = str(exc) or exc.__class__.__name__
            self.event_queue.put(
                (
                    "remote_image",
                    {"url": url, "image": image, "error": error},
                )
            )

        threading.Thread(target=worker, name="harness-image-loader", daemon=True).start()

    def _open_remote_image(self, url: str) -> None:
        try:
            webbrowser.open(url)
        except OSError as exc:
            self._write_gui_error(f"打开搜索图片失败：{exc}")

    def _handle_remote_image_event(self, message: dict[str, Any]) -> None:
        """Apply a background image result on the Tk thread."""
        url = str(message.get("url", ""))
        if not url:
            return
        self._remote_image_loading.discard(url)
        image = message.get("image")
        error = str(message.get("error", ""))
        if isinstance(image, Image.Image):
            self._remote_image_cache[url] = image
        else:
            self._remote_image_failures[url] = error or "图片无法读取"

        widgets = self._remote_image_widgets.pop(url, [])
        if not widgets:
            return
        photo: ImageTk.PhotoImage | None = None
        if isinstance(image, Image.Image):
            try:
                photo = ImageTk.PhotoImage(image, master=self.root)
            except tk.TclError:
                return
            self._chat_image_references.append(photo)

        for widget in widgets:
            try:
                if photo is not None:
                    widget.configure(image=photo, text="", width=0, height=0)
                    widget.image = photo
                else:
                    widget.configure(
                        image="",
                        text="图片加载失败（点击打开原图）",
                        fg=COLORS["muted"],
                        padx=10,
                        pady=7,
                    )
            except tk.TclError:
                continue
        self._schedule_card_wrap_refresh()
        self._schedule_chat_to_bottom()

    def _toggle_process_card(self, card: dict[str, Any]) -> str:
        """Expand or collapse one Think/Search/Pwsh row in place."""
        if not card.get("process"):
            return "break"
        body_label = card.get("body_label")
        preview_label = card.get("preview_label")
        indicator = card.get("process_indicator")
        if not isinstance(body_label, tk.Misc) or not isinstance(preview_label, tk.Misc):
            return "break"
        expanded = not bool(card.get("expanded"))
        card["expanded"] = expanded
        image_column = card.get("process_image_column")
        try:
            if expanded:
                preview_label.configure(text="")
                body_label.pack(fill="x", padx=32, pady=(0, 9))
                if isinstance(image_column, tk.Misc):
                    image_column.pack(fill="x", padx=15, pady=(0, 9))
                if isinstance(indicator, tk.Misc):
                    indicator.configure(text="⌄")
            else:
                body_label.pack_forget()
                if isinstance(image_column, tk.Misc):
                    image_column.pack_forget()
                preview_label.configure(
                    text=_compact_process_preview(card.get("display_body", ""))
                )
                if isinstance(indicator, tk.Misc):
                    indicator.configure(text="›")
            self._schedule_card_wrap_refresh()
            self._schedule_chat_to_bottom()
        except tk.TclError:
            return "break"
        return "break"

    @staticmethod
    def _event_text(message: Any) -> tuple[str, str]:
        """Extract the session id and text from a queued worker event."""
        if isinstance(message, dict):
            return str(message.get("session_id", "")), str(message.get("message", ""))
        return "", str(message)

    @staticmethod
    def _tool_summary_parts(summary: str) -> tuple[str, dict[str, Any]]:
        """Parse the safe ``tool_start`` summary without trusting its payload."""
        name, separator, raw_arguments = str(summary).partition(" ")
        if not separator:
            return name.strip(), {}
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError):
            return name.strip(), {}
        return name.strip(), arguments if isinstance(arguments, dict) else {}

    @classmethod
    def _process_title_and_body(cls, summary: str) -> tuple[str, str] | None:
        """Map a tool start event to the compact public process timeline."""
        name, arguments = cls._tool_summary_parts(summary)
        if name == "browser_search":
            query = str(arguments.get("query", "")).strip() or str(summary).strip()
            return "Search", f"查询：{query}"
        if name == "run_command":
            command = str(arguments.get("command", "")).strip() or str(summary).strip()
            return "Pwsh", f"命令：\n{command}"
        return None

    def _add_card(
        self,
        role: str,
        title: str,
        body: str,
        *,
        attachments: Sequence[str | Path] | None = None,
        prompt_text: str | None = None,
        created_at: str | None = None,
        save: bool = True,
        session_id: str | None = None,
        visible: bool = True,
    ) -> dict[str, Any] | None:
        target_id = session_id or self.current_session_id
        title = str(title)
        body = str(body)
        if role == "assistant":
            # Also clean assistant cards loaded from sessions created before
            # the reasoning/final-answer separation was added.
            body = _remove_visible_reasoning_blocks(body)
        attachment_paths = _normalize_attachment_paths(attachments)
        prompt_created_at = created_at
        if role == "user" and not prompt_created_at and save and not self._rendering_history:
            prompt_created_at = self._now()
        record: dict[str, Any] | None = None
        if save and not self._rendering_history:
            record = self._session_record(target_id)
            item: dict[str, Any] = {"role": role, "title": title, "body": body}
            if not visible:
                item["visible"] = False
            if attachment_paths:
                item["attachments"] = [str(path) for path in attachment_paths]
            if role == "user":
                item["prompt"] = str(prompt_text if prompt_text is not None else body)
                if prompt_created_at:
                    item["created_at"] = str(prompt_created_at)
            record["items"].append(item)
            record["updated_at"] = self._now()
            self._save_state()
        if not visible or target_id != self.current_session_id:
            return None

        display_body = _remove_remote_image_markdown(body)
        remote_image_refs = _extract_remote_image_refs(body)
        is_process = role == "tool" and title in PROCESS_TITLES
        chat_image_items: list[ImageTk.PhotoImage] = []
        if role == "user":
            for path in attachment_paths:
                if path.suffix.lower() not in IMAGE_ATTACHMENT_SUFFIXES:
                    continue
                image = _load_attachment_image(path, CHAT_IMAGE_MAX_SIZE)
                if image is None:
                    continue
                try:
                    photo = ImageTk.PhotoImage(image, master=self.root)
                except tk.TclError:
                    continue
                self._chat_image_references.append(photo)
                chat_image_items.append(photo)

        wrapper = tk.Frame(self.chat_inner, bg=COLORS["app"])
        wrapper.pack(fill="x", padx=self._chat_side_padding(), pady=(12, 5))
        self._card_wrappers.append(wrapper)
        if role == "user":
            bubble = _PillBubble(
                wrapper,
                bg=COLORS["user_bubble"],
                outer_bg=COLORS["app"],
            )
            bubble.pack(anchor="e", padx=(220, 0))
            bubble_content = bubble.content
            bubble_bg = COLORS["user_bubble"]
            title_color = COLORS["text"]
        elif is_process:
            bubble = tk.Frame(
                wrapper,
                bg=COLORS["app"],
                highlightthickness=0,
            )
            bubble.pack(anchor="w", fill="x")
            bubble_content = bubble
            bubble_bg = str(bubble["bg"])
            title_color = COLORS["muted"]
        elif role == "tool":
            bubble = tk.Frame(
                wrapper,
                bg=COLORS["tool"],
                highlightthickness=1,
                highlightbackground=COLORS["border"],
            )
            bubble.pack(anchor="w", fill="x")
            bubble_content = bubble
            bubble_bg = str(bubble["bg"])
            title_color = COLORS["warning"]
        else:
            bubble = tk.Frame(
                wrapper,
                bg=COLORS["assistant_bubble"],
                highlightthickness=0,
            )
            bubble.pack(anchor="w", fill="x")
            bubble_content = bubble
            bubble_bg = str(bubble["bg"])
            title_color = COLORS["text"]
        header: tk.Frame | None = None
        process_indicator: tk.Label | None = None
        preview_label: tk.Label | None = None
        if is_process:
            header = tk.Frame(bubble_content, bg=bubble_bg, cursor="hand2")
            header.pack(fill="x", padx=0, pady=(4, 6))
            process_indicator = tk.Label(
                header,
                text="›",
                bg=bubble_bg,
                fg=COLORS["subtle"],
                font=(UI_FONT, 11),
                width=2,
                anchor="w",
                cursor="hand2",
            )
            process_indicator.pack(side="left")
            tk.Label(
                header,
                text=PROCESS_ICONS.get(title, "·"),
                bg=bubble_bg,
                fg=COLORS["subtle"],
                font=(UI_FONT, 10),
                width=2,
                anchor="w",
                cursor="hand2",
            ).pack(side="left")
            tk.Label(
                header,
                text=title,
                bg=bubble_bg,
                fg=title_color,
                font=(UI_FONT, 9, "bold"),
                anchor="w",
                cursor="hand2",
            ).pack(side="left")
            tk.Label(
                header,
                text="·",
                bg=bubble_bg,
                fg=COLORS["subtle"],
                font=(UI_FONT, 9),
                padx=7,
                anchor="w",
                cursor="hand2",
            ).pack(side="left")
            preview_label = tk.Label(
                header,
                text=_compact_process_preview(display_body),
                bg=bubble_bg,
                fg=COLORS["muted"],
                font=(UI_FONT, 9),
                anchor="w",
                cursor="hand2",
            )
            preview_label.pack(side="left", fill="x", expand=True)
        elif role != "user":
            header = tk.Frame(bubble_content, bg=bubble_bg)
            header.pack(fill="x", padx=16, pady=(12, 6))
            tk.Label(
                header,
                text=title,
                bg=bubble_bg,
                fg=title_color,
                font=(UI_FONT, 9, "bold"),
                anchor="w",
            ).pack(side="left")
        body_label = tk.Text(
            bubble_content,
            bg=bubble_bg,
            fg=COLORS["text"] if role != "tool" else COLORS["muted"],
            wrap="word",
            font=(UI_FONT, 10),
            width=self._selectable_body_width(role, display_body),
            height=1,
            padx=20 if role == "user" else 17,
            pady=12 if role == "user" else 0,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            insertwidth=0,
            cursor="arrow",
            takefocus=False,
            exportselection=False,
            selectbackground="#dfe7fb",
            selectforeground=COLORS["text"],
            undo=False,
            autoseparators=False,
        )
        body_label.insert("1.0", display_body)
        # Keep the normal Text class bindings for mouse selection and Ctrl/Cmd-C,
        # while disabling all edits to the transcript itself.
        body_label.configure(state="disabled")
        has_images = bool(chat_image_items or remote_image_refs)
        image_column: tk.Frame | None = None
        if is_process:
            body_label.pack_forget()
        else:
            body_label.pack(
                fill="x",
                pady=(0, 7 if has_images else (0 if role == "user" else 13)),
            )
        self._body_labels.append(body_label)
        if has_images:
            image_column = tk.Frame(bubble_content, bg=bubble_bg)
            image_column.pack(fill="x", padx=15, pady=(0, 13))
            for photo in chat_image_items:
                tk.Label(
                    image_column,
                    image=photo,
                    bg=COLORS["app"],
                    highlightthickness=1,
                    highlightbackground="#31598a",
                ).pack(anchor="e", pady=(0, 7))
            for alt, url in remote_image_refs:
                cached_image = self._remote_image_cache.get(url)
                remote_label = tk.Label(
                    image_column,
                    bg=COLORS["app"],
                    fg=COLORS["muted"],
                    highlightthickness=1,
                    highlightbackground="#31598a",
                    anchor="e",
                    cursor="hand2",
                    text=(
                        "图片加载失败（点击打开原图）"
                        if url in self._remote_image_failures
                        else f"正在加载：{alt}…"
                    ),
                )
                remote_label.pack(anchor="e", pady=(0, 7))
                remote_label.bind(
                    "<Button-1>",
                    lambda _event, target=url: self._open_remote_image(target),
                )
                self._remote_image_widgets.setdefault(url, []).append(remote_label)
                if cached_image is not None:
                    try:
                        photo = ImageTk.PhotoImage(cached_image, master=self.root)
                    except tk.TclError:
                        photo = None
                    if photo is not None:
                        self._chat_image_references.append(photo)
                        remote_label.configure(image=photo, text="", width=0, height=0)
                        remote_label.image = photo
                else:
                    self._start_remote_image_load(url)
            if is_process:
                image_column.pack_forget()
        card: dict[str, Any] = {
            "body_label": body_label,
            "record": record,
            "record_item": record["items"][-1] if record and record.get("items") else None,
            "body": body,
            "display_body": display_body,
            "attachments": attachment_paths,
            "image_count": len(chat_image_items) + len(remote_image_refs),
            "remote_image_count": len(remote_image_refs),
            "process": is_process,
            "expanded": False,
            "preview_label": preview_label,
            "process_indicator": process_indicator,
            "process_image_column": image_column,
            "process_base_body": display_body,
        }
        if is_process:
            # Toggling belongs to the compact process header. Leaving the
            # body out of this binding is important: the body is selectable.
            for widget in header.winfo_children() + [header]:
                widget.bind(
                    "<Button-1>",
                    lambda _event, target=card: self._toggle_process_card(target),
                    add="+",
                )
        if role == "user":
            user_prompt = str(prompt_text if prompt_text is not None else body)
            prompt_actions = tk.Frame(bubble_content, bg=bubble_bg)
            button_options = {
                "bg": bubble_bg,
                "fg": COLORS["subtle"],
                "activebackground": COLORS["panel_hover"],
                "activeforeground": COLORS["text"],
                "relief": "flat",
                "borderwidth": 0,
                "highlightthickness": 0,
                "font": (UI_FONT, 8),
                "cursor": "hand2",
                "takefocus": False,
                "padx": 6,
                "pady": 2,
            }
            tk.Button(
                prompt_actions,
                text="修改",
                command=lambda value=user_prompt: self._edit_prompt(value),
                **button_options,
            ).pack(side="right", padx=(0, 2), pady=1)
            tk.Button(
                prompt_actions,
                text="复制",
                command=lambda value=user_prompt: self._copy_prompt(value),
                **button_options,
            ).pack(side="right", padx=(0, 2), pady=1)
            timestamp = self._format_prompt_time(prompt_created_at)
            if timestamp:
                tk.Label(
                    prompt_actions,
                    text=timestamp,
                    bg=bubble_bg,
                    fg=COLORS["subtle"],
                    font=(UI_FONT, 8),
                    padx=5,
                    pady=2,
                ).pack(side="right", padx=(0, 3), pady=1)
            prompt_actions.pack(fill="x", padx=12, pady=(0, 8))
            prompt_actions.pack_forget()
            card["prompt_actions"] = prompt_actions
            card["prompt_bubble"] = bubble
            card["prompt_actions_hide_after"] = None
            self._bind_prompt_actions_hover(bubble, card)
        if not self._rendering_history:
            self._bind_mousewheel_tree(wrapper)
        self._schedule_card_wrap_refresh()
        self._schedule_chat_to_bottom()
        return card

    def _update_tool_progress(self, session_id: str, message: str) -> None:
        """Update the currently visible Pwsh row without adding another card."""
        runtime = self._runtime(session_id)
        runtime["progress"] = message
        title = str(runtime.get("active_process_title", ""))
        if title != "Pwsh":
            return
        base_body = str(runtime.get("active_process_base_body", "")).rstrip()
        body = f"{base_body}\n\n进度：\n{message}" if base_body else str(message)
        self._set_process_body(session_id, body)

    def _set_process_body(self, session_id: str, body: str) -> None:
        """Update both the in-memory history item and the visible process row."""
        runtime = self._runtime(session_id)
        runtime["active_process_body"] = body
        active_item = runtime.get("active_process_item")
        if isinstance(active_item, dict):
            active_item["body"] = body
            active_item["updated_at"] = self._now()
        if session_id != self.current_session_id:
            return
        card = self._active_tool_cards.get(session_id)
        if card is None:
            return
        body_label = card.get("body_label")
        if not isinstance(body_label, tk.Misc):
            return
        try:
            display_body = _remove_remote_image_markdown(body)
            self._set_selectable_body_text(body_label, display_body)
            card["body"] = body
            card["display_body"] = display_body
            preview_label = card.get("preview_label")
            if isinstance(preview_label, tk.Misc) and not card.get("expanded"):
                preview_label.configure(text=_compact_process_preview(display_body))
            self._schedule_card_wrap_refresh()
            self._schedule_chat_to_bottom()
        except tk.TclError:
            self._active_tool_cards.pop(session_id, None)

    def _finish_process(self, session_id: str, result: str) -> None:
        """Attach a complete Search/Pwsh result to its existing process row."""
        runtime = self._runtime(session_id)
        title = str(runtime.get("active_process_title", ""))
        if title not in PROCESS_TITLES:
            return
        base_body = str(runtime.get("active_process_base_body", "")).rstrip()
        result_text = str(result)
        body = (
            f"{base_body}\n\n结果：\n{result_text}"
            if base_body
            else result_text
        )
        self._set_process_body(session_id, body)
        self._save_state()
        runtime["progress"] = ""
        runtime["active_process_title"] = ""
        runtime["active_process_body"] = ""
        runtime["active_process_base_body"] = ""
        runtime["active_process_item"] = None
        if session_id == self.current_session_id:
            self._active_tool_cards.pop(session_id, None)

    def _scroll_chat_to_bottom(self) -> None:
        """Refresh the canvas bounds before revealing the newest message."""
        try:
            self.chat_inner.update_idletasks()
            bounds = self.chat_canvas.bbox("all")
            if bounds is not None:
                self.chat_canvas.configure(scrollregion=bounds)
            self.chat_canvas.yview_moveto(1.0)
        except tk.TclError:
            pass

    def _schedule_chat_to_bottom(self) -> None:
        """Follow new output until nested Text/bubble geometry has settled."""
        self._chat_autoscroll_token = getattr(self, "_chat_autoscroll_token", 0) + 1
        token = self._chat_autoscroll_token
        try:
            self.root.after_idle(self._settle_chat_to_bottom, token, 0)
        except tk.TclError:
            pass

    def _cancel_chat_autoscroll(self) -> None:
        """Let a manual wheel action take control from pending output follow-up."""
        self._chat_autoscroll_token = getattr(self, "_chat_autoscroll_token", 0) + 1

    def _settle_chat_to_bottom(self, token: int, attempt: int) -> None:
        """Repeat bottom alignment while asynchronous card sizes stabilize."""
        if token != getattr(self, "_chat_autoscroll_token", 0) or getattr(
            self, "closing", False
        ):
            return
        self._scroll_chat_to_bottom()
        delays = (40, 80, 160, 320)
        if attempt >= len(delays):
            return
        try:
            self.root.after(
                delays[attempt],
                self._settle_chat_to_bottom,
                token,
                attempt + 1,
            )
        except tk.TclError:
            pass

    def _ensure_session(self, session_id: str | None = None) -> AgentSession:
        target_id = session_id or self.current_session_id
        runtime = self._runtime(target_id)
        if runtime["session"] is None:
            record = self._session_record(target_id)
            workspace = Path(record["workspace"]).resolve()
            model = self.model_var.get().strip() or None
            session = AgentSession(
                max_turns=self.max_turns,
                workspace=workspace,
                approval_mode=self.permission_var.get(),
                full_access=self.permission_var.get() == "full-access",
                model_name=model,
                event_callback=lambda kind, message: self.event_queue.put(
                    (kind, {"session_id": target_id, "message": message})
                ),
                approver=lambda command, cwd: self._gui_approver(target_id, command, cwd),
            )
            saved_messages = record.get("messages", [])
            if saved_messages:
                session.messages.extend(saved_messages)
            repaired = session.repair_tool_call_history()
            self.model_name = model or session.model_name
            self._refresh_composer_context()
            runtime["session"] = session
            if repaired:
                runtime["history_repaired"] = repaired
                self._add_card(
                    "tool",
                    "会话恢复",
                    f"检测到 {repaired} 个未完成的工具调用，已补齐中断结果，可以继续运行。",
                    session_id=target_id,
                    visible=False,
                )
                self._save_state()
        return runtime["session"]

    def send_message(self) -> None:
        runtime = self._active_runtime()
        if runtime["busy"] and not runtime["paused"]:
            # Enter keeps the fast path useful while the model is working:
            # plain Enter steers the active task; Ctrl+Enter queues a new
            # follow-up (see ``_queue_from_event``).
            self.adjust_direction()
            return
        if runtime["busy"] and (
            runtime["stop_pending"]
            or (
                runtime["worker"] is not None
                and runtime["worker"].is_alive()
            )
        ):
            # The composer stays editable while cooperative cancellation is
            # finishing, but do not start a second worker until the stopped
            # worker has handed its result back to the Tk event loop.
            self._set_status("正在停止，请稍候", COLORS["warning"])
            return
        task, attachments = self._composer_message()
        if not task:
            return
        if task == "/clear":
            self.new_conversation()
            self._clear_composer_message()
            return
        if task == "/help":
            self._clear_composer_message()
            self._add_card("assistant", "帮助", "可用命令：/clear 新建 Session，/permissions 切换权限模式。")
            return
        if task.startswith("/permissions "):
            requested = task.split(maxsplit=1)[1].strip()
            if requested in PERMISSION_LABELS:
                self.permission_var.set(requested)
                self.permission_display_var.set(PERMISSION_LABELS[requested])
                self.change_permission()
            else:
                self._add_card("assistant", "权限模式", "可选：ask（请求批准）、auto（帮我批准）、full-access（完全访问）")
            self._clear_composer_message()
            return

        self._clear_composer_message()
        session_id = self.current_session_id
        runtime = self._runtime(session_id)
        record = self._session_record(session_id)
        first_exchange = not record["items"]
        display_text = task
        if attachments:
            display_text += "\n\n附件：" + "、".join(path.name for path in attachments)
        self._add_card(
            "user",
            "你",
            display_text,
            attachments=attachments,
            prompt_text=task,
            session_id=session_id,
        )
        self._refresh_session_list()
        self.pending_attachments = []
        self._refresh_attachment_bar()
        self._start_task(
            session_id,
            task,
            attachments,
            generate_title=first_exchange,
        )

    def _start_task(
        self,
        session_id: str,
        task: str,
        attachments: Sequence[Path] | None = None,
        *,
        generate_title: bool = False,
    ) -> None:
        """Start one model turn for a Session, including queued follow-ups."""
        runtime = self._runtime(session_id)
        if runtime.get("rebuild_session_after_busy") and runtime.get("session") is not None:
            # A connection change must not orphan a running worker. If the
            # previous turn is paused and the user starts a new turn instead
            # of resuming it, preserve its transcript before rebuilding the
            # client with the new connection settings.
            self._snapshot_agent_messages()
            runtime["session"] = None
            runtime["rebuild_session_after_busy"] = False
        runtime["busy"] = True
        runtime["paused"] = False
        runtime["stop_pending"] = False
        if session_id == self.current_session_id:
            self._refresh_composer_state()
        self._refresh_session_status()
        try:
            session = self._ensure_session(session_id)
        except Exception as exc:
            runtime["busy"] = False
            self._refresh_composer_state()
            self._refresh_session_status()
            self._add_card("assistant", "无法连接模型", str(exc), session_id=session_id)
            return
        runtime["worker"] = threading.Thread(
            target=self._run_task,
            args=(session, task, list(attachments or ()), session_id, generate_title),
            daemon=True,
        )
        runtime["worker"].start()

    def _composer_message(self) -> tuple[str, list[Path]]:
        """Read the current composer without treating its placeholder as text."""
        raw_text = self.prompt.get("1.0", "end").strip()
        if self._prompt_placeholder_visible:
            # Tests, accessibility tools, and clipboard automation can insert
            # text without first firing Tk's FocusIn callback.  In that case
            # remove only the placeholder suffix instead of discarding the
            # user's actual input.
            task = raw_text.replace(PROMPT_PLACEHOLDER, "").strip()
        else:
            task = raw_text
        attachments = list(self.pending_attachments)
        if not task and attachments:
            task = "请分析这些附件。"
        return task, attachments

    def _clear_composer_message(self) -> None:
        self.prompt.delete("1.0", "end")
        self._prompt_placeholder_visible = False
        self.prompt.configure(fg=COLORS["text"])

    @staticmethod
    def _display_task(task: str, attachments: Sequence[Path]) -> str:
        display_text = task
        if attachments:
            display_text += "\n\n附件：" + "、".join(path.name for path in attachments)
        return display_text

    def queue_message(self) -> None:
        """Queue a follow-up to start automatically after the active turn."""
        runtime = self._active_runtime()
        if not runtime["busy"] or runtime["paused"]:
            self.send_message()
            return
        task, attachments = self._composer_message()
        if not task:
            return
        self._clear_composer_message()
        runtime["follow_up_queue"].append(
            {"task": task, "attachments": list(attachments)}
        )
        self._add_card(
            "user",
            "已排队",
            self._display_task(task, attachments),
            attachments=attachments,
            prompt_text=task,
            session_id=self.current_session_id,
        )
        self.pending_attachments = []
        self._refresh_attachment_bar()
        count = len(runtime["follow_up_queue"])
        self._set_status(f"已加入队列（{count} 条）", COLORS["warning"])
        self._refresh_session_list()

    def adjust_direction(self) -> None:
        """Submit a live steering instruction to the active AgentSession."""
        runtime = self._active_runtime()
        if not runtime["busy"] or runtime["paused"]:
            self.send_message()
            return
        task, attachments = self._composer_message()
        if not task:
            return
        session = runtime.get("session")
        request_direction = getattr(session, "request_direction_change", None)
        if not callable(request_direction):
            request_direction = getattr(session, "steer", None)
        if not callable(request_direction):
            self._set_status("当前 Session 不支持调整方向", COLORS["danger"])
            return
        try:
            position = request_direction(task, attachments=attachments)
        except Exception as exc:
            self._set_status(f"方向调整失败：{exc}", COLORS["danger"])
            return
        if not position:
            return
        self._clear_composer_message()
        self._add_card(
            "user",
            "调整方向",
            self._display_task(task, attachments),
            attachments=attachments,
            prompt_text=task,
            session_id=self.current_session_id,
        )
        self.pending_attachments = []
        self._refresh_attachment_bar()
        runtime["direction_pending"] = int(runtime.get("direction_pending", 0)) + 1
        self._set_status("已收到方向调整，将在下一安全节点生效", COLORS["warning"])
        self._refresh_session_list()

    def _run_task(
        self,
        session: AgentSession,
        task: str,
        attachments: list[Path],
        session_id: str,
        generate_title: bool,
    ) -> None:
        try:
            answer = session.ask(task, attachments=attachments)
            rendered_answer = answer or "Agent 没有返回文字结果。"
            title = ""
            if generate_title:
                try:
                    title = session.generate_session_title(task, rendered_answer, max_chars=11)
                except Exception as exc:
                    self._write_gui_error(f"Session 标题生成失败：{exc}")
            self.event_queue.put(
                (
                    "answer",
                    {
                        "answer": rendered_answer,
                        "session_id": session_id,
                        "title": title,
                    },
                )
            )
        except AgentPaused:
            self.event_queue.put(
                ("paused", {"session_id": session_id, "message": "运行已停止"})
            )
        except Exception as exc:
            self.event_queue.put(
                ("error", {"session_id": session_id, "message": str(exc)})
            )

    def _run_resume(self, session: AgentSession, session_id: str) -> None:
        try:
            answer = session.resume()
            self.event_queue.put(
                (
                    "answer",
                    {
                        "answer": answer or "Agent 没有返回文字结果。",
                        "session_id": session_id,
                        "title": "",
                    },
                )
            )
        except AgentPaused:
            self.event_queue.put(
                ("paused", {"session_id": session_id, "message": "运行已停止"})
            )
        except Exception as exc:
            self.event_queue.put(
                ("error", {"session_id": session_id, "message": str(exc)})
            )

    def _drain_events(self) -> None:
        try:
            while True:
                kind, message = self.event_queue.get_nowait()
                if kind == "approval_request":
                    if self.active_approval is None:
                        self._show_approval_dialog(message)
                    else:
                        self.approval_queue.append(message)
                elif kind == "model_catalog":
                    self._model_catalog_loading = False
                    request_id = str(message.get("request_id", ""))
                    callback = self._model_catalog_callbacks.pop(request_id, None)
                    models = message.get("models", [])
                    if callback is not None and isinstance(models, list):
                        callback([str(model) for model in models])
                    self._set_status(
                        f"已加载 {len(models)} 个模型",
                        COLORS["success"],
                    )
                elif kind == "model_catalog_error":
                    self._model_catalog_loading = False
                    request_id = str(message.get("request_id", ""))
                    self._model_catalog_callbacks.pop(request_id, None)
                    self._set_status(
                        str(message.get("message", "获取模型列表失败")),
                        COLORS["danger"],
                    )
                elif kind == "remote_image":
                    if isinstance(message, dict):
                        self._handle_remote_image_event(message)
                elif kind == "think":
                    session_id, text = self._event_text(message)
                    if text:
                        self._add_card("tool", "Think", text, session_id=session_id)
                elif kind == "direction_applied":
                    session_id, text = self._event_text(message)
                    runtime = self._runtime(session_id)
                    runtime["direction_pending"] = max(
                        0, int(runtime.get("direction_pending", 0)) - 1
                    )
                    if text:
                        self._add_card(
                            "tool",
                            "方向已调整",
                            f"已按最新指示重新规划：{text}",
                            save=False,
                            session_id=session_id,
                        )
                    if session_id == self.current_session_id:
                        self._set_status("方向调整已生效", COLORS["success"])
                elif kind == "approval_review":
                    session_id, review_text = self._event_text(message)
                    runtime = self._runtime(session_id)
                    runtime["pending_review_message"] = (
                        review_text if review_text.startswith("需要确认") else ""
                    )
                    # Keep the reviewer decision in the local transcript for
                    # diagnostics, but do not add another visible process row.
                    self._add_card(
                        "tool",
                        "审批审查",
                        review_text,
                        session_id=session_id,
                        visible=False,
                    )
                elif kind == "tool_start":
                    session_id, text = self._event_text(message)
                    runtime = self._runtime(session_id)
                    self._active_tool_cards.pop(session_id, None)
                    runtime["progress"] = ""
                    runtime["active_process_title"] = ""
                    runtime["active_process_body"] = ""
                    runtime["active_process_base_body"] = ""
                    runtime["active_process_item"] = None
                    # Retain the old low-level event as hidden history so
                    # existing diagnostics and session files remain useful.
                    self._add_card(
                        "tool",
                        "执行工具",
                        text,
                        session_id=session_id,
                        visible=False,
                    )
                    process = self._process_title_and_body(text)
                    if process is not None:
                        title, body = process
                        runtime["active_process_title"] = title
                        runtime["active_process_body"] = body
                        runtime["active_process_base_body"] = body
                        record = self._session_record(session_id)
                        item_count = len(record["items"])
                        card = self._add_card(
                            "tool",
                            title,
                            body,
                            session_id=session_id,
                        )
                        if len(record["items"]) > item_count:
                            runtime["active_process_item"] = record["items"][-1]
                        if card is not None:
                            card["process_base_body"] = body
                            self._active_tool_cards[session_id] = card
                elif kind == "tool_progress":
                    session_id, text = self._event_text(message)
                    self._update_tool_progress(session_id, text)
                elif kind == "model_retry":
                    session_id, text = self._event_text(message)
                    self._add_card("tool", "Think", text, save=False, session_id=session_id)
                elif kind in {"vision_start", "vision_result", "vision_error"}:
                    session_id, text = self._event_text(message)
                    titles = {
                        "vision_start": "正在分析图片",
                        "vision_result": "图片分析完成",
                        "vision_error": "图片分析失败",
                    }
                    self._add_card(
                        "tool",
                        "Think",
                        f"{titles[kind]}：{text}",
                        save=False,
                        session_id=session_id,
                    )
                elif kind == "tool_result":
                    session_id, text = self._event_text(message)
                    self._add_card(
                        "tool",
                        "工具结果",
                        text,
                        session_id=session_id,
                        visible=False,
                    )
                    self._finish_process(session_id, text)
                elif kind == "answer":
                    answer_text = message
                    session_id = ""
                    title = ""
                    if isinstance(message, dict):
                        answer_text = str(message.get("answer", ""))
                        session_id = str(message.get("session_id", ""))
                        title = str(message.get("title", ""))[:11]
                    if title:
                        record = self._session_record(session_id)
                        record["title"] = title
                        if session_id == self.current_session_id:
                            self.header_title.configure(text=title)
                        self._refresh_session_list()
                    self._active_tool_cards.pop(session_id, None)
                    self._runtime(session_id)["progress"] = ""
                    self._add_card("assistant", "AI Harness", str(answer_text), session_id=session_id)
                    self._finish_task(session_id, "就绪", COLORS["success"])
                elif kind == "error":
                    session_id = (
                        str(message.get("session_id", ""))
                        if isinstance(message, dict)
                        else ""
                    )
                    error_text = (
                        str(message.get("message", message))
                        if isinstance(message, dict)
                        else str(message)
                    )
                    self._active_tool_cards.pop(session_id, None)
                    self._runtime(session_id)["progress"] = ""
                    self._add_card("assistant", "执行失败", error_text, session_id=session_id)
                    self._finish_task(session_id, "发生错误", COLORS["danger"])
                elif kind == "paused":
                    session_id = (
                        str(message.get("session_id", ""))
                        if isinstance(message, dict)
                        else ""
                    )
                    self._active_tool_cards.pop(session_id, None)
                    self._runtime(session_id)["progress"] = ""
                    self._mark_paused(session_id)
        except queue.Empty:
            pass
        except Exception:
            self._write_gui_error(traceback.format_exc())
            try:
                self._set_status("界面发生错误", COLORS["danger"])
            except tk.TclError:
                pass
        finally:
            if not self.closing:
                self.root.after(100, self._drain_events)

    def _finish_task(self, session_id: str, status: str, color: str) -> None:
        runtime = self._runtime(session_id)
        next_task: dict[str, Any] | None = None
        if status in {"就绪", "发生错误"} and runtime.get("follow_up_queue"):
            next_task = runtime["follow_up_queue"].pop(0)
        runtime["busy"] = False
        runtime["paused"] = False
        runtime["stop_pending"] = False
        runtime["worker"] = None
        if runtime.pop("rebuild_session_after_busy", False):
            runtime["session"] = None
        self._session_record(session_id)["updated_at"] = self._now()
        if next_task is not None:
            # Keep the Session busy across the hand-off so the composer does
            # not briefly revert to the idle state between queued turns.
            runtime["busy"] = True
        if session_id == self.current_session_id:
            self._refresh_composer_state()
            self.prompt.focus_set()
        self._refresh_session_status()
        self._save_state()
        self._refresh_session_list()
        if next_task is not None:
            if session_id == self.current_session_id:
                remaining = len(runtime.get("follow_up_queue", []))
                self._set_status(
                    f"继续执行队列（剩余 {remaining} 条）",
                    COLORS["warning"],
                )
            self._start_task(
                session_id,
                str(next_task.get("task", "")),
                list(next_task.get("attachments", [])),
                generate_title=False,
            )

    def _refresh_composer_state(self) -> None:
        runtime = self._active_runtime()
        if runtime["busy"] and not runtime["paused"]:
            self.prompt.configure(state="normal")
            self.resume_button.pack_forget()
            self.queue_button.pack(side="right", padx=(0, 7))
            self.steer_button.pack(side="right", padx=(0, 7))
            self.send_button.configure(
                state="normal",
                text="停止",
                command=self.stop_running,
                style="Stop.TButton",
            )
        elif runtime["busy"] and runtime["paused"]:
            # A stopped turn is still resumable, but it must not lock the
            # composer.  The user can either send a new turn in this Session
            # or continue the interrupted turn with the separate button.
            self.prompt.configure(state="normal")
            self.queue_button.pack_forget()
            self.steer_button.pack_forget()
            self.resume_button.pack(side="right", padx=(0, 7))
            self.send_button.configure(
                state="normal",
                text="发送",
                command=self.send_message,
                style="Primary.TButton",
            )
            if runtime["stop_pending"]:
                self.send_button.configure(state="disabled", text="停止中")
                self.resume_button.pack_forget()
        else:
            self.prompt.configure(state="normal")
            self.queue_button.pack_forget()
            self.steer_button.pack_forget()
            self.resume_button.pack_forget()
            self.send_button.configure(
                state="normal",
                text="发送",
                command=self.send_message,
                style="Primary.TButton",
            )

    def _refresh_session_status(self) -> None:
        runtime = self._active_runtime()
        if runtime["busy"] and runtime["paused"]:
            self._set_status("已停止", COLORS["warning"])
        elif runtime["busy"]:
            self._set_status("工作中", COLORS["warning"])
        else:
            self._set_status("就绪", COLORS["success"])
        running = sum(
            1 for item in self.runtimes.values()
            if item["busy"] and not item["paused"]
        )
        if running:
            self.sidebar_status.configure(text=f"{running} 个任务运行中")
            self.status_dot.configure(fg=COLORS["warning"])

    def stop_running(self) -> None:
        runtime = self._active_runtime()
        if not runtime["busy"] or runtime["paused"]:
            return
        runtime["paused"] = True
        runtime["stop_pending"] = True
        session = runtime["session"]
        if session is not None:
            session.request_stop()
        self._resolve_active_approval(False, self.current_session_id)
        self._refresh_composer_state()
        self._set_status("正在停止", COLORS["warning"])

    def _mark_paused(self, session_id: str) -> None:
        runtime = self._runtime(session_id)
        runtime["busy"] = True
        runtime["paused"] = True
        runtime["stop_pending"] = False
        runtime["worker"] = None
        if session_id == self.current_session_id:
            self._refresh_composer_state()
            self.prompt.focus_set()
        self._refresh_session_status()
        self._snapshot_agent_messages()
        self._save_state()

    def resume_running(self) -> None:
        runtime = self._active_runtime()
        session = runtime["session"]
        if not runtime["paused"] or session is None:
            return
        if runtime["stop_pending"] or (
            runtime["worker"] is not None and runtime["worker"].is_alive()
        ):
            self._set_status("正在停止，请稍候", COLORS["warning"])
            return
        runtime["paused"] = False
        runtime["busy"] = True
        self._refresh_composer_state()
        self._set_status("继续运行", COLORS["warning"])
        runtime["worker"] = threading.Thread(
            target=self._run_resume,
            args=(session, self.current_session_id),
            daemon=True,
        )
        runtime["worker"].start()

    def _set_status(self, text: str, color: str) -> None:
        self.sidebar_status.configure(text=text)
        self.status_dot.configure(fg=color)
        # Status is carried by the dot and text color; a colored rectangle here
        # would add another visual box to an otherwise quiet header.
        surface = COLORS["app"]
        self.header_status_panel.configure(bg=surface)
        self.header_status_dot.configure(bg=surface, fg=color)
        self.header_status.configure(bg=surface, fg=color)
        self.header_status.configure(text=text)

    def _gui_approver(self, session_id: str, command: str, cwd: Path) -> bool:
        """Ask for approval on the Tk thread and wait from the worker thread."""
        request: dict[str, Any] = {
            "session_id": session_id,
            "command": command,
            "cwd": str(cwd),
            "decision": False,
            "event": threading.Event(),
            "cancelled": False,
        }
        self.event_queue.put(("approval_request", request))
        session = self._runtime(session_id)["session"]
        while not request["event"].wait(0.1):
            if self.closing or (session is not None and session.stop_event.is_set()):
                request["cancelled"] = True
                request["event"].set()
                return False
        return bool(request["decision"])

    def _show_approval_dialog(self, request: dict[str, Any]) -> None:
        if request["cancelled"] or request["event"].is_set():
            return
        session_id = str(request.get("session_id", ""))
        runtime = self._runtime(session_id)
        review_message = runtime["pending_review_message"]
        runtime["pending_review_message"] = ""
        dialog = tk.Toplevel(self.root)
        dialog.title("请求批准")
        dialog.geometry("760x560")
        dialog.minsize(680, 500)
        dialog.configure(bg=COLORS["app"])
        dialog.transient(self.root)
        dialog.grab_set()
        request["dialog"] = dialog
        self.active_approval = request

        body = tk.Frame(dialog, bg=COLORS["app"])
        body.pack(fill="both", expand=True, padx=32, pady=28)
        tk.Label(
            body,
            text="需要你的批准",
            bg=COLORS["app"],
            fg=COLORS["text"],
            font=(UI_FONT, 20, "bold"),
        ).pack(anchor="w")
        tk.Label(
            body,
            text=(
                "“帮我批准”无法安全地自动决定，请确认是否允许这次操作。"
                if review_message
                else "AI Harness 想要执行以下操作，请确认是否允许。"
            ),
            bg=COLORS["app"],
            fg=COLORS["muted"],
            font=(UI_FONT, 10),
        ).pack(anchor="w", pady=(6, 18))
        if review_message:
            tk.Label(
                body,
                text=review_message,
                bg=COLORS["warning_bg"],
                fg=COLORS["warning"],
                justify="left",
                anchor="w",
                wraplength=660,
                padx=14,
                pady=11,
                font=(UI_FONT, 10),
                highlightthickness=1,
                highlightbackground=COLORS["warning"],
            ).pack(fill="x", pady=(0, 14))
        tk.Label(
            body,
            text=f"工作目录：{request['cwd']}",
            bg=COLORS["app"],
            fg=COLORS["subtle"],
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(0, 9))

        # Pack the actions before the expanding command area so Tk always
        # reserves visible space for the decision buttons at high DPI.
        actions = tk.Frame(body, bg=COLORS["app"])
        actions.pack(side="bottom", fill="x", pady=(18, 0))
        ttk.Button(
            actions,
            text="拒绝",
            style="Ghost.TButton",
            command=lambda: self._resolve_active_approval(False),
        ).pack(side="right", padx=(8, 0))
        ttk.Button(
            actions,
            text="允许",
            style="Primary.TButton",
            command=lambda: self._resolve_active_approval(True),
        ).pack(side="right")

        command_box = ScrolledText(
            body,
            height=8,
            wrap="word",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["accent"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            highlightcolor=COLORS["accent"],
            font=("Cascadia Mono", 10),
            padx=12,
            pady=10,
        )
        command_box.pack(fill="both", expand=True)
        command_box.insert("1.0", request["command"])
        command_box.configure(state="disabled")
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._resolve_active_approval(False))
        dialog.bind("<Escape>", lambda _event: self._resolve_active_approval(False))
        self._focus_approval_dialog(dialog)

    @staticmethod
    def _release_approval_topmost(dialog: tk.Toplevel) -> None:
        """Drop the temporary Windows topmost flag after the approval dialog is visible."""
        try:
            if dialog.winfo_exists():
                dialog.attributes("-topmost", False)
        except tk.TclError:
            pass

    def _focus_approval_dialog(self, dialog: tk.Toplevel) -> None:
        """Bring a modal approval dialog to the foreground on desktop Windows."""
        try:
            dialog.update_idletasks()
            dialog.deiconify()
            if platform.system() == "Windows":
                # Tk's transient/grab relationship can still leave a Toplevel
                # behind another window when the worker requested it while the
                # user was focused elsewhere. Keep it topmost only long enough
                # to make the decision window discoverable.
                try:
                    dialog.attributes("-topmost", True)
                except tk.TclError:
                    # Foreground/focus promotion below is still useful when a
                    # window manager does not expose the topmost attribute.
                    pass
            dialog.lift()
            dialog.focus_force()
            dialog.grab_set()
            if platform.system() == "Windows":
                dialog.after(500, self._release_approval_topmost, dialog)
        except tk.TclError:
            self._write_gui_error(traceback.format_exc())

    def _resolve_active_approval(self, approved: bool, session_id: str | None = None) -> None:
        request = self.active_approval
        if request is None:
            return
        if session_id is not None and request.get("session_id") != session_id:
            return
        self.active_approval = None
        request["decision"] = approved
        request["event"].set()
        dialog = request.get("dialog")
        if dialog is not None and dialog.winfo_exists():
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()
        if self.approval_queue:
            next_request = self.approval_queue.pop(0)
            if not next_request["cancelled"] and not next_request["event"].is_set():
                self._show_approval_dialog(next_request)

    def change_permission(self, _event: tk.Event[Any] | None = None) -> None:
        if _event is not None:
            display = self.permission_display_var.get()
            mode = next(
                key for key, label in PERMISSION_LABELS.items() if label == display
            )
            self.permission_var.set(mode)
        else:
            mode = self.permission_var.get()
            self.permission_display_var.set(PERMISSION_LABELS[mode])
        self.permission_mode = mode
        self._refresh_composer_context()
        session = self._active_runtime()["session"]
        if session is not None:
            try:
                session.set_permission_mode(mode)
            except ValueError as exc:
                messagebox.showerror("权限模式", str(exc), parent=self.root)
                return
        self._set_status(f"权限：{PERMISSION_LABELS[mode]}", COLORS["warning"] if mode == "full-access" else COLORS["success"])

    def change_model(self, _event: tk.Event[Any] | None = None) -> None:
        """Apply a model selected from either the sidebar or the composer."""
        model = self.model_var.get().strip()
        if not model:
            self.model_var.set(self.model_name)
            self._set_status("模型不能为空", COLORS["danger"])
            return
        self._set_model_name(model)

    def _set_model_name(self, model: str) -> str:
        """Synchronize the selected model with every live GUI Session."""
        normalized = str(model).strip()
        if not normalized:
            raise ValueError("模型不能为空")

        self.model_name = normalized
        self.model_var.set(normalized)
        values = self._model_choices(
            list(self.model_entry.cget("values")),
            normalized,
        )
        self.model_entry.configure(values=values)
        if hasattr(self, "composer_model"):
            self.composer_model.configure(values=values)

        for runtime in self.runtimes.values():
            session = runtime.get("session")
            if session is None:
                continue
            setter = getattr(session, "set_model_name", None)
            if callable(setter):
                setter(normalized)
                continue
            # Keep compatibility with lightweight test doubles and older
            # Sessions while the real AgentSession uses its public setter.
            session.model_name = normalized
            vision_router = getattr(session, "vision_router", None)
            if vision_router is not None:
                vision_router.text_model = normalized
            rebuild_approver = getattr(session, "_build_approver", None)
            if callable(rebuild_approver):
                session.approver = rebuild_approver()

        self._refresh_composer_context()
        self._set_status(f"模型：{normalized}", COLORS["success"])
        return normalized

    @staticmethod
    def _model_choices(models: list[str] | tuple[str, ...], current: str = "") -> list[str]:
        """Keep the current value visible while de-duplicating provider models."""
        choices = list(dict.fromkeys(model.strip() for model in models if model.strip()))
        current = current.strip()
        if current and current not in choices:
            choices.insert(0, current)
        return choices

    def _request_model_catalog(
        self,
        target: ttk.Combobox | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Start a background model-list request before a dropdown is opened."""
        if self.closing or self._model_catalog_loading:
            return
        destination = target or self.model_entry
        endpoint = (api_url if api_url is not None else self.api_url).strip()
        key = api_key if api_key is not None else self.api_key
        if not endpoint:
            self._set_status("API URL 为空，无法获取模型列表", COLORS["danger"])
            return

        try:
            _model_catalog_url(endpoint)
        except ValueError as exc:
            self._set_status(str(exc), COLORS["danger"])
            return

        request_id = uuid.uuid4().hex
        self._model_catalog_loading = True

        def apply_models(models: list[str]) -> None:
            try:
                current = destination.get().strip()
                values = self._model_choices(models, current)
                destination.configure(values=values)
                if destination is getattr(self, "model_entry", None) or destination is getattr(
                    self, "composer_model", None
                ):
                    for widget in (self.model_entry, self.composer_model):
                        if widget is not destination:
                            widget.configure(values=values)
                destination.event_generate("<<ModelCatalogUpdated>>", when="tail")
            except tk.TclError:
                # The connection dialog may have been closed while the request ran.
                pass

        self._model_catalog_callbacks[request_id] = apply_models
        self._set_status("正在获取模型列表…", COLORS["muted"])
        threading.Thread(
            target=self._fetch_model_catalog_worker,
            args=(request_id, endpoint, key),
            daemon=True,
        ).start()

    def _fetch_model_catalog_worker(
        self,
        request_id: str,
        api_url: str,
        api_key: str,
    ) -> None:
        try:
            models = _fetch_model_catalog(api_url, api_key)
        except Exception as exc:
            self.event_queue.put(
                ("model_catalog_error", {"request_id": request_id, "message": str(exc)})
            )
            return
        self.event_queue.put(
            ("model_catalog", {"request_id": request_id, "models": models})
        )

    def open_connection_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("模型连接设置")
        dialog.geometry("620x500")
        dialog.resizable(False, False)
        dialog.configure(bg=COLORS["app"])
        dialog.transient(self.root)
        dialog.grab_set()

        body = tk.Frame(dialog, bg=COLORS["app"])
        body.pack(fill="both", expand=True, padx=28, pady=24)
        tk.Label(body, text="模型连接", bg=COLORS["app"], fg=COLORS["text"], font=(UI_FONT, 18, "bold")).pack(anchor="w")
        tk.Label(
            body,
            text="默认使用 DeepSeek。填写 API Key 后保存，新请求即可使用。",
            bg=COLORS["app"],
            fg=COLORS["muted"],
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(5, 20))

        key_var = tk.StringVar(value=self.api_key)
        url_var = tk.StringVar(value=self.api_url or "https://api.deepseek.com")
        model_var = tk.StringVar(value=self.model_name or "deepseek-v4-flash")

        def field(
            label: str,
            variable: tk.StringVar,
            *,
            secret: bool = False,
            combo_values: list[str] | tuple[str, ...] | None = None,
        ) -> Any:
            tk.Label(body, text=label, bg=COLORS["app"], fg=COLORS["muted"], font=(UI_FONT, 9)).pack(anchor="w", pady=(0, 5))
            if combo_values is not None:
                entry = ttk.Combobox(
                    body,
                    textvariable=variable,
                    values=self._model_choices(combo_values, variable.get()),
                    state="normal",
                    style="Dark.TCombobox",
                )
                entry.pack(fill="x", ipady=6, pady=(0, 13))
            else:
                entry = tk.Entry(
                    body,
                    textvariable=variable,
                    show="●" if secret else "",
                    bg=COLORS["panel"],
                    fg=COLORS["text"],
                    insertbackground=COLORS["accent"],
                    relief="flat",
                    highlightthickness=1,
                    highlightbackground=COLORS["border"],
                    highlightcolor=COLORS["accent"],
                    font=(UI_FONT, 10),
                )
                entry.pack(fill="x", ipady=8, pady=(0, 13))
            return entry

        key_entry = field("API Key", key_var, secret=True)
        field("API URL", url_var)
        model_entry = field("模型", model_var, combo_values=OPENCODE_GO_CHAT_MODELS)
        model_entry.configure(
            postcommand=lambda: self._request_model_catalog(
                model_entry,
                url_var.get(),
                key_var.get(),
            )
        )

        preset_row = tk.Frame(body, bg=COLORS["app"])
        preset_row.pack(fill="x", pady=(0, 8))
        tk.Label(
            preset_row,
            text="当前 Harness 可直接使用 Go 的 Chat Completions 模型："
            + "、".join(OPENCODE_GO_CHAT_MODELS),
            bg=COLORS["app"],
            fg=COLORS["subtle"],
            justify="left",
            wraplength=560,
            font=(UI_FONT, 8),
        ).pack(side="left", fill="x", expand=True, padx=(0, 10))

        def use_opencode_go_preset() -> None:
            url_var.set(OPENCODE_GO_BASE_URL)
            model_var.set(OPENCODE_GO_DEFAULT_MODEL)
            if not key_var.get().strip():
                key_var.set(os.getenv("OPENCODE_GO_API_KEY", ""))
            key_entry.focus_set()

        ttk.Button(
            preset_row,
            text="OpenCode Go 预设",
            style="Ghost.TButton",
            command=use_opencode_go_preset,
        ).pack(side="right", anchor="n")

        actions = tk.Frame(body, bg=COLORS["app"])
        actions.pack(fill="x", pady=(4, 0))

        def save() -> None:
            api_key = key_var.get().strip()
            api_url = url_var.get().strip()
            model = model_var.get().strip()
            if not api_key:
                messagebox.showwarning("缺少 API Key", "请输入 API Key。", parent=dialog)
                key_entry.focus_set()
                return
            if not api_url or not model:
                messagebox.showwarning("配置不完整", "API URL 和模型不能为空。", parent=dialog)
                return
            try:
                self._save_connection_values(api_key, api_url, model)
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc), parent=dialog)
                return
            dialog.destroy()
            self._set_status("模型已配置", COLORS["success"])

        ttk.Button(actions, text="取消", style="Ghost.TButton", command=dialog.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(actions, text="保存并使用", style="Primary.TButton", command=save).pack(side="right")
        key_entry.focus_set()

    def _save_connection_values(self, api_key: str, api_url: str, model: str) -> None:
        """Persist connection settings while preserving unrelated env entries."""
        managed = {
            "AI_HARNESS_API_KEY",
            "AI_HARNESS_BASE_URL",
            "AI_HARNESS_MODEL",
            "AI_HARNESS_PROVIDER",
            "OPENCODE_GO_API_KEY",
        }
        kept_lines: list[str] = []
        if self.config_path.is_file():
            for raw_line in self.config_path.read_text(encoding="utf-8").splitlines():
                candidate = raw_line.strip()
                if candidate.startswith("export "):
                    candidate = candidate[7:].lstrip()
                key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
                if key not in managed:
                    kept_lines.append(raw_line)
        values = {
            "AI_HARNESS_API_KEY": api_key,
            "AI_HARNESS_BASE_URL": api_url,
            "AI_HARNESS_MODEL": model,
        }
        kept_lines.extend(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items())
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.config_path.with_suffix(".tmp")
        temp_path.write_text("\n".join(kept_lines).rstrip() + "\n", encoding="utf-8")
        temp_path.replace(self.config_path)

        self._snapshot_agent_messages()
        self.api_key = api_key
        self.api_url = api_url
        self.model_name = model
        self.model_var.set(model)
        values = self._model_choices(list(self.model_entry.cget("values")), model)
        self.model_entry.configure(values=values)
        if hasattr(self, "composer_model"):
            self.composer_model.configure(values=values)
        self._refresh_composer_context()
        os.environ["AI_HARNESS_API_KEY"] = api_key
        os.environ["AI_HARNESS_BASE_URL"] = api_url
        os.environ["AI_HARNESS_MODEL"] = model
        os.environ["AI_HARNESS_ENV_FILE"] = str(self.config_path)
        self._invalidate_sessions_for_connection_change(self.runtimes.values())

    def _on_close(self) -> None:
        self.closing = True
        self._clear_mousewheel_queue()
        for runtime in self.runtimes.values():
            session = runtime["session"]
            if session is not None:
                session.request_stop()
        self._resolve_active_approval(False)
        self._save_state()
        self.root.destroy()


def launch_gui(
    *,
    workspace: str | Path | None = None,
    approval_mode: str = "auto",
    full_access: bool = False,
    model_name: str | None = None,
    max_turns: int = 100,
) -> None:
    """Start the desktop UI."""
    _enable_windows_dpi_awareness()
    _set_windows_app_user_model_id()
    _configure_tk_runtime()
    try:
        root = tk.Tk()
    except Exception as exc:
        _write_startup_error(f"Tk GUI 初始化失败：{exc}")
        raise RuntimeError(
            "GUI 启动失败：找不到可用的 Tcl/Tk 运行时或图形会话。"
            "详见 ~/.ai-harness/gui-startup-error.log"
        ) from exc
    # Tk creates a native top-level window before the Python-side layout is
    # complete. Keep that window withdrawn while state, widgets, and history
    # are built so Windows cannot show partially painted black client regions.
    root.withdraw()
    root.configure(bg=COLORS["app"])
    HarnessGUI(
        root,
        workspace=workspace,
        approval_mode=approval_mode,
        full_access=full_access,
        model_name=model_name,
        max_turns=max_turns,
    )
    root.update_idletasks()
    root.deiconify()
    root.update_idletasks()
    _force_windows_repaint(root)
    root.mainloop()
