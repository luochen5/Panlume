from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pandeng.config.schema import ConfigError
from pandeng.data.akshare_demo import run_akshare_live_minute
from pandeng.data.schedule import CHINA_TZ
from pandeng.desktop.live_acceptance import (
    DESKTOP_LIVE_CONFIRMATION,
    DesktopLiveAcceptanceService,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/provider_fixtures/eastmoney/minute_000001_20261117_post_close.json"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=CHINA_TZ)


def service(tmp_path, executor=None):
    kwargs = {} if executor is None else {"executor": executor}
    return DesktopLiveAcceptanceService(
        tmp_path / "personal",
        tmp_path / "personal/data/pandeng.db",
        now_provider=lambda: NOW,
        **kwargs,
    )


def plan(live):
    return live.make_plan("000001", "2026-11-17", "09:40")


def fixture_fetcher(calls):
    def fetch(**parameters):
        calls.append(dict(parameters))
        rows = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]
        return [
            copy.deepcopy(row) for row in rows
            if parameters["start_date"] <= row["时间"] <= parameters["end_date"]
        ]
    return fetch


def offline_executor(calls):
    def execute(*args, **kwargs):
        return run_akshare_live_minute(
            *args,
            **kwargs,
            fetcher=fixture_fetcher(calls),
            source="desktop_live_acceptance_offline",
            offline=True,
        )
    return execute


def test_wrong_confirmation_has_zero_request_and_zero_files(tmp_path):
    calls = []
    live = service(tmp_path, offline_executor(calls))
    with pytest.raises(ConfigError, match="确认文本不匹配"):
        live.execute(plan(live), confirmation="不同意")
    assert calls == []
    assert not live.database.path.exists()
    assert not (tmp_path / "personal/runtime").exists()


def test_plan_has_fixed_scope_and_single_use(tmp_path):
    calls = []

    def fake_executor(config, session_date, code, **kwargs):
        calls.append((config, session_date, code, kwargs))
        return SimpleNamespace(
            request_count=1,
            mapping=SimpleNamespace(raw_row_count=11),
            ingestion=SimpleNamespace(received=10, accepted=10, duplicates=0, rejected=0),
            audit_path=tmp_path / "fake-audit.json",
        )

    live = service(tmp_path, fake_executor)
    request_plan = plan(live)
    assert request_plan.start.isoformat() == "2026-11-17T09:31:00+08:00"
    assert request_plan.end.isoformat() == "2026-11-17T09:40:00+08:00"
    assert request_plan.planned_requests == 1
    assert request_plan.retry_count == 0
    result = live.execute(request_plan, confirmation=DESKTOP_LIVE_CONFIRMATION)
    assert result.request_count == 1
    config, _, code, kwargs = calls[0]
    assert code == "000001"
    assert config.storage.database == live.database.path
    assert not config.ollama.enabled and not config.cloud.enabled
    assert kwargs["request_end"] == request_plan.end
    assert kwargs["confirmed_through"] is None
    with pytest.raises(ConfigError, match="已经执行过"):
        live.execute(request_plan, confirmation=DESKTOP_LIVE_CONFIRMATION)
    assert len(calls) == 1


@pytest.mark.parametrize("day,end", [
    ("2026-11-19", "09:40"),
    ("2026-11-17", "12:00"),
    ("2026-11-18", "12:01"),
])
def test_invalid_scope_is_rejected_without_side_effects(tmp_path, day, end):
    live = service(tmp_path, lambda *args, **kwargs: pytest.fail("不应执行请求"))
    with pytest.raises(ConfigError):
        live.make_plan("000001", day, end)
    assert not live.database.path.exists()


def test_offline_response_runs_same_guarded_path_and_never_touches_main_db(tmp_path):
    calls = []
    live = service(tmp_path, offline_executor(calls))
    result = live.execute(plan(live), confirmation=DESKTOP_LIVE_CONFIRMATION)
    assert len(calls) == 1
    assert calls[0]["start_date"] == "2026-11-17 09:31:00"
    assert calls[0]["end_date"] == "2026-11-17 09:40:00"
    assert calls[0]["period"] == "1" and calls[0]["adjust"] == ""
    assert result.audit_path.is_file()
    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["request_count_actual"] == 1
    assert audit["retry_count"] == 0
    assert audit["model_called"] is False
    assert audit["position_written"] is False
    with live.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 10
        assert connection.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE confirmed=1"
        ).fetchone()[0] == 0
    assert not live.personal_database.exists()


def test_timeout_is_classified_once_without_retry(tmp_path):
    calls = []

    def failing_executor(*args, **kwargs):
        def fail(**parameters):
            calls.append(dict(parameters))
            raise TimeoutError("测试超时")
        return run_akshare_live_minute(
            *args,
            **kwargs,
            fetcher=fail,
            source="desktop_live_acceptance_offline",
            offline=True,
        )

    live = service(tmp_path, failing_executor)
    with pytest.raises(ConfigError, match="timeout"):
        live.execute(plan(live), confirmation=DESKTOP_LIVE_CONFIRMATION)
    assert len(calls) == 1
    audits = list(live.database.path.parent.glob("akshare-live-audit/*.json"))
    assert len(audits) == 1
    payload = json.loads(audits[0].read_text(encoding="utf-8"))
    assert payload["status"] == "request_failed"
    assert payload["error_category"] == "timeout"
    assert payload["request_count_actual"] == 1
    assert payload["retry_count"] == 0
    assert payload["database_written"] is False
    assert not live.personal_database.exists()


def test_plan_cannot_be_redirected_to_main_database(tmp_path):
    live = service(tmp_path, lambda *args, **kwargs: pytest.fail("不应执行请求"))
    redirected = replace(plan(live), database=live.personal_database)
    with pytest.raises(ConfigError):
        live.execute(redirected, confirmation=DESKTOP_LIVE_CONFIRMATION)
    assert not live.personal_database.exists()
