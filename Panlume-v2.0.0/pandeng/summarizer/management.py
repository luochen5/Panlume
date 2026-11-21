"""Ollama 正式 HTTP 管理能力；不读写 blobs、manifests 或模型进程。"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

from pandeng.reporting.models import infer_parameter_size_b, recommended_tier

from .ollama import OllamaError


@dataclass(frozen=True)
class OllamaModelInfo:
    name: str
    size_bytes: int | None
    parameter_size_b: float | None
    recommended_tier: str
    digest: str
    modified_at: str
    quantization: str
    generation: str = ""

    @property
    def size_gib(self) -> float | None:
        return None if self.size_bytes is None else self.size_bytes / (1024 ** 3)


@dataclass(frozen=True)
class OllamaInstallation:
    installed: bool
    executable_path: str
    online: bool
    version: str
    models: tuple[OllamaModelInfo, ...]
    message: str


@dataclass(frozen=True)
class PullProgress:
    status: str
    completed_bytes: int | None
    total_bytes: int | None
    digest: str

    @property
    def fraction(self) -> float | None:
        if self.completed_bytes is None or not self.total_bytes or self.total_bytes <= 0:
            return None
        return min(1.0, max(0.0, self.completed_bytes / self.total_bytes))


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class OllamaManagementClient:
    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._response = None

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json", "User-Agent": "pandeng/2.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
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
        if not isinstance(body, dict):
            raise OllamaError("invalid_response", "Ollama JSON 根节点必须是对象。")
        if isinstance(body.get("error"), str):
            raise OllamaError("operation_failed", body["error"])
        return body

    def version(self) -> str:
        body = self._request("GET", "/api/version")
        version = body.get("version", "")
        return version if isinstance(version, str) else ""

    def list_models(self) -> tuple[OllamaModelInfo, ...]:
        body = self._request("GET", "/api/tags")
        raw_models = body.get("models")
        if not isinstance(raw_models, list):
            raise OllamaError("invalid_response", "Ollama models 字段必须是数组。")
        models = []
        for item in raw_models:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                raise OllamaError("invalid_response", "Ollama 模型记录缺少有效名称。")
            details = item.get("details") if isinstance(item.get("details"), dict) else {}
            name = item["name"].strip()
            parameter_size = infer_parameter_size_b(name, details)
            models.append(OllamaModelInfo(
                name=name,
                size_bytes=_optional_int(item.get("size")),
                parameter_size_b=parameter_size,
                recommended_tier=recommended_tier(parameter_size),
                digest=item.get("digest", "") if isinstance(item.get("digest", ""), str) else "",
                modified_at=item.get("modified_at", "") if isinstance(item.get("modified_at", ""), str) else "",
                quantization=details.get("quantization_level", "") if isinstance(details.get("quantization_level", ""), str) else "",
            ))
        return tuple(models)

    def show_model(self, model_name: str) -> dict[str, Any]:
        if not model_name.strip():
            raise ValueError("模型名称不能为空。")
        return self._request("POST", "/api/show", {"model": model_name.strip()})

    def detect(self) -> OllamaInstallation:
        executable = shutil.which("ollama") or ""
        try:
            models = self.list_models()
            version = self.version()
        except OllamaError as exc:
            return OllamaInstallation(
                bool(executable), executable, False, "", (), str(exc),
            )
        return OllamaInstallation(
            True, executable, True, version, models,
            f"Ollama 在线，发现 {len(models)} 个本地模型。",
        )

    def cancel_download(self) -> None:
        self._cancel.set()
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except OSError:
                pass

    def pull(self, model_name: str) -> Iterator[PullProgress]:
        name = model_name.strip()
        if not name:
            raise ValueError("准备下载的模型名称不能为空。")
        self._cancel.clear()
        payload = json.dumps({"model": name, "stream": True}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/api/pull", data=payload, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "pandeng/2.0"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            raise OllamaError("http_error", f"下载返回 HTTP {exc.code}。") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise OllamaError("network_error", f"无法开始 Ollama 下载：{exc}") from exc
        with self._lock:
            if self._response is not None:
                response.close()
                raise OllamaError("download_active", "已有下载任务正在进行。")
            self._response = response
        succeeded = False
        try:
            while True:
                if self._cancel.is_set():
                    raise OllamaError("cancelled", "用户取消了下载；没有自动重试。")
                try:
                    raw = response.readline(65_537)
                except (OSError, ValueError) as exc:
                    if self._cancel.is_set():
                        raise OllamaError("cancelled", "用户取消了下载；没有自动重试。") from exc
                    raise OllamaError("network_error", f"下载进度读取失败：{exc}") from exc
                if len(raw) > 65_536:
                    raise OllamaError("invalid_response", "单行下载进度超过 64 KB 上限。")
                if not raw:
                    break
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise OllamaError("invalid_json", "下载进度不是有效 UTF-8 JSON。") from exc
                if not isinstance(body, dict) or not isinstance(body.get("status"), str):
                    raise OllamaError("invalid_response", "下载进度缺少 status。")
                if isinstance(body.get("error"), str):
                    raise OllamaError("operation_failed", body["error"])
                if body["status"] == "success":
                    succeeded = True
                progress = PullProgress(
                    status=body["status"],
                    completed_bytes=_optional_int(body.get("completed")),
                    total_bytes=_optional_int(body.get("total")),
                    digest=body.get("digest", "") if isinstance(body.get("digest", ""), str) else "",
                )
                yield progress
            if not succeeded:
                raise OllamaError("incomplete_download", "下载连接已结束，但未收到 Ollama 的 success 状态。")
        finally:
            with self._lock:
                self._response = None
            response.close()

    def delete_model(self, model_name: str) -> None:
        name = model_name.strip()
        if not name:
            raise ValueError("准备删除的模型名称不能为空。")
        self._request("DELETE", "/api/delete", {"model": name})
