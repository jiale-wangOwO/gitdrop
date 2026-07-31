from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


APP_NAME = "GitDrop"


@dataclass
class AppConfig:
    owner: str = ""
    repository: str = "gitdrop-inbox"
    branch: str = "main"
    remote_folder: str = "inbox"
    remember_token: bool = True


def config_dir() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / APP_NAME


def load_config() -> AppConfig:
    path = config_dir() / "config.json"
    if not path.exists():
        return AppConfig()
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        allowed = AppConfig.__dataclass_fields__.keys()
        return AppConfig(**{key: value for key, value in values.items() if key in allowed})
    except (OSError, ValueError, TypeError):
        return AppConfig()


def save_config(config: AppConfig) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_token(owner: str) -> str:
    if not owner:
        return ""
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost=github.com\nusername={owner}\n\n",
            text=True,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            return ""
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return values.get("password", "")
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def save_token(owner: str, token: str, remember: bool) -> None:
    if not owner or not remember or not token:
        return
    try:
        payload = f"protocol=https\nhost=github.com\nusername={owner}\n"
        payload += f"password={token}\n\n"
        result = subprocess.run(
            ["git", "credential", "approve"],
            input=payload,
            text=True,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "Git credential helper 返回错误"
            raise RuntimeError(detail)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"无法访问 Git 凭据存储：{exc}") from exc
