# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""Конфигурация: чтение .env / окружения, пути, дефолты."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val in (None, ""):
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # VK
    vk_token: str | None = field(default_factory=lambda: _env("VK_TOKEN"))
    vk_group_id: int | None = field(
        default_factory=lambda: int(_env("VK_GROUP_ID")) if _env("VK_GROUP_ID") else None
    )
    vk_api_version: str = field(default_factory=lambda: _env("VK_API_VERSION", "5.199"))
    vk_cookies_file: str | None = field(default_factory=lambda: _env("VK_COOKIES_FILE"))

    # Очередь
    daily_limit: int = field(default_factory=lambda: _env_int("DAILY_LIMIT", 6))
    max_retries: int = field(default_factory=lambda: _env_int("MAX_RETRIES", 3))
    new_first: bool = field(default_factory=lambda: _env_bool("NEW_FIRST", False))

    # Дашборд
    dash_password: str | None = field(default_factory=lambda: _env("DASH_PASSWORD"))
    dash_port: int = field(default_factory=lambda: _env_int("DASH_PORT", 8766))

    # YouTube
    youtube_client_secret: Path = field(
        default_factory=lambda: BASE_DIR / _env("YOUTUBE_CLIENT_SECRET", "client_secret.json")
    )
    youtube_token_path: Path = field(
        default_factory=lambda: BASE_DIR / _env("YOUTUBE_TOKEN_FILE", "token.json")
    )
    youtube_redirect_uri: str | None = field(default_factory=lambda: _env("YOUTUBE_REDIRECT_URI"))
    youtube_expected_channel_id: str | None = field(
        default_factory=lambda: _env("YOUTUBE_CHANNEL_ID")
    )

    # RuTube
    rutube_enabled: bool = field(default_factory=lambda: _env_bool("RUTUBE_ENABLED", True))
    rutube_login: str | None = field(default_factory=lambda: _env("RUTUBE_LOGIN"))
    rutube_password: str | None = field(default_factory=lambda: _env("RUTUBE_PASSWORD"))
    rutube_category_id: int = field(default_factory=lambda: _env_int("RUTUBE_CATEGORY_ID", 8))
    # id канала: аккаунт редакторский, роликами владеет канал, а не залогиненный
    # пользователь — по этому id берётся инвентарь и к нему привязывается заливка
    rutube_channel_id: int | None = field(
        default_factory=lambda: int(_env("RUTUBE_CHANNEL_ID")) if _env("RUTUBE_CHANNEL_ID") else None
    )
    rutube_public_base_url: str | None = field(
        default_factory=lambda: _env("RUTUBE_PUBLIC_BASE_URL")
    )
    rutube_ingest_dir: Path = field(
        default_factory=lambda: Path(_env("RUTUBE_INGEST_DIR", str(BASE_DIR / "ingest")))
    )
    rutube_ingest_timeout_h: float = field(
        default_factory=lambda: _env_float("RUTUBE_INGEST_TIMEOUT_H", 6.0)
    )
    # Сколько ждать встроенный импорт RuTube с YouTube, прежде чем лить напрямую
    rutube_import_grace_h: float = field(
        default_factory=lambda: _env_float("RUTUBE_IMPORT_GRACE_H", 72.0)
    )
    rutube_is_hidden: bool = field(default_factory=lambda: _env_bool("RUTUBE_IS_HIDDEN", False))
    rutube_max_file_mb: int = field(default_factory=lambda: _env_int("RUTUBE_MAX_FILE_MB", 2000))
    rutube_token_path: Path = field(default_factory=lambda: BASE_DIR / "rutube_token.json")

    # Пути
    downloads_dir: Path = field(default_factory=lambda: BASE_DIR / "downloads")
    plan_path: Path = field(default_factory=lambda: BASE_DIR / "plan.json")
    registry_path: Path = field(default_factory=lambda: BASE_DIR / "processed.json")
    log_path: Path = field(default_factory=lambda: BASE_DIR / "vk2yt.log")
    lock_path: Path = field(default_factory=lambda: BASE_DIR / "running.lock")

    def require_vk(self) -> None:
        missing = []
        if not self.vk_token:
            missing.append("VK_TOKEN")
        if not self.vk_group_id:
            missing.append("VK_GROUP_ID")
        if missing:
            raise RuntimeError(f"Не заданы обязательные переменные: {', '.join(missing)}")

    def require_rutube(self) -> None:
        missing = []
        if not self.rutube_login:
            missing.append("RUTUBE_LOGIN")
        if not self.rutube_password:
            missing.append("RUTUBE_PASSWORD")
        if not self.rutube_public_base_url:
            missing.append("RUTUBE_PUBLIC_BASE_URL")
        if missing:
            raise RuntimeError(f"Не заданы переменные для RuTube: {', '.join(missing)}")

    def ensure_dirs(self) -> None:
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        if self.rutube_enabled:
            self.rutube_ingest_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    return Config()
