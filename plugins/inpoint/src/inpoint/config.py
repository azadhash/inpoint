"""Runtime configuration, read once from .env + environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional at import time so `doctor` still runs
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False

PKG_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PKG_ROOT.parent.parent


def expand(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


@dataclass(frozen=True)
class Config:
    jira_base_url: str
    asset_root: Path
    downloads_dir: Path
    work_dir: Path
    whisper_model: str
    whisper_translate_model: str
    web_host: str
    web_port: int


def _user_config_path() -> Path:
    """Canonical config location, outside the plugin directory.

    A plugin's own folder is replaced wholesale when the plugin updates, so a
    token stored there would silently vanish. This survives updates.
    """
    override = os.getenv("INPOINT_CONFIG")
    if override:
        return expand(override)
    base = os.getenv("XDG_CONFIG_HOME") or "~/.config"
    return expand(base) / "inpoint" / ".env"


ENV_PATH = _user_config_path()
PROJECT_ENV_PATH = PROJECT_ROOT / ".env"


def read_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.is_file():
        return values
    for line in ENV_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env(updates: dict[str, str]) -> Path:
    """Merge `updates` into .env, preserving comments and key order.

    The file holds an API token, so it is written owner-only.
    """
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.is_file() else []
    remaining = dict(updates)
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)

    if remaining:
        if out and out[-1].strip():
            out.append("")
        for key, value in remaining.items():
            out.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(out).rstrip("\n") + "\n")
    ENV_PATH.chmod(0o600)
    get_config.cache_clear()
    for key, value in updates.items():
        os.environ[key] = value
    return ENV_PATH


@lru_cache(maxsize=1)
def get_config() -> Config:
    # Project .env first (handy when working in a checkout), then the user config,
    # which wins because it is the one `inpoint login` writes.
    load_dotenv(PROJECT_ENV_PATH)
    load_dotenv(ENV_PATH, override=True)
    return Config(
        jira_base_url=os.getenv("JIRA_BASE_URL", "").strip().rstrip("/"),
        asset_root=expand(os.getenv("ASSET_ROOT", "~/Desktop")),
        downloads_dir=expand(os.getenv("DOWNLOADS_DIR", "~/Downloads")),
        work_dir=expand(os.getenv("WORK_DIR", "~/Desktop/inpoint-work")),
        whisper_model=os.getenv("WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"),
        # The turbo model cannot translate -- it was distilled for transcription
        # only and silently returns the source language. A separate model is used
        # for the translation pass, downloaded on first use.
        whisper_translate_model=os.getenv(
            "WHISPER_TRANSLATE_MODEL", "mlx-community/whisper-large-v3-mlx"
        ),
        web_host=os.getenv("WEB_HOST", "127.0.0.1"),
        web_port=int(os.getenv("WEB_PORT", "8765")),
    )
