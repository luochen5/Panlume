from __future__ import annotations

import json

import pytest

from pandeng.reporting import FACT_PACKAGE_VERSION, FactPackage


def rule_event() -> dict:
    return {
        "event_id": 2,
        "rule_id": "rapid_rise",
        "event_name": "快速上涨",
        "status": "active",
        "first_triggered_at": "2026-01-07T09:32:00+08:00",
        "last_triggered_at": "2026-01-07T09:33:00+08:00",
        "resolved_at": None,
        "condition_expression": "return_1m_pct >= threshold_pct",
        "observed_values": {"return_1m_pct": 1.25},
        "thresholds": {"threshold_pct": 1.0},
        "time_window": {"minutes": 1},
        "merge_count": 1,
        "cooldown_until": "2026-01-07T10:03:00+08:00",
        "evidence_record_ids": {"event_check_ids": [3], "snapshot_ids": [4]},
    }


def package() -> FactPackage:
    return FactPackage(
        fact_package_version=FACT_PACKAGE_VERSION,
        report_type="realtime_event_summary",
        stock_code="600000",
        market_timezone="UTC+08:00",
        data_range={"start": "2026-01-07T09:31:00+08:00", "end": "2026-01-07T09:33:00+08:00"},
        source_records={"minute_bar_ids": [1, 2], "sources": ["simulator"]},
        data_quality={"errors": 0, "missing_minutes": []},
        confirmed_status={"minutes_confirmed": 2, "minutes_saved": 2},
        price_volume_statistics={"close": 10.25, "volume": 1200},
        timeline=[{"time": "2026-01-07T09:33:00+08:00", "kind": "rule_check"}],
        indicators={"latest_snapshot_id": 4, "values": {"close": 10.25}},
        rule_events=[rule_event()],
        holdings_context=[],
        limitations=[],
        traceability={"event_check_ids": [3], "analysis_run_ids": [5]},
    )


def test_fact_package_has_stable_json_and_content_hash() -> None:
    first = package()
    raw = first.to_dict()
    raw["source_records"] = dict(reversed(list(raw["source_records"].items())))
    second = FactPackage.from_dict(raw)

    assert second.canonical_json() == first.canonical_json()
    assert second.content_hash() == first.content_hash()
    assert json.loads(first.canonical_json())["fact_package_version"] == "1.0"
    assert "generated_at" not in raw


def test_model_copy_is_detached_from_original() -> None:
    facts = package()
    copied = facts.model_copy()
    copied["indicators"]["values"]["close"] = 999
    copied["rule_events"][0]["status"] = "cleared"

    assert facts.indicators["values"]["close"] == 10.25
    assert facts.rule_events[0]["status"] == "active"


def test_rule_event_requires_complete_fields_and_aware_timestamps() -> None:
    raw = package().to_dict()
    del raw["rule_events"][0]["thresholds"]
    with pytest.raises(ValueError, match="thresholds"):
        FactPackage.from_dict(raw)

    raw = package().to_dict()
    raw["rule_events"][0]["first_triggered_at"] = "2026-01-07T09:32:00"
    with pytest.raises(ValueError, match="时区"):
        FactPackage.from_dict(raw)


def test_fact_package_rejects_non_finite_numbers_and_unknown_type() -> None:
    raw = package().to_dict()
    raw["price_volume_statistics"]["close"] = float("nan")
    with pytest.raises(ValueError):
        FactPackage.from_dict(raw)

    raw = package().to_dict()
    raw["report_type"] = "invented_report"
    with pytest.raises(ValueError, match="报告类型"):
        FactPackage.from_dict(raw)
