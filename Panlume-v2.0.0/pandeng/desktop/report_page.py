"""报告中心 Tkinter 页面；所有业务事实由 DesktopReportService 提供。"""

from __future__ import annotations

import tkinter as tk
from datetime import date
from tkinter import filedialog, messagebox, scrolledtext, ttk

from pandeng.reporting.presets import ReportPreset, ReportPresetError


class ReportPage:
    def __init__(self, window) -> None:
        self.window = window
        self.service = window.report_service
        self.store = window.report_preset_store
        self.current_report = None
        self.current_preview = None
        self.event_ids: dict[str, int] = {}
        self.presets: tuple[ReportPreset, ...] = (ReportPreset(),)
        self._load_warning = ""
        try:
            self.presets = self.store.load()
        except ReportPresetError as exc:
            self._load_warning = str(exc)

    def build(self) -> None:
        window = self.window
        page = window.pages["reports"]
        window._page_header(
            page, "报告中心",
            "统一事实包驱动实时摘要与单票详细分析；显示预设不会删除底层事实或审计记录。",
        )
        selection = window._card(page)
        selection.pack(fill="x")
        ttk.Label(selection, text="报告与预设", style="Section.TLabel Surface.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10),
        )
        self.preset_names = {item.name: item for item in self.presets}
        self.preset_var = tk.StringVar(value=self.presets[0].name)
        self.preset_combo = ttk.Combobox(
            selection, textvariable=self.preset_var, values=tuple(self.preset_names),
            state="readonly", width=22,
        )
        ttk.Label(selection, text="已保存预设", style="Surface.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        self.preset_combo.grid(row=1, column=1, sticky="w", padx=(10, 20), pady=5)
        self.preset_combo.bind("<<ComboboxSelected>>", lambda _event: self._load_selected_preset())
        ttk.Label(selection, text="预设名称", style="Surface.TLabel").grid(row=1, column=2, sticky="w", pady=5)
        self.preset_name_var = tk.StringVar(value=self.presets[0].name)
        ttk.Entry(selection, textvariable=self.preset_name_var, width=24).grid(
            row=1, column=3, sticky="w", padx=10, pady=5,
        )
        self.type_names = {
            window.tr("单票收盘详细分析"): "daily_close_detail",
            window.tr("实时事件摘要"): "realtime_event_summary",
        }
        self.detail_names = {
            window.tr("简洁"): "brief", window.tr("标准"): "standard",
            window.tr("详细"): "detailed",
        }
        self.length_names = {
            window.tr("较短"): "short", window.tr("标准"): "standard",
            window.tr("完整"): "full",
        }
        self.language_names = {
            window.tr("跟随本次启动"): "startup", "简体中文": "zh-CN",
            "繁體中文": "zh-TW", "English": "en", "日本語": "ja",
        }
        self.tier_names = {
            window.tr("自动推荐"): "", "一级": "tier1", "二级": "tier2",
            "三级": "tier3", "三级增强": "tier3_plus",
        }
        self.type_var = tk.StringVar()
        self.detail_var = tk.StringVar()
        self.length_var = tk.StringVar()
        self.language_var = tk.StringVar()
        self.model_name_var = tk.StringVar()
        self.tier_var = tk.StringVar()
        self._combo_row(selection, 2, "报告类型", self.type_var, self.type_names, self._report_type_changed)
        self._combo_row(selection, 3, "详细程度", self.detail_var, self.detail_names)
        self._combo_row(selection, 4, "篇幅偏好", self.length_var, self.length_names)
        self._combo_row(selection, 5, "报告语言", self.language_var, self.language_names)
        ttk.Label(selection, text="本地模型偏好（留空为固定模板）", style="Surface.TLabel").grid(
            row=6, column=0, sticky="w", pady=5,
        )
        ttk.Combobox(
            selection, textvariable=self.model_name_var,
            values=("", window.settings.model_name), width=28,
        ).grid(row=6, column=1, columnspan=2, sticky="w", padx=10, pady=5)
        self._combo_row(selection, 7, "模型推荐等级覆盖", self.tier_var, self.tier_names)
        ttk.Label(
            selection, text="模型只生成可选叙述区；程序事实区始终保留，异常时只回退叙述。",
            style="SurfaceMuted.TLabel", wraplength=720,
        ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(2, 8))

        self.display_vars = {
            "show_data_quality": tk.BooleanVar(value=True),
            "show_indicators": tk.BooleanVar(value=True),
            "show_rule_basis": tk.BooleanVar(value=True),
            "show_timeline": tk.BooleanVar(value=True),
            "show_conditional_interpretation": tk.BooleanVar(value=True),
            "show_limitations_traceability": tk.BooleanVar(value=True),
        }
        display_labels = {
            "show_data_quality": "显示数据质量",
            "show_indicators": "显示指标明细",
            "show_rule_basis": "显示规则计算依据",
            "show_timeline": "显示时间线",
            "show_conditional_interpretation": "显示条件性解释",
            "show_limitations_traceability": "显示限制与追溯",
        }
        display = ttk.Frame(selection, style="Surface.TFrame")
        display.grid(row=9, column=0, columnspan=4, sticky="w", pady=4)
        for index, (name, variable) in enumerate(self.display_vars.items()):
            ttk.Checkbutton(display, text=display_labels[name], variable=variable).grid(
                row=index // 3, column=index % 3, sticky="w", padx=(0, 18), pady=3,
            )
        ttk.Label(selection, text="自定义格式说明（不能覆盖事实与系统约束）", style="Surface.TLabel").grid(
            row=10, column=0, columnspan=4, sticky="w", pady=(8, 3),
        )
        self.custom_text = tk.Text(selection, height=3, wrap="word", width=80)
        self.custom_text.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        preset_actions = ttk.Frame(selection, style="Surface.TFrame")
        preset_actions.grid(row=12, column=0, columnspan=4, sticky="w")
        ttk.Button(preset_actions, text="保存当前预设", command=self._save_preset).pack(side="left")
        ttk.Button(preset_actions, text="恢复默认预设", command=self._restore_default).pack(side="left", padx=10)

        source = window._card(page)
        source.pack(fill="x", pady=(12, 0))
        ttk.Label(source, text="股票、日期与事件", style="Section.TLabel Surface.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10),
        )
        codes = tuple(item.code for item in window.settings.stocks)
        self.code_var = tk.StringVar(value=codes[0] if codes else "600000")
        self.date_var = tk.StringVar(value=date.today().isoformat())
        ttk.Label(source, text="股票代码", style="Surface.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(source, textvariable=self.code_var, values=codes, width=20).grid(
            row=1, column=1, sticky="w", padx=10, pady=5,
        )
        ttk.Label(source, text="交易日", style="Surface.TLabel").grid(row=1, column=2, sticky="w", pady=5)
        ttk.Entry(source, textvariable=self.date_var, width=22).grid(
            row=1, column=3, sticky="w", padx=10, pady=5,
        )
        ttk.Label(source, text="事件检查（实时摘要）", style="Surface.TLabel").grid(row=2, column=0, sticky="w", pady=5)
        self.event_var = tk.StringVar()
        self.event_combo = ttk.Combobox(source, textvariable=self.event_var, state="readonly", width=70)
        self.event_combo.grid(row=2, column=1, columnspan=2, sticky="w", padx=10, pady=5)
        self.event_refresh_button = ttk.Button(source, text="刷新事件列表", command=self.refresh_events)
        self.event_refresh_button.grid(row=2, column=3, sticky="w", padx=10, pady=5)

        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(12, 0))
        self.preview_button = ttk.Button(actions, text="预览事实包摘要", command=self.preview)
        self.preview_button.pack(side="left")
        self.generate_button = ttk.Button(
            actions, text="生成报告", style="Primary.TButton", command=self.generate,
        )
        self.generate_button.pack(side="left", padx=10)
        self.export_json_button = ttk.Button(actions, text="导出 JSON", command=lambda: self.export("json"))
        self.export_md_button = ttk.Button(actions, text="导出 Markdown", command=lambda: self.export("markdown"))
        self.export_json_button.pack(side="left", padx=(20, 5))
        self.export_md_button.pack(side="left")
        self.history_button = ttk.Button(actions, text="相关事件历史", command=self.open_history)
        self.raw_button = ttk.Button(actions, text="人工复核原始证据", command=self.open_raw_evidence)
        self.history_button.pack(side="left", padx=(20, 5))
        self.raw_button.pack(side="left")
        for button in (self.export_json_button, self.export_md_button, self.history_button, self.raw_button):
            button.state(["disabled"])

        self.status_var = tk.StringVar(value=self._load_warning or window.tr("尚未生成；本页不会自动调用行情、Ollama 或云端。"))
        ttk.Label(page, textvariable=self.status_var, style="Muted.TLabel", wraplength=780).pack(
            anchor="w", pady=(8, 0),
        )
        self.model_status_var = tk.StringVar(value=window.tr("模型：未请求；回退原因：无；输出校验：不适用。"))
        ttk.Label(page, textvariable=self.model_status_var, style="Muted.TLabel", wraplength=780).pack(anchor="w")
        c = window.theme.colors
        ttk.Label(page, text="事实包摘要", style="Section.TLabel").pack(anchor="w", pady=(10, 3))
        self.preview_text = scrolledtext.ScrolledText(
            page, height=7, wrap="word", relief="flat", bg=c["bg"], fg=c["text"],
        )
        self.preview_text.pack(fill="x")
        ttk.Label(page, text="报告预览（可选择复制）", style="Section.TLabel").pack(anchor="w", pady=(10, 3))
        self.report_text = scrolledtext.ScrolledText(
            page, height=18, wrap="word", relief="flat", bg=c["bg"], fg=c["text"],
        )
        self.report_text.pack(fill="both", expand=True)
        self._set_text(self.preview_text, window.tr("选择股票、日期和报告类型后，点击“预览事实包摘要”。"))
        self._set_text(self.report_text, window.tr("报告尚未生成。无 Ollama 时固定模板仍包含完整程序事实区。"))
        self._load_selected_preset()

    def _combo_row(self, parent, row: int, label: str, variable, names, command=None) -> None:
        ttk.Label(parent, text=label, style="Surface.TLabel").grid(row=row, column=0, sticky="w", pady=5)
        combo = ttk.Combobox(
            parent, textvariable=variable, values=tuple(names), state="readonly", width=28,
        )
        combo.grid(row=row, column=1, columnspan=2, sticky="w", padx=10, pady=5)
        if command is not None:
            combo.bind("<<ComboboxSelected>>", lambda _event: command())

    @staticmethod
    def _set_text(widget, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _load_selected_preset(self) -> None:
        preset = self.preset_names.get(self.preset_var.get(), self.presets[0])
        self.preset_name_var.set(preset.name)
        self.type_var.set(next(key for key, value in self.type_names.items() if value == preset.report_type))
        self.detail_var.set(next(key for key, value in self.detail_names.items() if value == preset.detail_level))
        self.length_var.set(next(key for key, value in self.length_names.items() if value == preset.length_preference))
        self.language_var.set(next(key for key, value in self.language_names.items() if value == preset.report_language))
        self.model_name_var.set(preset.model_name)
        self.tier_var.set(next(key for key, value in self.tier_names.items() if value == preset.model_tier_override))
        for name, variable in self.display_vars.items():
            variable.set(getattr(preset, name))
        self.custom_text.delete("1.0", "end")
        self.custom_text.insert("1.0", preset.custom_format_instructions)
        self._report_type_changed(refresh=False)

    def _preset_from_ui(self) -> ReportPreset:
        def selected(names, value):
            return names[value]
        return ReportPreset(
            name=self.preset_name_var.get(),
            report_type=selected(self.type_names, self.type_var.get()),
            detail_level=selected(self.detail_names, self.detail_var.get()),
            length_preference=selected(self.length_names, self.length_var.get()),
            report_language=selected(self.language_names, self.language_var.get()),
            model_name=self.model_name_var.get(),
            model_tier_override=self.tier_names[self.tier_var.get()],
            custom_format_instructions=self.custom_text.get("1.0", "end").strip(),
            **{name: variable.get() for name, variable in self.display_vars.items()},
        )

    def _save_preset(self) -> None:
        try:
            self.presets = self.store.upsert(self._preset_from_ui())
        except (ReportPresetError, KeyError, OSError) as exc:
            messagebox.showerror(self.window.tr("预设未保存"), self.window.tr(str(exc)))
            return
        self.preset_names = {item.name: item for item in self.presets}
        self.preset_combo.configure(values=tuple(self.preset_names))
        self.preset_var.set(self.preset_name_var.get().strip())
        self.status_var.set(self.window.tr("预设已保存；未修改事实包、数据库或模型设置。"))

    def _restore_default(self) -> None:
        try:
            self.presets = self.store.restore_default()
        except (ReportPresetError, OSError) as exc:
            messagebox.showerror(self.window.tr("预设未恢复"), self.window.tr(str(exc)))
            return
        self.preset_names = {item.name: item for item in self.presets}
        self.preset_combo.configure(values=tuple(self.preset_names))
        self.preset_var.set("default")
        self._load_selected_preset()
        self.status_var.set(self.window.tr("默认预设已恢复；自定义预设仍保留。"))

    def _report_type_changed(self, refresh: bool = True) -> None:
        realtime = self.type_names.get(self.type_var.get()) == "realtime_event_summary"
        self.event_combo.configure(state="readonly" if realtime else "disabled")
        if not realtime:
            self.event_var.set("")
        if refresh:
            self.refresh_events()

    def refresh_events(self) -> None:
        if self.type_names.get(self.type_var.get()) != "realtime_event_summary":
            self.event_ids = {}
            self.event_combo.configure(values=())
            self.event_var.set("")
            return
        self.event_refresh_button.state(["disabled"])
        code = self.code_var.get()
        day = self.date_var.get()
        self.window._run_async(
            "report-events",
            lambda: self.service.event_choices(code, day),
            self._events_done,
        )

    def _events_done(self, choices) -> None:
        labels = []
        self.event_ids = {}
        for choice in choices:
            label = (
                f"#{choice.event_check_id} {choice.data_time} {choice.rule_id} "
                f"event={choice.event_id} {choice.status} notified={choice.notified}"
            )
            labels.append(label)
            self.event_ids[label] = choice.event_check_id
        self.event_combo.configure(values=labels)
        self.event_var.set(labels[0] if labels else "")
        self.status_var.set(self.window.trf("读取到 {count} 条事件检查；未生成报告或调用模型。", count=len(labels)))

    def _event_check_id(self) -> int | None:
        return self.event_ids.get(self.event_var.get())

    def preview(self) -> None:
        try:
            preset = self._preset_from_ui()
        except (KeyError, ReportPresetError) as exc:
            messagebox.showerror(self.window.tr("无法预览"), self.window.tr(str(exc)))
            return
        self.preview_button.state(["disabled"])
        code = self.code_var.get()
        day = self.date_var.get()
        event_check_id = self._event_check_id()
        self.window._run_async(
            "report-preview",
            lambda: self.service.preview(preset, code, day, event_check_id),
            self._preview_done,
        )

    def _preview_done(self, preview) -> None:
        self.current_preview = preview
        self._set_text(self.preview_text, preview.summary)
        self.status_var.set(self.window.tr("事实包预览完成；未修改数据库，也未调用模型。"))

    def generate(self) -> None:
        try:
            preset = self._preset_from_ui()
        except (KeyError, ReportPresetError) as exc:
            messagebox.showerror(self.window.tr("无法生成"), self.window.tr(str(exc)))
            return
        self.generate_button.state(["disabled"])
        code = self.code_var.get()
        day = self.date_var.get()
        event_check_id = self._event_check_id()
        self.window._run_async(
            "report-generate",
            lambda: self.service.generate(preset, code, day, event_check_id),
            self._generate_done,
        )

    def _generate_done(self, report) -> None:
        self.current_report = report
        self.current_preview = None
        self._set_text(self.preview_text, self.service._summary(report.package, report.language))
        self._set_text(self.report_text, report.text)
        size = self.window.tr("未知") if report.parameter_size_b is None else f"{report.parameter_size_b:g}B"
        if report.model_used:
            self.model_status_var.set(self.window.trf(
                "模型：已采用；参数规模 {size}；等级 {tier}；输出校验通过。",
                size=size, tier=report.model_tier,
            ))
        elif report.fallback_reason:
            reasons = "；".join(report.validation_reasons) or self.window.tr("无额外校验明细")
            self.model_status_var.set(self.window.trf(
                "模型未采用（{reason}），已回退固定事实模板；参数规模 {size}；等级 {tier}；{details}",
                reason=report.fallback_reason, size=size, tier=report.model_tier, details=reasons,
            ))
        else:
            self.model_status_var.set(self.window.tr(
                "模型：未请求，固定模板已生成；回退原因：无；输出校验：不适用（未使用模型）。"
            ))
        self.status_var.set(self.window.tr("报告生成完成；事实包与原始记录未修改。"))
        for button in (self.export_json_button, self.export_md_button, self.history_button, self.raw_button):
            button.state(["!disabled"])
        self.window.current_report = report
        self.window.show_page("results")

    def export(self, format_name: str) -> None:
        if self.current_report is None:
            return
        extension = ".json" if format_name == "json" else ".md"
        package = self.current_report.package
        selected = filedialog.asksaveasfilename(
            parent=self.window.root,
            title=self.window.tr("导出报告"),
            initialdir=self.window.paths.exports,
            initialfile=f"Panlume-{package.stock_code}-{package.data_range['session_date']}{extension}",
            defaultextension=extension,
            filetypes=[(extension.upper(), f"*{extension}")],
        )
        if not selected:
            return
        try:
            from pathlib import Path
            path = self.service.export(self.current_report, Path(selected), format_name)
        except (ValueError, OSError) as exc:
            messagebox.showerror(self.window.tr("导出失败"), self.window.tr(str(exc)))
            return
        self.status_var.set(self.window.trf("报告已导出：{path}", path=path))

    def open_history(self) -> None:
        if self.current_report is None:
            return
        package = self.current_report.package
        self.window.history_code_var.set(package.stock_code)
        self.window.history_date_var.set(package.data_range["session_date"])
        self.window._history_focus_event_id = (
            str(package.rule_events[0]["event_id"]) if package.rule_events else None
        )
        self.window.show_page("history")
        self.window._refresh_event_history()

    def open_raw_evidence(self) -> None:
        if self.current_report is None:
            return
        self.window._run_async(
            "report-evidence", lambda: self.service.raw_evidence(self.current_report),
            self._raw_evidence_done,
        )

    def _raw_evidence_done(self, text: str) -> None:
        dialog = tk.Toplevel(self.window.root)
        dialog.title(self.window.tr("原始证据与处理记录"))
        dialog.geometry("900x650")
        widget = scrolledtext.ScrolledText(dialog, wrap="word")
        widget.pack(fill="both", expand=True, padx=12, pady=12)
        self._set_text(widget, text)
        ttk.Button(dialog, text=self.window.tr("关闭"), command=dialog.destroy).pack(pady=(0, 12))
