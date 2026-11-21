"""D6 v1 可重复验收；每次运行追加 JSON 记录。"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "verifier" / "runs"
TARGETED = [
    "tests/test_desktop_demo.py", "tests/test_desktop_data.py",
    "tests/test_desktop_window.py", "tests/test_windows_build_scripts.py",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("targeted", "full"), default="targeted")
    args = parser.parse_args()
    demo = ROOT / "pandeng/desktop/demo.py"
    checks = {
        "version_0_6_0": 'APP_VERSION = "0.6.0"' in (ROOT / "pandeng/desktop/__init__.py").read_text(encoding="utf-8"),
        "integrated_demo": demo.is_file() and "DesktopHistoryService" in demo.read_text(encoding="utf-8"),
        "demo_tests": (ROOT / "tests/test_desktop_demo.py").is_file(),
    }
    output, exit_code = "", 1
    if all(checks.values()):
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        if args.suite == "targeted":
            command.extend(TARGETED)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(ROOT), "/mnt/agents/.pandeng-test-deps", env.get("PYTHONPATH", "")])
        completed = subprocess.run(command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
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
