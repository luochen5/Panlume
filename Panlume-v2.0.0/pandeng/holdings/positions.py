"""用户确认的持仓批次、追加审计与按交易日历计算 T+1。"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from pandeng.storage.db import Database


CONFIRM_ADD = "我确认添加该真实持仓批次"
CONFIRM_ARCHIVE = "我确认归档该持仓批次"


class PositionError(ValueError):
    """持仓输入或人工确认不符合要求。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _code(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{6}", value.strip()):
        raise PositionError("股票代码必须是带引号的 6 位数字字符串。")
    return value.strip()


def _quantity(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100_000_000:
        raise PositionError("持仓数量必须是 1～100000000 的整数。")
    if value % 100 != 0:
        raise PositionError("MVP 买入批次数量必须是 100 股的整数倍；零股变化请人工核对。")
    return value


def _date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PositionError("买入日期必须使用 YYYY-MM-DD。") from exc
    return parsed


def _stop(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PositionError("固定止损价必须是大于 0 的数值，或明确填写 null。")
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= 1_000_000:
        raise PositionError("固定止损价必须在 0～1000000 之间。")
    return result


def _row_json(row: Any) -> str:
    return json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str)


def add_position_lot(
    database: Database,
    *,
    stock_code: str,
    quantity: int,
    buy_date: str,
    stop_loss_price: float | None,
    confirmation: str,
    confirmed_at: datetime | None = None,
) -> int:
    """只有专用函数和精确确认文本可以新增真实持仓；模型无此入口。"""
    if confirmation != CONFIRM_ADD:
        raise PositionError(f"未写入：必须逐字确认“{CONFIRM_ADD}”。")
    code = _code(stock_code)
    count = _quantity(quantity)
    purchased = _date(buy_date)
    stop = _stop(stop_loss_price)
    timestamp = (confirmed_at or datetime.now(timezone.utc)).isoformat()
    with database.connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO position_lots(
                stock_code, quantity, buy_date, stop_loss_price, confirmed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (code, count, purchased.isoformat(), stop, timestamp),
        )
        lot_id = int(cursor.lastrowid)
        row = connection.execute("SELECT * FROM position_lots WHERE id = ?", (lot_id,)).fetchone()
        connection.execute(
            """
            INSERT INTO position_audit(
                lot_id, action, before_json, after_json,
                confirmation_text, confirmed_at, created_at
            ) VALUES (?, 'add', NULL, ?, ?, ?, ?)
            """,
            (lot_id, _row_json(row), confirmation, timestamp, _now()),
        )
    return lot_id


def archive_position_lot(
    database: Database,
    *,
    lot_id: int,
    confirmation: str,
    reason: str,
    confirmed_at: datetime | None = None,
) -> None:
    """归档保留原始批次，不根据行情或模型推断减仓。"""
    if confirmation != CONFIRM_ARCHIVE:
        raise PositionError(f"未归档：必须逐字确认“{CONFIRM_ARCHIVE}”。")
    if not isinstance(lot_id, int) or isinstance(lot_id, bool) or lot_id < 1:
        raise PositionError("批次 ID 必须是正整数。")
    if not isinstance(reason, str):
        raise PositionError("归档原因必须为 1～200 个字符。")
    reason = reason.strip()
    if not reason or len(reason) > 200:
        raise PositionError("归档原因必须为 1～200 个字符。")
    timestamp = (confirmed_at or datetime.now(timezone.utc)).isoformat()
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM position_lots WHERE id = ?", (lot_id,)).fetchone()
        if row is None:
            raise PositionError(f"持仓批次 {lot_id} 不存在。")
        if not bool(row["active"]):
            raise PositionError(f"持仓批次 {lot_id} 已归档，不重复修改。")
        connection.execute(
            """
            UPDATE position_lots
            SET active = 0, deactivated_at = ?, deactivation_reason = ?
            WHERE id = ?
            """,
            (timestamp, reason, lot_id),
        )
        after = connection.execute("SELECT * FROM position_lots WHERE id = ?", (lot_id,)).fetchone()
        connection.execute(
            """
            INSERT INTO position_audit(
                lot_id, action, before_json, after_json,
                confirmation_text, confirmed_at, created_at
            ) VALUES (?, 'archive', ?, ?, ?, ?, ?)
            """,
            (lot_id, _row_json(row), _row_json(after), confirmation, timestamp, _now()),
        )


@dataclass(frozen=True)
class LotAvailability:
    lot_id: int
    stock_code: str
    total_quantity: int
    sellable_quantity: int | None
    status: str
    reason: str
    next_open_date: str | None = None


@dataclass(frozen=True)
class PositionAvailability:
    stock_code: str
    as_of_date: str
    total_quantity: int
    sellable_quantity: int | None
    status: str
    lots: tuple[LotAvailability, ...]


def t1_availability(
    database: Database, stock_code: str, as_of_date: date
) -> PositionAvailability:
    """逐批检查买入日是否开市，以及买入后的首个开市日。

    已找到首个开市日后，更晚日期的日历缺失不会改变该批次已经可卖的事实；
    尚未找到时，任何一个中间日期缺失都会让该批次保持未知。
    """
    code = _code(stock_code)
    with database.connect() as connection:
        lots = list(connection.execute(
            "SELECT * FROM position_lots WHERE stock_code = ? AND active = 1 ORDER BY id",
            (code,),
        ))
        if lots:
            start = min(lot["buy_date"] for lot in lots)
            calendar = {
                row["trade_date"]: bool(row["is_open"])
                for row in connection.execute(
                    """
                    SELECT trade_date, is_open FROM trading_calendar
                    WHERE trade_date >= ? AND trade_date <= ?
                    """,
                    (start, as_of_date.isoformat()),
                )
            }
        else:
            calendar = {}

    results: list[LotAvailability] = []
    for lot in lots:
        lot_id = int(lot["id"])
        quantity = int(lot["quantity"])
        purchased = date.fromisoformat(lot["buy_date"])
        if purchased > as_of_date:
            results.append(LotAvailability(
                lot_id, code, quantity, None, "unknown",
                "买入日期晚于查询日，记录可能错误；请人工核对，不按有效持仓推断可卖量。",
            ))
            continue
        if lot["buy_date"] not in calendar:
            results.append(LotAvailability(
                lot_id, code, quantity, None, "unknown",
                "缺少买入日交易日历，不能验证该买入批次。",
            ))
            continue
        if not calendar[lot["buy_date"]]:
            results.append(LotAvailability(
                lot_id, code, quantity, None, "unknown",
                "交易日历显示买入日不开市，请人工核对买入日期或日历。",
            ))
            continue
        if purchased == as_of_date:
            results.append(LotAvailability(
                lot_id, code, quantity, 0, "ok", "当日买入批次按 T+1 暂不可卖。",
            ))
            continue

        cursor = date.fromordinal(purchased.toordinal() + 1)
        next_open: date | None = None
        missing: date | None = None
        while cursor <= as_of_date:
            key = cursor.isoformat()
            if key not in calendar:
                missing = cursor
                break
            if calendar[key]:
                next_open = cursor
                break
            cursor = date.fromordinal(cursor.toordinal() + 1)
        if next_open is not None:
            results.append(LotAvailability(
                lot_id, code, quantity, quantity, "ok",
                f"买入后的首个开市日为 {next_open.isoformat()}，该批次全部记录数量可卖。",
                next_open.isoformat(),
            ))
        elif missing is not None:
            results.append(LotAvailability(
                lot_id, code, quantity, None, "unknown",
                f"缺少 {missing.isoformat()} 的交易日历，尚不能确定买入后的首个开市日。",
            ))
        else:
            results.append(LotAvailability(
                lot_id, code, quantity, 0, "ok",
                "截至查询日，买入日之后均为明确休市日，该批次仍不可卖。",
            ))

    total = sum(int(lot["quantity"]) for lot in lots)
    if any(item.status == "unknown" for item in results):
        return PositionAvailability(code, as_of_date.isoformat(), total, None, "unknown", tuple(results))
    return PositionAvailability(
        code, as_of_date.isoformat(), total,
        sum(item.sellable_quantity or 0 for item in results), "ok", tuple(results),
    )
