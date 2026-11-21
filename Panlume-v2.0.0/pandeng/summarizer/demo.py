"""第四阶段 A 独立摘要演示：默认使用模拟模型响应，真实模型必须显式选择。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from pandeng.analyzer.service import AnalysisService
from pandeng.config.schema import AppConfig, ConfigError
from pandeng.data.base import MinuteBar
from pandeng.data.schedule import session_minutes
from pandeng.holdings.positions import CONFIRM_ADD, add_position_lot
from pandeng.storage.db import Database
from pandeng.storage.repositories import ingest_minutes
from pandeng.summarizer.ollama import OllamaResponse
from pandeng.summarizer.service import SummaryStore
from pandeng.summarizer.worker import SummaryService


class OfflineDemoModel:
    """只在 summary-demo 内使用；不打开网络连接。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> OllamaResponse:
        self.calls += 1
        facts = json.loads(prompt.split("已保存事实：", 1)[1])
        code = facts["stock_code"]
        stamp = facts["data_time"]
        payload = facts["payload"]
        if facts["event_type"] == "fixed_stop":
            final = (
                f"{code} {stamp} 固定止损触及，分钟收盘 {payload['close']}，"
                f"人工确认止损价 {payload['stop_loss_price']}。仅整理程序事实。"
            )
            text = "<think>这一段只能进入调用审计</think>" + final
        else:
            final = (
                f"{code} {stamp} 分钟收盘 {payload['close']}，"
                f"VWAP 估算 {payload['vwap_estimate']}，输入外数字 999。"
            )
            text = final
        return OllamaResponse(
            text=text,
            thinking="独立思考通道",
            model="offline-demo-model",
            raw_json=json.dumps({"response": text}, ensure_ascii=False),
            total_duration_ns=1000,
            load_duration_ns=100,
            eval_count=10,
            eval_duration_ns=500,
        )


@dataclass(frozen=True)
class SummaryDemoOutcome:
    notices: int
    tasks: int
    succeeded: int
    fallback: int
    model_calls: int
    drained: bool


def run_summary_demo(
    config: AppConfig, session_date: date, *, real_ollama: bool = False
) -> SummaryDemoOutcome:
    database = Database(config.storage.database)
    expected_name = "summary-live-test.db" if real_ollama else "summary-test.db"
    if database.path.name != expected_name:
        raise ConfigError(
            f"summary-demo 必须使用独立 {expected_name}；当前配置指向 {database.path.name}。"
        )
    if real_ollama and not config.ollama.enabled:
        raise ConfigError("真实 Ollama 演示要求 models.ollama.enabled=true，云端仍保持关闭。")
    if config.cloud.enabled:
        raise ConfigError("摘要演示要求云端保持关闭。")
    with database.connect() as connection:
        existing = connection.execute(
            "SELECT (SELECT COUNT(*) FROM summary_tasks) AS tasks, "
            "(SELECT COUNT(*) FROM position_lots) AS lots, "
            "(SELECT COUNT(*) FROM analysis_runs) AS runs"
        ).fetchone()
    if existing["tasks"] or existing["lots"] or existing["runs"]:
        raise ConfigError("该摘要演示库已有记录，为避免混合两场演示，本次没有覆盖或追加。")

    code = config.watchlist[0].code
    point = session_minutes(session_date)[0]
    ingest_minutes(
        database,
        [MinuteBar(code, point, 10, 10, 9, 9, 100, "summary-demo")],
        adapter="summary-demo",
        check_gaps=False,
    )
    add_position_lot(
        database,
        stock_code=code,
        quantity=100,
        buy_date=session_date.isoformat(),
        stop_loss_price=9,
        confirmation=CONFIRM_ADD,
    )
    analyzer = AnalysisService(database, rules=config.rules)
    result = analyzer.analyze_code(code, point, analysis_mode="live")
    if not result.notices:
        raise ConfigError("演示未能产生规则提醒，请检查独立演示数据。")

    fake_client = None if real_ollama else OfflineDemoModel()
    service = SummaryService(
        database,
        config.ollama,
        config.summary,
        client=fake_client,
        enabled=True,
    )
    service.start()
    print(
        "第四阶段 A 真实 Ollama 验收开始。"
        if real_ollama else
        "第四阶段 A 离线摘要验收开始：使用模拟模型响应，不会连接 Ollama 或云端。"
    )
    for notice in sorted(result.notices, key=lambda item: item.priority != "high"):
        # 固定告警始终先输出；队列、模型和校验都只处理后续文字整理。
        print(f"  【{notice.priority}/{notice.notification_kind}】{notice.message}")
        submission = service.submit(notice)
        if submission is not None:
            print(f"  摘要任务 {submission.task_id}：{submission.status} {submission.reason}".rstrip())
    drained = service.close()
    store = SummaryStore(database)
    counts = store.counts()
    rows = list(reversed(store.recent(limit=100)))
    for row in rows:
        print(
            f"  摘要任务 {row['id']}：{row['status']}；类型 {row['summary_kind']}；"
            f"内容：{row['summary_text']}"
        )
        if row["status"] == "fallback":
            print(f"  回退原因：{row['fallback_reason']}；{row['validation_reasons_json']}")
    print(
        f"摘要验收汇总：固定告警 {len(result.notices)} 条；任务 {len(rows)} 条；"
        f"模型摘要 {counts['succeeded']} 条；模板回退 {counts['fallback']} 条；"
        f"模型调用 {counts['model_calls']} 次；有限收尾完成={'是' if drained else '否'}。"
    )
    print("原始行情、指标、规则和人工持仓没有被摘要回写，也未执行任何交易或外部账户操作。")
    return SummaryDemoOutcome(
        len(result.notices), len(rows), counts["succeeded"], counts["fallback"],
        counts["model_calls"], drained,
    )
