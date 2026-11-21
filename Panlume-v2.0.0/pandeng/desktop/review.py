"""桌面本地复盘门面：输入校验、历史读取和展示，不创建模型客户端。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Callable

from pandeng.review.service import (
    LocalReviewOutcome,
    create_local_review,
    load_review,
    recent_reviews,
)
from pandeng.storage.db import Database


@dataclass(frozen=True)
class ReviewHistoryItem:
    review_id: int
    session_date: str
    stock_code: str
    data_status: str
    created_at: str


@dataclass(frozen=True)
class DesktopReviewDetail:
    review_id: int
    session_date: str
    stock_code: str
    data_status: str
    created_at: str
    input_hash: str
    report: str
    created: bool | None
    language: str = "zh-CN"

    @property
    def metadata(self) -> str:
        if self.language == "en":
            source = (
                "history" if self.created is None
                else "created now" if self.created else "existing input returned"
            )
            return (
                f"Review {self.review_id} | {source} | data status {self.data_status}\n"
                f"Created {self.created_at}\nInput SHA-256 {self.input_hash}\n"
                "Input facts are stored in review_runs.input_json; model, cloud, and live-market calls: 0."
            )
        source = (
            "历史读取" if self.created is None
            else "本次新建" if self.created
            else "相同输入已存在，幂等返回"
        )
        return (
            f"复盘 {self.review_id}｜{source}｜数据状态 {self.data_status}\n"
            f"创建时间 {self.created_at}\n"
            f"输入 SHA-256 {self.input_hash}\n"
            "输入事实 JSON 已保存在 review_runs.input_json；本次模型、云端和真实行情调用均为 0 次。"
        )


class DesktopReviewService:
    def __init__(
        self,
        database: Database,
        *,
        creator: Callable[..., LocalReviewOutcome] = create_local_review,
        language_provider: Callable[[], str] = lambda: "zh-CN",
    ) -> None:
        self.database = database
        self.creator = creator
        self.language_provider = language_provider

    @staticmethod
    def _arguments(stock_code: str, session_date_text: str) -> tuple[str, date]:
        code = stock_code.strip()
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError("股票代码必须是 6 位数字。")
        text = session_date_text.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError("复盘日期必须使用 YYYY-MM-DD 格式。")
        try:
            session_date = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("复盘日期不是有效日历日期。") from exc
        return code, session_date

    def generate(self, stock_code: str, session_date_text: str) -> DesktopReviewDetail:
        code, session_date = self._arguments(stock_code, session_date_text)
        if self.creator is create_local_review:
            outcome = self.creator(
                self.database, session_date, code, language=self.language_provider()
            )
        else:
            # 既有测试和嵌入方的三参数 creator 契约保持不变。
            outcome = self.creator(self.database, session_date, code)
        return self._detail(
            outcome.review_id, created=outcome.created, language=self.language_provider()
        )

    def history(self, limit: int = 50) -> list[ReviewHistoryItem]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("历史读取数量必须是 1～100 的整数。")
        return [
            ReviewHistoryItem(
                int(row["id"]), row["session_date"], row["stock_code"],
                row["data_status"], row["created_at"],
            )
            for row in recent_reviews(self.database, limit)
        ]

    def load(self, review_id: int) -> DesktopReviewDetail:
        return self._detail(review_id, created=None, language=self.language_provider())

    def _detail(
        self, review_id: int, *, created: bool | None, language: str = "zh-CN"
    ) -> DesktopReviewDetail:
        if isinstance(review_id, bool) or not isinstance(review_id, int) or review_id < 1:
            raise ValueError("复盘编号必须是正整数。")
        row = load_review(self.database, review_id)
        if row is None:
            raise ValueError(f"本地复盘 {review_id} 不存在。")
        return self._from_row(row, created=created, language=language)

    @staticmethod
    def _from_row(
        row: sqlite3.Row, *, created: bool | None, language: str = "zh-CN"
    ) -> DesktopReviewDetail:
        return DesktopReviewDetail(
            review_id=int(row["id"]),
            session_date=row["session_date"],
            stock_code=row["stock_code"],
            data_status=row["data_status"],
            created_at=row["created_at"],
            input_hash=row["input_hash"],
            report=row["local_report"],
            created=created,
            language=language,
        )
