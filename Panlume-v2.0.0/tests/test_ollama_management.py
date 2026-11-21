from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from pandeng.summarizer.management import OllamaManagementClient
from pandeng.summarizer.ollama import OllamaError


def serve(handler_class):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def stop(server, thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_detect_lists_model_size_parameters_quantization_and_version() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = {"version": "0.12.0"} if self.path == "/api/version" else {
                "models": [{
                    "name": "qwen3:4b", "size": 4_294_967_296,
                    "digest": "abc", "modified_at": "2026-01-01T00:00:00Z",
                    "details": {"parameter_size": "4B", "quantization_level": "Q4_K_M"},
                }, {"name": "custom", "details": {}}],
            }
            raw = json.dumps(body).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args) -> None:
            return

    server, thread = serve(Handler)
    try:
        result = OllamaManagementClient(f"http://127.0.0.1:{server.server_port}").detect()
    finally:
        stop(server, thread)

    assert result.online is True and result.version == "0.12.0"
    assert result.models[0].size_gib == 4.0
    assert result.models[0].parameter_size_b == 4.0
    assert result.models[0].recommended_tier == "tier1"
    assert result.models[0].quantization == "Q4_K_M"
    assert result.models[0].generation == ""
    assert result.models[1].parameter_size_b is None
    assert result.models[1].recommended_tier == "unknown"


def test_show_and_delete_use_official_api_with_explicit_model_name() -> None:
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.command, self.path, payload))
            raw = b'{"model_info": {"parameter_size": "8B"}}'
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def do_DELETE(self) -> None:  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.command, self.path, payload))
            raw = b'{}'
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def log_message(self, *args) -> None:
            return

    server, thread = serve(Handler)
    try:
        client = OllamaManagementClient(f"http://127.0.0.1:{server.server_port}")
        shown = client.show_model("qwen3:8b")
        client.delete_model("qwen3:8b")
    finally:
        stop(server, thread)

    assert shown["model_info"]["parameter_size"] == "8B"
    assert calls == [
        ("POST", "/api/show", {"model": "qwen3:8b"}),
        ("DELETE", "/api/delete", {"model": "qwen3:8b"}),
    ]


def test_pull_uses_real_progress_and_marks_unknown_total() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            raw = (
                b'{"status":"pulling","completed":50,"total":100}\n'
                b'{"status":"pulling","completed":50}\n'
                b'{"status":"success"}\n'
            )
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def log_message(self, *args) -> None:
            return

    server, thread = serve(Handler)
    try:
        progress = list(OllamaManagementClient(f"http://127.0.0.1:{server.server_port}").pull("qwen3:4b"))
    finally:
        stop(server, thread)

    assert progress[0].fraction == 0.5
    assert progress[1].total_bytes is None and progress[1].fraction is None
    assert progress[-1].status == "success"


def test_pull_failure_is_not_retried() -> None:
    raw = io.BytesIO(b'{"status":"pulling","error":"download denied"}\n')
    with patch("urllib.request.urlopen", return_value=raw) as request:
        client = OllamaManagementClient("http://127.0.0.1:11434")
        with pytest.raises(OllamaError, match="download denied"):
            list(client.pull("missing:4b"))

    assert request.call_count == 1


def test_cancel_download_stops_iteration_and_restores_client_for_later_explicit_use() -> None:
    first_sent = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            raw = b'{"status":"pulling","completed":1,"total":100}\n'
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers()
            self.wfile.write(raw); self.wfile.flush(); first_sent.set()
            release.wait(timeout=2)

        def log_message(self, *args) -> None:
            return

    server, thread = serve(Handler)
    client = OllamaManagementClient(f"http://127.0.0.1:{server.server_port}", timeout_seconds=1)
    try:
        stream = client.pull("qwen3:4b")
        first = next(stream)
        assert first.completed_bytes == 1 and first_sent.wait(timeout=1)
        client.cancel_download()
        with pytest.raises(OllamaError) as caught:
            next(stream)
        assert caught.value.kind == "cancelled"
    finally:
        release.set()
        stop(server, thread)


def test_offline_detection_returns_failure_without_model_or_retry() -> None:
    result = OllamaManagementClient("http://127.0.0.1:9", timeout_seconds=0.1).detect()
    assert result.online is False
    assert result.models == ()
    assert result.message



def test_pull_without_success_is_incomplete_not_fake_completion() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            raw = b'{"status":"pulling","completed":100,"total":100}\n'
            self.send_response(200); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def log_message(self, *args) -> None:
            return

    server, thread = serve(Handler)
    try:
        client = OllamaManagementClient(f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(OllamaError) as caught:
            list(client.pull("qwen3:4b"))
        assert caught.value.kind == "incomplete_download"
    finally:
        stop(server, thread)
