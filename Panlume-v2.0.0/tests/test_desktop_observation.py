from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

from pandeng.data.base import MinuteBar
from pandeng.data.schedule import CHINA_TZ
from pandeng.desktop.observation import ObservationRunner
from pandeng.desktop.settings import DesktopSettings, StockChoice
from pandeng.storage.db import Database


START = datetime(2026, 11, 17, 10, 0, tzinfo=CHINA_TZ)
END = datetime(2026, 11, 17, 10, 1, tzinfo=CHINA_TZ)


class FakeAdapter:
    name = "desktop_offline_test"

    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime, datetime]] = []
        self.revision = 0.0

    def fetch_minute_bars(self, stock_codes, start, end):
        code = tuple(stock_codes)[0]
        self.calls.append((code, start, end))
        return [MinuteBar(
            code, end, 10.0, 11.0 + self.revision, 9.0,
            10.5 + self.revision, 1000, self.name, confirmed=False,
        )]


class ImmediateEvent:
    def __init__(self, on_wait=None) -> None:
        self.stopped = False
        self.on_wait = on_wait

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        if self.on_wait:
            self.on_wait(seconds)
        return self.stopped


def make_settings(codes=("000001",), interval=60):
    return DesktopSettings(
        interval_seconds=interval,
        stocks=tuple(StockChoice(code) for code in codes),
        ollama_enabled=False,
    )


def make_runner(tmp_path, adapter, settings):
    database = Database(tmp_path / "observation.db")
    database.initialize()
    holder = {"settings": settings}
    runner = ObservationRunner(
        database, adapter, lambda: holder["settings"],
        request_start=START, request_end=END,
    )
    return database, holder, runner


def test_five_stocks_one_explicit_round(tmp_path):
    adapter = FakeAdapter()
    codes = ("000001", "000002", "000003", "000004", "000005")
    database, _, runner = make_runner(tmp_path, adapter, make_settings(codes))
    runner.run(max_rounds=1)
    assert [call[0] for call in adapter.calls] == list(codes)
    assert runner.snapshot().phase == "completed"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 5


def test_revision_keeps_before_after_and_updates_current_snapshot(tmp_path):
    adapter = FakeAdapter()
    database, _, runner = make_runner(tmp_path, adapter, make_settings())
    runner.stop_event = ImmediateEvent(lambda _: setattr(adapter, "revision", 0.5))
    runner.run(max_rounds=2)
    with database.connect() as connection:
        current = connection.execute("SELECT close, confirmed FROM minute_bars").fetchone()
        audit = connection.execute(
            "SELECT raw_json FROM data_quality_issues WHERE issue_type = 'unconfirmed_revision'"
        ).fetchone()
    assert current["close"] == 11.0
    assert current["confirmed"] == 0
    payload = json.loads(audit["raw_json"])
    assert payload["before"]["close"] == 10.5
    assert payload["after"]["close"] == 11.0


def test_new_settings_apply_only_to_next_round(tmp_path):
    adapter = FakeAdapter()
    database, holder, runner = make_runner(tmp_path, adapter, make_settings(("000001", "000002")))
    intervals = []

    def change_settings(seconds):
        intervals.append(seconds)
        holder["settings"] = replace(holder["settings"], interval_seconds=300)

    runner.stop_event = ImmediateEvent(change_settings)
    runner.run(max_rounds=2)
    assert intervals == [60]
    assert runner.snapshot().interval_seconds == 300


def test_integer_deadline_boundary_does_not_start_extra_round(tmp_path):
    adapter = FakeAdapter()
    _, _, runner = make_runner(tmp_path, adapter, make_settings())
    runner.run(max_rounds=5, deadline_monotonic=0.0)
    assert adapter.calls == []
    assert runner.snapshot().round_number == 0


def test_stop_before_run_makes_no_request(tmp_path):
    adapter = FakeAdapter()
    _, _, runner = make_runner(tmp_path, adapter, make_settings())
    runner.request_stop()
    runner.run(max_rounds=5)
    assert adapter.calls == []
    assert runner.snapshot().phase == "stopped"



def test_ten_stock_limit_and_disabled_items_do_not_request(tmp_path):
    adapter = FakeAdapter()
    codes = tuple(f"{index:06d}" for index in range(1, 11))
    settings = make_settings(codes)
    settings = replace(
        settings,
        stocks=tuple(
            StockChoice(item.code, enabled=index % 2 == 0)
            for index, item in enumerate(settings.stocks)
        ),
    )
    database, _, runner = make_runner(tmp_path, adapter, settings)

    runner.run(max_rounds=1)

    expected = [item.code for item in settings.stocks if item.enabled]
    assert len(settings.stocks) == 10
    assert [call[0] for call in adapter.calls] == expected
    assert runner.snapshot().stock_codes == tuple(expected)
