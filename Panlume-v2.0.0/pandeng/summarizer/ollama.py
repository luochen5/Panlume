"""Ollama 标准库 HTTP 客户端与 DeepSeek-R1 思考段分离。"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from pandeng.config.schema import OllamaConfig


class OllamaError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class OllamaResponse:
    text: str
    thinking: str
    model: str
    raw_json: str
    total_duration_ns: int | None = None
    load_duration_ns: int | None = None
    eval_count: int | None = None
    eval_duration_ns: int | None = None


def separate_r1_output(response: str, thinking: str = "") -> tuple[str, str]:
    """返回最终文本与思考文本；未闭合或错位的思考标签视为无效响应。"""
    final = response
    thoughts = [thinking.strip()] if thinking.strip() else []
    while "<think>" in final:
        start = final.find("<think>")
        end = final.find("</think>", start + len("<think>"))
        if end < 0:
            raise OllamaError("invalid_response", "R1 思考段没有结束标签，最终摘要不可信。")
        thoughts.append(final[start + len("<think>"):end].strip())
        final = final[:start] + final[end + len("</think>"):]
    if "</think>" in final or "<think>" in final:
        raise OllamaError("invalid_response", "R1 思考标签错位，最终摘要不可信。")
    return final.strip(), "\n".join(item for item in thoughts if item)


def _optional_int(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


class OllamaClient:
    """每次调用只访问配置中的 Ollama 地址，不启动或结束任何模型进程。"""

    def __init__(self, config: OllamaConfig) -> None:
        self.config = config

    def generate(self, prompt: str) -> OllamaResponse:
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        request = urllib.request.Request(
            self.config.base_url + "/api/generate",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "pandeng/0.4"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(2_000_001)
        except urllib.error.HTTPError as exc:
            raise OllamaError("http_error", f"Ollama 返回 HTTP {exc.code}。") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise OllamaError("timeout", "等待 Ollama 响应超时。") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise OllamaError("timeout", "等待 Ollama 响应超时。") from exc
            raise OllamaError("network_error", f"无法连接 Ollama：{reason}") from exc
        except OSError as exc:
            raise OllamaError("network_error", f"Ollama 网络读取失败：{exc}") from exc
        if len(raw) > 2_000_000:
            raise OllamaError("invalid_response", "Ollama 响应超过 2 MB 安全上限。")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OllamaError("invalid_json", "Ollama 响应不是有效 UTF-8 JSON。") from exc
        if not isinstance(body, dict) or not isinstance(body.get("response"), str):
            raise OllamaError("invalid_response", "Ollama JSON 缺少字符串 response 字段。")
        thinking = body.get("thinking", "")
        if not isinstance(thinking, str):
            raise OllamaError("invalid_response", "Ollama thinking 字段必须是字符串。")
        model = body.get("model", self.config.model)
        if not isinstance(model, str):
            model = self.config.model
        return OllamaResponse(
            text=body["response"],
            thinking=thinking,
            model=model,
            raw_json=json.dumps(body, ensure_ascii=False, sort_keys=True),
            total_duration_ns=_optional_int(body, "total_duration"),
            load_duration_ns=_optional_int(body, "load_duration"),
            eval_count=_optional_int(body, "eval_count"),
            eval_duration_ns=_optional_int(body, "eval_duration"),
        )
