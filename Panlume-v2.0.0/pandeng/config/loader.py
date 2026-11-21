"""从 YAML 文件安全加载配置。"""

from pathlib import Path

import yaml

from .schema import AppConfig, ConfigError, parse_config


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"找不到配置文件：{path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except UnicodeError as exc:
        raise ConfigError("配置文件必须使用 UTF-8 编码。") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 格式错误：{exc}") from exc
    return parse_config(raw, base_dir=Path.cwd())
