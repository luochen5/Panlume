"""Ollama 图形化管理；所有操作均由用户点击并沿用主窗口任务保护。"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk

from pandeng.summarizer.management import OllamaManagementClient, OllamaModelInfo
from pandeng.summarizer.ollama import OllamaError


@dataclass(frozen=True)
class ManagementOutcome:
    success: bool
    kind: str
    message: str


class OllamaManagerPage:
    def __init__(self, window, parent) -> None:
        self.window = window
        self.parent = parent
        self.models: tuple[OllamaModelInfo, ...] = ()
        self._download_client: OllamaManagementClient | None = None

    def build(self) -> None:
        card = self.window._card(self.parent)
        card.pack(fill="x", pady=(12, 0))
        ttk.Label(card, text="Ollama 安装、连接与本地模型", style="Section.TLabel Surface.TLabel").grid(
            row=0, column=0, columnspan=6, sticky="w", pady=(0, 10),
        )
        ttk.Label(card, text="连接地址", style="Surface.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        self.window.ollama_base_url_var = tk.StringVar(value=self.window.settings.ollama_base_url)
        ttk.Entry(card, textvariable=self.window.ollama_base_url_var, width=36).grid(
            row=1, column=1, columnspan=2, sticky="w", padx=10, pady=5,
        )
        self.detect_button = ttk.Button(card, text="检测安装并测试连接", command=self.detect)
        self.detect_button.grid(row=1, column=3, sticky="w", padx=10, pady=5)
        self.installation_var = tk.StringVar(value=self.window.tr("尚未检测；不会自动启动 Ollama 或下载模型。"))
        ttk.Label(card, textvariable=self.installation_var, style="SurfaceMuted.TLabel", wraplength=760).grid(
            row=2, column=0, columnspan=6, sticky="w", pady=(0, 8),
        )
        columns = ("name", "disk", "parameters", "tier", "quantization")
        self.tree = ttk.Treeview(card, columns=columns, show="headings", height=5, selectmode="browse")
        headings = {
            "name": "模型名称", "disk": "磁盘占用 GiB", "parameters": "参数规模 B",
            "tier": "推荐等级", "quantization": "量化",
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=190 if column == "name" else 105, minwidth=75)
        self.tree.grid(row=3, column=0, columnspan=6, sticky="ew", pady=(5, 0))
        self.tree.bind("<<TreeviewSelect>>", self._select_model)
        ttk.Label(
            card,
            text="推荐等级只是默认工作建议，不是能力认证；参数未知时显示“未知”。选择行只设置摘要模型，仍需保存设置。",
            style="SurfaceMuted.TLabel", wraplength=780,
        ).grid(row=4, column=0, columnspan=6, sticky="w", pady=(6, 8))

        download = self.window._card(self.parent)
        download.pack(fill="x", pady=(12, 0))
        ttk.Label(download, text="模型下载与删除", style="Section.TLabel Surface.TLabel").grid(
            row=0, column=0, columnspan=5, sticky="w", pady=(0, 10),
        )
        ttk.Label(download, text="准备下载的模型", style="Surface.TLabel").grid(row=1, column=0, sticky="w", pady=5)
        self.download_name_var = tk.StringVar()
        ttk.Entry(download, textvariable=self.download_name_var, width=32).grid(
            row=1, column=1, sticky="w", padx=10, pady=5,
        )
        self.download_button = ttk.Button(download, text="确认后下载", command=self.download)
        self.download_button.grid(row=1, column=2, sticky="w", padx=5, pady=5)
        self.cancel_button = ttk.Button(download, text="取消下载", command=self.cancel_download)
        self.cancel_button.grid(row=1, column=3, sticky="w", padx=5, pady=5)
        self.delete_button = ttk.Button(download, text="删除选中模型", command=self.delete_selected)
        self.delete_button.grid(row=1, column=4, sticky="w", padx=5, pady=5)
        self.cancel_button.state(["disabled"])
        self.progress = ttk.Progressbar(download, orient="horizontal", mode="determinate", maximum=100)
        self.progress.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(8, 4))
        self.progress_var = tk.StringVar(value=self.window.tr("没有下载任务；失败后不会自动重试。"))
        ttk.Label(download, textvariable=self.progress_var, style="SurfaceMuted.TLabel", wraplength=780).grid(
            row=3, column=0, columnspan=5, sticky="w", pady=(0, 4),
        )
        ttk.Label(
            download,
            text="下载前会再次确认；预计大小无法获得时显示未知。只调用 Ollama 正式 API，不修改其内部文件。",
            style="SurfaceMuted.TLabel", wraplength=780,
        ).grid(row=4, column=0, columnspan=5, sticky="w")

    def _client(self) -> OllamaManagementClient:
        return OllamaManagementClient(
            self.window.ollama_base_url_var.get().strip(),
            min(10, self.window.settings.ollama_timeout_seconds),
        )

    def detect(self) -> None:
        self.detect_button.state(["disabled"])
        client = self._client()
        self.installation_var.set(self.window.tr("正在检测安装和连接；不会生成文本或下载模型。"))
        self.window._run_async("ollama-management-detect", client.detect, self._detect_done)

    def _detect_done(self, result) -> None:
        self.models = result.models
        for item in self.tree.get_children():
            self.tree.delete(item)
        for model in self.models:
            self.tree.insert("", "end", iid=model.name, values=(
                model.name,
                self.window.tr("未知") if model.size_gib is None else f"{model.size_gib:.2f}",
                self.window.tr("未知") if model.parameter_size_b is None else f"{model.parameter_size_b:g}",
                model.recommended_tier,
                model.quantization or self.window.tr("未知"),
            ))
        names = tuple(item.name for item in self.models)
        self.window.model_combo.configure(values=names or (self.window.settings.model_name,))
        executable = result.executable_path or self.window.tr("未在 PATH 中找到可执行文件")
        self.installation_var.set(
            self.window.trf(
                "安装状态：{installed}；运行状态：{online}；版本：{version}；路径：{path}；{message}",
                installed=result.installed, online=result.online,
                version=result.version or self.window.tr("未知"), path=executable,
                message=result.message,
            )
        )
        self.window.ollama_health_var.set(result.message)

    def _select_model(self, _event=None) -> None:
        selected = self.tree.selection()
        if selected:
            self.window.model_var.set(selected[0])

    def _estimated_size(self, name: str) -> str:
        for model in self.models:
            if model.name == name and model.size_gib is not None:
                return f"{model.size_gib:.2f} GiB"
        return self.window.tr("未知")

    def download(self) -> None:
        name = self.download_name_var.get().strip()
        if not name:
            messagebox.showwarning(self.window.tr("未下载"), self.window.tr("请先输入或选择准备下载的模型名称。"))
            return
        confirmed = messagebox.askyesno(
            self.window.tr("确认下载模型"),
            self.window.trf(
                "模型：{name}\n预计大小：{size}\n\n下载可能占用大量磁盘和流量。只执行这一次，不自动重试。是否继续？",
                name=name, size=self._estimated_size(name),
            ),
        )
        if not confirmed:
            return
        client = self._client()
        self._download_client = client
        self.download_button.state(["disabled"])
        self.cancel_button.state(["!disabled"])
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(10)
        self.progress_var.set(self.window.tr("正在开始下载；等待 Ollama 返回真实进度。"))

        def work() -> ManagementOutcome:
            try:
                for progress in client.pull(name):
                    self.window._post_ui(lambda item=progress: self._pull_progress(item))
                return ManagementOutcome(True, "succeeded", self.window.tr("下载已完成；可点击检测刷新模型列表。"))
            except OllamaError as exc:
                return ManagementOutcome(False, exc.kind, str(exc))
            except Exception as exc:
                return ManagementOutcome(False, type(exc).__name__, str(exc))

        self.window._run_async("ollama-download", work, self._download_done)

    def _pull_progress(self, progress) -> None:
        fraction = progress.fraction
        if fraction is None:
            self.progress.configure(mode="indeterminate")
            self.progress.start(10)
            size = self.window.tr("未知")
            completed = self.window.tr("未知") if progress.completed_bytes is None else str(progress.completed_bytes)
            self.progress_var.set(self.window.trf(
                "{status}；已下载 {completed} 字节；总大小 {size}。",
                status=progress.status, completed=completed, size=size,
            ))
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=fraction * 100)
            self.progress_var.set(self.window.trf(
                "{status}；{completed}/{total} 字节（{percent:.1f}%）。",
                status=progress.status, completed=progress.completed_bytes,
                total=progress.total_bytes, percent=fraction * 100,
            ))

    def cancel_download(self) -> None:
        if self._download_client is not None:
            self._download_client.cancel_download()
            self.progress_var.set(self.window.tr("正在取消；不会自动重试，界面将在请求结束后恢复。"))

    def _download_done(self, outcome: ManagementOutcome) -> None:
        self.progress.stop()
        self._download_client = None
        self.download_button.state(["!disabled"])
        self.cancel_button.state(["disabled"])
        if outcome.success:
            self.progress.configure(mode="determinate", value=100)
        else:
            self.progress.configure(mode="determinate", value=0)
        self.progress_var.set(self.window.trf("{kind}：{message}", kind=outcome.kind, message=outcome.message))

    def delete_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning(self.window.tr("未删除"), self.window.tr("请先在本地模型列表中选择一行。"))
            return
        name = selected[0]
        confirmed = messagebox.askyesno(
            self.window.tr("再次确认删除模型"),
            self.window.trf(
                "将通过 Ollama 正式删除能力移除：{name}\n\n此操作可能无法撤销。不会手动删除 blobs 或 manifests。是否继续？",
                name=name,
            ),
        )
        if not confirmed:
            return
        client = self._client()
        self.delete_button.state(["disabled"])

        def work() -> ManagementOutcome:
            try:
                client.delete_model(name)
                return ManagementOutcome(True, "deleted", self.window.tr("模型已通过 Ollama 正式 API 删除；请点击检测刷新列表。"))
            except OllamaError as exc:
                return ManagementOutcome(False, exc.kind, str(exc))

        self.window._run_async("ollama-delete", work, self._delete_done)

    def _delete_done(self, outcome: ManagementOutcome) -> None:
        self.delete_button.state(["!disabled"])
        self.progress_var.set(self.window.trf("{kind}：{message}", kind=outcome.kind, message=outcome.message))

    def reset_buttons(self) -> None:
        for button in (self.detect_button, self.download_button, self.delete_button):
            button.state(["!disabled"])
        if "ollama-download" not in self.window.active_tasks:
            self.cancel_button.state(["disabled"])
            self.progress.stop()
            self._download_client = None
