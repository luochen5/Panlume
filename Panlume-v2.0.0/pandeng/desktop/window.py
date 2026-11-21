"""Panlume 桌面主窗口。所有真实动作均由用户显式点击。"""

from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pandeng.desktop import APP_NAME, APP_VERSION, BUILD_CHANNEL, SCHEMA_VERSION
from pandeng.desktop.audit import DesktopAuditResult, DesktopAuditService
from pandeng.desktop.data_manager import DesktopDataManager
from pandeng.desktop.demo import DesktopDemoService
from pandeng.desktop.dpi import configure_windows_dpi, initialize_tk_scaling
from pandeng.desktop.health import check_ollama
from pandeng.desktop.history import DesktopHistoryResult, DesktopHistoryService
from pandeng.desktop.i18n import (
    DEFAULT_LANGUAGE,
    translate,
    translate_observation_message,
    translate_widget_tree,
)
from pandeng.desktop.live_acceptance import (
    DESKTOP_LIVE_CONFIRMATION,
    DesktopLiveAcceptanceService,
)
from pandeng.desktop.observation import ObservationRunner, ObservationSnapshot
from pandeng.desktop.ollama_page import OllamaManagerPage
from pandeng.desktop.paths import DesktopPaths, resource_path
from pandeng.desktop.review import DesktopReviewDetail, DesktopReviewService
from pandeng.desktop.reports import DesktopReportService
from pandeng.desktop.report_page import ReportPage
from pandeng.desktop.report_results import ReportResultsPage
from pandeng.desktop.replay import CsvReplayCard
from pandeng.desktop.summary import ObservationSummaryService
from pandeng.desktop.settings import (
    DesktopSettings,
    DesktopSettingsError,
    DesktopSettingsStore,
    StockChoice,
)
from pandeng.desktop.theme import DesktopTheme
from pandeng.desktop.widgets import ScrollablePage
from pandeng.data.schedule import CHINA_TZ, recent_weekdays
from pandeng.data.simulator import demo_time_range, make_simulator
from pandeng.config.schema import OllamaConfig
from pandeng.reporting.presets import ReportPresetStore


PAGE_TITLES = {
    "dashboard": "总览",
    "observation": "观察设置",
    "review": "本地复盘",
    "reports": "报告中心",
    "results": "报告成果",
    "audit": "数据体检",
    "history": "事件与摘要",
    "model": "模型与输出",
    "language": "语言 / Language",
    "data": "数据管理",
    "demo": "离线演示",
    "about": "关于盘灯",
}


class PanlumeWindow:
    def __init__(
        self,
        root: tk.Tk,
        paths: DesktopPaths | None = None,
        *,
        default_language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self.root = root
        self.paths = paths or DesktopPaths.default()
        self.paths.ensure_directories()
        self.store = DesktopSettingsStore(self.paths.settings_file)
        self._startup_warning = ""
        try:
            self.settings = self.store.load(default_language=default_language)
        except DesktopSettingsError as exc:
            self.settings = DesktopSettings(language=default_language)
            self._startup_warning = str(exc)
        # 当前窗口固定使用启动语言；保存新语言后由下次重启生效。
        self._ui_language = self.settings.language
        self.data = DesktopDataManager(self.paths.database)
        self.data.initialize()
        self.demo = DesktopDemoService(
            self.paths.demo_database, language_provider=lambda: self._ui_language
        )
        self.observation_summary = ObservationSummaryService(
            self.data.database,
            lambda: replace(self.settings, language=self._ui_language),
            self.paths.logs,
        )
        self.live_acceptance = DesktopLiveAcceptanceService(
            self.paths.root, self.paths.database,
        )
        self.review_service = DesktopReviewService(
            self.data.database, language_provider=lambda: self._ui_language
        )
        self.audit_service = DesktopAuditService(self.data.database)
        self.history_service = DesktopHistoryService(self.data.database)
        self.report_service = DesktopReportService(
            self.data.database,
            language_provider=lambda: self._ui_language,
            ollama_config_provider=lambda: OllamaConfig(
                enabled=self.settings.ollama_enabled,
                base_url=self.settings.ollama_base_url,
                model=self.settings.model_name,
                timeout_seconds=self.settings.ollama_timeout_seconds,
            ),
        )
        self.report_preset_store = ReportPresetStore(self.paths.report_presets_file)
        self._history_focus_event_id: str | None = None
        self.current_report = None
        self.active_tasks: set[str] = set()
        self.current_page = "dashboard"
        self.pages: dict[str, ttk.Frame] = {}
        self.page_hosts: dict[str, ScrollablePage] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self._logo: tk.PhotoImage | None = None
        self._about_logo: tk.PhotoImage | None = None
        self._cleanup_preview = None
        self.observation_runner: ObservationRunner | None = None
        self._ui_queue: queue.Queue = queue.Queue()
        self._ui_after_id: str | None = None
        self._closing = False

        root.title(f"{self.tr(APP_NAME)} v{APP_VERSION}")
        root.geometry("1180x760")
        root.minsize(980, 640)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<F11>", lambda _event: self.toggle_fullscreen())
        root.bind("<Escape>", lambda _event: self.exit_fullscreen())
        self.fullscreen = False
        self.theme = DesktopTheme(root, self.settings.theme)
        self._load_icons()
        self._build_ui()
        self.show_page("dashboard")
        self._ui_after_id = root.after(50, self._poll_ui_queue)
        if self._startup_warning:
            root.after(
                100,
                lambda: messagebox.showwarning(
                    self.tr("设置读取提示"),
                    self._startup_warning + "\n" + self.tr("当前使用默认设置；保存前可返回检查原文件。"),
                ),
            )

    def tr(self, text: str) -> str:
        return translate(text, self._ui_language)

    def trf(self, text: str, /, **values: object) -> str:
        return self.tr(text).format(**values)

    def _load_icons(self) -> None:
        assets = resource_path("pandeng/desktop/assets")
        try:
            self._logo = tk.PhotoImage(file=str(assets / "logo-64.png"))
            self._about_logo = tk.PhotoImage(file=str(assets / "logo-128.png"))
            self.root.iconphoto(True, self._about_logo)
            if os.name == "nt":
                self.root.iconbitmap(str(assets / "pandeng.ico"))
        except tk.TclError:
            self._logo = None
            self._about_logo = None

    def _build_ui(self) -> None:
        for child in self.root.winfo_children():
            child.destroy()
        self.pages.clear()
        self.page_hosts.clear()
        self.nav_buttons.clear()
        c = self.theme.colors
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        sidebar = ttk.Frame(shell, style="Sidebar.TFrame", width=220, padding=(18, 24))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = ttk.Frame(sidebar, style="Sidebar.TFrame")
        brand.pack(fill="x", pady=(0, 12))
        if self._logo is not None:
            ttk.Label(brand, image=self._logo, style="Sidebar.TLabel").pack(anchor="w")
        ttk.Label(brand, text="盘灯", style="Sidebar.TLabel", font=(self.theme.family, 24, "bold")).pack(anchor="w", pady=(6, 0))
        ttk.Label(brand, text="PANLUME", style="SidebarMuted.TLabel", font=(self.theme.family, 10, "bold")).pack(anchor="w", pady=(2, 0))
        for key, title in PAGE_TITLES.items():
            button = tk.Button(
                sidebar,
                text=title,
                command=lambda page=key: self.show_page(page),
                anchor="w",
                padx=16,
                pady=4,
                relief="flat",
                bd=0,
                cursor="hand2",
                font=(self.theme.family, 11),
                bg=c["sidebar"],
                fg=c["sidebar_text"],
                activebackground=c["select"],
                activeforeground=c["text"],
                highlightthickness=0,
            )
            button.pack(fill="x", pady=1)
            self.nav_buttons[key] = button
        ttk.Label(sidebar, text=f"v{APP_VERSION} {self.tr('学习版')}", style="SidebarMuted.TLabel").pack(side="bottom", anchor="w")
        ttk.Label(sidebar, text="公开信息 · 本地研究", style="SidebarMuted.TLabel").pack(side="bottom", anchor="w", pady=(0, 6))

        right = ttk.Frame(shell)
        right.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(right, padding=(32, 26))
        self.content.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value=self.tr("就绪；启动时未请求行情或模型"))
        status = ttk.Label(right, textvariable=self.status_var, style="Muted.TLabel", padding=(32, 10))
        status.pack(fill="x", side="bottom")
        for key in PAGE_TITLES:
            host = ScrollablePage(self.content, background=c["bg"])
            self.page_hosts[key] = host
            self.pages[key] = host.body
        self._build_dashboard()
        self._build_observation()
        self._build_review()
        self.report_page = ReportPage(self)
        self.report_page.build()
        self.report_results = ReportResultsPage(self)
        self.report_results.build()
        self._build_audit()
        self._build_history()
        self._build_model()
        self._build_language()
        self._build_data()
        self._build_demo()
        self._build_about()
        translate_widget_tree(self.root, self._ui_language)

    def show_page(self, key: str) -> None:
        self.current_page = key
        for host in self.page_hosts.values():
            host.pack_forget()
        host = self.page_hosts[key]
        host.pack(fill="both", expand=True)
        host.scroll_to_top()
        host.bind_mousewheel()
        host._schedule_idle(host._sync_scrollregion)
        c = self.theme.colors
        for name, button in self.nav_buttons.items():
            selected = name == key
            button.configure(
                bg=c["accent"] if selected else c["sidebar"],
                fg=c["accent_text"] if selected else c["sidebar_text"],
                font=(self.theme.family, 11, "bold" if selected else "normal"),
            )
        if key == "dashboard":
            self._refresh_dashboard()
        elif key == "review":
            self._refresh_review_history()
        elif key == "results":
            self.report_results.refresh()
        elif key == "data":
            self._refresh_data_stats()

    def _page_header(self, page: ttk.Frame, title: str, subtitle: str) -> None:
        ttk.Label(page, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text=subtitle, style="Muted.TLabel", wraplength=760).pack(anchor="w", pady=(7, 22))

    def _card(self, parent: ttk.Frame) -> ttk.Frame:
        return ttk.Frame(parent, style="Surface.TFrame", padding=20)

    def _build_dashboard(self) -> None:
        page = self.pages["dashboard"]
        self._page_header(page, "让公开数据留下清晰的轨迹", "本地观察、确定性计算与可追溯摘要。启动不会自动联网。")
        metrics = ttk.Frame(page)
        metrics.pack(fill="x")
        self.dashboard_vars = {key: tk.StringVar() for key in ("stocks", "interval", "database", "model")}
        labels = {
            "stocks": "观察对象",
            "interval": "获取频率",
            "database": "个人数据库",
            "model": "本地模型",
        }
        for index, key in enumerate(labels):
            card = self._card(metrics)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 8, 0))
            ttk.Label(card, text=labels[key], style="SurfaceMuted.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=self.dashboard_vars[key], style="Metric.TLabel Surface.TLabel").pack(anchor="w", pady=(8, 0))
            metrics.columnconfigure(index, weight=1)
        state_card = self._card(page)
        state_card.pack(fill="x", pady=(20, 0))
        ttk.Label(state_card, text="当前边界", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        ttk.Label(
            state_card,
            text="本地复盘页手动整理已保存事实，保留历史报告与输入哈希。\n"
                 "观察页支持本地模拟轮询和受控真实短验收；Ollama 摘要手动触发，云端保持关闭。", 
            style="Surface.TLabel",
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(10, 18))
        actions = ttk.Frame(state_card, style="Surface.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="运行离线演示", style="Primary.TButton", command=lambda: self.show_page("demo")).pack(side="left")
        ttk.Button(actions, text="调整观察设置", style="Secondary.TButton", command=lambda: self.show_page("observation")).pack(side="left", padx=10)
        ttk.Button(actions, text="检查数据空间", style="Secondary.TButton", command=lambda: self.show_page("data")).pack(side="left")
        ttk.Button(actions, text="打开本地复盘", style="Secondary.TButton", command=lambda: self.show_page("review")).pack(side="left", padx=(10, 0))
        ttk.Button(actions, text="数据体检", style="Secondary.TButton", command=lambda: self.show_page("audit")).pack(side="left", padx=(10, 0))

    def _refresh_dashboard(self) -> None:
        if not hasattr(self, "dashboard_vars"):
            return
        self.dashboard_vars["stocks"].set(self.trf("{count} / 10 只", count=sum(item.enabled for item in self.settings.stocks)))
        self.dashboard_vars["interval"].set(self.trf("{seconds} 秒", seconds=self.settings.interval_seconds))
        self.dashboard_vars["database"].set(self.trf("结构 {version}", version=self.data.schema_version()))
        self.dashboard_vars["model"].set(self.tr("已启用" if self.settings.ollama_enabled else "已停用"))

    def _build_observation(self) -> None:
        page = self.pages["observation"]
        c = self.theme.colors
        self._page_header(page, "观察设置", "设置可随时修改；未来运行中的任务会在下一轮应用频率和股票变化。")
        general = self._card(page)
        general.pack(fill="x")
        ttk.Label(general, text="基本偏好", style="Section.TLabel Surface.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
        ttk.Label(general, text="界面主题", style="Surface.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 12))
        self.theme_var = tk.StringVar(value=self.tr("浅色" if self.settings.theme == "light" else "深色"))
        ttk.Combobox(
            general,
            textvariable=self.theme_var,
            values=(self.tr("浅色"), self.tr("深色")),
            state="readonly",
            width=12,
        ).grid(row=1, column=1, sticky="w", padx=(0, 30))
        ttk.Label(general, text="获取频率（秒）", style="Surface.TLabel").grid(row=1, column=2, sticky="w", padx=(0, 12))
        self.interval_var = tk.StringVar(value=str(self.settings.interval_seconds))
        ttk.Combobox(general, textvariable=self.interval_var, values=("60", "300", "600"), width=12).grid(row=1, column=3, sticky="w")
        ttk.Label(general, text="推荐 60、300、600；允许自定义 60～3600 秒。", style="SurfaceMuted.TLabel").grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))

        stock_card = self._card(page)
        stock_card.pack(fill="both", expand=True, pady=(12, 0))
        header = ttk.Frame(stock_card, style="Surface.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="观察股票", style="Section.TLabel Surface.TLabel").pack(side="left")
        self.stock_count_var = tk.StringVar()
        ttk.Label(header, textvariable=self.stock_count_var, style="SurfaceMuted.TLabel").pack(side="right")
        self.stock_tree = ttk.Treeview(stock_card, columns=("code", "name", "enabled"), show="headings", height=5)
        self.stock_tree.heading("code", text="6 位代码")
        self.stock_tree.heading("name", text="显示名称（可选）")
        self.stock_tree.heading("enabled", text="状态")
        self.stock_tree.column("code", width=140, anchor="center")
        self.stock_tree.column("name", width=330)
        self.stock_tree.column("enabled", width=90, anchor="center")
        self.stock_tree.pack(fill="both", expand=True, pady=10)
        for item in self.settings.stocks:
            self.stock_tree.insert("", "end", values=(
                item.code, item.name, self.tr("启用" if item.enabled else "停用")
            ))
        self._refresh_stock_count()
        editor = ttk.Frame(stock_card, style="Surface.TFrame")
        editor.pack(fill="x")
        self.new_code_var = tk.StringVar()
        self.new_name_var = tk.StringVar()
        ttk.Entry(editor, textvariable=self.new_code_var, width=14).pack(side="left")
        ttk.Entry(editor, textvariable=self.new_name_var, width=24).pack(side="left", padx=8)
        ttk.Button(editor, text="添加", style="Secondary.TButton", command=self._add_stock).pack(side="left")
        ttk.Button(editor, text="移除选中", style="Secondary.TButton", command=self._remove_stock).pack(side="left", padx=8)
        ttk.Button(editor, text="启停选中", style="Secondary.TButton", command=self._toggle_stock).pack(side="left")
        ttk.Button(editor, text="上移", style="Secondary.TButton", command=lambda: self._move_stock(-1)).pack(side="left", padx=8)
        ttk.Button(editor, text="下移", style="Secondary.TButton", command=lambda: self._move_stock(1)).pack(side="left")
        ttk.Label(stock_card, text="添加、移除、启停和排序只改变观察清单，不会自行发起行情请求或提高频率。", style="SurfaceMuted.TLabel").pack(anchor="w", pady=(8, 0))
        footer = ttk.Frame(page)
        footer.pack(fill="x", pady=(10, 0))
        ttk.Button(footer, text="保存观察设置", style="Primary.TButton", command=self._save_observation).pack(side="left")
        ttk.Button(footer, text="撤销未保存修改", style="Secondary.TButton", command=self._rebuild_current_page).pack(side="left", padx=10)

        run_card = self._card(page)
        run_card.pack(fill="x", pady=(12, 0))
        ttk.Label(run_card, text="离线观察台", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        ttk.Label(
            run_card,
            text="使用本地模拟分钟，不访问网络、不调用模型。先保存设置；开始后持续轮询，停止后才可退出。",
            style="SurfaceMuted.TLabel", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(6, 12))
        controls = ttk.Frame(run_card, style="Surface.TFrame")
        controls.pack(fill="x")
        self.observation_start_button = ttk.Button(
            controls, text="开始离线观察", style="Primary.TButton",
            command=self._start_offline_observation,
        )
        self.observation_start_button.pack(side="left")
        self.observation_stop_button = ttk.Button(
            controls, text="停止观察", style="Secondary.TButton",
            command=self._stop_observation,
        )
        self.observation_stop_button.pack(side="left", padx=10)
        self.observation_stop_button.state(["disabled"])
        self.observation_status_var = tk.StringVar(value=self.tr("观察未启动"))
        ttk.Label(run_card, textvariable=self.observation_status_var, style="SurfaceMuted.TLabel").pack(anchor="w", pady=(10, 8))
        self.observation_tree = ttk.Treeview(
            run_card,
            columns=("code", "source", "time", "confirmed", "close", "status"),
            show="headings", height=5,
        )
        headings = {
            "code": "代码", "source": "数据来源", "time": "行情时间",
            "confirmed": "确认状态", "close": "收盘价", "status": "状态",
        }
        for key, title in headings.items():
            self.observation_tree.heading(key, text=title)
            self.observation_tree.column(key, width=110 if key != "time" else 205, anchor="center")
        self.observation_tree.pack(fill="x")
        summary_actions = ttk.Frame(run_card, style="Surface.TFrame")
        summary_actions.pack(fill="x", pady=(10, 6))
        ttk.Button(
            summary_actions, text="生成观察摘要", style="Secondary.TButton",
            command=self._generate_observation_summary,
        ).pack(side="left")
        ttk.Label(
            summary_actions, text="仅读取当前清单的最近一条分钟；模型停用时使用固定模板。",
            style="SurfaceMuted.TLabel",
        ).pack(side="left", padx=10)
        self.observation_summary_text = tk.Text(
            run_card, height=7, wrap="word", relief="flat",
            bg=c["bg"], fg=c["text"], insertbackground=c["text"],
        )
        self.observation_summary_text.pack(fill="x")
        self.observation_summary_text.insert(
            "1.0", self.tr("尚未生成摘要。手动点击后才会读取数据或调用本机 Ollama。"),
        )
        self.observation_summary_text.configure(state="disabled")

        self.csv_replay_card = CsvReplayCard(self, page)
        self.csv_replay_card.build()

        live_card = self._card(page)
        live_card.pack(fill="x", pady=(12, 0))
        ttk.Label(live_card, text="真实短验收（默认关闭）", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        ttk.Label(
            live_card,
            text="每次只准备一只股票、一个时间闭区间和一次请求。生成计划后仍需逐字确认；失败不会自动重试。",
            style="SurfaceMuted.TLabel", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(6, 10))
        ttk.Button(
            live_card, text="准备单次真实短验收…", style="Secondary.TButton",
            command=self._open_live_acceptance,
        ).pack(anchor="w")
        self.live_result_text = tk.Text(
            live_card, height=6, wrap="word", relief="flat",
            bg=c["bg"], fg=c["text"], insertbackground=c["text"],
        )
        self.live_result_text.pack(fill="x", pady=(10, 0))
        self.live_result_text.insert(
            "1.0", self.tr("尚未准备真实请求。启动应用、查看本页和生成离线摘要都不会触发真实行情。"),
        )
        self.live_result_text.configure(state="disabled")
        if self.observation_runner is not None:
            self._render_observation(self.observation_runner.snapshot())

    def _open_live_acceptance(self) -> None:
        if "live-acceptance" in self.active_tasks:
            self.status_var.set(self.tr("真实短验收正在执行，请勿重复启动"))
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(self.tr("准备单次真实短验收"))
        dialog.geometry("700x650")
        dialog.minsize(620, 520)
        dialog.transient(self.root)
        dialog.grab_set()
        host = ScrollablePage(dialog, background=self.theme.colors["bg"])
        host.pack(fill="both", expand=True)
        body = ttk.Frame(host.body, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="单次真实请求计划", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text="仅在最后点击执行且确认文本完全一致时访问公开行情。所有常规分钟保持未确认。",
            style="Muted.TLabel", wraplength=630, justify="left",
        ).pack(anchor="w", pady=(6, 12))
        now = datetime.now(CHINA_TZ)
        code_var = tk.StringVar(value=self.settings.stocks[0].code)
        date_var = tk.StringVar(value=now.date().isoformat())
        end_var = tk.StringVar(value=now.strftime("%H:%M"))
        form = ttk.Frame(body)
        form.pack(fill="x")
        ttk.Label(form, text="股票代码").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Combobox(
            form, textvariable=code_var, state="readonly", width=18,
            values=tuple(item.code for item in self.settings.stocks),
        ).grid(row=0, column=1, sticky="w", padx=12, pady=5)
        ttk.Label(form, text="日期（YYYY-MM-DD）").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=date_var, width=20).grid(row=1, column=1, sticky="w", padx=12, pady=5)
        ttk.Label(form, text="请求终点（HH:MM）").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=end_var, width=20).grid(row=2, column=1, sticky="w", padx=12, pady=5)
        ttk.Label(
            body,
            text="起点固定为当日 09:31（UTC+8）；接口固定；预计请求 1 次；频率为单次；自动重试 0 次。",
            style="Muted.TLabel", wraplength=630, justify="left",
        ).pack(anchor="w", pady=(8, 12))
        plan_text = tk.Text(body, height=8, wrap="word", relief="solid", bd=1)
        plan_text.pack(fill="x")
        plan_text.insert("1.0", self.tr("请先检查上方范围，再生成请求计划。此时尚未创建数据库或审计。"))
        plan_text.configure(state="disabled")
        holder: dict[str, object] = {}

        def set_plan_text(text: str) -> None:
            plan_text.configure(state="normal")
            plan_text.delete("1.0", "end")
            plan_text.insert("1.0", text)
            plan_text.configure(state="disabled")

        def prepare() -> None:
            try:
                request_plan = self.live_acceptance.make_plan(
                    code_var.get(), date_var.get(), end_var.get(),
                )
            except ValueError as exc:
                messagebox.showerror(self.tr("无法生成请求计划"), self.tr(str(exc)), parent=dialog)
                return
            holder["plan"] = request_plan
            confirmation_var.set("")
            set_plan_text(request_plan.describe(language=self._ui_language))

        ttk.Button(
            body, text="生成请求计划（不联网）", style="Secondary.TButton",
            command=prepare,
        ).pack(anchor="w", pady=8)
        ttk.Label(
            body,
            text=self.tr("如确认以上唯一计划，请逐字输入：") + DESKTOP_LIVE_CONFIRMATION,
            style="Muted.TLabel", wraplength=630, justify="left",
        ).pack(anchor="w", pady=(4, 6))
        confirmation_var = tk.StringVar()
        ttk.Entry(body, textvariable=confirmation_var).pack(fill="x")
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(14, 0))

        def execute() -> None:
            request_plan = holder.get("plan")
            if request_plan is None:
                messagebox.showinfo(
                    self.tr("尚未生成计划"), self.tr("请先生成并核对请求计划。"), parent=dialog,
                )
                return
            confirmation = confirmation_var.get()
            dialog.destroy()
            self._run_async(
                "live-acceptance",
                lambda: self.live_acceptance.execute(
                    request_plan, confirmation=confirmation, language=self.settings.language,
                ),
                self._show_live_acceptance_result,
            )

        ttk.Button(
            actions, text="确认并执行一次真实请求", style="Primary.TButton",
            command=execute,
        ).pack(side="left")
        ttk.Button(
            actions, text="取消", style="Secondary.TButton", command=dialog.destroy,
        ).pack(side="left", padx=10)
        host.bind_mousewheel()
        translate_widget_tree(dialog, self._ui_language)

    def _show_live_acceptance_result(self, result) -> None:
        if hasattr(self, "live_result_text"):
            self.live_result_text.configure(state="normal")
            self.live_result_text.delete("1.0", "end")
            self.live_result_text.insert("1.0", result.text)
            self.live_result_text.configure(state="disabled")
        self.status_var.set(self.tr("真实短验收完成；实际请求 1 次；未写主库；没有自动重试"))

    def _start_offline_observation(self) -> None:
        if "offline-observation" in self.active_tasks:
            self.status_var.set(self.tr("离线观察已经在运行"))
            return
        codes = tuple(item.code for item in self.settings.stocks)
        session_date = recent_weekdays(datetime.now(CHINA_TZ).date(), 1)[0]
        start, end = demo_time_range(session_date, 10)
        adapter = make_simulator(codes, session_date)

        def current_settings():
            # 仅同步本地模拟器的代码白名单；真实适配器后续使用独立受控入口。
            adapter.stock_codes = tuple(item.code for item in self.settings.stocks)
            return self.settings

        runner = ObservationRunner(
            self.data.database,
            adapter,
            current_settings,
            request_start=start,
            request_end=end,
            on_change=self._on_observation_snapshot,
        )
        self.observation_runner = runner
        self.active_tasks.add("offline-observation")
        self.observation_start_button.state(["disabled"])
        self.observation_stop_button.state(["!disabled"])
        self.status_var.set(self.tr("离线观察已启动；没有网络或模型请求"))

        def work() -> None:
            try:
                # 明确的大整数上限只作线程泄漏保险；正常使用由停止按钮结束。
                runner.run(max_rounds=1_000_000)
            except Exception as exc:
                self._post_ui(lambda error=exc: self._observation_failed(error))
            finally:
                self._post_ui(self._observation_finished)

        threading.Thread(target=work, name="panlume-offline-observation", daemon=True).start()

    def _stop_observation(self) -> None:
        if self.observation_runner is None:
            return
        self.observation_runner.request_stop()
        self.observation_stop_button.state(["disabled"])
        self.status_var.set(self.tr("正在停止离线观察；不会启动新请求"))

    def _on_observation_snapshot(self, snapshot: ObservationSnapshot) -> None:
        self._post_ui(lambda: self._render_observation(snapshot))

    def _post_ui(self, callback) -> None:
        """工作线程只入队；Tk 调用统一由主线程轮询执行。"""
        if not self._closing:
            self._ui_queue.put(callback)

    def _poll_ui_queue(self) -> None:
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            callback()
        if not self._closing:
            self._ui_after_id = self.root.after(50, self._poll_ui_queue)

    def _render_observation(self, snapshot: ObservationSnapshot) -> None:
        if not hasattr(self, "observation_tree"):
            return
        self.observation_status_var.set(self.trf(
            "{message}；第 {round} 轮；频率 {seconds} 秒",
            message=translate_observation_message(snapshot.message, self._ui_language),
            round=snapshot.round_number,
            seconds=snapshot.interval_seconds,
        ))
        self.observation_tree.delete(*self.observation_tree.get_children())
        for item in snapshot.stocks:
            self.observation_tree.insert("", "end", values=(
                item.stock_code, item.source, item.bar_time,
                self.tr(item.confirmed), item.close, self.tr(item.status),
            ))
        running = snapshot.phase in {"running", "stopping"}
        self.observation_start_button.state(["disabled"] if running else ["!disabled"])
        self.observation_stop_button.state(["!disabled"] if snapshot.phase == "running" else ["disabled"])

    def _observation_failed(self, error: Exception) -> None:
        self.status_var.set(self.trf(
            "离线观察失败：{error}；没有自动重试", error=type(error).__name__,
        ))
        messagebox.showerror(
            self.tr("离线观察未完成"),
            self.trf("{error}\n没有自动重试。", error=f"{type(error).__name__}: {error}"),
        )

    def _observation_finished(self) -> None:
        self.active_tasks.discard("offline-observation")
        if hasattr(self, "observation_start_button"):
            self.observation_start_button.state(["!disabled"])
            self.observation_stop_button.state(["disabled"])
        self.status_var.set(self.tr("离线观察已结束；没有网络、模型请求或自动重试"))

    def _generate_observation_summary(self) -> None:
        self._run_async(
            "observation-summary",
            self.observation_summary.generate,
            self._show_observation_summary,
        )

    def _show_observation_summary(self, result) -> None:
        if hasattr(self, "observation_summary_text"):
            self.observation_summary_text.configure(state="normal")
            self.observation_summary_text.delete("1.0", "end")
            self.observation_summary_text.insert("1.0", result.note + "\n\n" + result.text)
            self.observation_summary_text.configure(state="disabled")
        self.status_var.set(result.note)
        self.report_results.show_observation(result)
        self.show_page("results")

    def _refresh_stock_count(self) -> None:
        self.stock_count_var.set(self.trf("已选 {count} / 10", count=len(self.stock_tree.get_children())))

    def _add_stock(self) -> None:
        code = self.new_code_var.get().strip()
        name = self.new_name_var.get().strip()
        try:
            item = StockChoice(code, name)
        except DesktopSettingsError as exc:
            messagebox.showerror(self.tr("无法添加"), self.tr(str(exc)))
            return
        rows = self.stock_tree.get_children()
        if len(rows) >= 10:
            messagebox.showerror(self.tr("无法添加"), self.tr("同时观察最多 10 只，请先移除一只。"))
            return
        if any(str(self.stock_tree.item(row, "values")[0]) == code for row in rows):
            messagebox.showerror(self.tr("无法添加"), self.tr("这只股票已经在观察清单中。"))
            return
        self.stock_tree.insert("", "end", values=(item.code, item.name, self.tr("启用")))
        self.new_code_var.set("")
        self.new_name_var.set("")
        self._refresh_stock_count()

    def _remove_stock(self) -> None:
        selected = self.stock_tree.selection()
        if not selected:
            messagebox.showinfo(self.tr("移除股票"), self.tr("请先在表格中选择一只股票。"))
            return
        if len(self.stock_tree.get_children()) - len(selected) < 1:
            messagebox.showerror(self.tr("无法移除"), self.tr("观察清单至少保留一只股票。"))
            return
        for row in selected:
            self.stock_tree.delete(row)
        self._refresh_stock_count()

    def _toggle_stock(self) -> None:
        selected = self.stock_tree.selection()
        if not selected:
            messagebox.showinfo(self.tr("启停股票"), self.tr("请先在表格中选择一只股票。"))
            return
        for row in selected:
            values = list(self.stock_tree.item(row, "values"))
            values[2] = self.tr("停用" if values[2] == self.tr("启用") else "启用")
            self.stock_tree.item(row, values=values)

    def _move_stock(self, offset: int) -> None:
        selected = self.stock_tree.selection()
        if len(selected) != 1:
            messagebox.showinfo(self.tr("调整顺序"), self.tr("请先选择一只股票。"))
            return
        row = selected[0]
        rows = self.stock_tree.get_children()
        index = rows.index(row)
        target = index + offset
        if 0 <= target < len(rows):
            self.stock_tree.move(row, "", target)

    def _save_observation(self) -> None:
        try:
            interval = int(self.interval_var.get().strip())
            stocks = tuple(
                StockChoice(
                    str(self.stock_tree.item(row, "values")[0]).zfill(6),
                    str(self.stock_tree.item(row, "values")[1]),
                    self.stock_tree.item(row, "values")[2] == self.tr("启用"),
                )
                for row in self.stock_tree.get_children()
            )
            updated = replace(
                self.settings,
                theme="light" if self.theme_var.get() == self.tr("浅色") else "dark",
                interval_seconds=interval,
                stocks=stocks,
            )
        except (ValueError, DesktopSettingsError) as exc:
            messagebox.showerror(self.tr("设置未保存"), self.tr(str(exc)))
            return
        self._commit_settings(
            updated, self.tr("观察设置已保存；未来任务将在下一轮使用新设置。"), "observation",
        )

    def _build_review(self) -> None:
        page = self.pages["review"]
        c = self.theme.colors
        self._page_header(
            page, "本地每日复盘",
            "手动整理 SQLite 中已保存的事实；相同输入幂等返回。不会调用模型、云端或真实行情。",
        )
        form_card = self._card(page)
        form_card.pack(fill="x")
        ttk.Label(form_card, text="生成复盘", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        form = ttk.Frame(form_card, style="Surface.TFrame")
        form.pack(fill="x", pady=(10, 8))
        self.review_code_var = tk.StringVar(value=self.settings.stocks[0].code)
        self.review_date_var = tk.StringVar(value=datetime.now(CHINA_TZ).date().isoformat())
        ttk.Label(form, text="股票代码", style="Surface.TLabel").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Combobox(
            form, textvariable=self.review_code_var, width=20,
            values=tuple(item.code for item in self.settings.stocks),
        ).grid(row=0, column=1, sticky="w", padx=12, pady=5)
        ttk.Label(form, text="日期（YYYY-MM-DD）", style="Surface.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.review_date_var, width=22).grid(
            row=1, column=1, sticky="w", padx=12, pady=5,
        )
        ttk.Label(
            form_card,
            text="可输入个人库中已有数据的历史代码。空数据也会留下 unknown 复盘，便于确认当时缺少哪些证据。",
            style="SurfaceMuted.TLabel", wraplength=760, justify="left",
        ).pack(anchor="w")
        actions = ttk.Frame(form_card, style="Surface.TFrame")
        actions.pack(fill="x", pady=(12, 0))
        self.review_generate_button = ttk.Button(
            actions, text="生成本地复盘", style="Primary.TButton",
            command=self._generate_local_review,
        )
        self.review_generate_button.pack(side="left")
        ttk.Button(
            actions, text="刷新历史", style="Secondary.TButton",
            command=self._refresh_review_history,
        ).pack(side="left", padx=10)
        self.review_status_var = tk.StringVar(value=self.tr("尚未生成复盘；打开本页不会写入数据库。"))
        ttk.Label(
            form_card, textvariable=self.review_status_var,
            style="SurfaceMuted.TLabel", wraplength=760,
        ).pack(anchor="w", pady=(10, 0))

        history_card = self._card(page)
        history_card.pack(fill="x", pady=(12, 0))
        ttk.Label(history_card, text="最近复盘（新到旧）", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        history_frame = ttk.Frame(history_card, style="Surface.TFrame")
        history_frame.pack(fill="x", pady=(10, 0))
        columns = ("id", "date", "code", "status", "created")
        self.review_tree = ttk.Treeview(
            history_frame, columns=columns, show="headings", height=6, selectmode="browse",
        )
        headings = {
            "id": "编号", "date": "日期", "code": "代码",
            "status": "数据状态", "created": "创建时间（UTC）",
        }
        widths = {"id": 60, "date": 100, "code": 90, "status": 90, "created": 230}
        for column in columns:
            self.review_tree.heading(column, text=headings[column])
            self.review_tree.column(column, width=widths[column], minwidth=55, stretch=column == "created")
        review_scrollbar = ttk.Scrollbar(history_frame, orient="vertical", command=self.review_tree.yview)
        self.review_tree.configure(yscrollcommand=review_scrollbar.set)
        self.review_tree.pack(side="left", fill="x", expand=True)
        review_scrollbar.pack(side="right", fill="y")
        self.review_tree.bind("<<TreeviewSelect>>", self._on_review_selected)
        self._current_review_id: int | None = None

        detail_card = self._card(page)
        detail_card.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(detail_card, text="报告与追溯", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        self.review_meta_var = tk.StringVar(
            value=self.tr("选择历史记录，或生成一份新复盘。输入事实 JSON 与报告共同保存在个人 SQLite 数据库。"),
        )
        ttk.Label(
            detail_card, textvariable=self.review_meta_var,
            style="SurfaceMuted.TLabel", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(10, 10))
        report_frame = ttk.Frame(detail_card, style="Surface.TFrame")
        report_frame.pack(fill="both", expand=True)
        self.review_report_text = tk.Text(
            report_frame, height=18, wrap="word", relief="flat",
            bg=c["bg"], fg=c["text"], insertbackground=c["text"],
        )
        report_scrollbar = ttk.Scrollbar(report_frame, orient="vertical", command=self.review_report_text.yview)
        self.review_report_text.configure(yscrollcommand=report_scrollbar.set)
        self.review_report_text.pack(side="left", fill="both", expand=True)
        report_scrollbar.pack(side="right", fill="y")
        self._set_review_report(self.tr("尚无复盘内容。本地复盘只整理已保存事实，不重新计算指标，不产生实时提醒。"))
        self._refresh_review_history()

    def _generate_local_review(self) -> None:
        if "local-review" in self.active_tasks:
            self.status_var.set(self.tr("本地复盘正在生成，请勿重复启动"))
            return
        self.review_generate_button.state(["disabled"])
        self.review_status_var.set(self.tr("正在读取已保存事实；模型、云端和真实行情调用均为 0 次。"))
        code = self.review_code_var.get()
        session_date = self.review_date_var.get()
        self._run_async(
            "local-review",
            lambda: self.review_service.generate(code, session_date),
            self._local_review_done,
        )

    def _local_review_done(self, detail: DesktopReviewDetail) -> None:
        self._current_review_id = None
        self._show_review_detail(detail)
        self._refresh_review_history()
        message = self.tr("本次新建" if detail.created else "相同输入已存在，幂等返回")
        self.review_status_var.set(self.trf(
            "复盘 {review_id}：{message}；未调用模型、云端或真实行情。",
            review_id=detail.review_id,
            message=message,
        ))
        self.status_var.set(self.trf(
            "本地复盘 {review_id} 已准备；{message}", review_id=detail.review_id, message=message,
        ))

    def _refresh_review_history(self) -> None:
        if not hasattr(self, "review_tree"):
            return
        rows = self.review_service.history()
        selected = self._current_review_id
        self.review_tree.delete(*self.review_tree.get_children())
        for item in rows:
            values = (
                item.review_id, item.session_date, item.stock_code,
                item.data_status, item.created_at,
            )
            kwargs = {"iid": str(item.review_id), "values": values}
            self.review_tree.insert("", "end", **kwargs)
        if selected is not None and self.review_tree.exists(str(selected)):
            self.review_tree.selection_set(str(selected))

    def _on_review_selected(self, _event=None) -> None:
        selected = self.review_tree.selection()
        if not selected:
            return
        review_id = int(selected[0])
        self._current_review_id = review_id
        detail = self.review_service.load(review_id)
        self._show_review_detail(detail)
        self.review_status_var.set(self.trf(
            "正在查看历史复盘 {review_id}；只读取已有记录，没有新建复盘。", review_id=review_id,
        ))

    def _show_review_detail(self, detail: DesktopReviewDetail) -> None:
        self.review_meta_var.set(detail.metadata)
        self._set_review_report(detail.report)

    def _set_review_report(self, text: str) -> None:
        self.review_report_text.configure(state="normal")
        self.review_report_text.delete("1.0", "end")
        self.review_report_text.insert("1.0", text)
        self.review_report_text.configure(state="disabled")

    def _build_audit(self) -> None:
        page = self.pages["audit"]
        c = self.theme.colors
        self._page_header(
            page, "只读数据体检",
            "检查当前观察清单的分钟完整性、质量问题和入库来源；不补数、不修复、不调用模型。",
        )
        form_card = self._card(page)
        form_card.pack(fill="x")
        ttk.Label(form_card, text="体检范围", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        codes = "、".join(item.code for item in self.settings.stocks)
        ttk.Label(
            form_card, text=self.trf("观察对象：{codes}", codes=codes),
            style="SurfaceMuted.TLabel", wraplength=760,
        ).pack(anchor="w", pady=(10, 4))
        form = ttk.Frame(form_card, style="Surface.TFrame")
        form.pack(fill="x", pady=(6, 0))
        self.audit_date_var = tk.StringVar(value=datetime.now(CHINA_TZ).date().isoformat())
        self.audit_time_var = tk.StringVar(value=datetime.now(CHINA_TZ).strftime("%H:%M"))
        ttk.Label(form, text="日期（YYYY-MM-DD）", style="Surface.TLabel").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.audit_date_var, width=22).grid(row=0, column=1, sticky="w", padx=12, pady=5)
        ttk.Label(form, text="截止分钟（HH:MM）", style="Surface.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.audit_time_var, width=22).grid(row=1, column=1, sticky="w", padx=12, pady=5)
        ttk.Label(
            form_card,
            text="截止分钟必须位于 09:31～11:30 或 13:01～15:00。日 K 状态单独报告，不与分钟完整性混用。",
            style="SurfaceMuted.TLabel", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(8, 0))
        self.audit_run_button = ttk.Button(
            form_card, text="开始只读体检", style="Primary.TButton",
            command=self._run_data_audit,
        )
        self.audit_run_button.pack(anchor="w", pady=(12, 0))
        self.audit_status_var = tk.StringVar(value=self.tr("尚未体检；打开本页不会读取详细证据或写入数据库。"))
        ttk.Label(
            form_card, textvariable=self.audit_status_var,
            style="SurfaceMuted.TLabel", wraplength=760,
        ).pack(anchor="w", pady=(10, 0))

        summary_card = self._card(page)
        summary_card.pack(fill="x", pady=(12, 0))
        ttk.Label(summary_card, text="完整性报告", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        self.audit_summary_text = tk.Text(
            summary_card, height=12, wrap="word", relief="flat",
            bg=c["bg"], fg=c["text"], insertbackground=c["text"],
        )
        self.audit_summary_text.pack(fill="x", pady=(10, 0))
        self._set_audit_text(
            self.audit_summary_text,
            self.tr("尚未生成体检报告。空数据、缺口、未确认和质量冲突都会保留原有状态。"),
        )

        issues_card = self._card(page)
        issues_card.pack(fill="x", pady=(12, 0))
        ttk.Label(issues_card, text="当日质量明细（最多 100 条）", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        issue_columns = ("id", "run", "kind", "code", "type", "severity", "message")
        self.audit_issue_tree = ttk.Treeview(
            issues_card, columns=issue_columns, show="headings", height=5, selectmode="browse",
        )
        issue_headings = {
            "id": "问题", "run": "批次", "kind": "数据", "code": "代码",
            "type": "类型", "severity": "级别", "message": "消息",
        }
        issue_widths = {"id": 55, "run": 55, "kind": 75, "code": 80, "type": 140, "severity": 65, "message": 280}
        for column in issue_columns:
            self.audit_issue_tree.heading(column, text=issue_headings[column])
            self.audit_issue_tree.column(column, width=issue_widths[column], minwidth=50, stretch=column == "message")
        self.audit_issue_tree.pack(fill="x", pady=(10, 0))
        self.audit_issue_tree.bind("<<TreeviewSelect>>", self._on_audit_issue_selected)

        runs_card = self._card(page)
        runs_card.pack(fill="x", pady=(12, 0))
        ttk.Label(runs_card, text="相关入库批次（最多 100 条）", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        run_columns = ("id", "kind", "adapter", "status", "received", "accepted", "duplicate", "rejected")
        self.audit_run_tree = ttk.Treeview(
            runs_card, columns=run_columns, show="headings", height=5, selectmode="browse",
        )
        run_headings = {
            "id": "批次", "kind": "数据", "adapter": "适配器", "status": "状态",
            "received": "收到", "accepted": "接受", "duplicate": "去重", "rejected": "拒绝",
        }
        for column in run_columns:
            self.audit_run_tree.heading(column, text=run_headings[column])
            self.audit_run_tree.column(
                column, width=150 if column == "adapter" else 75,
                minwidth=60, stretch=column == "adapter",
            )
        self.audit_run_tree.pack(fill="x", pady=(10, 0))
        self.audit_run_tree.bind("<<TreeviewSelect>>", self._on_audit_run_selected)

        detail_card = self._card(page)
        detail_card.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(detail_card, text="追溯详情", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        self.audit_detail_text = tk.Text(
            detail_card, height=8, wrap="word", relief="flat",
            bg=c["bg"], fg=c["text"], insertbackground=c["text"],
        )
        self.audit_detail_text.pack(fill="both", expand=True, pady=(10, 0))
        self._set_audit_text(
            self.audit_detail_text,
            self.tr("选择质量问题可查看原始 JSON；选择入库批次可查看适配器和接收、接受、去重、拒绝计数。"),
        )
        self._audit_issue_items = {}
        self._audit_run_items = {}

    def _run_data_audit(self) -> None:
        if "data-audit" in self.active_tasks:
            self.status_var.set(self.tr("数据体检正在运行，请勿重复启动"))
            return
        codes = tuple(item.code for item in self.settings.stocks)
        session_date = self.audit_date_var.get()
        as_of_time = self.audit_time_var.get()
        self.audit_run_button.state(["disabled"])
        self.audit_status_var.set(self.tr("正在只读 SQLite；不会补数、修复或调用模型。"))
        self._run_async(
            "data-audit",
            lambda: self.audit_service.run(codes, session_date, as_of_time),
            self._data_audit_done,
        )

    def _data_audit_done(self, result: DesktopAuditResult) -> None:
        self._set_audit_text(self.audit_summary_text, result.render(self._ui_language))
        self._audit_issue_items = {str(item.issue_id): item for item in result.issues}
        self._audit_run_items = {str(item.run_id): item for item in result.runs}
        self.audit_issue_tree.delete(*self.audit_issue_tree.get_children())
        for item in result.issues:
            self.audit_issue_tree.insert("", "end", iid=str(item.issue_id), values=(
                item.issue_id, item.run_id, item.data_kind, item.stock_code,
                item.issue_type, item.severity, item.message,
            ))
        self.audit_run_tree.delete(*self.audit_run_tree.get_children())
        for item in result.runs:
            self.audit_run_tree.insert("", "end", iid=str(item.run_id), values=(
                item.run_id, item.data_kind, item.adapter, item.status,
                item.received_count, item.accepted_count,
                item.duplicate_count, item.rejected_count,
            ))
        self._set_audit_text(
            self.audit_detail_text,
            self.trf(
                "读取到质量问题 {issues} 条、相关入库批次 {runs} 条。选择一行查看追溯详情。",
                issues=len(result.issues),
                runs=len(result.runs),
            ),
        )
        report = result.report
        self.audit_status_var.set(self.trf(
            "体检完成：完整 {complete}；仅价格线 {price_only}；未知 {unknown}；数据库写入 0 次。",
            complete=report.complete_count,
            price_only=report.price_only_count,
            unknown=report.unknown_count,
        ))
        self.status_var.set(self.tr("只读数据体检完成；没有补数、模型或真实行情请求"))

    def _on_audit_issue_selected(self, _event=None) -> None:
        selected = self.audit_issue_tree.selection()
        if selected:
            self._set_audit_text(
                self.audit_detail_text,
                self._audit_issue_items[selected[0]].render_detail(self._ui_language),
            )

    def _on_audit_run_selected(self, _event=None) -> None:
        selected = self.audit_run_tree.selection()
        if selected:
            self._set_audit_text(
                self.audit_detail_text,
                self._audit_run_items[selected[0]].render_detail(self._ui_language),
            )

    @staticmethod
    def _set_audit_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _build_history(self) -> None:
        page = self.pages["history"]
        c = self.theme.colors
        self._page_header(
            page, "事件与摘要历史",
            "只读串联规则事件、事件检查、摘要任务和模型调用审计；不会重新计算或生成内容。",
        )
        filter_card = self._card(page)
        filter_card.pack(fill="x")
        ttk.Label(filter_card, text="历史筛选", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        form = ttk.Frame(filter_card, style="Surface.TFrame")
        form.pack(fill="x", pady=(10, 0))
        codes = tuple(item.code for item in self.settings.stocks)
        self.history_code_var = tk.StringVar(value=self.tr("全部代码"))
        ttk.Label(form, text="股票代码", style="Surface.TLabel").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Combobox(
            form, textvariable=self.history_code_var, values=(self.tr("全部代码"), *codes),
            state="readonly", width=18,
        ).grid(row=0, column=1, sticky="w", padx=12, pady=5)
        ttk.Label(form, text="日期（留空为全部）", style="Surface.TLabel").grid(row=0, column=2, sticky="w", pady=5)
        self.history_date_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=self.history_date_var, width=20).grid(
            row=0, column=3, sticky="w", padx=12, pady=5,
        )
        ttk.Label(form, text="摘要结果", style="Surface.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        self.history_kind_names = {
            self.tr("全部结果"): "all", self.tr("模型摘要"): "model",
            self.tr("模板回退"): "template", self.tr("未完成"): "unfinished",
        }
        self.history_kind_var = tk.StringVar(value=self.tr("全部结果"))
        ttk.Combobox(
            form, textvariable=self.history_kind_var,
            values=tuple(self.history_kind_names), state="readonly", width=18,
        ).grid(row=1, column=1, sticky="w", padx=12, pady=5)
        self.history_refresh_button = ttk.Button(
            form, text="刷新只读历史", style="Primary.TButton",
            command=self._refresh_event_history,
        )
        self.history_refresh_button.grid(row=1, column=2, columnspan=2, sticky="w", padx=(0, 12), pady=5)
        ttk.Label(
            filter_card,
            text="事件与摘要各最多读取 100 条。模型请求、原始响应和 R1 思考文本不在本页展开。",
            style="SurfaceMuted.TLabel", wraplength=760,
        ).pack(anchor="w", pady=(8, 0))
        self.history_status_var = tk.StringVar(
            value=self.tr("尚未读取历史；打开本页不会自动查询、恢复任务或调用模型。")
        )
        ttk.Label(
            filter_card, textvariable=self.history_status_var,
            style="SurfaceMuted.TLabel", wraplength=760,
        ).pack(anchor="w", pady=(10, 0))

        events_card = self._card(page)
        events_card.pack(fill="x", pady=(12, 0))
        ttk.Label(events_card, text="规则事件", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        event_columns = ("id", "date", "code", "type", "status", "priority", "checks", "reminders", "message")
        self.history_event_tree = ttk.Treeview(
            events_card, columns=event_columns, show="headings", height=5, selectmode="browse",
        )
        event_headings = {
            "id": "事件", "date": "日期", "code": "代码", "type": "类型",
            "status": "状态", "priority": "优先级", "checks": "检查",
            "reminders": "提醒", "message": "最近确定性消息",
        }
        event_widths = {
            "id": 55, "date": 95, "code": 75, "type": 115, "status": 70,
            "priority": 65, "checks": 55, "reminders": 55, "message": 320,
        }
        for column in event_columns:
            self.history_event_tree.heading(column, text=event_headings[column])
            self.history_event_tree.column(
                column, width=event_widths[column], minwidth=50,
                stretch=column == "message",
            )
        self.history_event_tree.pack(fill="x", pady=(10, 0))
        self.history_event_tree.bind("<<TreeviewSelect>>", self._on_history_event_selected)

        summaries_card = self._card(page)
        summaries_card.pack(fill="x", pady=(12, 0))
        ttk.Label(summaries_card, text="摘要任务与最终结果", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        summary_columns = ("task", "event", "check", "code", "type", "status", "kind", "model")
        self.history_summary_tree = ttk.Treeview(
            summaries_card, columns=summary_columns, show="headings", height=5, selectmode="browse",
        )
        summary_headings = {
            "task": "任务", "event": "事件", "check": "检查", "code": "代码",
            "type": "类型", "status": "任务状态", "kind": "结果", "model": "结果模型",
        }
        for column in summary_columns:
            self.history_summary_tree.heading(column, text=summary_headings[column])
            self.history_summary_tree.column(
                column, width=170 if column == "model" else 90,
                minwidth=55, stretch=column == "model",
            )
        self.history_summary_tree.pack(fill="x", pady=(10, 0))
        self.history_summary_tree.bind("<<TreeviewSelect>>", self._on_history_summary_selected)

        detail_card = self._card(page)
        detail_card.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(detail_card, text="关联与摘要详情", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        self.history_detail_text = tk.Text(
            detail_card, height=14, wrap="word", relief="flat",
            bg=c["bg"], fg=c["text"], insertbackground=c["text"],
        )
        self.history_detail_text.pack(fill="both", expand=True, pady=(10, 0))
        self._set_audit_text(
            self.history_detail_text,
            self.tr("选择规则事件可查看状态、提醒计数和指纹；选择摘要任务可查看最终结果、输入事实、检查 payload 与调用元数据。"),
        )
        self._history_event_items = {}
        self._history_summary_items = {}

    def _refresh_event_history(self) -> None:
        if "event-history" in self.active_tasks:
            self.status_var.set(self.tr("事件与摘要历史正在读取，请勿重复启动"))
            return
        selected_code = self.history_code_var.get()
        code = "" if selected_code == self.tr("全部代码") else selected_code
        day = self.history_date_var.get()
        kind = self.history_kind_names[self.history_kind_var.get()]
        self.history_refresh_button.state(["disabled"])
        self.history_status_var.set(self.tr("正在只读 SQLite；不会重新计算、恢复任务或调用模型。"))
        self._run_async(
            "event-history",
            lambda: self.history_service.run(code, day, kind),
            self._event_history_done,
        )

    def _event_history_done(self, result: DesktopHistoryResult) -> None:
        self._history_event_items = {str(item.event_id): item for item in result.events}
        self._history_summary_items = {str(item.task_id): item for item in result.summaries}
        self.history_event_tree.delete(*self.history_event_tree.get_children())
        for item in result.events:
            self.history_event_tree.insert("", "end", iid=str(item.event_id), values=(
                item.event_id, item.session_date, item.stock_code, item.event_type,
                item.status, item.priority, item.check_count, item.reminder_count,
                item.latest_message,
            ))
        self.history_summary_tree.delete(*self.history_summary_tree.get_children())
        for item in result.summaries:
            self.history_summary_tree.insert("", "end", iid=str(item.task_id), values=(
                item.task_id, item.event_id, item.event_check_id, item.stock_code,
                item.event_type, item.status, item.summary_kind or self.tr("尚未生成"),
                item.model_name or self.tr("无"),
            ))
        self.history_status_var.set(result.render_summary(self._ui_language))
        self._set_audit_text(
            self.history_detail_text,
            self.trf(
                "读取到规则事件 {events} 条、摘要任务 {summaries} 条。选择一行查看关联详情。",
                events=len(result.events),
                summaries=len(result.summaries),
            ),
        )
        if self._history_focus_event_id in self.history_event_tree.get_children():
            self.history_event_tree.selection_set(self._history_focus_event_id)
            self.history_event_tree.see(self._history_focus_event_id)
            self._on_history_event_selected()
        self._history_focus_event_id = None
        self.status_var.set(self.tr("事件与摘要历史读取完成；没有写表、模型或真实行情请求"))

    def _on_history_event_selected(self, _event=None) -> None:
        selected = self.history_event_tree.selection()
        if selected:
            self._set_audit_text(
                self.history_detail_text,
                self._history_event_items[selected[0]].render_detail(self._ui_language),
            )

    def _on_history_summary_selected(self, _event=None) -> None:
        selected = self.history_summary_tree.selection()
        if selected:
            self._set_audit_text(
                self.history_detail_text,
                self._history_summary_items[selected[0]].render_detail(self._ui_language),
            )

    def _build_model(self) -> None:
        page = self.pages["model"]
        self._page_header(page, "模型与输出", "模型只整理已保存事实，不能修改行情、指标、规则或持仓。")
        card = self._card(page)
        card.pack(fill="x")
        self.ollama_enabled_var = tk.BooleanVar(value=self.settings.ollama_enabled)
        ttk.Checkbutton(card, text="允许在显式摘要任务中使用本机 Ollama", variable=self.ollama_enabled_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(card, text="模型", style="Surface.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 18), pady=5)
        self.model_var = tk.StringVar(value=self.settings.model_name)
        self.model_combo = ttk.Combobox(
            card, textvariable=self.model_var, values=("deepseek-r1:14b",),
            state="readonly", width=28,
        )
        self.model_combo.grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(card, text="单次超时（秒）", style="Surface.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 18), pady=5)
        self.timeout_var = tk.StringVar(value=str(self.settings.ollama_timeout_seconds))
        ttk.Spinbox(card, from_=5, to=300, textvariable=self.timeout_var, width=12).grid(row=2, column=1, sticky="w", pady=5)
        ttk.Label(card, text="摘要触发", style="Surface.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 18), pady=5)
        trigger_names = {
            "event_and_manual": self.tr("事件触发＋手动总结"),
            "manual_only": self.tr("仅手动总结"),
        }
        self.trigger_names = trigger_names
        self.trigger_var = tk.StringVar(value=trigger_names[self.settings.summary_trigger])
        ttk.Combobox(card, textvariable=self.trigger_var, values=tuple(trigger_names.values()), state="readonly", width=28).grid(row=3, column=1, sticky="w", pady=5)
        ttk.Label(card, text="输出偏向", style="Surface.TLabel").grid(row=4, column=0, sticky="w", padx=(0, 18), pady=5)
        style_names = {
            "brief": self.tr("简短事实＋数据限制"),
            "detailed": self.tr("详细事实＋后续观察"),
            "facts_only": self.tr("仅列事实"),
        }
        self.style_names = style_names
        self.output_style_var = tk.StringVar(value=style_names[self.settings.output_style])
        ttk.Combobox(card, textvariable=self.output_style_var, values=tuple(style_names.values()), state="readonly", width=28).grid(row=4, column=1, sticky="w", pady=5)

        debug = self._card(page)
        debug.pack(fill="x", pady=(12, 0))
        ttk.Label(debug, text="调试记录", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        self.debug_prompt_var = tk.BooleanVar(value=self.settings.save_prompt_debug)
        self.debug_raw_var = tk.BooleanVar(value=self.settings.save_raw_response_debug)
        self.debug_thinking_var = tk.BooleanVar(value=self.settings.save_thinking_debug)
        ttk.Checkbutton(debug, text="保存完整提示词（可能增加磁盘占用）", variable=self.debug_prompt_var).pack(anchor="w", pady=(8, 0))
        ttk.Checkbutton(debug, text="保存模型原始响应", variable=self.debug_raw_var).pack(anchor="w", pady=2)
        ttk.Checkbutton(debug, text="保存 R1 思考段到独立调试审计", variable=self.debug_thinking_var).pack(anchor="w", pady=2)
        ttk.Label(debug, text="这些选项不影响程序必需的事实校验和基础审计；不会保存云端密钥。", style="SurfaceMuted.TLabel").pack(anchor="w", pady=(8, 0))
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="保存模型设置", style="Primary.TButton", command=self._save_model).pack(side="left")
        ttk.Button(actions, text="检查本机 Ollama", style="Secondary.TButton", command=self._check_ollama).pack(side="left", padx=10)
        self.ollama_health_var = tk.StringVar(value=self.tr("尚未检查；检查只读取 /api/tags，不生成内容"))
        ttk.Label(page, textvariable=self.ollama_health_var, style="Muted.TLabel", wraplength=760).pack(anchor="w", pady=(8, 0))
        self.ollama_page = OllamaManagerPage(self, page)
        self.ollama_page.build()

    def _save_model(self) -> None:
        try:
            trigger = next(key for key, value in self.trigger_names.items() if value == self.trigger_var.get())
            output_style = next(key for key, value in self.style_names.items() if value == self.output_style_var.get())
            updated = replace(
                self.settings,
                ollama_enabled=self.ollama_enabled_var.get(),
                ollama_base_url=self.ollama_base_url_var.get(),
                model_name=self.model_var.get(),
                ollama_timeout_seconds=int(self.timeout_var.get()),
                summary_trigger=trigger,
                output_style=output_style,
                save_prompt_debug=self.debug_prompt_var.get(),
                save_raw_response_debug=self.debug_raw_var.get(),
                save_thinking_debug=self.debug_thinking_var.get(),
            )
        except (ValueError, StopIteration, DesktopSettingsError) as exc:
            messagebox.showerror(self.tr("设置未保存"), self.tr(str(exc)))
            return
        self._commit_settings(
            updated, self.tr("模型与输出偏好已保存；本次没有调用模型。"), "model",
        )

    def _check_ollama(self) -> None:
        self.ollama_health_var.set(self.tr("正在检查本机服务，不会生成文本……"))
        base_url = self.ollama_base_url_var.get().strip()
        def work():
            return check_ollama(base_url=base_url, language=self._ui_language)
        def done(result):
            selected = self.model_var.get()
            if result.online:
                available = selected in result.models
                suffix = (
                    self.tr("当前模型可用。")
                    if available else self.trf("未在列表中找到 {model}。", model=selected)
                )
                self.ollama_health_var.set(result.message + suffix + self.tr("；未生成文本。"))
            else:
                self.ollama_health_var.set(result.message)
        self._run_async("ollama-health", work, done)

    def _build_language(self) -> None:
        page = self.pages["language"]
        self._page_header(page, "语言与区域", "选择界面和新生成固定模板使用的语言。历史摘要与复盘保留原文。")
        card = self._card(page)
        card.pack(fill="x")
        ttk.Label(card, text="语言设置", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        row = ttk.Frame(card, style="Surface.TFrame")
        row.pack(fill="x", pady=(16, 8))
        ttk.Label(row, text="界面语言", style="Surface.TLabel").pack(side="left", padx=(0, 18))
        self.language_names = {
            "English": "en", "简体中文": "zh-CN", "繁體中文": "zh-TW", "日本語": "ja",
        }
        current = next(
            name for name, code in self.language_names.items() if code == self.settings.language
        )
        self.language_var = tk.StringVar(value=current)
        ttk.Combobox(
            row,
            textvariable=self.language_var,
            values=tuple(self.language_names),
            state="readonly",
            width=20,
        ).pack(side="left")
        ttk.Label(
            card,
            text="保存后重启盘灯生效；当前窗口不会立即切换。",
            style="Surface.TLabel",
            wraplength=720,
        ).pack(anchor="w", pady=(6, 4))
        ttk.Label(
            card,
            text="此设置只影响显示文字，不改变行情、规则、数据库或模型权限。",
            style="SurfaceMuted.TLabel",
            wraplength=720,
        ).pack(anchor="w", pady=(0, 16))
        ttk.Button(
            card,
            text="保存语言设置",
            style="Primary.TButton",
            command=self._save_language,
        ).pack(anchor="w")

    def _save_language(self) -> None:
        try:
            language = self.language_names[self.language_var.get()]
            updated = replace(self.settings, language=language)
        except (KeyError, DesktopSettingsError) as exc:
            messagebox.showerror(self.tr("设置未保存"), str(exc))
            return
        if self._ui_language == "en":
            message = "Language saved. Restart Panlume to apply it; historical reports are unchanged."
        else:
            message = "语言设置已保存；重启盘灯后生效，历史摘要与复盘保持原文。"
        self._commit_settings(updated, message, "language")

    def _build_data(self) -> None:
        page = self.pages["data"]
        self._page_header(page, "数据管理", "个人数据与程序分离；备份不会覆盖旧文件，清理必须先预览再确认。")
        location = self._card(page)
        location.pack(fill="x")
        ttk.Label(location, text="个人数据目录", style="SurfaceMuted.TLabel").pack(anchor="w")
        ttk.Label(location, text=str(self.paths.root), style="Surface.TLabel", wraplength=720).pack(anchor="w", pady=(5, 10))
        row = ttk.Frame(location, style="Surface.TFrame")
        row.pack(fill="x")
        ttk.Button(row, text="打开数据目录", style="Secondary.TButton", command=self._open_data_folder).pack(side="left")
        ttk.Button(row, text="创建数据库备份", style="Secondary.TButton", command=self._backup_database).pack(side="left", padx=10)
        self.db_size_var = tk.StringVar()
        ttk.Label(row, textvariable=self.db_size_var, style="SurfaceMuted.TLabel").pack(side="right")

        stats_card = self._card(page)
        stats_card.pack(fill="both", expand=True, pady=(16, 0))
        ttk.Label(stats_card, text="保存内容", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        self.stats_tree = ttk.Treeview(stats_card, columns=("kind", "count"), show="headings", height=5)
        self.stats_tree.heading("kind", text="数据类型")
        self.stats_tree.heading("count", text="记录数")
        self.stats_tree.column("kind", width=480)
        self.stats_tree.column("count", width=120, anchor="e")
        self.stats_tree.pack(fill="both", expand=True, pady=(10, 0))

        retention = self._card(page)
        retention.pack(fill="x", pady=(16, 0))
        ttk.Label(retention, text="保留与清理", style="Section.TLabel Surface.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 12))
        ttk.Label(retention, text="分钟保留天数", style="Surface.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10))
        self.minute_days_var = tk.StringVar(value=str(self.settings.minute_retention_days))
        ttk.Spinbox(retention, from_=5, to=365, textvariable=self.minute_days_var, width=8).grid(row=1, column=1, sticky="w", padx=(0, 24))
        ttk.Label(retention, text="日志保留天数", style="Surface.TLabel").grid(row=1, column=2, sticky="w", padx=(0, 10))
        self.log_days_var = tk.StringVar(value=str(self.settings.log_retention_days))
        ttk.Spinbox(retention, from_=7, to=365, textvariable=self.log_days_var, width=8).grid(row=1, column=3, sticky="w")
        ttk.Label(
            retention,
            text="日线、日历、人工复盘、持仓与关键审计不自动删除；被指标或复盘引用的旧分钟继续保留。",
            style="SurfaceMuted.TLabel",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(12, 0))
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="保存保留设置", style="Secondary.TButton", command=self._save_retention).pack(side="left")
        ttk.Button(actions, text="预览清理", style="Secondary.TButton", command=self._preview_cleanup).pack(side="left", padx=8)
        self.cleanup_button = ttk.Button(actions, text="执行已预览清理", style="Primary.TButton", command=self._execute_cleanup)
        self.cleanup_button.pack(side="left")
        self.cleanup_button.state(["disabled"])
        self.cleanup_var = tk.StringVar(value=self.tr("尚未生成清理预览；删除记录后不会自动压缩数据库文件。"))
        ttk.Label(page, textvariable=self.cleanup_var, style="Muted.TLabel", wraplength=760).pack(anchor="w", pady=(10, 0))

    def _refresh_data_stats(self) -> None:
        if not hasattr(self, "stats_tree"):
            return
        names = {
            "minute_bars": "分钟行情",
            "daily_qfq_bars": "前复权日线",
            "trading_calendar": "交易日历",
            "indicator_snapshots": "指标快照",
            "rule_events": "规则事件",
            "summary_tasks": "摘要任务",
            "review_runs": "人工复盘",
            "run_logs": "运行日志",
            "position_lots": "持仓批次",
        }
        self.stats_tree.delete(*self.stats_tree.get_children())
        try:
            stats = self.data.statistics()
            for key, count in stats.items():
                self.stats_tree.insert("", "end", values=(self.tr(names[key]), count))
            size = self.data.database_size()
            self.db_size_var.set(self.trf("数据库 {size:.2f} MB", size=size / 1024 / 1024))
        except Exception as exc:
            self.db_size_var.set(self.trf("读取失败：{error}", error=exc))

    def _open_data_folder(self) -> None:
        try:
            if os.name == "nt":
                os.startfile(self.paths.root)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.paths.root)])
            else:
                subprocess.Popen(["xdg-open", str(self.paths.root)])
        except OSError as exc:
            messagebox.showerror(
                self.tr("无法打开目录"),
                self.trf("{error}\n请手动打开：{path}", error=exc, path=self.paths.root),
            )

    def _backup_database(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.paths.backups / f"pandeng-backup-{stamp}.db"
        chosen = filedialog.asksaveasfilename(
            title=self.tr("选择新备份文件（不会覆盖已有文件）"),
            initialdir=self.paths.backups,
            initialfile=target.name,
            defaultextension=".db",
            filetypes=((self.tr("SQLite 数据库"), "*.db"),),
        )
        if not chosen:
            return
        path = Path(chosen)
        if path.exists():
            messagebox.showerror(self.tr("备份未执行"), self.tr("目标文件已存在，请选择新的文件名。"))
            return
        def work():
            return self.data.backup(path)
        def done(result):
            messagebox.showinfo(
                self.tr("备份完成"), self.trf("SQLite 在线备份及完整性检查通过：\n{path}", path=result),
            )
            self._refresh_data_stats()
        self._run_async("backup", work, done)

    def _save_retention(self) -> bool:
        try:
            updated = replace(
                self.settings,
                minute_retention_days=int(self.minute_days_var.get()),
                log_retention_days=int(self.log_days_var.get()),
            )
        except (ValueError, DesktopSettingsError) as exc:
            messagebox.showerror(self.tr("设置未保存"), self.tr(str(exc)))
            return False
        self._commit_settings(
            updated, self.tr("数据保留设置已保存；尚未删除任何记录。"), "data", notify=False,
        )
        return True

    def _preview_cleanup(self) -> None:
        try:
            minute_days = int(self.minute_days_var.get())
            log_days = int(self.log_days_var.get())
        except ValueError:
            messagebox.showerror(self.tr("无法预览"), self.tr("保留天数必须是整数。"))
            return
        self.cleanup_var.set(self.tr("正在统计可清理记录，尚未删除……"))
        def work():
            return self.data.preview_cleanup(minute_days, log_days)
        def done(result):
            self._cleanup_preview = result
            self.cleanup_var.set(self.trf(
                "预览：分钟 {minutes} 条、日志 {logs} 条可清理；另有 {protected} 条旧分钟因追溯保护而保留。执行前还需再次确认。",
                minutes=len(result.minute_ids),
                logs=len(result.log_ids),
                protected=result.protected_minutes,
            ))
            if result.total:
                self.cleanup_button.state(["!disabled"])
            else:
                self.cleanup_button.state(["disabled"])
        self._run_async("cleanup-preview", work, done)

    def _execute_cleanup(self) -> None:
        preview = self._cleanup_preview
        if preview is None or not preview.total:
            messagebox.showinfo(self.tr("清理"), self.tr("请先生成有效的清理预览。"))
            return
        confirmed = messagebox.askyesno(
            self.tr("确认执行清理"),
            self.trf(
                "将删除分钟 {minutes} 条、运行日志 {logs} 条。\n为保留追溯链，{protected} 条旧分钟不会删除。\n日线、日历、人工复盘、持仓和关键审计不在本次范围内。\n\n是否继续？",
                minutes=len(preview.minute_ids),
                logs=len(preview.log_ids),
                protected=preview.protected_minutes,
            ),
        )
        if not confirmed:
            return
        minute_days = int(self.minute_days_var.get())
        log_days = int(self.log_days_var.get())
        def work():
            return self.data.cleanup(preview, minute_days, log_days)
        def done(result):
            self._cleanup_preview = None
            self.cleanup_button.state(["disabled"])
            self.cleanup_var.set(self.trf(
                "清理完成：分钟 {minutes} 条、日志 {logs} 条；数据库文件未强制压缩。",
                minutes=result[0],
                logs=result[1],
            ))
            self._refresh_data_stats()
        self._run_async("cleanup", work, done)

    def _build_demo(self) -> None:
        page = self.pages["demo"]
        self._page_header(page, "一键整合离线演示", "依次检查观察恢复、固定模板摘要、本地复盘、数据体检和事件摘要；不联网、不调用模型。")
        card = self._card(page)
        card.pack(fill="x")
        ttk.Label(card, text="演示只写入以下独立数据库；个人数据库不参与。已有演示仅读取，旧版或不完整演示不会自动补写。", style="Surface.TLabel", wraplength=760).pack(anchor="w")
        ttk.Label(card, text=str(self.paths.demo_database), style="SurfaceMuted.TLabel", wraplength=760).pack(anchor="w", pady=(8, 14))
        self.demo_button = ttk.Button(card, text="开始／查看离线演示", style="Primary.TButton", command=self._run_demo)
        self.demo_button.pack(anchor="w")
        output = ttk.Frame(page, style="Surface.TFrame")
        output.pack(fill="both", expand=True, pady=(18, 0))
        demo_scrollbar = ttk.Scrollbar(output, orient="vertical")
        self.demo_text = tk.Text(
            output,
            wrap="word",
            height=14,
            relief="flat",
            padx=18,
            pady=16,
            font=(self.theme.family, 10),
            bg=self.theme.colors["surface"],
            fg=self.theme.colors["text"],
            insertbackground=self.theme.colors["text"],
            yscrollcommand=demo_scrollbar.set,
        )
        demo_scrollbar.configure(command=self.demo_text.yview)
        self.demo_text.pack(side="left", fill="both", expand=True)
        demo_scrollbar.pack(side="right", fill="y")
        self._set_demo_output(self.tr("点击“开始／查看离线演示”。首次运行会生成模拟历史、固定模板摘要和本地复盘，可能需要几秒。\n随后自动读取数据体检与事件摘要。期间可以切换页面，但不要关闭程序。"))

    def _set_demo_output(self, text: str) -> None:
        self.demo_text.configure(state="normal")
        self.demo_text.delete("1.0", "end")
        self.demo_text.insert("1.0", text)
        self.demo_text.configure(state="disabled")

    def _run_demo(self) -> None:
        self.demo_button.state(["disabled"])
        self._set_demo_output(self.tr("正在运行独立离线演示……\n真实网络请求 0 次；Ollama 调用 0 次；云端调用 0 次。"))
        def done(result):
            self._set_demo_output(result.output)
            messagebox.showinfo(
                self.tr("离线演示"),
                self.tr(
                    "结果已准备好。已有演示只读返回，没有混合两场数据。"
                    if result.reused else "首次离线演示完成，结果已保存到独立数据库。"
                ),
            )
        self._run_async("analysis-demo", self.demo.run, done)

    def _build_about(self) -> None:
        page = self.pages["about"]
        self._page_header(page, "关于盘灯", "Panlume｜本地公开信息观察、个人研究记录与软件学习。")
        card = self._card(page)
        card.pack(fill="both", expand=True)
        if self._about_logo is not None:
            ttk.Label(card, image=self._about_logo, style="Surface.TLabel").pack(anchor="w", pady=(0, 12))
        ttk.Label(card, text=f"{self.tr(APP_NAME)}  v{APP_VERSION}", style="Section.TLabel Surface.TLabel").pack(anchor="w")
        details = (
            self.trf("构建类型：{channel}", channel=BUILD_CHANNEL) + "\n"
            + self.trf("核心数据库结构：{version}", version=SCHEMA_VERSION) + "\n"
            + f"Python：{platform.python_version()}\n"
            + self.trf(
                "运行平台：{platform}", platform=f"{platform.system()} {platform.release()}",
            ) + "\n\n"
            + self.tr("已独立实测：东方财富盘中双探针、真实分钟观察、deepseek-r1:14b 事实摘要与模板回退。") + "\n"
            + self.tr("桌面能力：离线观察、受控短验收、本地复盘、数据体检、事件与摘要追溯、备份和保守清理。") + "\n"
            + self.tr("待继续验证：真实前复权日线、可靠交易日历、真实分钟自动轮询与本版 Windows 成品。") + "\n\n"
            + self.tr("项目只处理公开信息和个人研究记录，不连接交易账户，不执行订单，不构成投资建议。") + "\n"
            + self.tr("GitHub 尚未公开；本构建不显示虚构的项目链接。")
        )
        ttk.Label(card, text=details, style="Surface.TLabel", justify="left", wraplength=730).pack(anchor="w", pady=(14, 20))
        ttk.Button(card, text="复制诊断摘要", style="Secondary.TButton", command=self._copy_diagnostics).pack(anchor="w")

    def _copy_diagnostics(self) -> None:
        text = (
            f"{APP_NAME} v{APP_VERSION} ({BUILD_CHANNEL})\n"
            f"Schema: {self.data.schema_version()}\n"
            f"Python: {platform.python_version()}\n"
            f"Platform: {platform.platform()}\n"
            f"Data: {self.paths.root}\n"
            "Cloud: disabled\n"
            "Startup network/model calls: none"
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set(self.tr("诊断摘要已复制；不包含 API Key、账户或持仓内容"))

    def _commit_settings(self, updated: DesktopSettings, message: str, page: str, notify: bool = True) -> None:
        try:
            self.store.save(updated)
        except OSError as exc:
            messagebox.showerror(self.tr("设置未保存"), self.tr(str(exc)))
            return
        theme_changed = updated.theme != self.settings.theme
        self.settings = updated
        self._cleanup_preview = None
        if theme_changed:
            self.theme = DesktopTheme(self.root, updated.theme)
            self._build_ui()
        self.show_page(page)
        self.status_var.set(message)
        if notify:
            messagebox.showinfo(self.tr("已保存"), message)

    def _rebuild_current_page(self) -> None:
        current = self.current_page
        self._build_ui()
        self.show_page(current)
        self.status_var.set(self.tr("已恢复为上次保存的设置"))

    def _run_async(self, name, work, on_success) -> None:
        if name in self.active_tasks:
            self.status_var.set(self.tr("相同任务正在运行，请勿重复启动"))
            return
        self.active_tasks.add(name)
        self.status_var.set(self.trf("正在执行：{name}；不会自动重试", name=name))
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:
                error = exc
                detail = traceback.format_exc()
            else:
                error = None
                detail = ""
            def finish() -> None:
                if self._closing:
                    return
                self.active_tasks.discard(name)
                if hasattr(self, "demo_button"):
                    self.demo_button.state(["!disabled"])
                if hasattr(self, "review_generate_button"):
                    self.review_generate_button.state(["!disabled"])
                if hasattr(self, "audit_run_button"):
                    self.audit_run_button.state(["!disabled"])
                if hasattr(self, "history_refresh_button"):
                    self.history_refresh_button.state(["!disabled"])
                if hasattr(self, "report_page"):
                    for button_name in ("preview_button", "generate_button", "event_refresh_button"):
                        getattr(self.report_page, button_name).state(["!disabled"])
                if hasattr(self, "ollama_page"):
                    self.ollama_page.reset_buttons()
                if hasattr(self, "csv_replay_card"):
                    self.csv_replay_card.replay_button.state(["!disabled"])
                if error is not None:
                    if name == "data-audit":
                        self.audit_status_var.set(self.tr("数据体检未完成；没有自动重试、补数、模型或真实行情请求。"))
                    if name == "event-history":
                        self.history_status_var.set(self.tr("历史读取未完成；没有自动重试、任务恢复、写表或模型请求。"))
                    if name == "local-review":
                        self.review_status_var.set(self.tr("本地复盘未完成；没有自动重试，也没有调用模型或真实行情。"))
                    log_path = self.paths.logs / f"desktop-error-{datetime.now():%Y%m%d-%H%M%S}.log"
                    try:
                        log_path.write_text(detail, encoding="utf-8")
                    except OSError:
                        pass
                    self.status_var.set(self.trf("任务失败：{error}", error=type(error).__name__))
                    messagebox.showerror(
                        self.tr("任务未完成"),
                        self.trf(
                            "{error}\n\n没有自动重试。详细诊断保存在：{path}",
                            error=f"{type(error).__name__}: {error}",
                            path=log_path,
                        ),
                    )
                    if name == "analysis-demo":
                        self._set_demo_output(self.trf(
                            "离线演示未完成：{error}\n没有调用真实行情或模型，也没有自动重试。",
                            error=f"{type(error).__name__}: {error}",
                        ))
                    return
                self.status_var.set(self.tr("任务完成；没有自动重试"))
                on_success(result)
            self._post_ui(finish)
        threading.Thread(target=runner, name=f"panlume-{name}", daemon=True).start()

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)
        self.status_var.set(self.tr("已进入全屏；按 Esc 退出" if self.fullscreen else "已退出全屏"))

    def exit_fullscreen(self) -> None:
        if self.fullscreen:
            self.fullscreen = False
            self.root.attributes("-fullscreen", False)
            self.status_var.set(self.tr("已退出全屏"))

    def close(self) -> None:
        if self.active_tasks:
            messagebox.showwarning(
                self.tr("任务仍在运行"),
                self.tr("请先停止观察，或等待当前复盘、体检、历史读取、备份、演示或清理任务完成再退出，避免强制中断 SQLite 写入。"),
            )
            return
        self._closing = True
        if self._ui_after_id is not None:
            try:
                self.root.after_cancel(self._ui_after_id)
            except tk.TclError:
                pass
            self._ui_after_id = None
        self.root.destroy()


def main() -> int:
    configure_windows_dpi()
    root = tk.Tk()
    initialize_tk_scaling(root)
    try:
        PanlumeWindow(root, default_language="en")
    except Exception as exc:
        root.withdraw()
        messagebox.showerror(
            translate("盘灯无法启动", "en"),
            f"{type(exc).__name__}: {exc}\n"
            + translate("请检查个人数据目录权限，不要关闭杀毒软件。", "en"),
        )
        root.destroy()
        return 1
    root.mainloop()
    return 0
