"""v2.0.0 可重复发布验收；每次运行追加本地 JSON 记录。"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "verifier" / "runs"
TARGETED = [
    "tests/test_report_facts.py",
    "tests/test_report_builder.py",
    "tests/test_report_templates.py",
    "tests/test_desktop_reports.py",
    "tests/test_desktop_report_page.py",
    "tests/test_desktop_report_narrative.py",
    "tests/test_report_narrative.py",
    "tests/test_ollama_management.py",
    "tests/test_desktop_ollama_page.py",
    "tests/test_momentum.py",
    "tests/test_desktop_dpi_scroll.py",
    "tests/test_language_v2.py",
    "tests/test_v2_upgrade.py",
    "tests/test_windows_build_scripts.py",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("targeted", "full"), default="targeted")
    args = parser.parse_args()
    desktop = (ROOT / "pandeng/desktop/__init__.py").read_text(encoding="utf-8")
    window = (ROOT / "pandeng/desktop/window.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    checks = {
        "version_2_0_0": 'APP_VERSION = "2.0.0"' in desktop,
        "public_release_channel": 'BUILD_CHANNEL = "v2.0.0 unified reporting public release"' in desktop,
        "fact_package": (ROOT / "pandeng/reporting/facts.py").is_file(),
        "report_results_page": (ROOT / "pandeng/desktop/report_results.py").is_file(),
        "ollama_management": (ROOT / "pandeng/summarizer/management.py").is_file(),
        "momentum": (ROOT / "pandeng/indicators/momentum.py").is_file(),
        "twelve_navigation_entries": '"results": "报告成果"' in window,
        "english_homepage": "Current version: v2.0.0" in readme,
        "chinese_readme": "v2.0.0 统一报告正式版" in chinese,
        "report_guide": (ROOT / "docs/v2.0.0报告与模型使用说明.md").is_file(),
        "english_guide": (ROOT / "docs/v2.0.0-report-and-model-guide.md").is_file(),
        "acceptance_record": (ROOT / "docs/v2.0.0阶段8迁移与发布验收.md").is_file(),
        "portable_guide_current": "v2.0.0 便携四语言学习版" in (
            ROOT / "packaging/portable-readme.txt"
        ).read_text(encoding="utf-8"),
    }
    output, exit_code = "", 1
    if all(checks.values()):
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if args.suite == "targeted":
            command.extend(TARGETED)
        completed = subprocess.run(
            command, cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300,
        )
        output, exit_code = completed.stdout, completed.returncode
        match = re.search(r"(\d+) passed", output)
        checks["passed_count"] = int(match.group(1)) if match else None
        checks["pytest_exit_zero"] = exit_code == 0
    else:
        output = "静态前置检查未通过，未运行 pytest。\n"
        checks["pytest_exit_zero"] = False
    now = datetime.now(timezone.utc)
    record = {
        "timestamp": now.isoformat(),
        "command": [sys.executable, str(Path(__file__).resolve()), "--suite", args.suite],
        "suite": args.suite,
        "exit_code": exit_code,
        "checks": checks,
        "output_tail": output[-5000:],
    }
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{args.suite}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(output, end="")
    print(json.dumps({"exit_code": exit_code, "checks": checks}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
