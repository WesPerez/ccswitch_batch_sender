from __future__ import annotations

import datetime as dt
import json
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ccswitch_batch_sender import (
    APP_TITLE,
    DEFAULT_CONFIG,
    LEGACY_RANDOM_PROBE_PLACEHOLDER,
    PROMPT_CACHE_KEY_PLACEHOLDER,
    RANDOM_TASK_PLACEHOLDER,
    AttemptResult,
    ProgressEvent,
    Provider,
    ProviderCatalog,
    ProviderSummary,
    RunLogger,
    RunOutcome,
    SenderError,
    build_preview_body,
    build_result_dict,
    detect_codex_cli_version,
    endpoint_candidates,
    list_codex_providers,
    load_provider,
    normalize_config,
    resource_path,
    run_batch,
    save_result,
    save_saved_config,
)


BG = "#EEF2EF"
SURFACE = "#FFFFFF"
SURFACE_SOFT = "#F7F9F8"
HEADER = "#17352B"
HEADER_SOFT = "#24483B"
TEXT = "#17211D"
MUTED = "#64716A"
BORDER = "#D7DFDA"
ACCENT = "#18794E"
ACCENT_HOVER = "#12633F"
ACCENT_SOFT = "#DCEFE5"
DANGER = "#B42318"
DANGER_SOFT = "#FDE7E5"
WARNING = "#A15C0C"
WARNING_SOFT = "#FFF0D7"
SELECT = "#C7E7D5"

FONT_BODY = ("Noto Sans SC", 9)
FONT_BODY_MEDIUM = ("Noto Sans SC Medium", 9)
FONT_TITLE = ("Noto Sans SC Medium", 14)
FONT_SECTION = ("Noto Sans SC Medium", 10)
FONT_SMALL = ("Noto Sans SC", 8)
FONT_MONO = ("Cascadia Mono", 9)

ENDPOINT_LABELS = {
    "自动判断": "auto",
    "CC Switch 路径": "ccswitch",
    "OpenAI 标准路径": "openai",
}
ENDPOINT_VALUES = {value: label for label, value in ENDPOINT_LABELS.items()}


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.window: tk.Toplevel | None = None
        self.after_id: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: tk.Event[Any]) -> None:
        self._hide()
        self.after_id = self.widget.after(450, self._show)

    def _show(self) -> None:
        if not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + self.widget.winfo_width() // 2
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        label = tk.Label(
            self.window,
            text=self.text,
            bg=TEXT,
            fg="#FFFFFF",
            font=FONT_SMALL,
            padx=8,
            pady=5,
            relief="flat",
        )
        label.pack()
        self.window.update_idletasks()
        self.window.geometry(f"+{x - self.window.winfo_width() // 2}+{y}")

    def _hide(self, _event: tk.Event[Any] | None = None) -> None:
        if self.after_id is not None:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
        if self.window is not None:
            self.window.destroy()
            self.window = None


class IconStore:
    FILES = {
        "app": "assets/app-32.png",
        "send": "assets/send-light.png",
        "stop": "assets/square-dark.png",
        "check-square": "assets/square-check-dark.png",
        "refresh": "assets/refresh-dark.png",
        "save": "assets/save-dark.png",
        "copy": "assets/copy-dark.png",
        "download": "assets/download-dark.png",
        "reset": "assets/rotate-ccw-dark.png",
        "clear": "assets/trash-2-dark.png",
    }

    def __init__(self, root: tk.Tk) -> None:
        self.images: dict[str, tk.PhotoImage] = {}
        for name, relative in self.FILES.items():
            try:
                self.images[name] = tk.PhotoImage(master=root, file=str(resource_path(relative)))
            except tk.TclError:
                continue

    def get(self, name: str) -> tk.PhotoImage | None:
        return self.images.get(name)


class BatchSenderApp:
    def __init__(self, root: tk.Tk, initial_config: dict[str, Any], *, smoke_ui: bool = False) -> None:
        self.root = root
        self.base_config = normalize_config(initial_config)
        self.smoke_ui = smoke_ui
        self.icons = IconStore(root)
        self.stop_event = threading.Event()
        self.running_thread: threading.Thread | None = None
        self.running = False
        self.blocked_by_unfinished = False
        self.can_start = False
        self.preview_after_id: str | None = None
        self.notice_after_id: str | None = None
        self.elapsed_after_id: str | None = None
        self.run_started_at = 0.0
        self.latest_result: AttemptResult | None = None
        self.last_run_config: dict[str, Any] | None = None
        self.log_lines: list[str] = []
        self.catalog: ProviderCatalog | None = None
        self.provider_by_id: dict[str, ProviderSummary] = {}
        self.provider_value_map: dict[str, str] = {}
        self.preview_provider: Provider | None = None
        self._editable_ttk: list[ttk.Widget] = []

        self._init_window()
        self._init_style()
        self._init_variables()
        self._build_layout()
        self._bind_events()
        self._load_initial_values()
        self.refresh_providers(reset_to_current=True)
        self.schedule_preview()

        if self.smoke_ui:
            self.root.geometry("900x720+40+40")
            self.root.after(30000, self.root.destroy)

    def _init_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("900x720")
        self.root.minsize(820, 680)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        icon_path = resource_path("assets/app.ico")
        if icon_path.exists():
            try:
                self.root.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self.root.option_add("*TCombobox*Listbox.font", FONT_BODY)

    def _init_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background=BG)
        style.configure("Surface.TFrame", background=SURFACE)
        style.configure("Soft.TFrame", background=SURFACE_SOFT)
        style.configure("App.TLabel", background=BG, foreground=TEXT, font=FONT_BODY)
        style.configure("Surface.TLabel", background=SURFACE, foreground=TEXT, font=FONT_BODY)
        style.configure("Soft.TLabel", background=SURFACE_SOFT, foreground=TEXT, font=FONT_BODY)
        style.configure("Field.TLabel", background=SURFACE, foreground=MUTED, font=FONT_SMALL)
        style.configure("Section.TLabel", background=SURFACE, foreground=TEXT, font=FONT_SECTION)
        style.configure("Meta.TLabel", background=SURFACE, foreground=MUTED, font=FONT_SMALL)
        style.configure("Limit.TLabel", background=SURFACE, foreground=ACCENT, font=FONT_BODY_MEDIUM)
        style.configure(
            "Primary.TButton",
            background=ACCENT,
            foreground="#FFFFFF",
            bordercolor=ACCENT,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            padding=(14, 8),
            font=FONT_BODY_MEDIUM,
            relief="flat",
        )
        style.map(
            "Primary.TButton",
            background=[("pressed", ACCENT_HOVER), ("active", ACCENT_HOVER), ("disabled", "#9AB6A8")],
            foreground=[("disabled", "#EDF3F0")],
            bordercolor=[("pressed", ACCENT_HOVER), ("active", ACCENT_HOVER)],
        )
        style.configure(
            "Secondary.TButton",
            background=SURFACE,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            padding=(11, 8),
            font=FONT_BODY_MEDIUM,
            relief="flat",
        )
        style.map(
            "Secondary.TButton",
            background=[("pressed", "#E8EEEA"), ("active", SURFACE_SOFT), ("disabled", "#F1F3F2")],
            foreground=[("disabled", "#9AA39E")],
        )
        style.configure(
            "Tool.TButton",
            background=SURFACE,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            padding=6,
            relief="flat",
        )
        style.map(
            "Tool.TButton",
            background=[("pressed", "#E4EAE6"), ("active", SURFACE_SOFT), ("disabled", "#F1F3F2")],
        )
        style.configure(
            "Danger.TButton",
            background=SURFACE,
            foreground=DANGER,
            bordercolor="#E9C3C0",
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            padding=(11, 8),
            font=FONT_BODY_MEDIUM,
            relief="flat",
        )
        style.map(
            "Danger.TButton",
            background=[("pressed", "#F8D9D6"), ("active", DANGER_SOFT), ("disabled", "#F4F2F2")],
            foreground=[("disabled", "#B99C99")],
        )
        style.configure(
            "TEntry",
            fieldbackground=SURFACE_SOFT,
            foreground=TEXT,
            bordercolor=BORDER,
            insertcolor=TEXT,
            padding=(7, 6),
            font=FONT_BODY,
        )
        style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        style.configure(
            "TCombobox",
            fieldbackground=SURFACE_SOFT,
            background=SURFACE_SOFT,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            padding=(7, 6),
            font=FONT_BODY,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", SURFACE_SOFT), ("disabled", "#F0F2F1")],
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=SURFACE_SOFT,
            foreground=TEXT,
            bordercolor=BORDER,
            arrowcolor=MUTED,
            padding=(6, 5),
            font=FONT_BODY,
            arrowsize=12,
        )
        style.map("TSpinbox", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        style.configure("TCheckbutton", background=SURFACE, foreground=TEXT, font=FONT_BODY)
        style.map("TCheckbutton", background=[("active", SURFACE)])
        style.configure("Compact.TCheckbutton", background=SURFACE, foreground=TEXT, font=FONT_SMALL)
        style.map("Compact.TCheckbutton", background=[("active", SURFACE)])
        for name, font in (("Icon.TCheckbutton", FONT_BODY), ("IconCompact.TCheckbutton", FONT_SMALL)):
            style.configure(name, background=SURFACE, foreground=TEXT, font=font, padding=(0, 1))
            style.map(name, background=[("active", SURFACE)], foreground=[("disabled", "#9AA39E")])
            style.layout(
                name,
                [
                    (
                        "Checkbutton.padding",
                        {
                            "sticky": "nswe",
                            "children": [("Checkbutton.label", {"sticky": "nswe"})],
                        },
                    )
                ],
            )
        style.configure("Managed.TLabel", background=SURFACE, foreground=ACCENT, font=FONT_SMALL)
        style.configure("TNotebook", background=SURFACE, borderwidth=0, tabmargins=0)
        style.configure(
            "TNotebook.Tab",
            background=SURFACE,
            foreground=MUTED,
            padding=(12, 7),
            font=FONT_BODY_MEDIUM,
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", SURFACE), ("active", SURFACE_SOFT)],
            foreground=[("selected", TEXT), ("active", TEXT)],
        )
        style.configure(
            "Green.Horizontal.TProgressbar",
            background=ACCENT,
            troughcolor="#E2E9E5",
            bordercolor="#E2E9E5",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=7,
        )

    def _init_variables(self) -> None:
        self.provider_var = tk.StringVar()
        self.provider_meta_var = tk.StringVar(value="正在读取 CC Switch provider")
        self.provider_state_var = tk.StringVar(value="")
        self.random_probe_var = tk.BooleanVar()
        self.request_count_var = tk.StringVar()
        self.retry_count_var = tk.StringVar()
        self.retry_interval_var = tk.StringVar()
        self.model_override_var = tk.StringVar()
        self.base_url_override_var = tk.StringVar()
        self.endpoint_style_var = tk.StringVar()
        self.max_tokens_var = tk.StringVar()
        self.request_timeout_var = tk.StringVar()
        self.max_wait_var = tk.StringVar()
        self.poll_interval_var = tk.StringVar()
        self.unique_cache_var = tk.BooleanVar()
        self.send_codex_version_var = tk.BooleanVar()
        self.save_full_response_var = tk.BooleanVar()
        self.custom_body_var = tk.BooleanVar()
        self.body_mode_var = tk.StringVar(value="自动生成 · 只读")
        self.limit_var = tk.StringVar()
        self.progress_text_var = tk.StringVar(value="未运行")
        self.metrics_var = tk.StringVar(value="已发送 0 · 已完成 0 · 失败 0")
        self.elapsed_var = tk.StringVar(value="0.0 s")
        self.result_title_var = tk.StringVar(value="首个成功结果")
        self.notice_var = tk.StringVar(value="")

    def _build_layout(self) -> None:
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self.root, bg=HEADER, height=58)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)
        app_icon = self.icons.get("app")
        if app_icon is not None:
            tk.Label(header, image=app_icon, bg=HEADER, bd=0).grid(row=0, column=0, padx=(16, 10), pady=12)
        else:
            tk.Frame(header, width=8, height=30, bg=ACCENT).grid(row=0, column=0, padx=(16, 10), pady=14)
        tk.Label(header, text=APP_TITLE, bg=HEADER, fg="#FFFFFF", font=FONT_TITLE).grid(
            row=0, column=1, sticky="w"
        )
        self.status_chip = tk.Label(
            header,
            text="就绪",
            bg=HEADER_SOFT,
            fg="#D8EEE3",
            font=FONT_BODY_MEDIUM,
            padx=12,
            pady=6,
        )
        self.status_chip.grid(row=0, column=2, padx=16)

        main = ttk.Frame(self.root, style="App.TFrame", padding=(14, 12, 14, 12))
        main.grid(row=1, column=0, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        provider_band = ttk.Frame(main, style="Surface.TFrame", padding=(12, 10))
        provider_band.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        provider_band.grid_columnconfigure(1, weight=1)
        ttk.Label(provider_band, text="Provider", style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.provider_combo = ttk.Combobox(provider_band, textvariable=self.provider_var, state="readonly")
        self.provider_combo.grid(row=0, column=1, sticky="ew")
        self._editable_ttk.append(self.provider_combo)
        self.refresh_button = self._tool_button(provider_band, "refresh", self.refresh_providers, "刷新 provider")
        self.refresh_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(provider_band, textvariable=self.provider_meta_var, style="Meta.TLabel").grid(
            row=1, column=1, sticky="w", pady=(5, 0)
        )
        self.provider_state_label = ttk.Label(provider_band, textvariable=self.provider_state_var, style="Meta.TLabel")
        self.provider_state_label.grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(5, 0))

        content = ttk.Frame(main, style="App.TFrame")
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=47, uniform="content")
        content.grid_columnconfigure(1, weight=53, uniform="content")
        content.grid_rowconfigure(0, weight=1)

        left = ttk.Frame(content, style="Surface.TFrame", padding=(12, 10))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=1)
        settings_tabs = ttk.Notebook(left)
        settings_tabs.grid(row=0, column=0, sticky="nsew")
        common_tab = ttk.Frame(settings_tabs, style="Surface.TFrame", padding=(2, 9, 2, 0))
        advanced_tab = ttk.Frame(settings_tabs, style="Surface.TFrame", padding=(2, 9, 2, 0))
        settings_tabs.add(common_tab, text="发送设置")
        settings_tabs.add(advanced_tab, text="高级设置")
        self._build_common_settings(common_tab)
        self._build_advanced_settings(advanced_tab)

        right = ttk.Frame(content, style="Surface.TFrame", padding=(12, 10))
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)
        self.output_tabs = ttk.Notebook(right)
        self.output_tabs.grid(row=0, column=0, sticky="nsew")
        body_tab = ttk.Frame(self.output_tabs, style="Surface.TFrame", padding=(2, 8, 2, 0))
        log_tab = ttk.Frame(self.output_tabs, style="Surface.TFrame", padding=(2, 8, 2, 0))
        self.output_tabs.add(body_tab, text="请求体")
        self.output_tabs.add(log_tab, text="运行日志")
        self._build_body_tab(body_tab)
        self._build_log_tab(log_tab)

        status_band = ttk.Frame(main, style="Surface.TFrame", padding=(12, 10))
        status_band.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        status_band.grid_columnconfigure(0, weight=42, uniform="status")
        status_band.grid_columnconfigure(1, weight=58, uniform="status")
        self._build_progress_panel(status_band)
        self._build_result_panel(status_band)

        action_bar = ttk.Frame(main, style="App.TFrame")
        action_bar.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        action_bar.grid_columnconfigure(3, weight=1)
        self.start_button = self._button(
            action_bar,
            "发送 20 个",
            "send",
            self.start_run,
            "Primary.TButton",
        )
        self.start_button.grid(row=0, column=0, sticky="w")
        self.stop_button = self._button(action_bar, "停止", "stop", self.stop_run, "Danger.TButton")
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.stop_button.state(["disabled"])
        self.save_settings_button = self._button(
            action_bar,
            "保存默认",
            "save",
            self.save_defaults,
            "Secondary.TButton",
        )
        self.save_settings_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(action_bar, textvariable=self.notice_var, style="App.TLabel").grid(
            row=0, column=3, sticky="e", padx=(12, 8)
        )
        self.reset_button = self._tool_button(action_bar, "reset", self.reset_defaults, "恢复默认设置")
        self.reset_button.grid(row=0, column=4, sticky="e")

    def _build_common_settings(self, parent: ttk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        prompt_header = ttk.Frame(parent, style="Surface.TFrame")
        prompt_header.grid(row=0, column=0, sticky="ew")
        prompt_header.grid_columnconfigure(0, weight=1)
        ttk.Label(prompt_header, text="固定提示词", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.random_probe_check = self._checkbutton(
            prompt_header,
            "每次随机任务",
            self.random_probe_var,
            compact=True,
        )
        self.random_probe_check.grid(row=0, column=1, sticky="e")
        self._editable_ttk.append(self.random_probe_check)
        self.prompt_text = tk.Text(
            parent,
            height=4,
            wrap="word",
            undo=True,
            bg=SURFACE_SOFT,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=SELECT,
            selectforeground=TEXT,
            font=("Noto Sans SC", 10),
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            padx=8,
            pady=7,
        )
        self.prompt_text.grid(row=1, column=0, sticky="nsew", pady=(5, 9))

        numeric = ttk.Frame(parent, style="Surface.TFrame")
        numeric.grid(row=2, column=0, sticky="ew")
        for column in range(3):
            numeric.grid_columnconfigure(column, weight=1, uniform="numeric")
        self.request_count_spin = self._spin_field(
            numeric,
            0,
            "请求次数",
            self.request_count_var,
            1,
            100,
        )
        self.retry_count_spin = self._spin_field(
            numeric,
            1,
            "重试次数",
            self.retry_count_var,
            0,
            10,
        )
        self.retry_interval_spin = self._spin_field(
            numeric,
            2,
            "重试间隔（秒）",
            self.retry_interval_var,
            0,
            3600,
            increment=1,
        )
        ttk.Label(parent, textvariable=self.limit_var, style="Limit.TLabel").grid(
            row=3, column=0, sticky="w", pady=(10, 0)
        )

    def _build_advanced_settings(self, parent: ttk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=1)

        ttk.Label(parent, text="模型覆盖", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(parent, text="Endpoint 模式", style="Field.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.model_entry = ttk.Entry(parent, textvariable=self.model_override_var)
        self.model_entry.grid(row=1, column=0, sticky="ew", pady=(4, 9))
        self.endpoint_combo = ttk.Combobox(
            parent,
            textvariable=self.endpoint_style_var,
            values=list(ENDPOINT_LABELS),
            state="readonly",
        )
        self.endpoint_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(4, 9))

        options_line = ttk.Frame(parent, style="Surface.TFrame")
        options_line.grid(row=2, column=0, columnspan=2, sticky="ew")
        options_line.grid_columnconfigure(0, weight=1, minsize=54)
        ttk.Label(options_line, text="地址覆盖", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.unique_check = self._checkbutton(
            options_line,
            "唯一缓存",
            self.unique_cache_var,
            compact=True,
        )
        self.unique_check.grid(row=0, column=1, sticky="e", padx=(8, 0))
        Tooltip(self.unique_check, "为每个请求生成不同的 prompt_cache_key")
        self.full_response_check = self._checkbutton(
            options_line,
            "完整响应",
            self.save_full_response_var,
            compact=True,
        )
        self.full_response_check.grid(row=0, column=2, sticky="e", padx=(8, 0))
        Tooltip(self.full_response_check, "导出结果时包含完整响应")
        self.codex_version_check = self._checkbutton(
            options_line,
            "Codex 版本",
            self.send_codex_version_var,
            compact=True,
        )
        self.codex_version_check.grid(row=0, column=3, sticky="e", padx=(8, 0))
        Tooltip(self.codex_version_check, "向 provider 附带本机 Codex CLI 版本请求头")
        self.base_url_entry = ttk.Entry(parent, textvariable=self.base_url_override_var)
        self.base_url_entry.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 9))

        timing = ttk.Frame(parent, style="Surface.TFrame")
        timing.grid(row=4, column=0, columnspan=2, sticky="ew")
        for column in range(3):
            timing.grid_columnconfigure(column, weight=1, uniform="timing")
        self.max_tokens_spin = self._spin_field(
            timing,
            0,
            "输出 token",
            self.max_tokens_var,
            1,
            4096,
        )
        self.request_timeout_spin = self._spin_field(
            timing,
            1,
            "单次超时（秒）",
            self.request_timeout_var,
            1,
            86400,
        )
        self.max_wait_spin = self._spin_field(
            timing,
            2,
            "总等待（秒）",
            self.max_wait_var,
            0,
            86400,
        )

        self._editable_ttk.extend(
            [
                self.model_entry,
                self.endpoint_combo,
                self.base_url_entry,
                self.max_tokens_spin,
                self.request_timeout_spin,
                self.max_wait_spin,
                self.unique_check,
                self.full_response_check,
                self.codex_version_check,
            ]
        )

    def _build_body_tab(self, parent: ttk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        toolbar = ttk.Frame(parent, style="Surface.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        toolbar.grid_columnconfigure(1, weight=1)
        self.custom_body_check = self._checkbutton(
            toolbar,
            "自定义 JSON",
            self.custom_body_var,
            command=self.toggle_custom_body,
        )
        self.custom_body_check.grid(row=0, column=0, sticky="w")
        self.body_mode_label = ttk.Label(toolbar, textvariable=self.body_mode_var, style="Managed.TLabel")
        self.body_mode_label.grid(row=0, column=1, sticky="w", padx=(10, 0))
        Tooltip(self.body_mode_label, "高亮占位符会在每个请求发送前由应用替换")
        self.body_copy_button = self._tool_button(toolbar, "copy", self.copy_request_body, "复制请求体")
        self.body_copy_button.grid(row=0, column=2, padx=(6, 0))
        self.body_reset_button = self._tool_button(toolbar, "reset", self.reset_request_body, "恢复自动生成")
        self.body_reset_button.grid(row=0, column=3, padx=(6, 0))
        self._editable_ttk.extend([self.custom_body_check, self.body_reset_button])

        frame = tk.Frame(parent, bg=SURFACE)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        self.body_text = tk.Text(
            frame,
            wrap="none",
            bg="#14211C",
            fg="#DCE9E2",
            insertbackground="#DCE9E2",
            selectbackground="#315C4A",
            selectforeground="#FFFFFF",
            font=FONT_MONO,
            relief="flat",
            highlightthickness=0,
            padx=9,
            pady=8,
            undo=True,
        )
        body_y = ttk.Scrollbar(frame, orient="vertical", command=self.body_text.yview)
        body_x = ttk.Scrollbar(frame, orient="horizontal", command=self.body_text.xview)
        self.body_text.configure(yscrollcommand=body_y.set, xscrollcommand=body_x.set)
        self.body_text.tag_configure("managed", foreground="#B9F0D0", background="#24483B")
        self.body_text.grid(row=0, column=0, sticky="nsew")
        body_y.grid(row=0, column=1, sticky="ns")
        body_x.grid(row=1, column=0, sticky="ew")

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        toolbar = ttk.Frame(parent, style="Surface.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        toolbar.grid_columnconfigure(0, weight=1)
        ttk.Label(toolbar, text="本次会话", style="Meta.TLabel").grid(row=0, column=0, sticky="w")
        copy_button = self._tool_button(toolbar, "copy", self.copy_log, "复制日志")
        copy_button.grid(row=0, column=1, padx=(6, 0))
        export_button = self._tool_button(toolbar, "download", self.export_log, "导出日志")
        export_button.grid(row=0, column=2, padx=(6, 0))
        clear_button = self._tool_button(toolbar, "clear", self.clear_log, "清空日志")
        clear_button.grid(row=0, column=3, padx=(6, 0))

        frame = tk.Frame(parent, bg=SURFACE)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            frame,
            wrap="word",
            state="disabled",
            bg=SURFACE_SOFT,
            fg=TEXT,
            selectbackground=SELECT,
            selectforeground=TEXT,
            font=FONT_MONO,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            padx=8,
            pady=7,
        )
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    def _build_progress_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, style="Surface.TFrame")
        frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        frame.grid_columnconfigure(0, weight=1)
        heading = ttk.Frame(frame, style="Surface.TFrame")
        heading.grid(row=0, column=0, sticky="ew")
        heading.grid_columnconfigure(0, weight=1)
        ttk.Label(heading, text="运行状态", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(heading, textvariable=self.elapsed_var, style="Meta.TLabel").grid(row=0, column=1, sticky="e")
        ttk.Label(frame, textvariable=self.progress_text_var, style="Surface.TLabel").grid(
            row=1, column=0, sticky="w", pady=(6, 5)
        )
        self.progressbar = ttk.Progressbar(frame, mode="determinate", style="Green.Horizontal.TProgressbar", maximum=20)
        self.progressbar.grid(row=2, column=0, sticky="ew")
        ttk.Label(frame, textvariable=self.metrics_var, style="Meta.TLabel").grid(
            row=3, column=0, sticky="w", pady=(5, 0)
        )

    def _build_result_panel(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, style="Surface.TFrame")
        frame.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        frame.grid_columnconfigure(0, weight=1)
        heading = ttk.Frame(frame, style="Surface.TFrame")
        heading.grid(row=0, column=0, sticky="ew")
        heading.grid_columnconfigure(0, weight=1)
        ttk.Label(heading, textvariable=self.result_title_var, style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.result_copy_button = self._tool_button(heading, "copy", self.copy_result, "复制结果")
        self.result_copy_button.grid(row=0, column=1, padx=(6, 0))
        self.result_export_button = self._tool_button(heading, "download", self.export_result, "导出结果")
        self.result_export_button.grid(row=0, column=2, padx=(6, 0))
        self.result_copy_button.state(["disabled"])
        self.result_export_button.state(["disabled"])
        self.result_text = tk.Text(
            frame,
            height=4,
            wrap="word",
            state="disabled",
            bg=SURFACE_SOFT,
            fg=TEXT,
            selectbackground=SELECT,
            selectforeground=TEXT,
            font=FONT_BODY,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            padx=8,
            pady=6,
        )
        self.result_text.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self._set_readonly_text(self.result_text, "等待首个成功响应")

    def _spin_field(
        self,
        parent: ttk.Frame,
        column: int,
        label: str,
        variable: tk.StringVar,
        minimum: float,
        maximum: float,
        *,
        increment: float = 1,
    ) -> ttk.Spinbox:
        holder = ttk.Frame(parent, style="Surface.TFrame")
        holder.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))
        holder.grid_columnconfigure(0, weight=1)
        ttk.Label(holder, text=label, style="Field.TLabel").grid(row=0, column=0, sticky="w")
        spin = ttk.Spinbox(holder, textvariable=variable, from_=minimum, to=maximum, increment=increment)
        spin.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._editable_ttk.append(spin)
        return spin

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        icon_name: str,
        command: Callable[[], None],
        style: str,
    ) -> ttk.Button:
        image = self.icons.get(icon_name)
        options: dict[str, Any] = {"text": text, "command": command, "style": style}
        if image is not None:
            options.update({"image": image, "compound": "left"})
        return ttk.Button(parent, **options)

    def _checkbutton(
        self,
        parent: tk.Misc,
        text: str,
        variable: tk.BooleanVar,
        *,
        compact: bool = False,
        command: Callable[[], None] | None = None,
    ) -> ttk.Checkbutton:
        options: dict[str, Any] = {
            "text": text,
            "variable": variable,
            "style": "IconCompact.TCheckbutton" if compact else "Icon.TCheckbutton",
        }
        if command is not None:
            options["command"] = command
        unchecked = self.icons.get("stop")
        checked = self.icons.get("check-square")
        if unchecked is not None and checked is not None:
            options.update({"image": (unchecked, "selected", checked), "compound": "left"})
        return ttk.Checkbutton(parent, **options)

    def _tool_button(
        self,
        parent: tk.Misc,
        icon_name: str,
        command: Callable[[], None],
        tooltip: str,
    ) -> ttk.Button:
        image = self.icons.get(icon_name)
        button = ttk.Button(
            parent,
            text="" if image is not None else tooltip,
            image=image,
            command=command,
            style="Tool.TButton",
            width=3 if image is not None else 0,
        )
        if image is not None:
            Tooltip(button, tooltip)
        return button

    def _bind_events(self) -> None:
        self.provider_combo.bind("<<ComboboxSelected>>", lambda _event: self.schedule_preview())
        self.endpoint_combo.bind("<<ComboboxSelected>>", lambda _event: self.schedule_preview())
        self.prompt_text.bind("<<Modified>>", self._on_prompt_modified)
        self.body_text.bind("<<Modified>>", self._on_body_modified)
        for variable in (
            self.request_count_var,
            self.retry_count_var,
            self.retry_interval_var,
            self.model_override_var,
            self.base_url_override_var,
            self.max_tokens_var,
            self.request_timeout_var,
            self.max_wait_var,
            self.poll_interval_var,
        ):
            variable.trace_add("write", lambda *_args: self.schedule_preview())
        self.unique_cache_var.trace_add("write", lambda *_args: self.schedule_preview())
        self.send_codex_version_var.trace_add("write", lambda *_args: self.schedule_preview())
        self.save_full_response_var.trace_add("write", lambda *_args: self.schedule_preview())
        self.random_probe_var.trace_add("write", lambda *_args: self.schedule_preview())
        self.root.bind("<Control-Return>", lambda _event: self.start_run())
        self.root.bind("<Escape>", lambda _event: self.stop_run())
        self.root.bind("<F5>", lambda _event: self.refresh_providers())
        self.root.bind("<Control-s>", lambda _event: self.save_defaults())

    def _load_initial_values(self) -> None:
        config = self.base_config
        self._replace_text(self.prompt_text, str(config["message"]))
        self.random_probe_var.set(bool(config["random_probe_enabled"]))
        self.request_count_var.set(str(config["request_count"]))
        self.retry_count_var.set(str(config["retry_count"]))
        self.retry_interval_var.set(self._number_text(config["retry_interval_seconds"]))
        self.model_override_var.set(str(config["model"]))
        self.base_url_override_var.set(str(config["base_url"]))
        self.endpoint_style_var.set(ENDPOINT_VALUES.get(str(config["endpoint_style"]), "自动判断"))
        self.max_tokens_var.set(str(config["max_output_tokens"]))
        self.request_timeout_var.set(self._number_text(config["request_timeout_seconds"]))
        self.max_wait_var.set(self._number_text(config["max_wait_seconds"]))
        self.poll_interval_var.set(self._number_text(config["poll_interval_seconds"]))
        self.unique_cache_var.set(bool(config["unique_prompt_cache_key"]))
        self.send_codex_version_var.set(bool(config["send_codex_version_header"]))
        self.save_full_response_var.set(bool(config["save_full_response"]))
        self.custom_body_var.set(bool(config["custom_body_enabled"]))
        if self.custom_body_var.get() and isinstance(config.get("custom_body"), dict):
            self._replace_text(self.body_text, json.dumps(config["custom_body"], ensure_ascii=False, indent=2))
            self.body_text.configure(state="normal")
        else:
            self.body_text.configure(state="disabled")
        self._update_body_management()
        self._update_limit_text()

    @staticmethod
    def _number_text(value: Any) -> str:
        number = float(value)
        return str(int(number)) if number.is_integer() else str(number)

    def refresh_providers(self, reset_to_current: bool = True) -> None:
        if self.running:
            return
        try:
            self.catalog = list_codex_providers(self.base_config)
            self.provider_by_id = {item.provider_id: item for item in self.catalog.providers}
            values: list[str] = []
            mapping: dict[str, str] = {}
            current = self.provider_by_id.get(self.catalog.current_provider_id)
            if current is not None:
                label = self._provider_label(current, current=True)
                values.append(label)
                mapping[label] = "current"
            else:
                label = "当前 provider · 未识别"
                values.append(label)
                mapping[label] = "current"
            for item in self.catalog.providers:
                if item.provider_id == self.catalog.current_provider_id:
                    continue
                label = self._provider_label(item, current=False)
                if label in mapping:
                    label = f"{label} · {item.provider_id[:8]}"
                values.append(label)
                mapping[label] = item.provider_id
            self.provider_value_map = mapping
            self.provider_combo.configure(values=values)
            if reset_to_current or self.provider_var.get() not in mapping:
                self.provider_var.set(values[0])
            self._show_notice(f"已读取 {len(self.catalog.providers)} 个 provider")
            self.schedule_preview()
        except SenderError as exc:
            self.catalog = None
            self.provider_by_id = {}
            self.provider_value_map = {}
            self.provider_combo.configure(values=[])
            self.provider_var.set("")
            self.provider_meta_var.set(str(exc).replace("\n", " "))
            self.provider_state_var.set("不可用")
            self.can_start = False
            self._refresh_start_state()

    @staticmethod
    def _provider_label(summary: ProviderSummary, *, current: bool) -> str:
        prefix = "当前 · " if current else ""
        model = f" · {summary.model}" if summary.model else ""
        unavailable = f" · {summary.unavailable_reason}" if not summary.available else ""
        return f"{prefix}{summary.name}{model}{unavailable}"

    def _selected_provider_id(self) -> str:
        return self.provider_value_map.get(self.provider_var.get(), "current")

    def _selected_summary(self) -> ProviderSummary | None:
        provider_id = self._selected_provider_id()
        if provider_id == "current":
            provider_id = self.catalog.current_provider_id if self.catalog is not None else ""
        return self.provider_by_id.get(provider_id)

    def _on_prompt_modified(self, _event: tk.Event[Any]) -> None:
        if self.prompt_text.edit_modified():
            self.prompt_text.edit_modified(False)
            self.schedule_preview()

    def _on_body_modified(self, _event: tk.Event[Any]) -> None:
        if self.body_text.edit_modified():
            self.body_text.edit_modified(False)
            self._update_body_management()
            if self.custom_body_var.get():
                self.schedule_preview()

    def schedule_preview(self) -> None:
        self._update_limit_text()
        if self.preview_after_id is not None:
            self.root.after_cancel(self.preview_after_id)
        self.preview_after_id = self.root.after(160, self.refresh_preview)

    def refresh_preview(self) -> None:
        self.preview_after_id = None
        if self.running:
            return
        try:
            config = self.collect_config()
            provider = load_provider(config)
            self.preview_provider = provider
            if not self.custom_body_var.get():
                body = build_preview_body(provider, config)
                self._set_body_text(json.dumps(body, ensure_ascii=False, indent=2), editable=False)
            else:
                self.body_text.configure(state="normal")
                self._update_body_management()
            host = urllib.parse.urlsplit(provider.base_url).netloc or provider.base_url
            format_label = "Responses" if provider.api_format == "openai_responses" else "Chat Completions"
            codex_version = detect_codex_cli_version().version or "未检测"
            self.provider_meta_var.set(f"{provider.model}  ·  {format_label}  ·  {host}  ·  Codex CLI {codex_version}")
            if self._selected_provider_id() == "current":
                self.provider_state_var.set("跟随 CC Switch")
            else:
                self.provider_state_var.set("仅本次使用")
            self.can_start = True
            self._refresh_start_state()
        except (SenderError, json.JSONDecodeError) as exc:
            self.preview_provider = None
            message = str(exc).replace("\n", " ")
            self.provider_meta_var.set(message)
            self.provider_state_var.set("需检查")
            self.can_start = False
            self._refresh_start_state()

    def collect_config(self) -> dict[str, Any]:
        data = dict(self.base_config)
        data.update(
            {
                "provider_id": self._selected_provider_id(),
                "model": self.model_override_var.get().strip(),
                "base_url": self.base_url_override_var.get().strip(),
                "message": self.prompt_text.get("1.0", "end-1c"),
                "random_probe_enabled": self.random_probe_var.get(),
                "request_count": self.request_count_var.get(),
                "retry_count": self.retry_count_var.get(),
                "retry_interval_seconds": self.retry_interval_var.get(),
                "max_output_tokens": self.max_tokens_var.get(),
                "request_timeout_seconds": self.request_timeout_var.get(),
                "max_wait_seconds": self.max_wait_var.get(),
                "poll_interval_seconds": self.poll_interval_var.get(),
                "endpoint_style": ENDPOINT_LABELS.get(self.endpoint_style_var.get(), "auto"),
                "unique_prompt_cache_key": self.unique_cache_var.get(),
                "send_codex_version_header": self.send_codex_version_var.get(),
                "save_full_response": self.save_full_response_var.get(),
                "custom_body_enabled": self.custom_body_var.get(),
            }
        )
        if self.custom_body_var.get():
            raw_body = self.body_text.get("1.0", "end-1c").strip()
            try:
                data["custom_body"] = json.loads(raw_body)
            except json.JSONDecodeError as exc:
                raise SenderError(f"自定义请求体 JSON 错误：第 {exc.lineno} 行第 {exc.colno} 列。") from exc
        else:
            data["custom_body"] = None
        return normalize_config(data)

    def toggle_custom_body(self) -> None:
        if self.running:
            return
        if self.custom_body_var.get():
            try:
                config = self.collect_config()
                provider = load_provider(config)
                if not self.body_text.get("1.0", "end-1c").strip():
                    self._set_body_text(
                        json.dumps(build_preview_body(provider, config), ensure_ascii=False, indent=2),
                        editable=True,
                    )
                else:
                    self.body_text.configure(state="normal")
            except SenderError:
                self.body_text.configure(state="normal")
        else:
            self.schedule_preview()
        self.schedule_preview()

    def reset_request_body(self) -> None:
        if self.running:
            return
        self.custom_body_var.set(False)
        self.schedule_preview()

    def _update_limit_text(self) -> None:
        try:
            count = int(self.request_count_var.get())
            retries = int(self.retry_count_var.get())
            cap = count * (1 + retries)
            self.limit_var.set(f"单次运行最多 {cap} 个 POST")
            self.start_button.configure(text=f"发送 {count} 个") if hasattr(self, "start_button") else None
        except (TypeError, ValueError):
            self.limit_var.set("请求次数或重试次数无效")

    def start_run(self) -> None:
        if self.running or self.blocked_by_unfinished or not self.can_start:
            return
        try:
            config = self.collect_config()
            load_provider(config)
        except SenderError as exc:
            self._show_notice(str(exc).replace("\n", " "), error=True)
            self.schedule_preview()
            return

        self.running = True
        self.stop_event.clear()
        self.latest_result = None
        self.last_run_config = config
        self.run_started_at = time.monotonic()
        self.progressbar.configure(maximum=int(config["request_count"]) * (1 + int(config["retry_count"])), value=0)
        self.progress_text_var.set("正在准备请求")
        self.metrics_var.set("已发送 0 · 已完成 0 · 失败 0")
        self.result_title_var.set("首个成功结果")
        self._set_readonly_text(self.result_text, "等待首个成功响应")
        self.result_copy_button.state(["disabled"])
        self.result_export_button.state(["disabled"])
        self.clear_log()
        self.output_tabs.select(1)
        self._set_status("running")
        self._set_editing_enabled(False)
        self.stop_button.state(["!disabled"])
        self._start_elapsed_clock()
        logger = RunLogger(callback=self.append_log_threadsafe)

        def job() -> None:
            try:
                outcome = run_batch(
                    config,
                    logger,
                    stop_event=self.stop_event,
                    on_winner=self.on_winner_threadsafe,
                    on_progress=self.on_progress_threadsafe,
                )
                self.root.after(0, lambda: self.finish_run(outcome))
            except Exception as exc:
                self.append_log_threadsafe(f"ERROR {type(exc).__name__}: {exc}")
                self.root.after(0, lambda: self.finish_error(exc))

        self.running_thread = threading.Thread(target=job, name="ccswitch-runner", daemon=True)
        self.running_thread.start()

    def stop_run(self) -> None:
        if not self.running or self.stop_event.is_set():
            return
        self.stop_event.set()
        self.progress_text_var.set("正在停止，等待已发送请求结束")
        self._set_status("stopping")
        self.stop_button.state(["disabled"])

    def on_winner_threadsafe(self, result: AttemptResult) -> None:
        self.root.after(0, lambda: self.show_winner(result))

    def on_progress_threadsafe(self, event: ProgressEvent) -> None:
        self.root.after(0, lambda: self.apply_progress(event))

    def append_log_threadsafe(self, line: str) -> None:
        self.root.after(0, lambda: self.append_log(line))

    def apply_progress(self, event: ProgressEvent) -> None:
        self.progressbar.configure(maximum=max(1, event.total_cap), value=event.completed_total)
        self.metrics_var.set(
            f"已发送 {event.launched_total}/{event.total_cap} · 已完成 {event.completed_total} · 失败 {event.failed_total}"
        )
        if event.kind == "round_start":
            self.progress_text_var.set(f"第 {event.round_no}/{event.max_rounds} 批正在发送")
        elif event.kind == "first_success" and event.winner is not None:
            self.progress_text_var.set(
                f"第 {event.round_no} 批 #{event.winner.index} 先返回，其余请求继续收尾"
            )
            self._set_status("success")
        elif event.kind == "retry_wait":
            self.progress_text_var.set(f"第 {event.round_no} 批无成功响应，{event.message}")
            self._set_status("waiting")
        elif event.kind == "stopping":
            self.progress_text_var.set(event.message)
            self._set_status("stopping")
        elif event.kind == "timeout":
            self.progress_text_var.set(event.message)
            self._set_status("warning")
        elif event.kind == "no_result":
            self.progress_text_var.set("已达到重试上限，没有成功响应")
        elif event.kind == "done":
            self.progress_text_var.set("本次运行已完成")

    def show_winner(self, result: AttemptResult) -> None:
        self.latest_result = result
        text = result.text.strip() or "已收到成功响应"
        lines = [f"第 {result.round_no} 批 #{result.index} · HTTP {result.status or '-'} · {result.latency_ms} ms"]
        if result.request_prompt:
            lines.append(f"请求：{result.request_prompt}")
        lines.append(f"响应：{text}")
        summary = "\n".join(lines)
        self.result_title_var.set("首个成功结果")
        self._set_readonly_text(self.result_text, summary)
        self.result_copy_button.state(["!disabled"])
        self.result_export_button.state(["!disabled"])

    def finish_run(self, outcome: RunOutcome) -> None:
        self.running = False
        self.stop_button.state(["disabled"])
        self._stop_elapsed_clock()
        if outcome.unfinished > 0:
            self.blocked_by_unfinished = True
            self.progress_text_var.set(f"仍有 {outcome.unfinished} 个请求未结束；关闭应用可中断")
            self._set_status("warning")
            self._set_editing_enabled(True)
            self.start_button.state(["disabled"])
            return
        self._set_editing_enabled(True)
        if outcome.winner is not None:
            self.progress_text_var.set("本次运行已完成")
            self._set_status("success")
        elif outcome.code == 130:
            self.progress_text_var.set("已停止")
            self._set_status("idle")
        else:
            self.progress_text_var.set("没有成功响应，请查看运行日志")
            self.result_title_var.set("尚无成功结果")
            self._set_readonly_text(self.result_text, "本次运行没有成功响应")
            self._set_status("error")
        self._refresh_start_state()

    def finish_error(self, exc: Exception) -> None:
        self.running = False
        self.stop_button.state(["disabled"])
        self._stop_elapsed_clock()
        self._set_editing_enabled(True)
        self.progress_text_var.set(str(exc).replace("\n", " "))
        self.result_title_var.set("运行失败")
        self._set_readonly_text(self.result_text, str(exc))
        self._set_status("error")
        self._refresh_start_state()

    def save_defaults(self) -> None:
        if self.running:
            return
        try:
            config = self.collect_config()
            config["provider_id"] = "current"
            save_saved_config(config)
            self.base_config.update(config)
            self._show_notice("默认设置已保存")
        except (SenderError, OSError) as exc:
            self._show_notice(str(exc).replace("\n", " "), error=True)

    def reset_defaults(self) -> None:
        if self.running:
            return
        self.base_config = normalize_config()
        self._load_initial_values()
        self.refresh_providers(reset_to_current=True)
        try:
            save_saved_config(self.base_config)
            self._show_notice("已恢复默认设置")
        except OSError as exc:
            self._show_notice(str(exc), error=True)

    def copy_request_body(self) -> None:
        self._copy_to_clipboard(self.body_text.get("1.0", "end-1c"), "请求体已复制")

    def copy_log(self) -> None:
        self._copy_to_clipboard("\n".join(self.log_lines), "日志已复制")

    def copy_result(self) -> None:
        if self.latest_result is None or self.last_run_config is None:
            return
        text = json.dumps(build_result_dict(self.latest_result, self.last_run_config), ensure_ascii=False, indent=2)
        self._copy_to_clipboard(text, "结果已复制")

    def export_result(self) -> None:
        if self.latest_result is None or self.last_run_config is None:
            return
        default_name = f"ccswitch-result-{dt.datetime.now():%Y%m%d-%H%M%S}.json"
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出结果",
            defaultextension=".json",
            initialfile=default_name,
            filetypes=[("JSON", "*.json"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            save_result(Path(selected), self.latest_result, self.last_run_config)
            self._show_notice("结果已导出")
        except OSError as exc:
            self._show_notice(f"导出失败：{exc}", error=True)

    def export_log(self) -> None:
        if not self.log_lines:
            self._show_notice("当前没有日志")
            return
        default_name = f"ccswitch-log-{dt.datetime.now():%Y%m%d-%H%M%S}.log"
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出日志",
            defaultextension=".log",
            initialfile=default_name,
            filetypes=[("日志", "*.log"), ("文本", "*.txt"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            Path(selected).write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
            self._show_notice("日志已导出")
        except OSError as exc:
            self._show_notice(f"导出失败：{exc}", error=True)

    def clear_log(self) -> None:
        self.log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def append_log(self, line: str) -> None:
        self.log_lines.append(line)
        if len(self.log_lines) > 1200:
            self.log_lines = self.log_lines[-1000:]
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        if int(float(self.log_text.index("end-1c").split(".")[0])) > 1200:
            self.log_text.delete("1.0", "201.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _copy_to_clipboard(self, text: str, notice: str) -> None:
        if not text:
            self._show_notice("没有可复制的内容")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.root.update_idletasks()
        self._show_notice(notice)

    def _show_notice(self, text: str, *, error: bool = False) -> None:
        if self.notice_after_id is not None:
            self.root.after_cancel(self.notice_after_id)
        self.notice_var.set(text[:90])
        if error and not self.running:
            self._set_status("error")
        self.notice_after_id = self.root.after(3200, lambda: self.notice_var.set(""))

    def _set_status(self, state: str) -> None:
        states = {
            "idle": ("就绪", HEADER_SOFT, "#D8EEE3"),
            "running": ("发送中", "#2A5B49", "#E0F5E9"),
            "success": ("已成功", "#1E6B47", "#E5F7EC"),
            "waiting": ("等待重试", "#70501F", "#FFF1D9"),
            "stopping": ("停止中", "#70501F", "#FFF1D9"),
            "warning": ("需注意", "#70501F", "#FFF1D9"),
            "error": ("失败", "#6D2B27", "#FDECEA"),
        }
        text, bg, fg = states.get(state, states["idle"])
        self.status_chip.configure(text=text, bg=bg, fg=fg)

    def _set_editing_enabled(self, enabled: bool) -> None:
        for widget in self._editable_ttk:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="readonly" if enabled else "disabled")
            else:
                widget.state(["!disabled"] if enabled else ["disabled"])
        self.prompt_text.configure(state="normal" if enabled else "disabled")
        if enabled and self.custom_body_var.get():
            self.body_text.configure(state="normal")
        else:
            self.body_text.configure(state="disabled")
        self.refresh_button.state(["!disabled"] if enabled else ["disabled"])
        self.save_settings_button.state(["!disabled"] if enabled else ["disabled"])
        self.reset_button.state(["!disabled"] if enabled else ["disabled"])
        self._refresh_start_state()

    def _refresh_start_state(self) -> None:
        if self.running or self.blocked_by_unfinished or not self.can_start:
            self.start_button.state(["disabled"])
        else:
            self.start_button.state(["!disabled"])

    def _start_elapsed_clock(self) -> None:
        self._stop_elapsed_clock()

        def tick() -> None:
            if not self.running:
                return
            self.elapsed_var.set(f"{time.monotonic() - self.run_started_at:.1f} s")
            self.elapsed_after_id = self.root.after(250, tick)

        tick()

    def _stop_elapsed_clock(self) -> None:
        if self.elapsed_after_id is not None:
            self.root.after_cancel(self.elapsed_after_id)
            self.elapsed_after_id = None

    def _set_body_text(self, text: str, *, editable: bool) -> None:
        self.body_text.configure(state="normal")
        self.body_text.delete("1.0", "end")
        self.body_text.insert("1.0", text)
        self.body_text.edit_modified(False)
        self._update_body_management()
        self.body_text.configure(state="normal" if editable else "disabled")

    def _update_body_management(self) -> None:
        self.body_text.tag_remove("managed", "1.0", "end")
        found = False
        for placeholder in (
            PROMPT_CACHE_KEY_PLACEHOLDER,
            RANDOM_TASK_PLACEHOLDER,
            LEGACY_RANDOM_PROBE_PLACEHOLDER,
        ):
            needle = json.dumps(placeholder, ensure_ascii=False)
            start = "1.0"
            while True:
                index = self.body_text.search(needle, start, stopindex="end")
                if not index:
                    break
                line = index.split(".", 1)[0]
                self.body_text.tag_add("managed", f"{line}.0", f"{line}.end")
                start = f"{index}+{len(needle)}c"
                found = True
        if not self.custom_body_var.get():
            self.body_mode_var.set("自动生成 · 只读")
            self.body_mode_label.configure(style="Meta.TLabel")
        elif found:
            self.body_mode_var.set("高亮占位符发送时自动替换")
            self.body_mode_label.configure(style="Managed.TLabel")
        else:
            self.body_mode_var.set("自定义内容原样发送")
            self.body_mode_label.configure(style="Meta.TLabel")

    @staticmethod
    def _replace_text(widget: tk.Text, text: str) -> None:
        previous = str(widget.cget("state"))
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.edit_modified(False)
        widget.configure(state=previous)

    @staticmethod
    def _set_readonly_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def on_close(self) -> None:
        if self.running or self.blocked_by_unfinished:
            if not messagebox.askyesno(
                "关闭应用",
                "仍有请求在运行。关闭会中断这些连接，是否继续？",
                parent=self.root,
            ):
                return
        self.stop_event.set()
        self.root.destroy()


def launch_gui(initial_config: dict[str, Any], *, smoke_ui: bool = False) -> int:
    root = tk.Tk()
    BatchSenderApp(root, initial_config, smoke_ui=smoke_ui)
    root.mainloop()
    return 0
