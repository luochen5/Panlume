"""D7 v2 可重复双语验收；每次运行追加 JSON 记录。"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "verifier" / "runs"
TARGETED = [
    "tests/test_desktop_settings.py",
    "tests/test_desktop_summary.py",
    "tests/test_summary_content.py",
    "tests/test_review.py",
    "tests/test_desktop_demo.py",
    "tests/test_desktop_window.py",
    "tests/test_windows_build_scripts.py",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("targeted", "full"), default="targeted")
    args = parser.parse_args()
    window = (ROOT / "pandeng/desktop/window.py").read_text(encoding="utf-8")
    i18n = (ROOT / "pandeng/desktop/i18n.py").read_text(encoding="utf-8")
    checks = {
        "version_0_7_0": 'APP_VERSION = "0.7.0"' in (
            ROOT / "pandeng/desktop/__init__.py"
        ).read_text(encoding="utf-8"),
        "public_default_english": 'PUBLIC_DEFAULT_LANGUAGE = "en"' in i18n,
        "legacy_default_chinese": 'DEFAULT_LANGUAGE = "zh-CN"' in i18n,
        "dedicated_language_page": '"language": "语言 / Language"' in window,
        "language_waits_for_restart": "lambda: self._ui_language" in window,
        "english_homepage": (ROOT / "README.md").is_file()
        and "D7 bilingual release candidate" in (ROOT / "README.md").read_text(encoding="utf-8"),
        "chinese_readme": (ROOT / "README.zh-CN.md").is_file(),
        "d7_guide": (ROOT / "docs/双语发布D7使用说明.md").is_file(),
        "bilingual_tests": (ROOT / "tests/test_desktop_window.py").is_file(),
    }
    output, exit_code = "", 1
    if all(checks.values()):
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if args.suite == "targeted":
            command.extend(TARGETED)
        env = os.environ.copy()
        python_paths = [str(ROOT)]
        development_deps = Path("/mnt/agents/.pandeng-test-deps")
        if development_deps.exists():
            python_paths.append(str(development_deps))
        if env.get("PYTHONPATH"):
            python_paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=300,
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
