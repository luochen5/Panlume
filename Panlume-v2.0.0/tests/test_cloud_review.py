from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import yaml

from pandeng.app.cli import main
from pandeng.config.schema import AppConfig, ConfigError, parse_config
from pandeng.review.cloud import (
    CLOUD_REVIEW_CONFIRMATION,
    CloudError,
    CloudResponse,
    CloudReviewService,
    OpenAICompatibleCloudClient,
)
from pandeng.review.service import create_local_review
from pandeng.storage.db import Database


VALID_TEXT = "事实：本地复盘已保存。\n推测：无。\n后续观察：核对后续数据质量。"
DAY = date(2026, 1, 7)


class FakeCloudClient:
    def __init__(self, result: CloudResponse | Exception | None = None) -> None:
        self.result = result or CloudResponse(
            text=VALID_TEXT,
            model="fake-cloud-model",
            raw_json=json.dumps({"choices": [{"message": {"content": VALID_TEXT}}]}),
            prompt_tokens=20,
            completion_tokens=10,
            total_tokens=30,
        )
        self.payloads: list[dict[str, Any]] = []

    def generate(self, payload: dict[str, Any]) -> CloudResponse:
        self.payloads.append(payload)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _service_setup(
    raw_config: dict,
    tmp_path: Path,
    *,
    enabled: bool = True,
    whitelist: tuple[str, ...] = ("daily_review",),
    key_configured: bool = True,
) -> tuple[AppConfig, Database, int]:
    raw_config["storage"]["database"] = "runtime/cloud-review-test.db"
    raw_config["models"]["ollama"]["enabled"] = False
    config = parse_config(raw_config, tmp_path)
    config = replace(
        config,
        cloud=replace(
            config.cloud,
            enabled=enabled,
            base_url="http://127.0.0.1:9/v1",
            model="offline-cloud-test",
            api_key_configured=key_configured,
            event_whitelist=whitelist,
        ),
    )
    database = Database(config.storage.database)
    database.initialize()
    review = create_local_review(database, DAY, "600000")
    return config, database, review.review_id


def _calls(database: Database) -> list:
    with database.connect() as connection:
        return list(connection.execute("SELECT * FROM cloud_review_calls ORDER BY id"))


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        ("confirmation", "确认文本不匹配"),
        ("disabled", "总开关关闭"),
        ("whitelist", "白名单未包含"),
        ("key", "API Key 未配置"),
        ("missing_review", "不存在"),
    ],
)
def test_cloud_gates_make_zero_requests_and_write_zero_audits(
    raw_config: dict, tmp_path: Path, gate: str, expected: str
) -> None:
    config, database, review_id = _service_setup(
        raw_config,
        tmp_path,
        enabled=gate != "disabled",
        whitelist=() if gate == "whitelist" else ("daily_review",),
        key_configured=gate != "key",
    )
    client = FakeCloudClient()
    service = CloudReviewService(config, database, client=client)
    confirmation = (
        "错误确认" if gate == "confirmation" else CLOUD_REVIEW_CONFIRMATION
    )
    target_review = 9999 if gate == "missing_review" else review_id

    with pytest.raises(ConfigError, match=expected):
        service.run(target_review, confirmation=confirmation)

    assert client.payloads == []
    assert _calls(database) == []


def test_cloud_success_persists_one_audited_request_and_blocks_repeat(
    raw_config: dict, tmp_path: Path
) -> None:
    config, database, review_id = _service_setup(raw_config, tmp_path)
    client = FakeCloudClient()
    service = CloudReviewService(config, database, client=client)
    with database.connect() as connection:
        review_before = connection.execute(
            "SELECT input_json, local_report, data_status FROM review_runs WHERE id = ?",
            (review_id,),
        ).fetchone()

    outcome = service.run(review_id, confirmation=CLOUD_REVIEW_CONFIRMATION)

    assert outcome.status == "succeeded"
    assert outcome.request_count == 1
    assert outcome.result_text == VALID_TEXT
    assert len(client.payloads) == 1
    assert client.payloads[0]["model"] == "offline-cloud-test"
    user_content = client.payloads[0]["messages"][1]["content"]
    assert "本地复盘文本" in user_content and "结构化事实" in user_content
    calls = _calls(database)
    assert len(calls) == 1
    call = calls[0]
    assert call["status"] == "succeeded"
    assert call["review_id"] == review_id
    assert call["result_text"] == VALID_TEXT
    assert call["prompt_tokens"] == 20
    assert call["completion_tokens"] == 10
    assert call["total_tokens"] == 30
    assert call["finished_at"] is not None
    request_audit = json.loads(call["request_json"])
    assert "Authorization" not in call["request_json"]
    assert request_audit["messages"] == client.payloads[0]["messages"]
    with database.connect() as connection:
        review_after = connection.execute(
            "SELECT input_json, local_report, data_status FROM review_runs WHERE id = ?",
            (review_id,),
        ).fetchone()
    assert tuple(review_after) == tuple(review_before)

    with pytest.raises(ConfigError, match="不会自动重试"):
        service.run(review_id, confirmation=CLOUD_REVIEW_CONFIRMATION)
    assert len(client.payloads) == 1
    assert len(_calls(database)) == 1


@pytest.mark.parametrize("kind", ["network_error", "timeout", "rate_limited"])
def test_cloud_transport_failure_is_audited_without_retry(
    raw_config: dict, tmp_path: Path, kind: str
) -> None:
    config, database, review_id = _service_setup(raw_config, tmp_path)
    client = FakeCloudClient(CloudError(kind, f"离线模拟失败：{kind}"))
    service = CloudReviewService(config, database, client=client)

    outcome = service.run(review_id, confirmation=CLOUD_REVIEW_CONFIRMATION)

    assert outcome.status == "failed"
    assert outcome.error_type == kind
    assert len(client.payloads) == 1
    call = _calls(database)[0]
    assert call["status"] == "failed"
    assert call["error_type"] == kind
    assert call["result_text"] is None
    assert call["finished_at"] is not None
    with pytest.raises(ConfigError, match="已有云端调用审计"):
        service.run(review_id, confirmation=CLOUD_REVIEW_CONFIRMATION)
    assert len(client.payloads) == 1


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("事实：已保存。\n推测：无。", "后续观察"),
        (VALID_TEXT + "\n建议买入。", "交易指令"),
        (VALID_TEXT + "\n保证收益。", "收益承诺"),
    ],
)
def test_invalid_cloud_text_is_rejected_but_response_audit_is_preserved(
    raw_config: dict, tmp_path: Path, text: str, reason: str
) -> None:
    response = CloudResponse(text=text, model="fake", raw_json=json.dumps({"text": text}))
    config, database, review_id = _service_setup(raw_config, tmp_path)
    client = FakeCloudClient(response)

    outcome = CloudReviewService(config, database, client=client).run(
        review_id, confirmation=CLOUD_REVIEW_CONFIRMATION
    )

    assert outcome.status == "failed"
    assert outcome.error_type == "validation_failed"
    assert reason in (outcome.error_message or "")
    call = _calls(database)[0]
    assert call["status"] == "failed"
    assert call["result_text"] is None
    assert call["response_text"] == response.raw_json
    assert reason in call["validation_reasons_json"]


def test_cloud_success_audit_and_result_never_persist_dummy_api_key(
    raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    secret = "dummy-success-key-7f3b"
    monkeypatch.setenv("PANDENG_CLOUD_API_KEY", secret)
    text = f"事实：测试脱敏 {secret}。\n推测：无。\n后续观察：检查审计。"
    response = CloudResponse(
        text=text,
        model=f"model-{secret}",
        raw_json=json.dumps({"echo": secret, "content": text}),
    )
    config, database, review_id = _service_setup(raw_config, tmp_path)

    outcome = CloudReviewService(config, database, client=FakeCloudClient(response)).run(
        review_id, confirmation=CLOUD_REVIEW_CONFIRMATION
    )

    assert outcome.status == "succeeded"
    assert secret not in (outcome.result_text or "")
    serialized = json.dumps([dict(row) for row in _calls(database)], ensure_ascii=False)
    assert secret not in serialized
    assert "[已隐藏]" in serialized


def test_cloud_error_audit_never_persists_dummy_api_key(
    raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    secret = "dummy-error-key-9c21"
    monkeypatch.setenv("PANDENG_CLOUD_API_KEY", secret)
    config, database, review_id = _service_setup(raw_config, tmp_path)
    client = FakeCloudClient(CloudError("network_error", f"模拟错误包含 {secret}"))

    outcome = CloudReviewService(config, database, client=client).run(
        review_id, confirmation=CLOUD_REVIEW_CONFIRMATION
    )

    assert outcome.status == "failed"
    assert secret not in (outcome.error_message or "")
    serialized = json.dumps([dict(row) for row in _calls(database)], ensure_ascii=False)
    assert secret not in serialized
    assert "[已隐藏]" in serialized


def test_cloud_refuses_local_input_that_contains_current_dummy_key(
    raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    secret = "dummy-input-key-441a"
    monkeypatch.setenv("PANDENG_CLOUD_API_KEY", secret)
    config, database, review_id = _service_setup(raw_config, tmp_path)
    with database.connect() as connection:
        connection.execute(
            "UPDATE review_runs SET local_report = ? WHERE id = ?", (secret, review_id)
        )
    client = FakeCloudClient()

    with pytest.raises(ConfigError, match="包含当前 API Key"):
        CloudReviewService(config, database, client=client).run(
            review_id, confirmation=CLOUD_REVIEW_CONFIRMATION
        )
    assert client.payloads == []
    assert _calls(database) == []


class _LocalHandler(BaseHTTPRequestHandler):
    records: list[dict[str, Any]] = []
    status = 200
    response_body: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).records.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "json": json.loads(body.decode("utf-8")),
        })
        payload = json.dumps(type(self).response_body).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _run_local_server(status: int, body: dict[str, Any]):
    handler = type("LocalTestHandler", (_LocalHandler,), {
        "records": [], "status": status, "response_body": body,
    })
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, handler


def test_openai_compatible_client_uses_loopback_dummy_server(
    raw_config: dict, tmp_path: Path, monkeypatch
) -> None:
    body = {
        "model": "loopback-model",
        "choices": [{"message": {"content": VALID_TEXT}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13},
    }
    server, thread, handler = _run_local_server(200, body)
    try:
        raw_config["models"]["cloud"] = {
            "enabled": True,
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "model": "configured-model",
            "event_whitelist": ["daily_review"],
        }
        monkeypatch.setenv("PANDENG_CLOUD_API_KEY", "dummy-loopback-key")
        config = parse_config(raw_config, tmp_path)
        payload = {
            "model": "configured-model",
            "messages": [{"role": "user", "content": "仅本机回环测试"}],
            "stream": False,
        }

        response = OpenAICompatibleCloudClient(config).generate(payload)

        assert response.text == VALID_TEXT
        assert response.model == "loopback-model"
        assert response.total_tokens == 13
        assert len(handler.records) == 1
        record = handler.records[0]
        assert record["path"] == "/v1/chat/completions"
        assert record["authorization"] == "Bearer dummy-loopback-key"
        assert record["json"] == payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("status", "kind"),
    [(400, "parameter_error"), (401, "authentication_failed"),
     (403, "permission_denied"), (429, "rate_limited"), (500, "http_error")],
)
def test_openai_compatible_client_classifies_loopback_http_errors(
    raw_config: dict, tmp_path: Path, monkeypatch, status: int, kind: str
) -> None:
    server, thread, handler = _run_local_server(status, {"error": "offline test"})
    try:
        raw_config["models"]["cloud"] = {
            "enabled": True,
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "model": "configured-model",
            "event_whitelist": ["daily_review"],
        }
        monkeypatch.setenv("PANDENG_CLOUD_API_KEY", "dummy-http-error-key")
        config = parse_config(raw_config, tmp_path)

        with pytest.raises(CloudError) as caught:
            OpenAICompatibleCloudClient(config).generate({"messages": []})
        assert caught.value.kind == kind
        assert len(handler.records) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_cloud_review_cli_dispatches_once_and_status_reads_audit(
    raw_config: dict, tmp_path: Path, monkeypatch, capsys
) -> None:
    raw_config["storage"]["database"] = "runtime/cloud-cli.db"
    raw_config["watchlist"] = [{"code": "600000", "name": "云端门控"}]
    raw_config["models"]["ollama"]["enabled"] = False
    raw_config["models"]["cloud"] = {
        "enabled": True,
        "base_url": "http://127.0.0.1:9/v1",
        "model": "cli-offline-model",
        "event_whitelist": ["daily_review"],
    }
    monkeypatch.setenv("PANDENG_CLOUD_API_KEY", "dummy-cli-key")
    path = tmp_path / "configs" / "cloud.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(raw_config, allow_unicode=True), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_generate(self, payload: dict[str, Any]) -> CloudResponse:
        calls.append(payload)
        return CloudResponse(text=VALID_TEXT, model="cli-fake", raw_json='{"ok": true}')

    monkeypatch.setattr(OpenAICompatibleCloudClient, "generate", fake_generate)
    base = ["--config", str(path)]
    assert main([*base, "init"]) == 0
    assert main([
        *base, "review-daily", "--session-date", DAY.isoformat(), "--code", "600000"
    ]) == 0
    capsys.readouterr()

    assert main([
        *base, "cloud-review", "--review-id", "1",
        "--confirmation", CLOUD_REVIEW_CONFIRMATION,
    ]) == 0
    output = capsys.readouterr().out
    assert "实际请求 1 次；自动重试 0 次" in output
    assert VALID_TEXT in output
    assert len(calls) == 1

    assert main([
        *base, "cloud-review", "--review-id", "1",
        "--confirmation", CLOUD_REVIEW_CONFIRMATION,
    ]) == 1
    assert "不会自动重试" in capsys.readouterr().out
    assert len(calls) == 1
    assert main([*base, "review-status", "--review-id", "1"]) == 0
    status = capsys.readouterr().out
    assert "状态 succeeded" in status
    assert "云端研究记录" in status
    assert "dummy-cli-key" not in status
