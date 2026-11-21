import json
from pathlib import Path

import yaml

from pandeng.app.cli import main
from pandeng.config.loader import load_config
from pandeng.storage.db import Database


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_FILE = PROJECT_ROOT / "examples/provider_fixtures/tushare/offline_responses.json"


def prepare_config(raw_config: dict, database_name: str = "real-data-test.db") -> None:
    raw_config["storage"]["database"] = f"runtime/{database_name}"
    raw_config["models"]["ollama"]["enabled"] = False
    raw_config["models"]["cloud"]["enabled"] = False
    raw_config["watchlist"] = [
        {"code": "600000", "name": "虚拟样例甲"},
        {"code": "000001", "name": "虚拟样例乙"},
    ]


def save_config(config_file: Path, raw_config: dict) -> None:
    config_file.write_text(
        yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def test_provider_fixture_demo_end_to_end_trace_and_audit(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    prepare_config(raw_config)
    save_config(config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    assert main([
        *base, "provider-fixture-demo", "--session-date", "2026-01-12",
        "--fixture-file", str(FIXTURE_FILE),
    ]) == 0
    output = capsys.readouterr().out
    assert "只读取人工编写的虚拟 Tushare 响应" in output
    assert "没有 HTTP 客户端，不读取 Token" in output
    assert "连续竞价分钟：收到 4；新增 4；拒绝 0" in output
    assert "单独排除 2 条；没有改写为 09:31" in output
    assert "confirmed=false，保持未确认" in output
    assert "C1 验收汇总：分钟 4 条；前复权日线 2 条；交易日历 1 条；09:30 排除 2 条" in output
    assert "只读体检：分钟总体 ok；完整 2 只；未知 0 只" in output

    db = Database(tmp_path / "runtime" / "real-data-test.db")
    with db.connect() as connection:
        minutes = list(connection.execute(
            "SELECT stock_code, bar_time, source, raw_json FROM minute_bars ORDER BY id"
        ))
        daily = list(connection.execute(
            "SELECT stock_code, volume, confirmed, adjustment, raw_json FROM daily_qfq_bars"
        ))
        assert connection.execute("SELECT COUNT(*) FROM trading_calendar").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM data_quality_issues").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM position_lots").fetchone()[0] == 0
    assert len(minutes) == 4
    assert {row["source"] for row in minutes} == {"tushare_fixture_offline"}
    assert not any("09:30:00" in row["bar_time"] for row in minutes)
    raw_minute = json.loads(minutes[0]["raw_json"])
    assert raw_minute["provider_raw"]["ts_code"] == "600000.SH"
    assert raw_minute["provider_request_id"] == "fixture-minute-virtual-001"
    assert "token" not in minutes[0]["raw_json"].lower()
    daily_by_code = {row["stock_code"]: row for row in daily}
    assert daily_by_code["600000"]["volume"] == 1234550
    assert json.loads(daily_by_code["600000"]["raw_json"])["provider_raw"]["vol"] == 12345.5
    assert all(row["confirmed"] == 0 for row in daily)
    assert all(row["adjustment"] == "qfq" for row in daily)

    assert main([
        *base, "data-audit", "--session-date", "2026-01-12",
        "--as-of-time", "2026-01-12T09:32:00+08:00",
    ]) == 0
    audit = capsys.readouterr().out
    assert "分钟总体状态 ok；完整 2 只" in audit
    assert audit.count("分钟已保存 2/2") == 2
    assert audit.count("当日前复权日 K 未确认") == 2
    assert audit.count("连续检查点 无") == 2
    assert "本次体检只读 SQLite" in audit


def test_provider_fixture_demo_refuses_rerun_without_appending(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    prepare_config(raw_config)
    save_config(config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    arguments = [*base, "provider-fixture-demo", "--fixture-file", str(FIXTURE_FILE)]
    assert main([*base, "init"]) == 0
    assert main(arguments) == 0
    capsys.readouterr()
    assert main(arguments) == 1
    output = capsys.readouterr().out
    assert "只使用全新的 real-data-test.db" in output
    assert "本次没有覆盖或追加" in output
    with Database(tmp_path / "runtime" / "real-data-test.db").connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 3


def test_provider_fixture_demo_rejects_main_database_before_reading_fixture(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    prepare_config(raw_config, database_name="pandeng.db")
    save_config(config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    assert main([
        *base, "provider-fixture-demo", "--fixture-file", str(tmp_path / "missing.json")
    ]) == 1
    output = capsys.readouterr().out
    assert "只允许写入文件名 real-data-test.db" in output
    with Database(tmp_path / "runtime" / "pandeng.db").connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0


def test_provider_fixture_demo_rejects_enabled_model_before_reading_fixture(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    prepare_config(raw_config)
    raw_config["models"]["ollama"]["enabled"] = True
    save_config(config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    assert main([
        *base, "provider-fixture-demo", "--fixture-file", str(tmp_path / "missing.json")
    ]) == 1
    assert "Ollama 和云端均关闭" in capsys.readouterr().out


def test_provider_fixture_demo_wrong_date_fails_before_any_ingestion(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    prepare_config(raw_config)
    save_config(config_file, raw_config)
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()
    assert main([
        *base, "provider-fixture-demo", "--session-date", "2026-01-13",
        "--fixture-file", str(FIXTURE_FILE),
    ]) == 1
    assert "虚拟响应未覆盖当前配置" in capsys.readouterr().out
    with Database(tmp_path / "runtime" / "real-data-test.db").connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 0


def test_shipped_real_data_config_loads_without_editing() -> None:
    config = load_config(PROJECT_ROOT / "configs/real-data-test.yaml")
    assert config.storage.database.as_posix().endswith("runtime/real-data-test.db")
    assert config.monitor.interval_seconds == 300
    assert config.ollama.enabled is False
    assert config.cloud.enabled is False
    assert [item.code for item in config.watchlist] == ["600000", "000001"]
