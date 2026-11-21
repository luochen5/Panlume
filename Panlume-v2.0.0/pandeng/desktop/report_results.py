"""整页报告成果展示；复用报告中心的生成、导出和证据服务。"""

from __future__ import annotations

import tkinter as tk
from tkinter import scrolledtext, ttk


class ReportResultsPage:
    def __init__(self, window) -> None:
        self.window = window
        self._observation_result = None

    def build(self) -> None:
        window = self.window
        page = window.pages["results"]
        window._page_header(
            page, "报告成果",
            "生成结果是项目的核心输出；程序事实与可选模型叙述分区展示，原始证据始终可追溯。",
        )
        self.meta_var = tk.StringVar()
        self.status_var = tk.StringVar()
        ttk.Label(page, textvariable=self.meta_var, style="Section.TLabel", wraplength=780).pack(anchor="w")
        ttk.Label(page, textvariable=self.status_var, style="Muted.TLabel", wraplength=780).pack(
            anchor="w", pady=(4, 10),
        )
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 10))
        self.export_json_button = ttk.Button(
            actions, text="导出 JSON", command=lambda: window.report_page.export("json")
        )
        self.export_md_button = ttk.Button(
            actions, text="导出 Markdown", command=lambda: window.report_page.export("markdown")
        )
        self.history_button = ttk.Button(
            actions, text="相关事件历史", command=window.report_page.open_history
        )
        self.raw_button = ttk.Button(
            actions, text="人工复核原始证据", command=window.report_page.open_raw_evidence
        )
        for button in (
            self.export_json_button, self.export_md_button, self.history_button, self.raw_button,
        ):
            button.pack(side="left", padx=(0, 8))

        self.notebook = ttk.Notebook(page)
        self.notebook.pack(fill="both", expand=True)
        c = window.theme.colors
        self.texts = {}
        for key, label in (
            ("full", "完整成果"), ("facts", "程序事实"), ("narrative", "模型叙述"),
        ):
            frame = ttk.Frame(self.notebook)
            text = scrolledtext.ScrolledText(
                frame, wrap="word", relief="flat", bg=c["bg"], fg=c["text"],
                insertbackground=c["text"],
            )
            text.pack(fill="both", expand=True)
            self.notebook.add(frame, text=label)
            self.texts[key] = text
        self.refresh()

    @staticmethod
    def _set_text(widget, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def refresh(self) -> None:
        report = self.window.current_report
        if report is None:
            if self._observation_result is not None:
                return
            self.meta_var.set(self.window.tr("尚未生成成果"))
            self.status_var.set(self.window.tr("请先在报告中心生成报告，或在观察页生成观察摘要。"))
            self._set_text(self.texts["full"], self.window.tr("成果将在这里以大文本区域展示。"))
            self._set_text(self.texts["facts"], self.window.tr("无程序事实报告。"))
            self._set_text(self.texts["narrative"], self.window.tr("无模型叙述。"))
            for button in (
                self.export_json_button, self.export_md_button, self.history_button, self.raw_button,
            ):
                button.state(["disabled"])
            return
        self._observation_result = None
        self.window.report_page.current_report = report
        package = report.package
        self.meta_var.set(f"{package.stock_code} | {package.data_range['session_date']} | {package.report_type}")
        size = self.window.tr("未知") if report.parameter_size_b is None else f"{report.parameter_size_b:g}B"
        if report.model_used:
            status = self.window.trf(
                "模型叙述已采用；参数规模 {size}；等级 {tier}；事实校验通过。",
                size=size, tier=report.model_tier,
            )
        elif report.fallback_reason:
            status = self.window.trf(
                "模型未采用（{reason}）；完整固定事实报告保留。{details}",
                reason=report.fallback_reason,
                details="；".join(report.validation_reasons),
            )
        else:
            status = self.window.tr("使用完整固定模板；本次未请求模型。")
        self.status_var.set(status)
        self._set_text(self.texts["full"], report.text)
        self._set_text(self.texts["facts"], report.program_facts_text or report.text)
        narrative = report.narrative_text or self.window.tr(
            "本次没有采用模型叙述；完整固定事实报告不受影响。"
        )
        self._set_text(self.texts["narrative"], narrative)
        self.notebook.select(2 if report.model_used else 0)
        for button in (
            self.export_json_button, self.export_md_button, self.history_button, self.raw_button,
        ):
            button.state(["!disabled"])

    def show_observation(self, result) -> None:
        self.window.current_report = None
        self._observation_result = result
        self.meta_var.set(self.window.tr("本地观察摘要"))
        self.status_var.set(result.note)
        self._set_text(self.texts["full"], result.text)
        self._set_text(
            self.texts["facts"], result.text if not result.used_model else self.window.tr(
                "本次为模型观察摘要；请返回观察页核对原始事实。"
            )
        )
        self._set_text(
            self.texts["narrative"], result.text if result.used_model else self.window.tr(
                "本次使用固定模板，没有模型叙述。"
            )
        )
        self.notebook.select(2 if result.used_model else 0)
        for button in (
            self.export_json_button, self.export_md_button, self.history_button, self.raw_button,
        ):
            button.state(["disabled"])
