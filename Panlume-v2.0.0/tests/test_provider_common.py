import json

import pytest

from pandeng.data.provider_common import (
    ProviderFormatError,
    decode_json_response,
    load_json_document,
    pandeng_code_to_tushare,
    parse_china_provider_time,
    parse_compact_date,
    tushare_code_to_pandeng,
    volume_to_shares,
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600000", "600000.SH"),
        ("000001", "000001.SZ"),
        ("300750", "300750.SZ"),
        ("430047", "430047.BJ"),
        ("830799", "830799.BJ"),
        ("920001", "920001.BJ"),
    ],
)
def test_code_conversion_between_pandeng_and_tushare(code: str, expected: str) -> None:
    assert pandeng_code_to_tushare(code) == expected
    assert tushare_code_to_pandeng(expected) == code


@pytest.mark.parametrize("value", ["60000", "abcdef", "200001", 600000, None])
def test_pandeng_code_rejects_unknown_or_non_string(value: object) -> None:
    with pytest.raises(ProviderFormatError):
        pandeng_code_to_tushare(value)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value", ["600000", "600000.XX", "000001.SH", "300750.SH", "920001.SZ", None]
)
def test_tushare_code_rejects_bad_suffix_or_exchange_mismatch(value: object) -> None:
    with pytest.raises(ProviderFormatError):
        tushare_code_to_pandeng(value)


def test_naive_provider_time_uses_fixed_china_offset() -> None:
    parsed = parse_china_provider_time("2026-01-12 09:32:00")
    assert parsed.isoformat() == "2026-01-12T09:32:00+08:00"


def test_aware_provider_time_is_converted_to_china_offset() -> None:
    parsed = parse_china_provider_time("2026-01-12T01:32:00+00:00")
    assert parsed.isoformat() == "2026-01-12T09:32:00+08:00"


@pytest.mark.parametrize("value", ["2026-01-12 09:32:01", "bad", "", None])
def test_provider_time_rejects_invalid_or_non_minute_values(value: object) -> None:
    with pytest.raises(ProviderFormatError):
        parse_china_provider_time(value)


def test_compact_date_is_strict() -> None:
    assert parse_compact_date("20260112").isoformat() == "2026-01-12"
    for value in ("2026-01-12", "20260230", 20260112, None):
        with pytest.raises(ProviderFormatError):
            parse_compact_date(value)


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [("12000", "share", 12000), ("12345.5", "lot_100", 1234550), (0, "share", 0)],
)
def test_volume_conversion_is_explicit(value: object, unit: str, expected: int) -> None:
    assert volume_to_shares(value, unit=unit) == expected


@pytest.mark.parametrize("value", [None, True, -1, "NaN", "Infinity", "0.001", "abc"])
def test_volume_conversion_rejects_unsafe_values(value: object) -> None:
    with pytest.raises(ProviderFormatError):
        volume_to_shares(value, unit="share")


def test_volume_conversion_rejects_unknown_unit() -> None:
    with pytest.raises(ProviderFormatError, match="未知成交量单位"):
        volume_to_shares(1, unit="hand_guess")


def test_json_response_size_utf8_and_root_object_are_checked(tmp_path) -> None:
    payload = json.dumps({"code": 0}, ensure_ascii=False).encode("utf-8")
    assert decode_json_response(b"\xef\xbb\xbf" + payload)["code"] == 0
    with pytest.raises(ProviderFormatError, match="上限"):
        decode_json_response(payload, max_bytes=3)
    with pytest.raises(ProviderFormatError, match="根节点"):
        decode_json_response(b"[]")
    with pytest.raises(ProviderFormatError, match="找不到"):
        load_json_document(tmp_path / "missing.json")
