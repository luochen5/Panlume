from __future__ import annotations

import gc
import time
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from pandeng.desktop.paths import DesktopPaths
from pandeng.desktop.replay import DesktopReplayService
from pandeng.desktop.settings import StockChoice
from pandeng.desktop.window import PanlumeWindow
from pandeng.storage.db import Database


EXAMPLES = Path("examples").resolve()


def test_csv_replay_service_is_offline_and_idempotent(tmp_path) -> None:
    database = Database(tmp_path / "replay.db")
    database.initialize()
    service = DesktopReplayService(database)
    arguments = (
        str(EXAMPLES / "minute_bars.csv"), str(EXAMPLES / "daily_qfq.csv"),
        str(EXAMPLES / "trading_calendar.csv"), ("600000", "000001"),
        "2026-01-05T09:33:00+08:00",
    )

    first = service.run(*arguments)
    second = service.run(*arguments)

    assert [item.accepted for item in first.summaries] == [5, 6, 5]
    assert [item.duplicates for item in second.summaries] == [5, 6, 5]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM daily_qfq_bars").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0] == 5


def test_observation_csv_replay_card_runs_without_terminal(tmp_path) -> None:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前没有可用显示环境：{exc}")
    root.withdraw()
    app = PanlumeWindow(root, DesktopPaths(tmp_path / "personal"))
    try:
        app.settings = replace(
            app.settings,
            stocks=(StockChoice("600000"), StockChoice("000001")),
        )
        card = app.csv_replay_card
        card.vars["minute"].set(str(EXAMPLES / "minute_bars.csv"))
        card.vars["daily"].set(str(EXAMPLES / "daily_qfq.csv"))
        card.vars["calendar"].set(str(EXAMPLES / "trading_calendar.csv"))
        card.clock_var.set("2026-01-05T09:33:00+08:00")

        with patch("pandeng.desktop.replay.messagebox.askyesno", return_value=True):
            card.run()
            for _ in range(500):
                root.update()
                time.sleep(0.01)
                if "csv-replay" not in app.active_tasks:
                    break
        assert "csv-replay" not in app.active_tasks
        assert "accepted=5" in card.status_var.get()
        assert "duplicates=0" in card.status_var.get()
    finally:
        root.destroy()
        gc.collect()
