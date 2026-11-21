from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from pandeng.analyzer.demo import run_analysis_demo
from pandeng.app.cli import main
from pandeng.config.schema import AppConfig, parse_config
from pandeng.review.service import create_local_review, load_review
from pandeng.storage.db import Database


SESSION_DATE = date(2026, 1, 7)
CODE = "600000"


def _analysis_database(raw_config: dict, tmp_path: Path) -> tuple[AppConfig, Database]:
    raw_config["storage"]["database"] = "runtime/analysis-test.db"
    raw_config["models"]["ollama"]["enabled"] = False
    config = parse_config(raw_config, tmp_path)
    database = Database(config.storage.database)
    database.initialize()
    run_analysis_demo(config, SESSION_DATE)
    return config, database


def _table_snapshot(database: Database, *, exclude_reviews: bool = False) -> dict[str, list[tuple]]:
    with database.connect() as connection:
        names = [
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if row["name"] != "sqlite_sequence"
            and not (exclude_reviews and row["name"] == "review_runs")
        ]
        return {
            name: [tuple(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")]
            for name in names
        }


def test_local_review_reads_complete_evidence_and_only_appends_review(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    _, database = _analysis_database(raw_config, tmp_path)
    capsys.readouterr()
    before = _table_snapshot(database, exclude_reviews=True)

    outcome = create_local_review(database, SESSION_DATE, CODE)

    assert outcome.created is True
    assert outcome.data_status == "ok"
    assert "分钟 240 条，已确认 240" in outcome.report
    assert "日线结论：状态 ok" in outcome.report
    assert "本次本地复盘未调用 Ollama 或云端" in outcome.report
    assert _table_snapshot(database, exclude_reviews=True) == before
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cloud_review_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
        saved = connection.execute("SELECT input_json, local_report FROM review_runs").fetchone()
    saved_input = json.loads(saved["input_json"])
    assert saved_input["format"] == "pandeng.daily-review-input.v1"
    assert saved_input["fact_package"]["fact_package_version"] == "1.0"
    assert saved_input["fact_package"]["report_type"] == "daily_close_detail"
    assert saved_input["fact_package"]["rule_events"]
    assert saved["local_report"] == outcome.report


def test_local_review_same_input_is_idempotent_without_any_table_change(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    _, database = _analysis_database(raw_config, tmp_path)
    capsys.readouterr()
    first = create_local_review(database, SESSION_DATE, CODE)
    before = _table_snapshot(database)

    second = create_local_review(database, SESSION_DATE, CODE)

    assert second.created is False
    assert second.review_id == first.review_id
    assert second.input_hash == first.input_hash
    assert second.report == first.report
    assert _table_snapshot(database) == before


def test_local_review_changed_evidence_creates_a_new_immutable_run(
    raw_config: dict, tmp_path: Path, capsys
) -> None:
    _, database = _analysis_database(raw_config, tmp_path)
    capsys.readouterr()
    first = create_local_review(database, SESSION_DATE, CODE)
    with database.connect() as connection:
        changed = connection.execute(
            """
            UPDATE monitor_code_states SET unknown_reason = '离线测试：新增一条状态证据'
            WHERE session_date = ? AND stock_code = ?
            """,
            (SESSION_DATE.isoformat(), CODE),
        ).rowcount
    assert changed == 1

    second = create_local_review(database, SESSION_DATE, CODE)

    assert second.created is True
    assert second.review_id != first.review_id
    assert second.input_hash != first.input_hash
    with database.connect() as connection:
        rows = list(connection.execute(
            "SELECT id, input_json, local_report FROM review_runs ORDER BY id"
        ))
    assert len(rows) == 2
    assert rows[0]["input_json"] != rows[1]["input_json"]
    assert rows[0]["local_report"] == first.report


def test_local_review_without_data_stays_unknown(tmp_path: Path) -> None:
    database = Database(tmp_path / "empty.db")
    database.initialize()

    outcome = create_local_review(database, SESSION_DATE, CODE)

    assert outcome.data_status == "unknown"
    assert "数据状态 unknown" in outcome.report
    assert "最近指标快照：无" in outcome.report
    assert "收盘本身不代表日 K 已确认" in outcome.report


def test_english_review_is_fixed_template_and_does_not_rewrite_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "english-review.db")
    database.initialize()

    outcome = create_local_review(database, SESSION_DATE, CODE, language="en")

    assert outcome.created is True
    assert "Data status unknown" in outcome.report
    assert "Latest indicator snapshot: none" in outcome.report
    assert "No Ollama or cloud call was made" in outcome.report
    before = _table_snapshot(database)

    same_facts_in_chinese = create_local_review(database, SESSION_DATE, CODE, language="zh-CN")

    assert same_facts_in_chinese.created is False
    assert same_facts_in_chinese.report == outcome.report
    assert _table_snapshot(database) == before


@pytest.mark.parametrize("mutation", ["quality_error", "unconfirmed_daily"])
def test_local_review_quality_error_or_unconfirmed_daily_stays_unknown(
    raw_config: dict, tmp_path: Path, capsys, mutation: str
) -> None:
    _, database = _analysis_database(raw_config, tmp_path)
    capsys.readouterr()
    with database.connect() as connection:
        if mutation == "quality_error":
            run_id = connection.execute("SELECT MIN(id) FROM ingestion_runs").fetchone()[0]
            connection.execute(
                """
                INSERT INTO data_quality_issues(
                    run_id, data_kind, stock_code, data_time, issue_type,
                    severity, message, raw_json, created_at
                ) VALUES (?, 'minute', ?, ?, 'offline_test_conflict', 'error',
                          '离线测试质量错误', '{}', '2026-01-07T08:00:00+00:00')
                """,
                (run_id, CODE, f"{SESSION_DATE.isoformat()}T15:00:00+08:00"),
            )
        else:
            connection.execute(
                "UPDATE daily_qfq_bars SET confirmed = 0 WHERE stock_code = ? AND trade_date = ?",
                (CODE, SESSION_DATE.isoformat()),
            )

    outcome = create_local_review(database, SESSION_DATE, CODE)

    assert outcome.data_status == "unknown"
    if mutation == "quality_error":
        assert "质量错误 1 条" in outcome.report
    else:
        assert json.loads(load_review(database, outcome.review_id)["input_json"])[
            "daily_bar"
        ]["confirmed"] == 0


def test_review_cli_dispatches_and_never_calls_enabled_models(
    raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    raw_config["storage"]["database"] = "runtime/review-cli.db"
    raw_config["watchlist"] = [{"code": CODE, "name": "本地复盘"}]
    raw_config["models"]["ollama"]["enabled"] = True
    raw_config["models"]["cloud"] = {
        "enabled": True,
        "base_url": "http://127.0.0.1:9/v1",
        "model": "offline-gate-test",
        "event_whitelist": ["daily_review"],
    }
    monkeypatch.setenv("PANDENG_CLOUD_API_KEY", "dummy-review-key")
    path = tmp_path / "configs" / "review.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(raw_config, allow_unicode=True), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def forbidden_call(*args, **kwargs):
        raise AssertionError("本地复盘不应创建任何模型请求")

    monkeypatch.setattr(
        "pandeng.summarizer.ollama.OllamaClient.generate", forbidden_call
    )
    monkeypatch.setattr(
        "pandeng.review.cloud.OpenAICompatibleCloudClient.generate", forbidden_call
    )
    base = ["--config", str(path)]
    assert main([*base, "init"]) == 0
    capsys.readouterr()

    assert main([
        *base, "review-daily", "--session-date", SESSION_DATE.isoformat(), "--code", CODE
    ]) == 0
    first_output = capsys.readouterr().out
    assert "本地复盘编号 1；本次新建" in first_output
    assert "本次云端调用 0 次" in first_output

    assert main([
        *base, "review-daily", "--session-date", SESSION_DATE.isoformat(), "--code", CODE
    ]) == 0
    assert "相同输入已存在，幂等返回" in capsys.readouterr().out
    assert main([*base, "review-status"]) == 0
    assert "云端 未调用" in capsys.readouterr().out
    assert main([*base, "review-status", "--review-id", "1"]) == 0
    detail = capsys.readouterr().out
    assert "云端：未调用" in detail
    assert "本命令没有发起新请求" in detail

    database = Database((tmp_path / "runtime" / "review-cli.db").resolve())
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cloud_review_calls").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0

    assert main([
        *base, "review-daily", "--session-date", SESSION_DATE.isoformat(), "--code", "000001"
    ]) == 1
    assert "必须属于当前 YAML" in capsys.readouterr().out
