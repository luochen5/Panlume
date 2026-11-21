import csv
from pathlib import Path

from pandeng.app.cli import main
from test_cleaner import minute_record


def test_simulate_and_data_status(
    config_file: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    arguments = [*base, "simulate", "--minutes", "2", "--daily-days", "3"]
    assert main(arguments) == 0
    first = capsys.readouterr().out
    assert "原始分钟行情" in first and "新增 4" in first
    assert "前复权日 K" in first and "新增 6" in first
    assert "未访问网络或调用模型" in first

    assert main(arguments) == 0
    second = capsys.readouterr().out
    assert "去重 4" in second

    assert main([*base, "data-status"]) == 0
    status = capsys.readouterr().out
    assert "分钟记录：4" in status
    assert "前复权日线：6" in status
    assert "600000" in status and "000001" in status


def test_csv_replay(
    config_file: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(config_file)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    path = tmp_path / "minute.csv"
    rows = [
        minute_record(bar_time="2026-01-05T09:31:00+08:00"),
        minute_record(bar_time="2026-01-05T09:32:00+08:00"),
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    assert main([*base, "replay", "--minute-file", str(path)]) == 0
    output = capsys.readouterr().out
    assert "收到 2" in output  # 同一只股票的连续两条分钟记录
    assert "新增 2" in output
    assert "CSV 回放处理完成" in output
