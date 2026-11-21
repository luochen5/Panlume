from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pandeng.data.eastmoney_adapter import (
    FIXTURE_SOURCE,
    AkshareEastmoneyMinuteAdapter,
    EastmoneyMinuteFixtureAdapter,
    ProviderEmptyResponse,
    ProviderFormatError,
    ProviderLiveError,
    classify_live_exception,
    dataframe_to_rows,
    map_eastmoney_minute_rows,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "examples/provider_fixtures/eastmoney/minute_000001_20261117_post_close.json"
START = datetime.fromisoformat("2026-11-17T09:31:00+08:00")
END = datetime.fromisoformat("2026-11-17T15:00:00+08:00")


def payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def rows() -> list[dict]:
    return copy.deepcopy(payload()["rows"])


def map_rows(items: list[dict], through=None):
    return map_eastmoney_minute_rows(
        items,
        stock_code="000001",
        start=START,
        end=END,
        confirmed_through=through,
    )


def test_complete_capture_has_exact_pandeng_minute_grid() -> None:
    result = map_rows(rows())
    assert result.raw_row_count == 241
    assert len(result.records) == 240
    assert len(result.excluded_0930) == 1
    actual = [datetime.fromisoformat(str(row["bar_time"])) for row in result.records]
    morning = [START + timedelta(minutes=index) for index in range(120)]
    afternoon_start = datetime.fromisoformat("2026-11-17T13:01:00+08:00")
    afternoon = [afternoon_start + timedelta(minutes=index) for index in range(120)]
    assert actual == morning + afternoon
    assert actual[-1].strftime("%H:%M") == "15:00"
    assert not any(item.strftime("%H:%M") == "13:00" for item in actual)
    excluded = result.excluded_0930[0]
    assert excluded["reason"] == "09:30_special_point"
    assert excluded["provider_raw"]["成交量"] == 5643


def test_volume_lots_zero_row_and_raw_trace_are_preserved() -> None:
    result = map_rows(rows())
    first = result.records[0]
    assert first["volume"] == 48888 * 100
    assert first["adjustment"] == "none"
    assert first["provider_request"]["period"] == "1"
    assert first["provider_request"]["adjust"] == ""
    assert first["provider_raw"]["时间"] == "2026-11-17 09:31:00"
    zero = [row for row in result.records if row["volume"] == 0]
    assert len(zero) == 1
    assert zero[0]["bar_time"] == "2026-11-17T14:59:00+08:00"
    assert zero[0]["close"] == 12.27
    assert result.records[-1]["volume"] == 994100


def test_confirmation_is_explicit_and_never_inferred_from_last_row() -> None:
    unconfirmed = map_rows(rows(), through=None)
    assert sum(bool(row["confirmed"]) for row in unconfirmed.records) == 0
    through_1459 = map_rows(rows(), through="2026-11-17T14:59:00+08:00")
    assert sum(bool(row["confirmed"]) for row in through_1459.records) == 239
    assert through_1459.records[-1]["confirmed"] is False
    through_close = map_rows(rows(), through=END)
    assert sum(bool(row["confirmed"]) for row in through_close.records) == 240


def test_empty_and_only_0930_are_distinct_empty_data() -> None:
    with pytest.raises(ProviderEmptyResponse) as empty:
        map_rows([])
    assert empty.value.category == "empty_data"
    with pytest.raises(ProviderEmptyResponse):
        map_rows(rows()[:1])


def test_duplicate_and_out_of_order_times_are_rejected() -> None:
    duplicate = rows()
    duplicate[2] = copy.deepcopy(duplicate[1])
    with pytest.raises(ProviderFormatError, match="重复时间"):
        map_rows(duplicate)
    disordered = rows()
    disordered[1], disordered[2] = disordered[2], disordered[1]
    with pytest.raises(ProviderFormatError, match="严格递增"):
        map_rows(disordered)


def test_raw_scope_and_session_time_are_checked_before_exclusion_or_storage() -> None:
    outside = rows()
    outside[-1]["时间"] = "2026-11-18 09:31:00"
    with pytest.raises(ProviderFormatError, match="超出请求闭区间"):
        map_rows(outside)
    wrong_0930_date = rows()
    wrong_0930_date[0]["时间"] = "2026-11-16 09:30:00"
    with pytest.raises(ProviderFormatError, match="09:30 特殊点超出"):
        map_rows(wrong_0930_date)
    wrong_session = rows()
    wrong_session[-1]["时间"] = "2026-11-17 13:00:00"
    with pytest.raises(ProviderFormatError, match="严格递增|不在盘灯"):
        map_rows(wrong_session)


def test_1300_is_rejected_when_it_is_the_only_raw_row() -> None:
    row = copy.deepcopy(rows()[1])
    row["时间"] = "2026-11-17 13:00:00"
    boundary = datetime.fromisoformat("2026-11-17T13:00:00+08:00")
    with pytest.raises(ProviderFormatError, match="不在盘灯"):
        map_eastmoney_minute_rows(
            [row], stock_code="000001", start=boundary, end=boundary,
            confirmed_through=None,
        )


def test_missing_column_and_fractional_share_conversion_are_rejected() -> None:
    missing = rows()
    del missing[4]["成交额"]
    with pytest.raises(ProviderFormatError, match="缺少字段"):
        map_rows(missing)
    fractional = rows()
    fractional[1]["成交量"] = 0.001
    with pytest.raises(ProviderFormatError, match="不是整数"):
        map_rows(fractional)


def test_confirmation_after_request_end_is_rejected() -> None:
    with pytest.raises(ProviderFormatError, match="不能晚于"):
        map_rows(rows(), through="2026-11-17T15:01:00+08:00")


def test_fixture_adapter_is_explicitly_offline_and_filters_closed_interval() -> None:
    adapter = EastmoneyMinuteFixtureAdapter(FIXTURE, confirmed_through=None)
    assert adapter.name == FIXTURE_SOURCE
    selected = list(adapter.fetch_minute_bars(("000001",), START, START))
    assert len(selected) == 1
    assert selected[0]["source"] == FIXTURE_SOURCE
    assert selected[0]["confirmed"] is False
    with pytest.raises(ProviderFormatError, match="完全匹配"):
        adapter.fetch_minute_bars(("600000",), START, END)


def test_live_adapter_uses_exact_parameters_with_injected_offline_fetcher() -> None:
    calls = []

    def fetcher(**parameters):
        calls.append(parameters)
        return rows()

    adapter = AkshareEastmoneyMinuteAdapter(confirmed_through=None, fetcher=fetcher)
    records = list(adapter.fetch_minute_bars(("000001",), START, END))
    assert len(records) == 240
    assert adapter.request_count == 1
    assert calls == [{
        "symbol": "000001",
        "start_date": "2026-11-17 09:31:00",
        "end_date": "2026-11-17 15:00:00",
        "period": "1",
        "adjust": "",
    }]
    assert all(record["confirmed"] is False for record in records)
    assert len(adapter.last_mappings[0].excluded_0930) == 1


def test_live_adapter_parameter_errors_happen_before_fetch() -> None:
    calls = []
    adapter = AkshareEastmoneyMinuteAdapter(
        confirmed_through="2026-11-17T15:01:00+08:00",
        fetcher=lambda **kwargs: calls.append(kwargs),
    )
    with pytest.raises(ProviderLiveError) as caught:
        adapter.fetch_minute_bars(("000001",), START, END)
    assert caught.value.category == "parameter_error"
    assert calls == []
    assert adapter.request_count == 0


class FakeFrame:
    def __init__(self, columns, records, empty=False):
        self.columns = columns
        self._records = records
        self.empty = empty

    def to_dict(self, *, orient):
        assert orient == "records"
        return copy.deepcopy(self._records)


def test_dataframe_contract_checks_columns_and_empty_response() -> None:
    frame = FakeFrame(["时间", "开盘"], [], empty=True)
    assert dataframe_to_rows(frame) == []
    with pytest.raises(ProviderFormatError, match="缺少字段"):
        dataframe_to_rows(FakeFrame(["时间", "开盘"], [{"时间": "x", "开盘": 1}]))
    with pytest.raises(ProviderFormatError, match="重复列名"):
        dataframe_to_rows(FakeFrame(["时间", "时间"], [{"时间": "x"}]))


class HTTPFailure(Exception):
    def __init__(self, status):
        super().__init__(f"status={status}")
        self.response = SimpleNamespace(status_code=status)


@pytest.mark.parametrize("status,category", [
    (400, "parameter_error"),
    (401, "authentication_failed"),
    (403, "permission_denied"),
    (408, "timeout"),
    (429, "rate_limited"),
    (500, "http_error"),
])
def test_http_failure_categories(status, category) -> None:
    assert classify_live_exception(HTTPFailure(status))[0] == category


@pytest.mark.parametrize("exception,category", [
    (TimeoutError("late"), "timeout"),
    (ConnectionError("offline"), "network_error"),
    (ValueError("unknown"), "request_failed"),
])
def test_transport_and_unknown_failure_categories(exception, category) -> None:
    assert classify_live_exception(exception)[0] == category


def test_live_transport_error_is_not_reported_as_empty_data() -> None:
    def fetcher(**kwargs):
        raise TimeoutError("simulated timeout")

    adapter = AkshareEastmoneyMinuteAdapter(confirmed_through=None, fetcher=fetcher)
    with pytest.raises(ProviderLiveError) as caught:
        adapter.fetch_minute_bars(("000001",), START, END)
    assert caught.value.category == "timeout"
    assert adapter.request_count == 1
