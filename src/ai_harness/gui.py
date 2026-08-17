"""Polished Tkinter desktop interface for AI Harness."""

from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import queue
import sys
import threading
import tkinter as tk
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image, ImageGrab

from . import __version__
from .agent import AgentPaused, AgentSession
from .config import (
    OPENCODE_GO_BASE_URL,
    OPENCODE_GO_CHAT_MODELS,
    OPENCODE_GO_DEFAULT_MODEL,
    is_opencode_go_provider,
    load_env_file,
)


COLORS = {
    "app": "#0b0f14",
    "sidebar": "#11161d",
    "sidebar_alt": "#151b23",
    "panel": "#171e27",
    "panel_hover": "#202936",
    "composer": "#151c25",
    "border": "#283342",
    "border_bright": "#3a4a5f",
    "text": "#eef3f8",
    "muted": "#9ba8b7",
    "subtle": "#697789",
    "accent": "#6ea8fe",
    "accent_hover": "#8bb9ff",
    "accent_text": "#08111f",
    "user_bubble": "#1f3a5f",
    "assistant_bubble": "#141b23",
    "tool": "#18212b",
    "success": "#6dd6a0",
    "warning": "#f0bd66",
    "danger": "#ff8585",
}


PERMISSION_LABELS = {
    "ask": "请求批准",
    "auto": "帮我批准",
    "full-access": "完全访问",
}

UI_FONT = "Microsoft YaHei UI" if platform.system() == "Windows" else "TkDefaultFont"
MAX_RENDERED_HISTORY_ITEMS = 80
MOUSEWHEEL_SCROLL_FACTOR = 0.2
MODEL_CATALOG_TIMEOUT = 10.0


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
        # of 120. Preserve the direction and make both usable as scroll units.
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


def _scale_mousewheel_units(units: int, remainder: float) -> tuple[int, float]:
    """Reduce wheel speed while preserving fractional movement between events."""
    scaled = units * MOUSEWHEEL_SCROLL_FACTOR + remainder
    scroll_units = math.trunc(scaled)
    return scroll_units, scaled - scroll_units


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
        self._rendering_history = False
        self._drag_project_path = ""
        self._drag_start_y = 0
        self._drag_moved = False
        self._drop_project_path = ""
        self._body_labels: list[tk.Label] = []
        self._active_tool_cards: dict[str, dict[str, Any]] = {}
        self._wrap_refresh_scheduled = False
        self._mousewheel_remainders: dict[str, float] = {}
        self._model_catalog_loading = False
        self._model_catalog_callbacks: dict[str, Callable[[list[str]], None]] = {}

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
        self._bind_mousewheel(self.project_tree)
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
            "active_approval": None,
            "pending_review_message": "",
            "progress": "",
        }

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
            background=COLORS["accent"],
            foreground=COLORS["accent_text"],
            borderwidth=0,
            focusthickness=0,
            padding=(16, 10),
            font=(UI_FONT, 9, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", COLORS["accent_hover"]), ("disabled", COLORS["subtle"])],
        )
        style.configure(
            "Run.TButton",
            background=COLORS["accent"],
            foreground=COLORS["accent_text"],
            borderwidth=0,
            focusthickness=0,
            padding=(18, 7),
            font=(UI_FONT, 15, "bold"),
        )
        style.map("Run.TButton", background=[("active", COLORS["accent_hover"])])
        style.configure(
            "Ghost.TButton",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            borderwidth=1,
            bordercolor=COLORS["border"],
            focusthickness=0,
            padding=(10, 7),
        )
        style.map("Ghost.TButton", background=[("active", COLORS["panel_hover"])])
        style.configure(
            "Suggestion.TButton",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            borderwidth=0,
            focusthickness=0,
            anchor="w",
            padding=(14, 12),
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
        )
        style.map("Icon.TButton", background=[("active", COLORS["panel_hover"])], foreground=[("active", COLORS["text"])])
        style.configure(
            "Dark.TCombobox",
            fieldbackground=COLORS["panel"],
            background=COLORS["panel"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["border"],
            padding=5,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", COLORS["panel"])],
            background=[("readonly", COLORS["panel"])],
            foreground=[("readonly", COLORS["text"])],
            selectbackground=[("readonly", COLORS["panel"])],
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
            rowheight=30,
            font=(UI_FONT, 9),
        )
        style.map(
            "Project.Treeview",
            background=[("selected", COLORS["user_bubble"])],
            foreground=[("selected", COLORS["text"])],
        )

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame")
        outer.pack(fill="both", expand=True)

        accent_rail = tk.Frame(outer, width=3, bg=COLORS["accent"])
        accent_rail.pack(side="left", fill="y")
        self.sidebar = ttk.Frame(outer, width=292, style="Sidebar.TFrame")
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
        brand.pack(fill="x", padx=18, pady=(17, 12))
        logo = tk.Label(
            brand,
            text="H",
            width=2,
            height=1,
            bg=COLORS["accent"],
            fg=COLORS["accent_text"],
            font=(UI_FONT, 13, "bold"),
        )
        logo.pack(side="left", padx=(0, 10))
        brand_text = tk.Frame(brand, bg=COLORS["sidebar"])
        brand_text.pack(side="left", fill="x", expand=True)
        tk.Label(brand_text, text="AI Harness", bg=COLORS["sidebar"], fg=COLORS["text"], font=(UI_FONT, 14, "bold")).pack(anchor="w")
        tk.Label(brand_text, text="开发者：张杰", bg=COLORS["sidebar"], fg=COLORS["accent"], font=(UI_FONT, 8)).pack(anchor="w", pady=(2, 0))
        tk.Label(brand, text=f"v{__version__}", bg=COLORS["sidebar"], fg=COLORS["subtle"], font=(UI_FONT, 8)).pack(side="right", anchor="n")

        ttk.Button(self.sidebar, text="＋  新建 Session", style="Primary.TButton", command=self.new_conversation).pack(fill="x", padx=15, pady=(0, 14))

        project_header = self._section_header("项目与 Sessions", "＋", self.add_project)
        ttk.Button(
            project_header,
            text="－",
            width=2,
            style="Icon.TButton",
            command=self.delete_selected_tree_item,
        ).pack(side="right")
        project_header.pack(fill="x", padx=15)
        tree_frame = tk.Frame(
            self.sidebar,
            bg=COLORS["sidebar"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        tree_frame.pack(fill="both", expand=True, padx=15, pady=(5, 13))
        self.project_tree = ttk.Treeview(
            tree_frame,
            show="tree",
            selectmode="browse",
            style="Project.Treeview",
        )
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.project_tree.yview)
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

        settings = tk.Frame(self.sidebar, bg=COLORS["sidebar_alt"], highlightthickness=1, highlightbackground=COLORS["border"])
        settings.pack(fill="x", padx=15, pady=(0, 10))
        top = tk.Frame(settings, bg=COLORS["sidebar_alt"])
        top.pack(fill="x", padx=10, pady=(9, 7))
        tk.Label(top, text="运行设置", bg=COLORS["sidebar_alt"], fg=COLORS["muted"], font=(UI_FONT, 8, "bold")).pack(side="left")
        ttk.Button(top, text="模型连接", style="Icon.TButton", command=self.open_connection_settings).pack(side="right")

        controls = tk.Frame(settings, bg=COLORS["sidebar_alt"])
        controls.pack(fill="x", padx=9, pady=(0, 9))
        self.permission_var = tk.StringVar(value=self.permission_mode)
        self.permission_display_var = tk.StringVar(
            value=PERMISSION_LABELS[self.permission_mode]
        )
        permission_row = tk.Frame(controls, bg=COLORS["sidebar_alt"])
        permission_row.pack(fill="x", pady=(0, 6))
        tk.Label(permission_row, text="权限", width=5, anchor="w", bg=COLORS["sidebar_alt"], fg=COLORS["subtle"], font=(UI_FONT, 8)).pack(side="left")
        self.permission_menu = ttk.Combobox(
            permission_row,
            textvariable=self.permission_display_var,
            values=list(PERMISSION_LABELS.values()),
            state="readonly",
            style="Dark.TCombobox",
            width=18,
        )
        self.permission_menu.pack(side="left", fill="x", expand=True)
        self.permission_menu.bind("<<ComboboxSelected>>", self.change_permission)
        self.model_var = tk.StringVar(value=self.model_name)
        model_row = tk.Frame(controls, bg=COLORS["sidebar_alt"])
        model_row.pack(fill="x")
        tk.Label(model_row, text="模型", width=5, anchor="w", bg=COLORS["sidebar_alt"], fg=COLORS["subtle"], font=(UI_FONT, 8)).pack(side="left")
        initial_models = list(OPENCODE_GO_CHAT_MODELS)
        if self.model_name and self.model_name not in initial_models:
            initial_models.insert(0, self.model_name)
        self.model_entry = ttk.Combobox(
            model_row,
            textvariable=self.model_var,
            values=initial_models,
            state="normal",
            style="Dark.TCombobox",
            width=18,
            postcommand=self._request_model_catalog,
        )
        self.model_entry.pack(side="left", fill="x", expand=True, ipady=5)

        footer = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        footer.pack(fill="x", padx=18, pady=(0, 13))
        self.status_dot = tk.Label(footer, text="●", bg=COLORS["sidebar"], fg=COLORS["success"], font=(UI_FONT, 9))
        self.status_dot.pack(side="left")
        self.sidebar_status = tk.Label(footer, text="就绪", bg=COLORS["sidebar"], fg=COLORS["muted"], font=(UI_FONT, 9))
        self.sidebar_status.pack(side="left", padx=(6, 0))

    def _section_header(self, title: str, action: str, command: Any) -> tk.Frame:
        frame = tk.Frame(self.sidebar, bg=COLORS["sidebar"])
        tk.Label(frame, text=title.upper(), bg=COLORS["sidebar"], fg=COLORS["subtle"], font=(UI_FONT, 8, "bold")).pack(side="left")
        ttk.Button(frame, text=action, width=2, style="Icon.TButton", command=command).pack(side="right")
        return frame

    def _build_header(self, parent: ttk.Frame) -> None:
        # Let the header follow the real font metrics. A fixed 78 px height
        # clips the breadcrumb on Windows when DPI scaling is above 100%.
        header = tk.Frame(parent, bg=COLORS["app"])
        header.pack(fill="x")
        title_group = tk.Frame(header, bg=COLORS["app"])
        title_group.pack(side="left", padx=34, pady=(12, 11))
        self.header_title = tk.Label(title_group, text="新任务", bg=COLORS["app"], fg=COLORS["text"], font=(UI_FONT, 17, "bold"))
        self.header_title.pack(anchor="w")
        self.header_breadcrumb = tk.Label(title_group, text="", bg=COLORS["app"], fg=COLORS["subtle"], font=(UI_FONT, 8))
        self.header_breadcrumb.pack(anchor="w", pady=(3, 0))
        status_panel = tk.Frame(header, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["border"])
        status_panel.pack(side="right", padx=32)
        self.header_status_dot = tk.Label(status_panel, text="●", bg=COLORS["panel"], fg=COLORS["success"], font=(UI_FONT, 8))
        self.header_status_dot.pack(side="left", padx=(10, 5), pady=6)
        self.header_status = tk.Label(status_panel, text="连接就绪", bg=COLORS["panel"], fg=COLORS["muted"], font=(UI_FONT, 8))
        self.header_status.pack(side="left", padx=(0, 10), pady=6)
        tk.Frame(parent, height=1, bg=COLORS["border"]).pack(fill="x")

    def _build_chat(self, parent: ttk.Frame) -> None:
        chat_area = tk.Frame(parent, bg=COLORS["app"])
        chat_area.pack(fill="both", expand=True, padx=20)
        self.chat_canvas = tk.Canvas(chat_area, bg=COLORS["app"], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(chat_area, orient="vertical", command=self.chat_canvas.yview)
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
        composer_wrap.pack(fill="x", padx=40, pady=(10, 24))
        outer_border = tk.Frame(composer_wrap, bg=COLORS["border_bright"], padx=1, pady=1)
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
            selectbackground=COLORS["user_bubble"],
            relief="flat",
            borderwidth=0,
            padx=16,
            pady=12,
            font=(UI_FONT, 11),
        )
        self.prompt.pack(fill="x", expand=True)
        self.prompt.bind("<Return>", self._send_from_event)
        self.prompt.bind("<Shift-Return>", self._insert_newline)
        self.prompt.bind("<Control-v>", self._paste_from_clipboard)
        self.attachment_bar = tk.Frame(self.composer, bg=COLORS["composer"])
        self.composer_controls = tk.Frame(self.composer, bg=COLORS["composer"])
        self.composer_controls.pack(fill="x", padx=12, pady=(0, 10))
        ttk.Button(
            self.composer_controls,
            text="＋  附件",
            style="Ghost.TButton",
            command=self.select_attachments,
        ).pack(side="left", padx=(0, 8))
        workspace_badge = tk.Frame(self.composer_controls, bg=COLORS["panel"])
        workspace_badge.pack(side="left")
        tk.Label(workspace_badge, text="◆", bg=COLORS["panel"], fg=COLORS["accent"], font=(UI_FONT, 7)).pack(side="left", padx=(8, 4), pady=5)
        self.composer_project = tk.Label(workspace_badge, text=self.workspace.name, bg=COLORS["panel"], fg=COLORS["muted"], font=(UI_FONT, 8))
        self.composer_project.pack(side="left", padx=(0, 8), pady=5)
        self.send_button = ttk.Button(self.composer_controls, text="发送  ↑", style="Primary.TButton", command=self.send_message)
        self.send_button.pack(side="right")

    def _send_from_event(self, _event: tk.Event[Any]) -> str:
        self.send_message()
        return "break"

    def _insert_newline(self, _event: tk.Event[Any]) -> str:
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
                bg=COLORS["panel"],
                highlightthickness=1,
                highlightbackground=COLORS["border"],
            )
            chip.pack(side="left", padx=(0, 7))
            icon = "▧" if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"} else "▤"
            tk.Label(chip, text=f"{icon}  {path.name}", bg=COLORS["panel"], fg=COLORS["muted"], font=(UI_FONT, 8)).pack(side="left", padx=(8, 4), pady=6)
            tk.Button(
                chip,
                text="×",
                command=lambda item=path: self._remove_attachment(item),
                bg=COLORS["panel"],
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
        """Handle scrolling before native widget class bindings can run."""
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(sequence, self._on_mousewheel, add="+")

    def _bind_mousewheel_tree(self, widget: tk.Misc) -> None:
        """Install the early wheel binding on a canvas and all content widgets."""
        self._bind_mousewheel(widget)
        for child in widget.winfo_children():
            self._bind_mousewheel_tree(child)

    def _on_mousewheel(self, event: tk.Event[Any]) -> str | None:
        units = _mousewheel_units(event)
        if not units:
            return None
        widget = getattr(event, "widget", None)
        if widget is not None and self._widget_is_inside(widget, self.chat_canvas):
            self._scroll_with_mousewheel(self.chat_canvas, units)
            return "break"
        if widget is not None and self._widget_is_inside(widget, self.project_tree):
            self._scroll_with_mousewheel(self.project_tree, units)
            return "break"
        return None

    def _scroll_with_mousewheel(self, widget: tk.Misc, units: int) -> None:
        """Scroll at a reduced rate without dropping small trackpad movements."""
        key = str(widget)
        scroll_units, remainder = _scale_mousewheel_units(
            units, self._mousewheel_remainders.get(key, 0.0)
        )
        self._mousewheel_remainders[key] = remainder
        if scroll_units:
            widget.yview_scroll(scroll_units, "units")

    def _refresh_project_list(self) -> None:
        self._refresh_project_tree()

    def _refresh_session_list(self) -> None:
        self._refresh_project_tree()

    def _refresh_project_tree(self) -> None:
        children = self.project_tree.get_children()
        if children:
            self.project_tree.delete(*children)
        selected_iid = ""
        for index, project in enumerate(self.projects):
            project_iid = f"project-{index}"
            self.project_tree.insert(
                "",
                "end",
                iid=project_iid,
                text=f"  ▰  {project['name']}",
                open=True,
            )
            records = sorted(
                [item for item in self.sessions if item["workspace"] == project["path"]],
                key=lambda item: item["updated_at"],
                reverse=True,
            )
            for record in records:
                session_iid = f"session-{record['id']}"
                prefix = "●" if record["id"] == self.current_session_id else "○"
                self.project_tree.insert(
                    project_iid,
                    "end",
                    iid=session_iid,
                    text=f"  {prefix}  {record['title']}",
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

    def _render_current_session(self) -> None:
        record = self._current_record()
        for child in self.chat_inner.winfo_children():
            child.destroy()
        self._body_labels.clear()
        self._active_tool_cards.clear()
        self._rendering_history = True
        items = record["items"]
        if len(items) > MAX_RENDERED_HISTORY_ITEMS:
            self._add_card(
                "tool",
                "历史记录已折叠",
                f"当前 Session 共 {len(items)} 条记录，启动时显示最近 {MAX_RENDERED_HISTORY_ITEMS} 条。完整记录仍保存在本地会话文件中。",
                save=False,
            )
            items = items[-MAX_RENDERED_HISTORY_ITEMS:]
        if items:
            for item in items:
                self._add_card(item.get("role", "assistant"), item.get("title", "AI Harness"), item.get("body", ""), save=False)
        else:
            self._show_welcome()
        self._rendering_history = False
        runtime = self._runtime(record["id"])
        if runtime["busy"] and runtime.get("progress"):
            card = self._add_card(
                "tool",
                "执行工具（运行中）",
                runtime["progress"],
                save=False,
                session_id=record["id"],
            )
            if card is not None:
                self._active_tool_cards[record["id"]] = card
        self.header_title.configure(text=record["title"])
        self.header_breadcrumb.configure(text=f"{self.workspace.name}   /   Session {record['id'][:6]}")
        self.composer_project.configure(text=self.workspace.name)
        self._refresh_composer_state()
        self._refresh_session_status()
        self._bind_mousewheel_tree(self.chat_inner)

    def _show_welcome(self) -> None:
        hero = tk.Frame(self.chat_inner, bg=COLORS["app"])
        hero.pack(fill="x", padx=105, pady=(58, 18))
        tk.Label(hero, text="✦", bg=COLORS["app"], fg=COLORS["accent"], font=(UI_FONT, 24)).pack(anchor="w")
        tk.Label(hero, text="今天想构建什么？", bg=COLORS["app"], fg=COLORS["text"], font=(UI_FONT, 22, "bold")).pack(anchor="w", pady=(8, 4))
        tk.Label(hero, text=f"当前项目：{self.workspace.name}。描述任务，AI Harness 会在你的授权范围内完成它。", bg=COLORS["app"], fg=COLORS["muted"], font=(UI_FONT, 10)).pack(anchor="w")
        self._add_suggestions()

    def _add_suggestions(self) -> None:
        row = tk.Frame(self.chat_inner, bg=COLORS["app"])
        row.pack(fill="x", padx=105, pady=(0, 24))
        suggestions = (
            ("⌕", "理解项目", "检查结构并说明关键模块"),
            ("◇", "修复问题", "定位 Bug 并给出可靠修复"),
            ("✓", "运行验证", "执行测试并总结结果"),
        )
        for icon, title, task in suggestions:
            card = tk.Frame(row, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["border"])
            card.pack(side="left", fill="x", expand=True, padx=(0, 10))
            button = ttk.Button(
                card,
                text=f"{icon}  {title}\n{task}",
                command=lambda value=task: self._use_suggestion(value),
                style="Suggestion.TButton",
            )
            button.pack(fill="both", expand=True)

    def _use_suggestion(self, text: str) -> None:
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", text)
        self.prompt.focus_set()

    def _initial_card_wraplength(self, role: str) -> int:
        """Choose a safe first wrap width before Tk has laid out the card."""
        canvas_width = self.chat_canvas.winfo_width()
        if canvas_width <= 1:
            return 420
        right_margin = 160 if role == "user" else 110 if role == "tool" else 90
        available = canvas_width - 190 - right_margin - 30
        return max(180, min(760, available))

    def _schedule_card_wrap_refresh(self) -> None:
        if self._wrap_refresh_scheduled:
            return
        self._wrap_refresh_scheduled = True
        self.root.after_idle(self._refresh_card_wraps)

    def _refresh_card_wraps(self) -> None:
        self._wrap_refresh_scheduled = False
        try:
            self.chat_inner.update_idletasks()
            for label in self._body_labels:
                if not label.winfo_exists():
                    continue
                bubble = label.master
                available = bubble.winfo_width() - 30
                if available > 0:
                    wraplength = max(180, available)
                    if int(label.cget("wraplength")) != wraplength:
                        label.configure(wraplength=wraplength)
        except tk.TclError:
            pass

    def _add_card(
        self,
        role: str,
        title: str,
        body: str,
        *,
        save: bool = True,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        target_id = session_id or self.current_session_id
        title = str(title)
        body = str(body)
        record: dict[str, Any] | None = None
        if save and not self._rendering_history:
            record = self._session_record(target_id)
            record["items"].append({"role": role, "title": title, "body": body})
            record["updated_at"] = self._now()
            self._save_state()
        if target_id != self.current_session_id:
            return None

        wrapper = tk.Frame(self.chat_inner, bg=COLORS["app"])
        wrapper.pack(fill="x", padx=95, pady=(10, 5))
        if role == "user":
            bubble = tk.Frame(wrapper, bg=COLORS["user_bubble"], highlightthickness=1, highlightbackground="#31598a")
            bubble.pack(anchor="e", padx=(160, 0))
            title_color = "#b9d6ff"
            avatar = "你"
        elif role == "tool":
            bubble = tk.Frame(wrapper, bg=COLORS["tool"], highlightthickness=1, highlightbackground=COLORS["border"])
            bubble.pack(anchor="w", padx=(0, 110), fill="x")
            title_color = COLORS["warning"]
            avatar = "⚙"
        else:
            bubble = tk.Frame(wrapper, bg=COLORS["assistant_bubble"], highlightthickness=1, highlightbackground=COLORS["border"])
            bubble.pack(anchor="w", padx=(0, 90), fill="x")
            title_color = COLORS["accent"]
            avatar = "H"
        header = tk.Frame(bubble, bg=bubble["bg"])
        header.pack(fill="x", padx=14, pady=(11, 5))
        tk.Label(header, text=avatar, width=2, bg=COLORS["accent"] if role == "assistant" else bubble["bg"], fg=COLORS["accent_text"] if role == "assistant" else title_color, font=(UI_FONT, 8, "bold")).pack(side="left", padx=(0, 7))
        tk.Label(header, text=title, bg=bubble["bg"], fg=title_color, font=(UI_FONT, 9, "bold"), anchor="w").pack(side="left")
        body_label = tk.Label(
            bubble,
            text=body,
            bg=bubble["bg"],
            fg=COLORS["text"] if role != "tool" else COLORS["muted"],
            justify="left",
            anchor="w",
            wraplength=self._initial_card_wraplength(role),
            font=(UI_FONT, 10),
            padx=15,
            pady=0,
        )
        body_label.pack(fill="x", pady=(0, 13))
        self._body_labels.append(body_label)
        if not self._rendering_history:
            self._bind_mousewheel_tree(wrapper)
        self._schedule_card_wrap_refresh()
        self.root.after_idle(self._scroll_chat_to_bottom)
        self.root.after(80, self._scroll_chat_to_bottom)
        return {
            "body_label": body_label,
            "record": record,
            "body": body,
        }

    def _update_tool_progress(self, session_id: str, message: str) -> None:
        """Update one transient tool card instead of appending history rows."""
        runtime = self._runtime(session_id)
        runtime["progress"] = message
        if session_id != self.current_session_id:
            return
        card = self._active_tool_cards.get(session_id)
        if card is None:
            card = self._add_card("tool", "执行工具（运行中）", message, session_id=session_id)
            if card is not None:
                self._active_tool_cards[session_id] = card
            return
        body_label = card.get("body_label")
        if body_label is None:
            return
        try:
            body_label.configure(text=message)
            card["body"] = message
            self._schedule_card_wrap_refresh()
            self.root.after_idle(self._scroll_chat_to_bottom)
        except tk.TclError:
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

    def _ensure_session(self, session_id: str | None = None) -> AgentSession:
        target_id = session_id or self.current_session_id
        runtime = self._runtime(target_id)
        if runtime["session"] is None:
            record = self._session_record(target_id)
            workspace = Path(record["workspace"]).resolve()
            model = self.model_entry.get().strip() or None
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
            runtime["session"] = session
            if repaired:
                runtime["history_repaired"] = repaired
                self._add_card(
                    "tool",
                    "会话恢复",
                    f"检测到 {repaired} 个未完成的工具调用，已补齐中断结果，可以继续运行。",
                    session_id=target_id,
                )
                self._save_state()
        return runtime["session"]

    def send_message(self) -> None:
        if self._active_runtime()["busy"]:
            return
        task = self.prompt.get("1.0", "end").strip()
        attachments = list(self.pending_attachments)
        if not task and attachments:
            task = "请分析这些附件。"
        if not task:
            return
        if task == "/clear":
            self.new_conversation()
            self.prompt.delete("1.0", "end")
            return
        if task == "/help":
            self.prompt.delete("1.0", "end")
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
            self.prompt.delete("1.0", "end")
            return

        self.prompt.delete("1.0", "end")
        session_id = self.current_session_id
        runtime = self._runtime(session_id)
        record = self._session_record(session_id)
        first_exchange = not record["items"]
        display_text = task
        if attachments:
            display_text += "\n\n附件：" + "、".join(path.name for path in attachments)
        self._add_card("user", "你", display_text, session_id=session_id)
        self._refresh_session_list()
        runtime["busy"] = True
        runtime["paused"] = False
        runtime["stop_pending"] = False
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
        self.pending_attachments = []
        self._refresh_attachment_bar()
        runtime["worker"] = threading.Thread(
            target=self._run_task,
            args=(session, task, attachments, session_id, first_exchange),
            daemon=True,
        )
        runtime["worker"].start()

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
                elif kind == "approval_review":
                    session_id = (
                        str(message.get("session_id", ""))
                        if isinstance(message, dict)
                        else ""
                    )
                    review_text = (
                        str(message.get("message", ""))
                        if isinstance(message, dict)
                        else str(message)
                    )
                    runtime = self._runtime(session_id)
                    runtime["pending_review_message"] = (
                        review_text if review_text.startswith("需要确认") else ""
                    )
                    self._add_card("tool", "审批审查", review_text, session_id=session_id)
                elif kind == "tool_start":
                    session_id = (
                        str(message.get("session_id", ""))
                        if isinstance(message, dict)
                        else ""
                    )
                    text = (
                        str(message.get("message", ""))
                        if isinstance(message, dict)
                        else str(message)
                    )
                    self._active_tool_cards.pop(session_id, None)
                    card = self._add_card("tool", "执行工具", text, session_id=session_id)
                    if card is not None:
                        self._active_tool_cards[session_id] = card
                elif kind == "tool_progress":
                    session_id = (
                        str(message.get("session_id", ""))
                        if isinstance(message, dict)
                        else ""
                    )
                    text = (
                        str(message.get("message", ""))
                        if isinstance(message, dict)
                        else str(message)
                    )
                    self._update_tool_progress(session_id, text)
                elif kind == "model_retry":
                    session_id = (
                        str(message.get("session_id", ""))
                        if isinstance(message, dict)
                        else ""
                    )
                    text = (
                        str(message.get("message", ""))
                        if isinstance(message, dict)
                        else str(message)
                    )
                    self._add_card("tool", "连接重试", text, save=False, session_id=session_id)
                elif kind == "tool_result":
                    session_id = (
                        str(message.get("session_id", ""))
                        if isinstance(message, dict)
                        else ""
                    )
                    text = (
                        str(message.get("message", ""))
                        if isinstance(message, dict)
                        else str(message)
                    )
                    self._active_tool_cards.pop(session_id, None)
                    self._runtime(session_id)["progress"] = ""
                    self._add_card("tool", "工具结果", text, session_id=session_id)
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
        runtime["busy"] = False
        runtime["paused"] = False
        runtime["stop_pending"] = False
        runtime["worker"] = None
        self._session_record(session_id)["updated_at"] = self._now()
        if session_id == self.current_session_id:
            self._refresh_composer_state()
            self.prompt.focus_set()
        self._refresh_session_status()
        self._save_state()
        self._refresh_session_list()

    def _refresh_composer_state(self) -> None:
        runtime = self._active_runtime()
        if runtime["busy"]:
            self.prompt.configure(state="disabled")
            self.send_button.configure(
                state="normal",
                text="▶" if runtime["paused"] else "■",
                command=self.resume_running if runtime["paused"] else self.stop_running,
                style="Run.TButton",
            )
        else:
            self.prompt.configure(state="normal")
            self.send_button.configure(
                state="normal",
                text="发送  ↑",
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
        running = sum(1 for item in self.runtimes.values() if item["busy"])
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
        if session_id == self.current_session_id:
            self._refresh_composer_state()
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
        self.header_status.configure(text=text)
        self.header_status_dot.configure(fg=color)

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
        dialog.geometry("720x540")
        dialog.minsize(640, 480)
        dialog.configure(bg=COLORS["app"])
        dialog.transient(self.root)
        dialog.grab_set()
        request["dialog"] = dialog
        self.active_approval = request

        body = tk.Frame(dialog, bg=COLORS["app"])
        body.pack(fill="both", expand=True, padx=28, pady=24)
        tk.Label(
            body,
            text="请求批准",
            bg=COLORS["app"],
            fg=COLORS["text"],
            font=(UI_FONT, 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            body,
            text=(
                "“帮我批准”无法安全地自动决定，请你确认是否允许。"
                if review_message
                else "AI Harness 想要执行以下操作，请确认是否允许。"
            ),
            bg=COLORS["app"],
            fg=COLORS["muted"],
            font=(UI_FONT, 9),
        ).pack(anchor="w", pady=(5, 15))
        if review_message:
            tk.Label(
                body,
                text=review_message,
                bg=COLORS["panel"],
                fg=COLORS["warning"],
                justify="left",
                anchor="w",
                wraplength=620,
                padx=12,
                pady=9,
                font=(UI_FONT, 9),
            ).pack(fill="x", pady=(0, 12))
        tk.Label(
            body,
            text=f"工作目录：{request['cwd']}",
            bg=COLORS["app"],
            fg=COLORS["subtle"],
            font=(UI_FONT, 8),
        ).pack(anchor="w", pady=(0, 7))

        # Pack the actions before the expanding command area so Tk always
        # reserves visible space for the decision buttons at high DPI.
        actions = tk.Frame(body, bg=COLORS["app"])
        actions.pack(side="bottom", fill="x", pady=(16, 0))
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
            font=("Cascadia Mono", 9),
            padx=12,
            pady=10,
        )
        command_box.pack(fill="both", expand=True)
        command_box.insert("1.0", request["command"])
        command_box.configure(state="disabled")
        dialog.protocol("WM_DELETE_WINDOW", lambda: self._resolve_active_approval(False))
        dialog.bind("<Escape>", lambda _event: self._resolve_active_approval(False))

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
        session = self._active_runtime()["session"]
        if session is not None:
            try:
                session.set_permission_mode(mode)
            except ValueError as exc:
                messagebox.showerror("权限模式", str(exc), parent=self.root)
                return
        self._set_status(f"权限：{PERMISSION_LABELS[mode]}", COLORS["warning"] if mode == "full-access" else COLORS["success"])

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
                destination.configure(values=self._model_choices(models, current))
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
        self.model_entry.configure(
            values=self._model_choices(list(self.model_entry.cget("values")), model)
        )
        os.environ["AI_HARNESS_API_KEY"] = api_key
        os.environ["AI_HARNESS_BASE_URL"] = api_url
        os.environ["AI_HARNESS_MODEL"] = model
        os.environ["AI_HARNESS_ENV_FILE"] = str(self.config_path)
        for runtime in self.runtimes.values():
            runtime["session"] = None

    def _on_close(self) -> None:
        self.closing = True
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
    HarnessGUI(
        root,
        workspace=workspace,
        approval_mode=approval_mode,
        full_access=full_access,
        model_name=model_name,
        max_turns=max_turns,
    )
    root.mainloop()
