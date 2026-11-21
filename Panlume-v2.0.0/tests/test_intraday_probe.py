from __future__ import annotations

import copy
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pandeng.app.cli import main
from pandeng.config.loader import load_config
from pandeng.config.schema import ConfigError
from pandeng.data.akshare_demo import LIVE_CONFIRMATION, run_akshare_live_minute
from pandeng.data.intraday_probe import (
    PROBE_CONFIRMATION,
    PROBE_FIXTURE_SOURCE,
    compare_probe_responses,
    run_akshare_intraday_probe,
)
from pandeng.monitor.clock import ManualClock
from pandeng.storage.db import Database


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/provider_fixtures/eastmoney/minute_000001_20261117_post_close.json"
DAY = date(2026, 11, 17)


def fixture_rows() -> list[dict]:
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"])


def save_live_config(path: Path, raw: dict, name: str = "akshare-live-test.db") -> None:
    raw["storage"]["database"] = f"runtime/{name}"
    raw["models"]["ollama"]["enabled"] = False
    raw["models"]["cloud"]["enabled"] = False
    raw["watchlist"] = [{"code": "000001", "name": "C3 双探针"}]
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def initialize(tmp_path: Path, config_file: Path, raw_config: dict) -> None:
    save_live_config(config_file, raw_config)
    Database(tmp_path / "runtime/akshare-live-test.db").initialize()


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def selected_rows(parameters: dict) -> list[dict]:
    return [
        copy.deepcopy(row) for row in fixture_rows()
        if parameters["start_date"] <= row["时间"] <= parameters["end_date"]
    ]


class IncrementalFetcher:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.change_second_latest = False
        self.drop_second_time: str | None = None
        self.second_only_new = False

    def __call__(self, **parameters: dict) -> list[dict]:
        self.calls.append(dict(parameters))
        rows = selected_rows(parameters)
        if len(self.calls) == 2:
            if self.second_only_new:
                first_end = self.calls[0]["end_date"]
                rows = [row for row in rows if row["时间"] > first_end]
            if self.drop_second_time is not None:
                rows = [row for row in rows if row["时间"] != self.drop_second_time]
            if self.change_second_latest:
                first_end = self.calls[0]["end_date"]
                row = next(item for item in rows if item["时间"] == first_end)
                row["收盘"] = float(row["收盘"]) + 0.01
        return rows


def run_probe(config_file: Path, fetcher: IncrementalFetcher, clock: ManualClock):
    return run_akshare_intraday_probe(
        load_config(config_file),
        DAY,
        "000001",
        confirmation=PROBE_CONFIRMATION,
        fetcher=fetcher,
        clock=clock,
    )


def test_two_probes_compare_unchanged_overlap_and_write_only_independent_db(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    clock = ManualClock(at("2026-11-17T09:40:20+08:00"))
    fetcher = IncrementalFetcher()

    outcome = run_probe(config_file, fetcher, clock)

    assert outcome.request_count == 2
    assert outcome.actual_interval_seconds == 300
    assert outcome.comparison.status == "unchanged"
    assert outcome.comparison.overlap_count == 10
    assert outcome.comparison.unchanged_count == 10
    assert outcome.comparison.changed_times == ()
    assert outcome.comparison.missing_in_second == ()
    assert len(outcome.comparison.new_in_second) == 5
    assert outcome.first.ingestion.accepted == 10
    assert outcome.second.ingestion.accepted == 5
    assert outcome.second.ingestion.duplicates == 10
    assert outcome.first.audit_path != outcome.second.audit_path
    assert outcome.audit_path.is_file()
    assert clock.now() == at("2026-11-17T09:45:20+08:00")

    assert [call["start_date"] for call in fetcher.calls] == [
        "2026-11-17 09:31:00", "2026-11-17 09:31:00"
    ]
    assert [call["end_date"] for call in fetcher.calls] == [
        "2026-11-17 09:40:00", "2026-11-17 09:45:00"
    ]
    assert all(call["period"] == "1" and call["adjust"] == "" for call in fetcher.calls)

    summary = json.loads(outcome.audit_path.read_text(encoding="utf-8"))
    assert summary["format"] == "pandeng.eastmoney-intraday-probe.v1"
    assert summary["status"] == "completed"
    assert summary["planned_request_count"] == 2
    assert summary["request_count_actual"] == 2
    assert summary["retry_count"] == 0
    assert summary["confirmed_through"] is None
    assert summary["calendar_status"] == "not_checked_probe_only"
    assert summary["model_called"] is False and summary["position_written"] is False
    assert summary["comparison"]["status"] == "unchanged"
    assert len(summary["completed_request_audits"]) == 2

    database = Database(tmp_path / "runtime/akshare-live-test.db")
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 15
        assert connection.execute("SELECT COUNT(*) FROM minute_bars WHERE confirmed=1").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM data_quality_issues WHERE severity='error'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM daily_qfq_bars").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM position_lots").fetchone()[0] == 0
    assert not (tmp_path / "runtime/pandeng.db").exists()
    output = capsys.readouterr().out
    assert "预计请求 2 次，间隔 300 秒，自动重试 0 次" in output
    assert "该结果只是证据，不会自动生成确认边界" in output


def test_cli_offline_probe_demo_isolated_audited_and_refuses_rerun(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    save_live_config(
        config_file, raw_config, "akshare-probe-fixture/akshare-live-test.db"
    )
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    arguments = [
        *base, "eastmoney-probe-fixture-demo", "--fixture-file", str(FIXTURE)
    ]
    assert main(arguments) == 0
    output = capsys.readouterr().out
    assert "真实网络请求 0 次" in output
    assert "不实际等待" in output
    assert "重叠一致 10；新增 5" in output
    assert "重叠比较状态：unchanged" in output
    assert "不会自动生成确认边界" in output

    database = Database(
        tmp_path / "runtime/akshare-probe-fixture/akshare-live-test.db"
    )
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 15
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE confirmed=1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM data_quality_issues WHERE severity='error'"
        ).fetchone()[0] == 0
        assert {
            row[0] for row in connection.execute("SELECT DISTINCT source FROM minute_bars")
        } == {PROBE_FIXTURE_SOURCE}
    audit_root = tmp_path / "runtime/akshare-probe-fixture/akshare-live-audit"
    summary_path = next(audit_root.glob("*-summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["request_mode"] == "offline_fixture"
    assert summary["source"] == PROBE_FIXTURE_SOURCE
    assert summary["request_count_actual"] == 2
    assert summary["retry_count"] == 0
    assert len(summary["completed_request_audits"]) == 2
    assert not (tmp_path / "runtime/akshare-live-test.db").exists()
    assert not (tmp_path / "runtime/pandeng.db").exists()

    assert main(arguments) == 1
    assert "只使用全新的 akshare-probe-fixture/akshare-live-test.db" in capsys.readouterr().out
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 15
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 2


def test_existing_previous_day_live_database_accepts_next_day_probe(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    previous_day = run_akshare_live_minute(
        load_config(config_file),
        DAY,
        "000001",
        confirmation=LIVE_CONFIRMATION,
        confirmed_through=None,
        fetcher=lambda **parameters: selected_rows(parameters),
        request_end=at("2026-11-17T15:00:00+08:00"),
    )
    assert previous_day.ingestion.accepted == 240

    class NextDayFetcher(IncrementalFetcher):
        def __call__(self, **parameters: dict) -> list[dict]:
            self.calls.append(dict(parameters))
            previous_day_parameters = {
                key: value.replace("2026-11-18", "2026-11-17")
                if key in {"start_date", "end_date"} else value
                for key, value in parameters.items()
            }
            rows = selected_rows(previous_day_parameters)
            for row in rows:
                row["时间"] = row["时间"].replace("2026-11-17", "2026-11-18")
            return rows

    next_day = date(2026, 11, 18)
    fetcher = NextDayFetcher()
    outcome = run_akshare_intraday_probe(
        load_config(config_file),
        next_day,
        "000001",
        confirmation=PROBE_CONFIRMATION,
        fetcher=fetcher,
        clock=ManualClock(at("2026-11-18T09:40:00+08:00")),
    )

    assert outcome.first.ingestion.accepted == 10
    assert outcome.second.ingestion.accepted == 5
    assert outcome.second.ingestion.duplicates == 10
    assert outcome.comparison.status == "unchanged"
    report = outcome.second.audit
    assert report.codes[0].expected_minutes == 15
    assert report.codes[0].saved_minutes == 15
    assert report.codes[0].missing_times == ()
    assert report.codes[0].unconfirmed_minutes == 15
    assert report.codes[0].quality_error_count == 0
    assert report.codes[0].latest_minute_time == "2026-11-18T09:45:00+08:00"
    assert report.status == "unknown"

    database = Database(tmp_path / "runtime/akshare-live-test.db")
    with database.connect() as connection:
        counts = {
            day: connection.execute(
                "SELECT COUNT(*) FROM minute_bars WHERE bar_time LIKE ?",
                (f"{day}%",),
            ).fetchone()[0]
            for day in ("2026-11-17", "2026-11-18")
        }
        assert counts == {"2026-11-17": 240, "2026-11-18": 15}
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM data_quality_issues WHERE severity='error'"
        ).fetchone()[0] == 0
    assert not (tmp_path / "runtime/pandeng.db").exists()


def test_changed_overlap_is_audited_and_never_overwrites_first_bar(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    fetcher = IncrementalFetcher()
    fetcher.change_second_latest = True

    outcome = run_probe(
        config_file, fetcher, ManualClock(at("2026-11-17T09:40:00+08:00"))
    )

    changed_time = "2026-11-17T09:40:00+08:00"
    assert outcome.comparison.status == "changed"
    assert outcome.comparison.changed_times == (changed_time,)
    assert outcome.second.ingestion.status == "unknown"
    assert outcome.second.ingestion.rejected == 1
    assert outcome.second.ingestion.accepted == 5
    summary = json.loads(outcome.audit_path.read_text(encoding="utf-8"))
    assert summary["status"] == "completed_with_quality_errors"
    assert summary["ingestion_status"] == "unknown"
    database = Database(tmp_path / "runtime/akshare-live-test.db")
    first_raw = json.loads(outcome.first.audit_path.read_text(encoding="utf-8"))["raw_rows"]
    original_close = next(row["收盘"] for row in first_raw if row["时间"] == "2026-11-17 09:40:00")
    with database.connect() as connection:
        row = connection.execute(
            "SELECT close, confirmed FROM minute_bars WHERE stock_code='000001' AND bar_time=?",
            (changed_time,),
        ).fetchone()
        assert row["close"] == original_close and row["confirmed"] == 0
        issues = list(connection.execute(
            "SELECT issue_type, severity FROM data_quality_issues WHERE severity='error'"
        ))
    assert [(row["issue_type"], row["severity"]) for row in issues] == [
        ("conflicting_duplicate", "error")
    ]


def test_missing_and_no_overlap_responses_are_distinct_comparison_results(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    fetcher = IncrementalFetcher()
    fetcher.drop_second_time = "2026-11-17 09:35:00"
    missing = run_probe(
        config_file, fetcher, ManualClock(at("2026-11-17T09:40:00+08:00"))
    )
    assert missing.comparison.status == "changed"
    assert missing.comparison.missing_in_second == ("2026-11-17T09:35:00+08:00",)

    other = tmp_path / "other"
    other.mkdir()
    config_path = other / "live.yaml"
    save_live_config(config_path, copy.deepcopy(raw_config))
    monkeypatch.chdir(other)
    Database(other / "runtime/akshare-live-test.db").initialize()
    disjoint = IncrementalFetcher()
    disjoint.second_only_new = True
    no_overlap = run_probe(
        config_path, disjoint, ManualClock(at("2026-11-17T09:40:00+08:00"))
    )
    assert no_overlap.comparison.status == "no_overlap"
    assert no_overlap.comparison.overlap_count == 0
    assert len(no_overlap.comparison.new_in_second) == 5
    assert len(no_overlap.comparison.missing_in_second) == 10


def test_wrong_confirmation_has_zero_requests_audits_and_writes(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    called = False

    def fetcher(**kwargs):
        nonlocal called
        called = True
        return []

    with pytest.raises(ConfigError, match="没有发起请求、创建审计或写数据库"):
        run_akshare_intraday_probe(
            load_config(config_file), DAY, "000001", confirmation="错误确认",
            fetcher=fetcher, clock=ManualClock(at("2026-11-17T09:40:00+08:00")),
        )
    assert called is False
    assert not (tmp_path / "runtime/akshare-live-audit").exists()
    with Database(tmp_path / "runtime/akshare-live-test.db").connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0


@pytest.mark.parametrize("now", [
    "2026-11-17T11:26:00+08:00",
    "2026-11-17T12:00:00+08:00",
    "2026-11-17T14:56:00+08:00",
    "2026-11-17T16:00:00+08:00",
])
def test_invalid_two_request_window_stops_before_first_request(
    now: str, config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    fetcher = IncrementalFetcher()
    with pytest.raises(ConfigError):
        run_probe(config_file, fetcher, ManualClock(at(now)))
    assert fetcher.calls == []
    assert not (tmp_path / "runtime/akshare-live-audit").exists()


def test_wrong_session_date_stops_before_first_request(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    fetcher = IncrementalFetcher()
    with pytest.raises(ConfigError, match="当天日期一致"):
        run_akshare_intraday_probe(
            load_config(config_file), date(2026, 11, 18), "000001",
            confirmation=PROBE_CONFIRMATION, fetcher=fetcher,
            clock=ManualClock(at("2026-11-17T09:40:00+08:00")),
        )
    assert fetcher.calls == []
    assert not (tmp_path / "runtime/akshare-live-audit").exists()


def test_first_failure_stops_without_sleep_or_second_request(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    calls = 0

    def failed(**kwargs):
        nonlocal calls
        calls += 1
        raise ConnectionError("injected first failure")

    clock = ManualClock(at("2026-11-17T09:40:00+08:00"))
    with pytest.raises(ConfigError, match="network_error"):
        run_akshare_intraday_probe(
            load_config(config_file), DAY, "000001", confirmation=PROBE_CONFIRMATION,
            fetcher=failed, clock=clock,
        )
    assert calls == 1
    assert clock.now() == at("2026-11-17T09:40:00+08:00")
    summaries = list((tmp_path / "runtime/akshare-live-audit").glob("*-summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["request_count_actual"] == 1
    assert summary["completed_request_audits"] == []
    with Database(tmp_path / "runtime/akshare-live-test.db").connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0


def test_second_failure_preserves_first_and_has_no_third_request(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    calls = 0

    def failed_second(**parameters):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("injected second timeout")
        return selected_rows(parameters)

    with pytest.raises(ConfigError, match="timeout"):
        run_akshare_intraday_probe(
            load_config(config_file), DAY, "000001", confirmation=PROBE_CONFIRMATION,
            fetcher=failed_second, clock=ManualClock(at("2026-11-17T09:40:00+08:00")),
        )
    assert calls == 2
    summary_path = next((tmp_path / "runtime/akshare-live-audit").glob("*-summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "failed"
    assert summary["request_count_actual"] == 2
    assert len(summary["completed_request_audits"]) == 1
    with Database(tmp_path / "runtime/akshare-live-test.db").connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 10
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1


class InterruptClock(ManualClock):
    def sleep(self, seconds: float) -> None:
        raise KeyboardInterrupt


def test_ctrl_c_during_wait_keeps_first_and_skips_second(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    fetcher = IncrementalFetcher()
    with pytest.raises(KeyboardInterrupt):
        run_akshare_intraday_probe(
            load_config(config_file), DAY, "000001", confirmation=PROBE_CONFIRMATION,
            fetcher=fetcher, clock=InterruptClock(at("2026-11-17T09:40:00+08:00")),
        )
    assert len(fetcher.calls) == 1
    summary_path = next((tmp_path / "runtime/akshare-live-audit").glob("*-summary.json"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["status"] == "interrupted"
    assert summary["request_count_actual"] == 1
    assert len(summary["completed_request_audits"]) == 1
    assert not Path(summary["request_audits"][1]).exists()
    with Database(tmp_path / "runtime/akshare-live-test.db").connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 10
    assert "不会自动补发第二次" in capsys.readouterr().out


def test_probe_rejects_main_database_before_request(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    save_live_config(config_file, raw_config, "pandeng.db")
    monkeypatch.chdir(tmp_path)
    Database(tmp_path / "runtime/pandeng.db").initialize()
    fetcher = IncrementalFetcher()
    with pytest.raises(ConfigError, match="只允许写入 akshare-live-test.db"):
        run_probe(config_file, fetcher, ManualClock(at("2026-11-17T09:40:00+08:00")))
    assert fetcher.calls == []
    assert not (tmp_path / "runtime/akshare-live-audit").exists()


def test_single_request_internal_end_validation_has_no_side_effects(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    initialize(tmp_path, config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    fetcher = IncrementalFetcher()
    with pytest.raises(ConfigError, match="交易整分钟"):
        run_akshare_live_minute(
            load_config(config_file), DAY, "000001", confirmation=LIVE_CONFIRMATION,
            confirmed_through=None, fetcher=fetcher,
            request_end=at("2026-11-17T12:00:00+08:00"),
        )
    assert fetcher.calls == []
    assert not (tmp_path / "runtime/akshare-live-audit").exists()


def test_cli_dispatches_probe_with_exact_confirmation(monkeypatch, config_file: Path, raw_config: dict, capsys) -> None:
    save_live_config(config_file, raw_config)
    captured = {}

    def fake(config, session_date, code, *, confirmation):
        captured.update(session_date=session_date, code=code, confirmation=confirmation)
        return SimpleNamespace(
            request_count=2,
            comparison=SimpleNamespace(overlap_count=10, changed_times=()),
            audit_path=Path("probe-summary.json"),
            first=SimpleNamespace(ingestion=SimpleNamespace(status="ok")),
            second=SimpleNamespace(ingestion=SimpleNamespace(status="ok")),
        )

    monkeypatch.setattr("pandeng.app.cli.run_akshare_intraday_probe", fake)
    result = main([
        "--config", str(config_file), "akshare-live-probe",
        "--session-date", "2026-11-18", "--code", "000001",
        "--confirmation", PROBE_CONFIRMATION,
    ])
    assert result == 0
    assert captured == {
        "session_date": date(2026, 11, 18), "code": "000001",
        "confirmation": PROBE_CONFIRMATION,
    }
    assert "C3 双探针汇总：实际请求 2 次" in capsys.readouterr().out
