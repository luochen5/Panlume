import copy
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from pandeng.data.provider_common import ProviderFormatError
from pandeng.data.schedule import CHINA_TZ
from pandeng.data.tushare_adapter import (
    FIXTURE_FORMAT,
    FIXTURE_SOURCE,
    TushareFixtureAdapter,
    map_calendar_response,
    map_daily_qfq_response,
    map_minute_response,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_FILE = PROJECT_ROOT / "examples/provider_fixtures/tushare/offline_responses.json"


def fixture_payload() -> dict:
    return json.loads(FIXTURE_FILE.read_text(encoding="utf-8"))


def write_fixture(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_fixture_adapter_maps_all_three_interfaces_and_preserves_raw_rows() -> None:
    adapter = TushareFixtureAdapter(FIXTURE_FILE)
    start = datetime(2026, 1, 12, 9, 31, tzinfo=CHINA_TZ)
    end = datetime(2026, 1, 12, 9, 32, tzinfo=CHINA_TZ)
    minutes = list(adapter.fetch_minute_bars(("600000", "000001"), start, end))
    daily = list(adapter.fetch_daily_qfq(
        ("600000", "000001"), date(2026, 1, 12), date(2026, 1, 12)
    ))
    calendar = list(adapter.fetch_trading_calendar(date(2026, 1, 12), date(2026, 1, 12)))

    assert len(minutes) == 4
    assert len(daily) == 2
    assert len(calendar) == 1
    assert len(adapter.excluded_opening_auction) == 2
    assert all(record["adjustment"] == "none" for record in minutes)
    assert all(record["confirmed"] is True for record in minutes)
    assert all(record["source"] == FIXTURE_SOURCE for record in minutes)
    assert minutes[0]["provider_raw"]["ts_code"] == "600000.SH"
    assert minutes[0]["provider_request_id"] == "fixture-minute-virtual-001"
    assert {record["volume"] for record in daily} == {1234550, 2345600}
    assert all(record["adjustment"] == "qfq" for record in daily)
    assert all(record["confirmed"] is False for record in daily)
    assert calendar[0]["is_open"] is True


def test_opening_auction_is_explicitly_excluded_never_relabelled() -> None:
    mapped = map_minute_response(
        fixture_payload()["minute_response"],
        confirmed_through=datetime(2026, 1, 12, 9, 32, tzinfo=CHINA_TZ),
    )
    times = [str(record["bar_time"]) for record in mapped.records]
    assert len(mapped.excluded_opening_auction) == 2
    assert all(row["trade_time"].endswith("09:30:00") for row in mapped.excluded_opening_auction)
    assert not any(value.endswith("09:30:00+08:00") for value in times)
    assert sum(value.endswith("09:31:00+08:00") for value in times) == 2
    assert sum(value.endswith("09:32:00+08:00") for value in times) == 2


def test_minutes_after_confirmed_through_remain_unconfirmed() -> None:
    payload = fixture_payload()["minute_response"]
    payload["data"]["items"].append(
        ["600000.SH", "2026-01-12 09:33:00", 10.15, 10.2, 10.1, 10.18, 100, 1018.0]
    )
    mapped = map_minute_response(
        payload, confirmed_through=datetime(2026, 1, 12, 9, 32, tzinfo=CHINA_TZ)
    )
    future = next(record for record in mapped.records if "09:33:00" in str(record["bar_time"]))
    assert future["confirmed"] is False


def test_fixture_adapter_filters_codes_and_closed_ranges() -> None:
    adapter = TushareFixtureAdapter(FIXTURE_FILE)
    start = datetime(2026, 1, 12, 9, 31, tzinfo=CHINA_TZ)
    minutes = list(adapter.fetch_minute_bars(("600000",), start, start))
    assert len(minutes) == 1
    assert minutes[0]["stock_code"] == "600000"
    assert minutes[0]["bar_time"] == "2026-01-12T09:31:00+08:00"
    assert len(adapter.minute_requests) == 1
    assert adapter.minute_requests[0].start == start
    assert list(adapter.fetch_daily_qfq(
        ("000001",), date(2026, 1, 12), date(2026, 1, 12)
    ))[0]["stock_code"] == "000001"
    assert list(adapter.fetch_trading_calendar(date(2026, 1, 13), date(2026, 1, 13))) == []


def test_provider_error_code_is_reported_before_data_mapping() -> None:
    payload = fixture_payload()["minute_response"]
    payload["code"] = 2002
    payload["msg"] = "虚拟权限不足"
    with pytest.raises(ProviderFormatError, match="code=2002.*虚拟权限不足"):
        map_minute_response(
            payload, confirmed_through=datetime(2026, 1, 12, 9, 32, tzinfo=CHINA_TZ)
        )


def test_missing_required_provider_field_is_rejected() -> None:
    payload = fixture_payload()["minute_response"]
    fields = payload["data"]["fields"]
    index = fields.index("vol")
    fields.pop(index)
    for row in payload["data"]["items"]:
        row.pop(index)
    with pytest.raises(ProviderFormatError, match="缺少必要字段.*vol"):
        map_minute_response(
            payload, confirmed_through=datetime(2026, 1, 12, 9, 32, tzinfo=CHINA_TZ)
        )


def test_row_length_mismatch_is_rejected() -> None:
    payload = fixture_payload()["minute_response"]
    payload["data"]["items"][0].pop()
    with pytest.raises(ProviderFormatError, match="长度不一致"):
        map_minute_response(
            payload, confirmed_through=datetime(2026, 1, 12, 9, 32, tzinfo=CHINA_TZ)
        )


def test_bad_exchange_suffix_and_fractional_share_volume_are_rejected() -> None:
    payload = fixture_payload()["minute_response"]
    payload["data"]["items"][1][0] = "600000.SZ"
    with pytest.raises(ProviderFormatError, match="后缀不一致"):
        map_minute_response(
            payload, confirmed_through=datetime(2026, 1, 12, 9, 32, tzinfo=CHINA_TZ)
        )
    payload = fixture_payload()["minute_response"]
    payload["data"]["items"][1][6] = 0.5
    with pytest.raises(ProviderFormatError, match="不是整数"):
        map_minute_response(
            payload, confirmed_through=datetime(2026, 1, 12, 9, 32, tzinfo=CHINA_TZ)
        )


def test_daily_qfq_adjustment_and_confirmation_must_be_explicit() -> None:
    payload = fixture_payload()["daily_qfq_response"]
    with pytest.raises(ProviderFormatError, match="只接受明确的 qfq"):
        map_daily_qfq_response(payload, adjustment="none", confirmed=False)
    with pytest.raises(ProviderFormatError, match="布尔值"):
        map_daily_qfq_response(payload, adjustment="qfq", confirmed="false")  # type: ignore[arg-type]


def test_calendar_open_flag_is_strict() -> None:
    payload = copy.deepcopy(fixture_payload()["calendar_response"])
    payload["data"]["items"][0][2] = True
    with pytest.raises(ProviderFormatError, match="is_open"):
        map_calendar_response(payload)
    payload["data"]["items"][0][2] = 2
    with pytest.raises(ProviderFormatError, match="is_open"):
        map_calendar_response(payload)


def test_fixture_envelope_rejects_extra_keys_and_real_source_claim(tmp_path: Path) -> None:
    payload = fixture_payload()
    payload["unexpected"] = True
    with pytest.raises(ProviderFormatError, match="额外"):
        TushareFixtureAdapter(write_fixture(tmp_path, payload))
    payload = fixture_payload()
    payload["source"] = "tushare_real"
    with pytest.raises(ProviderFormatError, match="伪装"):
        TushareFixtureAdapter(write_fixture(tmp_path, payload))


def test_fixture_envelope_requires_current_format(tmp_path: Path) -> None:
    payload = fixture_payload()
    assert payload["format"] == FIXTURE_FORMAT
    payload["format"] = "other"
    with pytest.raises(ProviderFormatError, match="format"):
        TushareFixtureAdapter(write_fixture(tmp_path, payload))


def test_fixture_interface_rejects_empty_codes_and_naive_request_time() -> None:
    adapter = TushareFixtureAdapter(FIXTURE_FILE)
    aware = datetime(2026, 1, 12, 9, 31, tzinfo=CHINA_TZ)
    with pytest.raises(ValueError, match="至少需要"):
        adapter.fetch_minute_bars((), aware, aware)
    with pytest.raises(ValueError, match="带时区"):
        adapter.fetch_minute_bars(("600000",), datetime(2026, 1, 12, 9, 31), aware)
