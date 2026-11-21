"""观察摘要：SQLite 限量取事实，Ollama 只读整理，异常回退 Python 模板。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from pandeng.config.schema import OllamaConfig
from pandeng.desktop.settings import DesktopSettings
from pandeng.storage.db import Database
from pandeng.summarizer.ollama import OllamaClient, OllamaResponse, separate_r1_output


@dataclass(frozen=True)
class ObservationSummaryResult:
    text: str
    used_model: bool
    note: str


class ObservationSummaryService:
    def __init__(
        self,
        database: Database,
        settings_provider: Callable[[], DesktopSettings],
        debug_dir: Path,
        client_factory: Callable[[OllamaConfig], OllamaClient] = OllamaClient,
    ) -> None:
        self.database = database
        self.settings_provider = settings_provider
        self.debug_dir = debug_dir
        self.client_factory = client_factory

    def _facts(self) -> list[dict[str, object]]:
        codes = tuple(
            item.code for item in self.settings_provider().stocks if item.enabled
        )[:10]
        facts: list[dict[str, object]] = []
        with self.database.connect() as connection:
            for code in codes:
                row = connection.execute(
                    """
                    SELECT stock_code, bar_time, close, source, confirmed
                    FROM minute_bars WHERE stock_code = ?
                    ORDER BY bar_time DESC LIMIT 1
                    """,
                    (code,),
                ).fetchone()
                if row is None:
                    facts.append({"stock_code": code, "status": "missing"})
                    continue
                revisions = connection.execute(
                    """
                    SELECT COUNT(*) FROM data_quality_issues
                    WHERE stock_code = ? AND issue_type = 'unconfirmed_revision'
                    """,
                    (code,),
                ).fetchone()[0]
                facts.append({
                    "stock_code": row["stock_code"],
                    "bar_time": row["bar_time"],
                    "close": f"{row['close']:.2f}",
                    "source": row["source"],
                    "confirmed": bool(row["confirmed"]),
                    "revision_audits": revisions,
                })
        return facts

    @staticmethod
    def _template(facts: list[dict[str, object]], language: str = "zh-CN") -> str:
        english = language == "en"
        lines = [
            "Observation facts (prepared by local Python)" if english
            else "观察事实摘要（由本地 Python 整理）"
        ]
        for fact in facts:
            if "status" in fact:
                lines.append(
                    f"{fact['stock_code']}: no saved minute bars."
                    if english else f"{fact['stock_code']}：暂无已保存分钟。"
                )
            elif english:
                lines.append(
                    f"{fact['stock_code']}: market time {fact['bar_time']}; close {fact['close']}; "
                    f"{'confirmed' if fact['confirmed'] else 'unconfirmed'}; source {fact['source']}; "
                    f"revision audits {fact['revision_audits']}."
                )
            else:
                lines.append(
                    f"{fact['stock_code']}：行情时间 {fact['bar_time']}；收盘 {fact['close']}；"
                    f"{'已确认' if fact['confirmed'] else '未确认'}；来源 {fact['source']}；"
                    f"修订审计 {fact['revision_audits']} 条。"
                )
        lines.append(
            "Public-data observation only; not investment advice." if english
            else "以上仅为公开数据观察记录，不构成投资建议。"
        )
        return "\n".join(lines)

    def generate(self) -> ObservationSummaryResult:
        settings = self.settings_provider()
        facts = self._facts()
        template = self._template(facts, settings.language)
        if not settings.ollama_enabled:
            note = (
                "Model disabled; fixed template used" if settings.language == "en"
                else "模型已停用，本次使用固定模板"
            )
            return ObservationSummaryResult(template, False, note)
        fact_lines = [line for line in template.splitlines()[1:-1]]
        if settings.language == "en":
            style = {
                "brief": "Summarize briefly in no more than 180 characters",
                "detailed": "List each stock in detail in no more than 500 characters",
                "facts_only": "Only restate each fact without interpretation",
            }[settings.output_style]
            prompt = (
                "You are a local public-market summary tool. Use only the facts below. "
                "Do not add news, predictions, trading advice, indicators, or holdings conclusions. "
                "Preserve codes, values, sources, and confirmation states; copy timestamps exactly.\n"
                f"{style}. If data is missing, state that clearly.\n\n" + "\n".join(fact_lines)
            )
        else:
            style = {
                "brief": "用不超过 180 字简洁概括",
                "detailed": "分股票详细列出，不超过 500 字",
                "facts_only": "只逐项复述事实，不作解释",
            }[settings.output_style]
            prompt = (
                "你是本地公开行情摘要工具。只能使用下面给出的事实，不得补充新闻、预测、"
                "买卖建议、指标或持仓结论。保留代码、数值、来源和确认状态；时间原样复制。\n"
                f"请{style}。若没有数据，明确写暂无数据。\n\n" + "\n".join(fact_lines)
            )
        config = OllamaConfig(
            enabled=True,
            base_url=settings.ollama_base_url,
            model=settings.model_name,
            timeout_seconds=settings.ollama_timeout_seconds,
        )
        try:
            response = self.client_factory(config).generate(prompt)
            text, thinking = separate_r1_output(response.text, response.thinking)
            if not text or len(text) > 4000:
                raise ValueError("模型摘要为空或超过 4000 字上限。")
            self._save_debug(settings, prompt, response, thinking)
            note = (
                "Ollama summary completed; the model did not modify the database"
                if settings.language == "en" else "Ollama 摘要完成；数据库未被模型修改"
            )
            return ObservationSummaryResult(text, True, note)
        except Exception as exc:
            # 模型和调试文件失败都不能影响原始行情；固定模板始终可用。
            note = (
                f"Model output not used ({type(exc).__name__}); fixed template restored"
                if settings.language == "en"
                else f"模型未采用（{type(exc).__name__}），已回退固定模板"
            )
            return ObservationSummaryResult(template, False, note)

    def _save_debug(
        self,
        settings: DesktopSettings,
        prompt: str,
        response: OllamaResponse,
        thinking: str,
    ) -> None:
        if not any((
            settings.save_prompt_debug,
            settings.save_raw_response_debug,
            settings.save_thinking_debug,
        )):
            return
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        stem = datetime.now().strftime("observation-%Y%m%d-%H%M%S-%f")
        outputs = (
            (settings.save_prompt_debug, ".prompt.txt", prompt),
            (settings.save_raw_response_debug, ".response.json", response.raw_json),
            (settings.save_thinking_debug, ".thinking.txt", thinking),
        )
        for enabled, suffix, content in outputs:
            if enabled:
                (self.debug_dir / (stem + suffix)).write_text(content, encoding="utf-8")
