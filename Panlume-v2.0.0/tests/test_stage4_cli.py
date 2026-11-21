from __future__ import annotations

from pathlib import Path

import yaml

from pandeng.app.cli import main
from pandeng.storage.db import Database


def summary_config(raw_config: dict, tmp_path: Path, name: str = "summary-test.db") -> Path:
    raw_config["storage"]["database"] = f"runtime/{name}"
    raw_config["watchlist"] = [{"code": "600000", "name": "摘要演示"}]
    raw_config["models"]["ollama"]["enabled"] = False
    raw_config["summary"] = {
        "queue_capacity": 8,
        "shutdown_timeout_seconds": 10,
        "max_chars": 400,
    }
    path = tmp_path / "configs" / "summary.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(raw_config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def test_stage4_offline_demo_status_and_trace(
    raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    path = summary_config(raw_config, tmp_path)
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(path)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    assert main([*base, "summary-demo", "--session-date", "2026-01-08"]) == 0
    demo = capsys.readouterr().out
    assert "使用模拟模型响应，不会连接 Ollama 或云端" in demo
    assert demo.index("【high/initial】") < demo.index("  摘要任务 1：")
    assert "模型摘要 1 条；模板回退 1 条；模型调用 2 次" in demo
    assert "validation_failed" in demo

    assert main([*base, "summary-status"]) == 0
    status = capsys.readouterr().out
    assert "模型成功 1；模板回退 1；模型调用 2" in status
    assert "输入外数字 999" not in status  # 被拒绝的模型文本不能成为最终模板结果。

    assert main([*base, "summary-trace", "--task-id", "1"]) == 0
    trace = capsys.readouterr().out
    assert "事件检查" in trace and "指标快照" in trace
    assert "total_duration_ns" in trace and "eval_count" in trace
    assert "R1 思考文本已单独审计" in trace

    database = Database((tmp_path / "runtime" / "summary-test.db").resolve())
    assert database.metadata("schema_version") == "6"
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM summary_tasks").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM summary_results WHERE summary_kind = 'template'"
        ).fetchone()[0] == 1
        thinking = connection.execute(
            "SELECT thinking_text FROM model_calls WHERE task_id = 1"
        ).fetchone()[0]
        final = connection.execute(
            "SELECT text FROM summary_results WHERE task_id = 1"
        ).fetchone()[0]
    assert "响应" not in final and "think" not in final
    assert "这一段只能进入调用审计" in thinking

    assert main([*base, "summary-demo", "--session-date", "2026-01-08"]) == 1
    assert "没有覆盖或追加" in capsys.readouterr().out


def test_real_ollama_requires_explicit_config_and_dedicated_database(
    raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    path = summary_config(raw_config, tmp_path, name="summary-live-test.db")
    monkeypatch.chdir(tmp_path)
    base = ["--config", str(path)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    assert main([*base, "summary-demo", "--real-ollama"]) == 1
    output = capsys.readouterr().out
    assert "enabled=true" in output
    with Database((tmp_path / "runtime" / "summary-live-test.db").resolve()).connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM summary_tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0] == 0
