"""统一报告事实包与固定模板。"""

from .builder import build_daily_fact_package, build_event_fact_package
from .facts import FACT_PACKAGE_VERSION, FactPackage
from .templates import render_detailed_report, render_realtime_summary, render_report

__all__ = [
    "FACT_PACKAGE_VERSION", "FactPackage", "build_daily_fact_package",
    "build_event_fact_package", "render_detailed_report", "render_realtime_summary",
    "render_report",
]
