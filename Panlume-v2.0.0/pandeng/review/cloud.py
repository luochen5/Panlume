"""OpenAI 兼容云端的一次性手动复盘门控；密钥只存在于请求头内存中。"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pandeng.config.schema import AppConfig, ConfigError
from pandeng.storage.db import Database

from .service import load_review


CLOUD_REVIEW_CONFIRMATION = "确认对指定本地复盘执行一次云端请求并保存审计"
CLOUD_REVIEW_EVENT = "daily_review"
MAX_CLOUD_RESPONSE_BYTES = 2_000_000
MAX_CLOUD_REQUEST_CHARS = 100_000
MAX_CLOUD_RESULT_CHARS = 6_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CloudError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class CloudResponse:
    text: str
    model: str
    raw_json: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class CloudReviewOutcome:
    call_id: int
    review_id: int
    status: str
    result_text: str | None
    error_type: str | None
    error_message: str | None
    request_count: int = 1


def _optional_int(body: dict[str, Any], key: str) -> int | None:
    value = body.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class OpenAICompatibleCloudClient:
    """只有显式 cloud-review 才会实例化并调用此客户端。"""

    def __init__(self, config: AppConfig, *, timeout_seconds: int = 60) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    def generate(self, request_payload: dict[str, Any]) -> CloudResponse:
        api_key = os.environ.get("PANDENG_CLOUD_API_KEY", "").strip()
        if not api_key:
            raise CloudError("authentication_failed", "云端 API Key 未配置，未发起 HTTP 请求。")
        base_url = self.config.cloud.base_url.rstrip("/")
        url = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(
                request_payload, ensure_ascii=False, allow_nan=False
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "pandeng/0.6",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(MAX_CLOUD_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            kind = {
                400: "parameter_error", 401: "authentication_failed",
                403: "permission_denied", 404: "parameter_error",
                422: "parameter_error", 429: "rate_limited",
            }.get(exc.code, "http_error")
            raise CloudError(kind, f"云端返回 HTTP {exc.code}，分类为 {kind}。") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise CloudError("timeout", "等待云端响应超时。") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise CloudError("timeout", "等待云端响应超时。") from exc
            raise CloudError("network_error", f"无法连接云端：{reason}") from exc
        except OSError as exc:
            raise CloudError("network_error", f"云端网络读取失败：{exc}") from exc
        if len(raw) > MAX_CLOUD_RESPONSE_BYTES:
            raise CloudError("response_format", "云端响应超过 2 MB 安全上限。")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloudError("response_format", "云端响应不是有效 UTF-8 JSON。") from exc
        if not isinstance(body, dict):
            raise CloudError("response_format", "云端 JSON 根节点必须是对象。")
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CloudError(
                "response_format", "云端 JSON 缺少 choices[0].message.content。"
            ) from exc
        if not isinstance(content, str):
            raise CloudError("response_format", "云端消息 content 必须是字符串。")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        model = body.get("model", self.config.cloud.model)
        if not isinstance(model, str) or not model.strip():
            model = self.config.cloud.model
        return CloudResponse(
            text=content,
            model=model,
            raw_json=json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False),
            prompt_tokens=_optional_int(usage, "prompt_tokens"),
            completion_tokens=_optional_int(usage, "completion_tokens"),
            total_tokens=_optional_int(usage, "total_tokens"),
        )


def _validate_result(text: str) -> tuple[str, ...]:
    stripped = text.strip()
    if not stripped:
        return ("云端结果为空。",)
    reasons: list[str] = []
    if len(stripped) > MAX_CLOUD_RESULT_CHARS:
        reasons.append(f"云端结果超过 {MAX_CLOUD_RESULT_CHARS} 字符。")
    for heading in ("事实：", "推测：", "后续观察："):
        if heading not in stripped:
            reasons.append(f"云端结果缺少“{heading}”部分。")
    forbidden = (
        "买入", "卖出", "加仓", "减仓", "清仓", "满仓",
        "自动下单", "保证收益", "稳赚", "包赚",
    )
    if any(term in stripped for term in forbidden):
        reasons.append("云端结果包含交易指令或收益承诺用语，已拒绝作为研究结论保存。")
    return tuple(reasons)


def _request_payload(review: Any, model: str) -> dict[str, Any]:
    local_report = str(review["local_report"])
    facts = json.loads(review["input_json"])
    user_content = (
        "请只根据以下已保存材料整理研究记录，不补充新闻或输入外数字，不预测确定收益，"
        "不提供交易指令。输出必须依次包含“事实：”“推测：”“后续观察：”三个部分；"
        "没有依据的推测请写“无”。云端文字不会修改本地程序结论。\n"
        f"本地复盘文本：\n{local_report}\n"
        f"结构化事实：{json.dumps(facts, ensure_ascii=False, sort_keys=True, allow_nan=False)}"
    )
    return {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是研究记录整理器，只区分事实、推测与后续观察，不执行任何外部操作。",
            },
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "temperature": 0.1,
        "max_tokens": 1200,
    }


class CloudReviewService:
    """总开关、daily_review 白名单、逐字确认和单次审计缺一不可。"""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        *,
        client: Any | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.client = client if client is not None else OpenAICompatibleCloudClient(config)

    @staticmethod
    def _scrub(message: str) -> str:
        secret = os.environ.get("PANDENG_CLOUD_API_KEY", "").strip()
        return message.replace(secret, "[已隐藏]") if secret else message

    def run(self, review_id: int, *, confirmation: str) -> CloudReviewOutcome:
        if confirmation != CLOUD_REVIEW_CONFIRMATION:
            raise ConfigError("云端复盘确认文本不匹配；未调用云端，也未写入调用审计。")
        if not self.config.cloud.enabled:
            raise ConfigError("云端总开关关闭；未调用云端。默认本地复盘不需要云端。")
        if CLOUD_REVIEW_EVENT not in self.config.cloud.event_whitelist:
            raise ConfigError("云端白名单未包含 daily_review；未调用云端。")
        if not self.config.cloud.api_key_configured:
            raise ConfigError("云端 API Key 未配置；未调用云端。")
        review = load_review(self.database, review_id)
        if review is None:
            raise ConfigError(f"本地复盘 {review_id} 不存在；未调用云端。")
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT id, status FROM cloud_review_calls WHERE review_id = ?", (review_id,)
            ).fetchone()
        if existing is not None:
            raise ConfigError(
                f"本地复盘 {review_id} 已有云端调用审计 {existing['id']}（{existing['status']}）；"
                "不会自动重试或重复收费。"
            )

        payload = _request_payload(review, self.config.cloud.model)
        request_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if len(request_json) > MAX_CLOUD_REQUEST_CHARS:
            raise ConfigError("云端复盘输入超过安全上限；未调用云端。")
        configured_secret = os.environ.get("PANDENG_CLOUD_API_KEY", "").strip()
        if configured_secret and configured_secret in request_json:
            raise ConfigError(
                "本地复盘输入包含当前 API Key；未调用云端，也未写入调用审计。"
            )
        started_at = _now()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cloud_review_calls(
                    review_id, provider, model_name, status, request_json, started_at
                ) VALUES (?, 'openai_compatible', ?, 'running', ?, ?)
                """,
                (review_id, self.config.cloud.model, request_json, started_at),
            )
            call_id = int(cursor.lastrowid)

        started = time.monotonic()
        try:
            response = self.client.generate(payload)
        except CloudError as exc:
            return self._failed(call_id, review_id, started, exc.kind, str(exc))
        except Exception as exc:
            return self._failed(
                call_id, review_id, started, "client_error", f"{type(exc).__name__}: {exc}"
            )

        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        safe_response_text = self._scrub(response.text)
        safe_raw_json = self._scrub(response.raw_json)
        safe_model = self._scrub(response.model)
        reasons = _validate_result(safe_response_text)
        if reasons:
            message = "；".join(reasons)
            with self.database.connect() as connection:
                connection.execute(
                    """
                    UPDATE cloud_review_calls
                    SET status = 'failed', model_name = ?, response_text = ?,
                        validation_reasons_json = ?, error_type = 'validation_failed',
                        error_message = ?, elapsed_ms = ?, prompt_tokens = ?,
                        completion_tokens = ?, total_tokens = ?, finished_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (
                        safe_model, safe_raw_json,
                        json.dumps(reasons, ensure_ascii=False), self._scrub(message), elapsed_ms,
                        response.prompt_tokens, response.completion_tokens, response.total_tokens,
                        _now(), call_id,
                    ),
                )
            return CloudReviewOutcome(
                call_id, review_id, "failed", None, "validation_failed", message
            )

        result_text = safe_response_text.strip()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE cloud_review_calls
                SET status = 'succeeded', model_name = ?, response_text = ?,
                    result_text = ?, elapsed_ms = ?, prompt_tokens = ?,
                    completion_tokens = ?, total_tokens = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    safe_model, safe_raw_json, result_text, elapsed_ms,
                    response.prompt_tokens, response.completion_tokens, response.total_tokens,
                    _now(), call_id,
                ),
            )
        return CloudReviewOutcome(call_id, review_id, "succeeded", result_text, None, None)

    def _failed(
        self, call_id: int, review_id: int, started: float, kind: str, message: str
    ) -> CloudReviewOutcome:
        safe_message = self._scrub(message)
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE cloud_review_calls
                SET status = 'failed', error_type = ?, error_message = ?,
                    elapsed_ms = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    kind, safe_message, max(0, int((time.monotonic() - started) * 1000)),
                    _now(), call_id,
                ),
            )
        return CloudReviewOutcome(call_id, review_id, "failed", None, kind, safe_message)
