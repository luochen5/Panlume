import json
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

import pytest

from pandeng.data.json_feed import FEED_FORMAT, FeedFormatError, JsonFeedAdapter
from pandeng.data.schedule import CHINA_TZ
from test_cleaner import daily_record, minute_record


def feed_payload() -> dict:
    minute = minute_record(
        bar_time="2026-01-12T09:31:00+08:00", confirmed=True
    )
    minute.pop("source")
    daily = daily_record(trade_date="2026-01-12")
    daily.pop("source")
    return {
        "format": FEED_FORMAT,
        "source": "offline_test",
        "minute_bars": [minute],
        "daily_qfq": [daily],
        "trading_calendar": [{"trade_date": "2026-01-12", "is_open": True}],
    }


def write_feed(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "feed.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_three_interfaces_filter_ranges_and_inherit_source(tmp_path: Path) -> None:
    payload = feed_payload()
    second = deepcopy(payload["minute_bars"][0])
    second["bar_time"] = "2026-01-12T09:32:00+08:00"
    payload["minute_bars"].append(second)
    adapter = JsonFeedAdapter(write_feed(tmp_path, payload))

    start = datetime(2026, 1, 12, 9, 32, tzinfo=CHINA_TZ)
    minutes = list(adapter.fetch_minute_bars(["600000"], start, start))
    daily = list(adapter.fetch_daily_qfq(
        ["600000"], date(2026, 1, 12), date(2026, 1, 12)
    ))
    calendar = list(adapter.fetch_trading_calendar(
        date(2026, 1, 12), date(2026, 1, 12)
    ))

    assert len(minutes) == 1 and minutes[0].bar_time == start
    assert minutes[0].source == "offline_test"
    assert len(daily) == 1 and daily[0].adjustment == "qfq"
    assert len(calendar) == 1 and calendar[0].is_open
    assert len(adapter.minute_requests) == 1


@pytest.mark.parametrize("field", ["confirmed", "adjustment", "volume"])
def test_minute_contract_requires_explicit_fields(tmp_path: Path, field: str) -> None:
    payload = feed_payload()
    payload["minute_bars"][0].pop(field)
    with pytest.raises(FeedFormatError, match="缺少明确字段"):
        JsonFeedAdapter(write_feed(tmp_path, payload))


def test_daily_must_be_explicit_qfq_not_implicitly_relabelled(tmp_path: Path) -> None:
    payload = feed_payload()
    payload["daily_qfq"][0]["adjustment"] = "none"
    with pytest.raises(FeedFormatError, match="前复权"):
        JsonFeedAdapter(write_feed(tmp_path, payload))


def test_record_source_cannot_conflict_with_feed_source(tmp_path: Path) -> None:
    payload = feed_payload()
    payload["minute_bars"][0]["source"] = "another_provider"
    with pytest.raises(FeedFormatError, match="source 与根节点不一致"):
        JsonFeedAdapter(write_feed(tmp_path, payload))


def test_invalid_ohlc_and_naive_time_are_rejected_before_ingestion(tmp_path: Path) -> None:
    payload = feed_payload()
    payload["minute_bars"][0]["high"] = 1
    with pytest.raises(FeedFormatError, match="OHLC"):
        JsonFeedAdapter(write_feed(tmp_path, payload))

    payload = feed_payload()
    payload["minute_bars"][0]["bar_time"] = "2026-01-12 09:31:00"
    with pytest.raises(FeedFormatError, match="必须带时区"):
        JsonFeedAdapter(write_feed(tmp_path, payload))


def test_unknown_root_fields_and_oversized_file_are_rejected(tmp_path: Path) -> None:
    payload = feed_payload()
    payload["unexpected"] = True
    with pytest.raises(FeedFormatError, match="根节点字段不符"):
        JsonFeedAdapter(write_feed(tmp_path, payload))

    path = tmp_path / "large.json"
    path.write_bytes(b" " * (10 * 1024 * 1024 + 1))
    with pytest.raises(FeedFormatError, match="10 MB"):
        JsonFeedAdapter(path)
