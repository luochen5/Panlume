"""桌面版资源定位与个人数据目录；启动时不读取旧项目主库。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_DIR_NAME = "Panlume"


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_path(relative: str) -> Path:
    bundle = getattr(sys, "_MEIPASS", None)
    root = Path(bundle) if bundle else Path(__file__).resolve().parents[2]
    return root / relative


@dataclass(frozen=True)
class DesktopPaths:
    root: Path

    @classmethod
    def default(cls) -> "DesktopPaths":
        override = os.environ.get("PANDENG_DATA_DIR", "").strip()
        if override:
            return cls(Path(override).expanduser().resolve())
        program_dir = executable_dir()
        if (program_dir / "panlume.portable").is_file():
            return cls((program_dir.parent / "PanlumeData").resolve())
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            return cls((base / APP_DIR_NAME).resolve())
        return cls((Path.home() / ".local" / "share" / APP_DIR_NAME).resolve())

    @property
    def settings_file(self) -> Path:
        return self.root / "config" / "desktop-settings.json"

    @property
    def report_presets_file(self) -> Path:
        return self.root / "config" / "report-presets.json"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def database(self) -> Path:
        return self.root / "data" / "pandeng.db"

    @property
    def demo_database(self) -> Path:
        return self.root / "runtime" / "analysis-demo" / "analysis-test.db"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def audits(self) -> Path:
        return self.root / "audits"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def ensure_directories(self) -> None:
        for path in (
            self.root,
            self.settings_file.parent,
            self.database.parent,
            self.demo_database.parent,
            self.backups,
            self.audits,
            self.logs,
            self.exports,
        ):
            path.mkdir(parents=True, exist_ok=True)
