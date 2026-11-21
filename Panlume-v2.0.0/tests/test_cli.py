from pathlib import Path

from pandeng.app.cli import main


def test_init_status_and_empty_holdings(
    config_file: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    arguments = ["--config", str(config_file)]

    assert main([*arguments, "init"]) == 0
    assert "初始化/升级完成" in capsys.readouterr().out

    assert main([*arguments, "status"]) == 0
    output = capsys.readouterr().out
    assert "数据库已初始化：是" in output
    assert "真实持仓批次数：0" in output
    assert "不启动轮询或调用模型" in output

    assert main([*arguments, "holdings", "list"]) == 0
    assert "没有已确认的真实持仓批次" in capsys.readouterr().out


def test_show_config_does_not_print_api_key(
    config_file: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PANDENG_CLOUD_API_KEY", "never-print-this")

    assert main(["--config", str(config_file), "show-config"]) == 0
    output = capsys.readouterr().out
    assert "配置检查通过" in output
    assert "API Key=已配置" in output
    assert "never-print-this" not in output


def test_holdings_requires_initialization(
    config_file: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    result = main(["--config", str(config_file), "holdings", "list"])

    assert result == 1
    assert "数据库尚未初始化" in capsys.readouterr().out
