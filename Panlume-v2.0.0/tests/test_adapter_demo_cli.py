from pathlib import Path

from pandeng.app.cli import main
from pandeng.storage.db import Database


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def prepare_config(raw_config: dict, database_name: str = "adapter-test.db") -> None:
    raw_config["monitor"]["interval_seconds"] = 60
    raw_config["storage"]["database"] = f"runtime/{database_name}"
    raw_config["models"]["ollama"]["enabled"] = False
    raw_config["models"]["cloud"]["enabled"] = False
    raw_config["watchlist"] = [
        {"code": "600000", "name": "离线样例一"},
        {"code": "000001", "name": "离线样例二"},
    ]


def copy_feed(tmp_path: Path) -> Path:
    destination = tmp_path / "examples" / "market_feed.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((PROJECT_ROOT / "examples" / "market_feed.json").read_bytes())
    return destination


def test_adapter_demo_cli_incremental_counts_trace_and_no_model_calls(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    prepare_config(raw_config)
    # 配置文件由 fixture 生成，因此显式重写一次，确保使用本测试修改后的内容。
    import yaml
    config_file.write_text(
        yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    feed = copy_feed(tmp_path)
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    assert main([
        *base, "adapter-demo", "--session-date", "2026-01-12",
        "--feed-file", str(feed),
    ]) == 0
    output = capsys.readouterr().out
    assert "首轮：状态 ok；收到 8；新增 8" in output
    assert "增量：状态 ok；收到 4；新增 4" in output
    assert "分钟 12 条；前复权日线 2 条；交易日历 1 条；只读分钟请求 4 次" in output
    assert "未访问真实行情、未调用 Ollama 或云端、未修改真实持仓" in output
    assert "只读数据体检：分钟总体 ok；完整 2 只" in output

    db = Database(tmp_path / "runtime" / "adapter-test.db")
    with db.connect() as connection:
        checkpoints = list(connection.execute(
            "SELECT stock_code, last_contiguous_time, status FROM monitor_code_states "
            "ORDER BY stock_code"
        ))
        model_calls = connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
        sources = {
            row[0] for row in connection.execute("SELECT DISTINCT source FROM minute_bars")
        }
    assert model_calls == 0
    assert sources == {"offline_contract_fixture"}
    assert [row["status"] for row in checkpoints] == ["ok", "ok"]
    assert {row["last_contiguous_time"] for row in checkpoints} == {
        "2026-01-12T09:36:00+08:00"
    }

    assert main([*base, "monitor-status"]) == 0
    status = capsys.readouterr().out
    assert "连续到 2026-01-12T09:36:00+08:00" in status

    assert main([
        *base, "data-audit", "--session-date", "2026-01-12",
        "--as-of-time", "2026-01-12T09:36:00+08:00",
    ]) == 0
    audit = capsys.readouterr().out
    assert "分钟总体状态 ok；完整 2 只" in audit
    assert "分钟已保存 6/6" in audit
    assert "当日前复权日 K 已确认" in audit
    assert "本次体检只读 SQLite" in audit


def test_adapter_demo_refuses_rerun_without_adding_or_overwriting(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    prepare_config(raw_config)
    import yaml
    config_file.write_text(
        yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    feed = copy_feed(tmp_path)
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    arguments = [*base, "adapter-demo", "--feed-file", str(feed)]
    assert main(arguments) == 0
    capsys.readouterr()

    assert main(arguments) == 1
    output = capsys.readouterr().out
    assert "只使用全新的 adapter-test.db" in output
    assert "本次没有覆盖或追加" in output
    db = Database(tmp_path / "runtime" / "adapter-test.db")
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 12
        assert connection.execute("SELECT COUNT(*) FROM monitor_rounds").fetchone()[0] == 2


def test_adapter_demo_rejects_non_dedicated_database_before_reading_feed(
    config_file: Path, raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    prepare_config(raw_config, database_name="pandeng.db")
    import yaml
    config_file.write_text(
        yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    assert main([
        *base, "adapter-demo", "--feed-file", str(tmp_path / "does-not-exist.json")
    ]) == 1
    output = capsys.readouterr().out
    assert "只允许写入文件名 adapter-test.db" in output
    db = Database(tmp_path / "runtime" / "pandeng.db")
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM monitor_rounds").fetchone()[0] == 0
