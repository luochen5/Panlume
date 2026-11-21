from __future__ import annotations

from datetime import date

import pytest

from pandeng.config.schema import MomentumConfig, parse_config
from pandeng.data.base import MinuteBar
from pandeng.data.schedule import session_minutes
from pandeng.indicators.momentum import (
    MomentumInput, calculate_macd, calculate_momentum, calculate_momentum_records,
    calculate_rsi,
)
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_minutes


def test_macd_known_sequence_and_warmup() -> None:
    result = calculate_macd([1, 2, 3, 4, 5], fast=2, slow=3, signal=3)

    assert result is not None
    assert result.fast_ema == pytest.approx(4.5061728395)
    assert result.slow_ema == pytest.approx(4.0625)
    assert result.macd == pytest.approx(0.4436728395)
    assert result.signal == pytest.approx(0.3688271605)
    assert result.histogram == pytest.approx(0.0748456790)
    assert calculate_macd([1, 2, 3], fast=2, slow=3, signal=3) is None
    assert calculate_macd(list(range(33))) is None


def test_rsi_known_wilder_sequence_warmup_and_constant_series() -> None:
    assert calculate_rsi([1, 2, 1, 3, 2], period=3) == pytest.approx(54.54545454545455)
    assert calculate_rsi([1, 2, 3], period=3) is None
    assert calculate_rsi([5] * 40) == 50.0
    assert calculate_rsi(list(range(1, 40))) == 100.0
    assert calculate_rsi(list(range(40, 1, -1))) == 0.0


def records(count: int = 40) -> list[MomentumInput]:
    return [
        MomentumInput(index, f"2026-01-01T09:{index:02d}:00+08:00", 10 + index / 10, "600000", "minute", "none")
        for index in range(count)
    ]


def test_momentum_result_contains_parameters_range_samples_and_evidence() -> None:
    result = calculate_momentum_records(records())
    raw = result.to_dict()

    assert result.status == "ok"
    assert raw["parameters"] == {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "rsi_period": 14}
    assert raw["sample_count"] == 40 and raw["adjustment"] == "none"
    assert raw["input_range"]["start"].startswith("2026-01-01")
    assert len(raw["source_record_ids"]) == 40
    assert raw["macd"]["histogram"] != 0 and raw["rsi"] == 100.0


def test_insufficient_data_never_invents_values() -> None:
    result = calculate_momentum_records(records(10))
    assert result.status == "insufficient_data"
    assert result.macd is None and result.rsi is None
    assert "数据不足" in result.reason


@pytest.mark.parametrize("mutation", ["missing", "unordered", "duplicate", "mixed", "gap"])
def test_invalid_inputs_are_rejected(mutation) -> None:
    items = records()
    expected = None
    if mutation == "missing":
        items[3] = MomentumInput(3, items[3].timestamp, None, "600000", "minute", "none")
    elif mutation == "unordered":
        items[2], items[3] = items[3], items[2]
    elif mutation == "duplicate":
        items[3] = MomentumInput(3, items[2].timestamp, 10.3, "600000", "minute", "none")
    elif mutation == "mixed":
        items[3] = MomentumInput(3, items[3].timestamp, 10.3, "600000", "daily", "qfq")
    else:
        expected = [item.timestamp for item in items]
        items.pop(4)

    result = calculate_momentum_records(items, expected_timestamps=expected)
    assert result.status == "invalid_data"
    assert result.macd is None and result.rsi is None


def test_minute_database_replay_is_repeatable_and_never_mixes_adjustment(tmp_path) -> None:
    database = Database(tmp_path / "momentum.db")
    database.initialize()
    points = session_minutes(date(2026, 1, 5))
    ingest_minutes(database, [
        MinuteBar("600000", point, 10, 20, 1, 10 + index / 10, 100, "test")
        for index, point in enumerate(points[:40])
    ], adapter="test")

    first = calculate_momentum(database, "600000", "minute", points[39])
    second = calculate_momentum(database, "600000", "minute", points[39])

    assert first.status == "ok"
    assert first.adjustment == "none"
    assert first.to_dict() == second.to_dict()
    assert first.macd is not None and first.rsi is not None


def test_momentum_config_defaults_and_validation(raw_config) -> None:
    from pathlib import Path
    config = parse_config(raw_config, Path.cwd())
    assert config.momentum == MomentumConfig()
    raw_config["indicators"] = {"macd_fast": 26, "macd_slow": 12}
    with pytest.raises(Exception, match="macd_slow"):
        parse_config(raw_config, Path.cwd())
