from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pandeng.config.schema import OllamaConfig
from pandeng.summarizer.ollama import OllamaClient, OllamaError, separate_r1_output


def test_r1_thinking_is_separate_and_broken_tags_are_rejected() -> None:
    final, thinking = separate_r1_output(
        "前言<think>第一段</think>600000 最终摘要", "独立思考通道"
    )
    assert final == "前言600000 最终摘要"
    assert "独立思考通道" in thinking and "第一段" in thinking

    with pytest.raises(OllamaError, match="没有结束标签"):
        separate_r1_output("<think>没有结束")
    with pytest.raises(OllamaError, match="错位"):
        separate_r1_output("最终</think>")


def test_standard_library_client_parses_local_json_and_metrics() -> None:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            length = int(self.headers["Content-Length"])
            requests.append(json.loads(self.rfile.read(length)))
            body = json.dumps({
                "model": "deepseek-r1:14b",
                "response": "<think>推理</think>600000 最终摘要",
                "thinking": "独立通道",
                "total_duration": 123,
                "load_duration": 45,
                "eval_count": 7,
                "eval_duration": 89,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = OllamaConfig(
            enabled=True,
            base_url=f"http://127.0.0.1:{server.server_port}",
            model="deepseek-r1:14b",
            timeout_seconds=2,
        )
        response = OllamaClient(config).generate("只使用测试提示词")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert response.text.startswith("<think>")
    assert response.total_duration_ns == 123 and response.load_duration_ns == 45
    assert response.eval_count == 7 and response.eval_duration_ns == 89
    assert requests[0]["stream"] is False
    assert requests[0]["model"] == "deepseek-r1:14b"
    assert requests[0]["prompt"] == "只使用测试提示词"
