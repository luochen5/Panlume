"""图形化 CSV 历史回放服务与观察页卡片。"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from pandeng.data.replay import CsvReplayAdapter
from pandeng.data.schedule import CHINA_TZ
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_calendar, ingest_daily, ingest_minutes


@dataclass(frozen=True)
class CsvReplayResult:
    summaries: tuple

    def render(self, language: str = "zh-CN") -> str:
        if language == "en":
            lines = ["CSV replay complete. No network, Ollama, or cloud calls; conflicts were not overwritten."]
        else:
            lines = ["CSV 回放完成；未访问网络、Ollama 或云端，冲突记录未被覆盖。"]
        for item in self.summaries:
            lines.append(
                f"{item.data_kind}: received={item.received}, accepted={item.accepted}, "
                f"duplicates={item.duplicates}, rejected={item.rejected}, status={item.status}"
            )
        return "\n".join(lines)


class DesktopReplayService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def run(
        self,
        minute_file: str,
        daily_file: str,
        calendar_file: str,
        stock_codes: tuple[str, ...],
        replay_clock: str = "",
    ) -> CsvReplayResult:
        paths = [value.strip() for value in (minute_file, daily_file, calendar_file)]
        if not any(paths):
            raise ValueError("至少选择一个分钟、日线或交易日历 CSV 文件。")
        if not stock_codes:
            raise ValueError("至少需要启用一个观察对象才能回放。")
        parsed_clock = None
        if replay_clock.strip():
            try:
                parsed_clock = datetime.fromisoformat(replay_clock.strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("回放时刻必须是带时区的 ISO 时间。") from exc
            if parsed_clock.tzinfo is None or parsed_clock.utcoffset() is None:
                raise ValueError("回放时刻必须带时区，例如 +08:00。")
            parsed_clock = parsed_clock.astimezone(CHINA_TZ)
        adapter = CsvReplayAdapter(
            Path(paths[0]) if paths[0] else None,
            Path(paths[1]) if paths[1] else None,
            Path(paths[2]) if paths[2] else None,
        )
        start_date, end_date = date(2000, 1, 1), date(2100, 12, 31)
        start_time = datetime(2000, 1, 1, tzinfo=CHINA_TZ)
        end_time = datetime(2100, 12, 31, 23, 59, tzinfo=CHINA_TZ)
        summaries = []
        if adapter.minute_file:
            summaries.append(ingest_minutes(
                self.database,
                adapter.fetch_minute_bars(stock_codes, start_time, end_time),
                adapter=adapter.name, replay_clock=parsed_clock,
            ))
        if adapter.daily_file:
            summaries.append(ingest_daily(
                self.database,
                adapter.fetch_daily_qfq(stock_codes, start_date, end_date),
                adapter=adapter.name,
            ))
        if adapter.calendar_file:
            summaries.append(ingest_calendar(
                self.database,
                adapter.fetch_trading_calendar(start_date, end_date),
                adapter=adapter.name,
            ))
        return CsvReplayResult(tuple(summaries))


class CsvReplayCard:
    def __init__(self, window, parent) -> None:
        self.window = window
        self.parent = parent
        self.service = DesktopReplayService(window.data.database)

    def build(self) -> None:
        card = self.window._card(self.parent)
        card.pack(fill="x", pady=(12, 0))
        ttk.Label(card, text="CSV 历史回放", style="Section.TLabel Surface.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10),
        )
        self.vars = {}
        for row, (name, label) in enumerate((
            ("minute", "分钟 CSV"), ("daily", "前复权日线 CSV"), ("calendar", "交易日历 CSV"),
        ), start=1):
            ttk.Label(card, text=label, style="Surface.TLabel").grid(row=row, column=0, sticky="w", pady=4)
            variable = tk.StringVar()
            self.vars[name] = variable
            ttk.Entry(card, textvariable=variable, width=72).grid(row=row, column=1, sticky="ew", padx=10, pady=4)
            ttk.Button(card, text="选择文件", command=lambda key=name: self.choose(key)).grid(row=row, column=2, sticky="w")
        ttk.Label(card, text="回放时刻（可选 ISO，须带时区）", style="Surface.TLabel").grid(row=4, column=0, sticky="w", pady=4)
        self.clock_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.clock_var, width=32).grid(row=4, column=1, sticky="w", padx=10, pady=4)
        self.replay_button = ttk.Button(card, text="确认并开始 CSV 回放", command=self.run)
        self.replay_button.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.status_var = tk.StringVar(value=self.window.tr("文件只读；去重写入当前数据目录，冲突记录不覆盖，不访问网络或模型。"))
        ttk.Label(card, textvariable=self.status_var, style="SurfaceMuted.TLabel", wraplength=760).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(8, 0),
        )

    def choose(self, name: str) -> None:
        selected = filedialog.askopenfilename(
            parent=self.window.root, title=self.window.tr("选择 CSV 文件"),
            filetypes=[("CSV", "*.csv")],
        )
        if selected:
            self.vars[name].set(selected)

    def run(self) -> None:
        values = {name: variable.get().strip() for name, variable in self.vars.items()}
        if not any(values.values()):
            messagebox.showwarning(self.window.tr("无法回放"), self.window.tr("请至少选择一个 CSV 文件。"))
            return
        confirmed = messagebox.askyesno(
            self.window.tr("确认 CSV 回放"),
            self.window.tr("将按现有清洗和冲突规则去重写入当前数据目录，不覆盖冲突记录，不访问网络或模型。是否继续？"),
        )
        if not confirmed:
            return
        self.replay_button.state(["disabled"])
        codes = tuple(item.code for item in self.window.settings.stocks if item.enabled)
        clock = self.clock_var.get()
        self.window._run_async(
            "csv-replay",
            lambda: self.service.run(
                values["minute"], values["daily"], values["calendar"], codes, clock,
            ),
            self._done,
        )

    def _done(self, result: CsvReplayResult) -> None:
        self.status_var.set(result.render(self.window._ui_language))
