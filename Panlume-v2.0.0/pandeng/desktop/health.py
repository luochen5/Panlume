"""本机 Ollama 只读健康检查；不生成文本、不调用云端。"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class OllamaHealth:
    online: bool
    models: tuple[str, ...]
    message: str


def check_ollama(
    base_url: str = "http://127.0.0.1:11434",
    timeout: float = 3.0,
    *,
    language: str = "zh-CN",
) -> OllamaHealth:
    request = urllib.request.Request(base_url.rstrip("/") + "/api/tags", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                message = (
                    f"Local service returned HTTP {response.status}."
                    if language == "en" else f"本机服务返回 HTTP {response.status}。"
                )
                return OllamaHealth(False, (), message)
            payload = json.loads(response.read(1_000_000).decode("utf-8"))
        models = payload.get("models", [])
        if not isinstance(models, list):
            raise ValueError("models 字段格式异常")
        names = tuple(
            str(item["name"]) for item in models
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )
        message = (
            f"Local Ollama is online; {len(names)} models found."
            if language == "en" else f"本机 Ollama 在线，发现 {len(names)} 个模型。"
        )
        return OllamaHealth(True, names, message)
    except Exception as exc:
        message = (
            f"Could not connect to local Ollama: {type(exc).__name__}: {exc}"
            if language == "en" else f"无法连接本机 Ollama：{type(exc).__name__}: {exc}"
        )
        return OllamaHealth(False, (), message)
