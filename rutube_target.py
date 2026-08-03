# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""Клиент RuTube: логин, заливка по ссылке, ingest-каталог для отдачи файла наружу.

RuTube не принимает файл загрузкой — ему дают публичный URL, и он приходит за
файлом сам. Поэтому скачанный ролик на время кладётся в ingest-каталог (хардлинком,
чтобы не тратить диск) и раздаётся через nginx.

Важно: на дефолтный User-Agent библиотеки requests RuTube отвечает 403 Forbidden.
Браузерный UA обязателен во всех запросах — см. _SESSION ниже.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import requests

from config import Config

logger = logging.getLogger("vk2yt.rutube")

API = "https://rutube.ru/api"
TOKEN_URL = f"{API}/accounts/token_auth/"
VIDEO_URL = f"{API}/video/"
PERSON_URL = f"{API}/video/person/"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

TITLE_MAX = 100
DESC_MAX = 5000


class RutubeError(RuntimeError):
    pass


class RutubeQuotaExceeded(RutubeError):
    """Суточный лимит загрузок RuTube исчерпан — до завтра пробовать бессмысленно."""


class RutubeClient:
    def __init__(self, config: Config):
        self.config = config
        self._token: str | None = None
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })

    # --- авторизация ---

    def _load_cached_token(self) -> str | None:
        p = self.config.rutube_token_path
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("token")
        except (ValueError, OSError):
            return None

    def login(self, force: bool = False) -> str:
        if not force:
            cached = self._load_cached_token()
            if cached:
                self._token = cached
                return cached

        resp = self._session.post(
            TOKEN_URL,
            json={"username": self.config.rutube_login, "password": self.config.rutube_password},
            timeout=30,
        )
        if not resp.ok:
            raise RutubeError(f"Ошибка авторизации RuTube ({resp.status_code}): {resp.text[:500]}")
        token = resp.json().get("token")
        if not token:
            raise RutubeError(f"RuTube не вернул токен: {resp.text[:500]}")

        self._token = token
        self.config.rutube_token_path.write_text(json.dumps({"token": token}), encoding="utf-8")
        try:
            self.config.rutube_token_path.chmod(0o600)
        except OSError:
            pass
        return token

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if not self._token:
            self.login()
        headers = {"Authorization": f"Token {self._token}"}
        resp = self._session.request(method, url, headers=headers, timeout=60, **kwargs)
        if resp.status_code == 401:
            self.login(force=True)
            headers = {"Authorization": f"Token {self._token}"}
            resp = self._session.request(method, url, headers=headers, timeout=60, **kwargs)
        return resp

    # --- видео ---

    def upload_by_url(
        self, file_url: str, title: str, description: str, category_id: int, is_hidden: bool
    ) -> str:
        body = {
            "url": file_url,
            "title": (title or "")[:TITLE_MAX],
            "description": (description or "")[:DESC_MAX],
            "category_id": category_id,
            "is_hidden": is_hidden,
        }
        # Аккаунт редакторский: без author ролик уйдёт в личный профиль редактора,
        # а не на канал сообщества.
        if self.config.rutube_channel_id:
            body["author"] = self.config.rutube_channel_id
        resp = self._request("POST", VIDEO_URL, json=body)
        if not resp.ok:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except ValueError:
                pass
            if "загрузок в сутки" in detail:
                raise RutubeQuotaExceeded(detail)
            raise RutubeError(f"Ошибка заливки RuTube ({resp.status_code}): {resp.text[:500]}")
        data = resp.json()
        video_id = data.get("video_id") or data.get("id")
        if not video_id:
            raise RutubeError(f"RuTube не вернул video_id: {resp.text[:500]}")
        return str(video_id)

    def update_meta_if_changed(self, video_id: str, title: str, description: str) -> bool:
        """Обновляет метаданные, только если они реально отличаются."""
        try:
            info = self.get_video(video_id)
        except RutubeError as e:
            logger.warning("Не удалось прочитать %s перед обновлением: %s", video_id, e)
            info = {}

        new_title = (title or "")[:TITLE_MAX]
        new_desc = (description or "")[:DESC_MAX]
        if info.get("title") == new_title and (info.get("description") or "") == new_desc:
            return False

        self.patch_video(video_id, title=new_title, description=new_desc)
        return True

    def patch_video(self, video_id: str, **fields) -> None:
        if "title" in fields:
            fields["title"] = (fields["title"] or "")[:TITLE_MAX]
        if "description" in fields:
            fields["description"] = (fields["description"] or "")[:DESC_MAX]
        resp = self._request("PATCH", f"{VIDEO_URL}{video_id}/", json=fields)
        if not resp.ok:
            logger.warning(
                "Не удалось обновить метаданные RuTube %s (%s): %s",
                video_id, resp.status_code, resp.text[:300],
            )

    def get_video(self, video_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"{VIDEO_URL}{video_id}/")
        if not resp.ok:
            raise RutubeError(
                f"Ошибка получения статуса RuTube ({resp.status_code}): {resp.text[:300]}"
            )
        return resp.json()

    def is_ready(self, video_id: str) -> tuple[bool, dict[str, Any]]:
        info = self.get_video(video_id)
        return bool(info.get("duration")), info

    def list_channel_videos(
        self, channel_id: int | None = None, limit: int = 100, max_pages: int = 100
    ) -> list[dict[str, Any]]:
        """Ролики канала — для сверки с тем, что уже залито или утянуто импортом.

        Аккаунт у нас редакторский: собственных роликов у него нет, всё принадлежит
        каналу. Поэтому при заданном RUTUBE_CHANNEL_ID берём список канала
        (`/video/person/<id>/`), а не «свои видео».
        """
        channel_id = channel_id or self.config.rutube_channel_id
        url = f"{PERSON_URL}{channel_id}/" if channel_id else PERSON_URL

        items: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            resp = self._request("GET", url, params={"page": page, "limit": limit})
            if not resp.ok:
                raise RutubeError(
                    f"Ошибка получения видео канала RuTube ({resp.status_code}): {resp.text[:300]}"
                )
            data = resp.json()
            results = data.get("results", [])
            items += results
            if not data.get("has_next") or not results:
                break
            page += 1
        return items


def video_url(video_id: str) -> str:
    return f"https://rutube.ru/video/{video_id}/"


# --- ingest-каталог ---

def place_in_ingest(config: Config, local_path: Path) -> tuple[Path, str]:
    """Кладёт файл в ingest/ под случайным именем и возвращает (путь, публичный URL).

    Используется хардлинк — downloads/ и ingest/ на одной ФС, так что копия не
    занимает лишнего места. Если хардлинк невозможен, откатываемся на копирование.
    """
    config.rutube_ingest_dir.mkdir(parents=True, exist_ok=True)
    name = f"{secrets.token_urlsafe(24)}{local_path.suffix}"
    ingest_path = config.rutube_ingest_dir / name
    try:
        os.link(local_path, ingest_path)
    except OSError as e:
        logger.warning("Хардлинк не удался (%s), копирую файл", e)
        import shutil
        shutil.copy2(local_path, ingest_path)
    ingest_path.chmod(0o644)
    return ingest_path, f"{config.rutube_public_base_url.rstrip('/')}/{name}"


def ingest_size_mb(config: Config) -> float:
    if not config.rutube_ingest_dir.exists():
        return 0.0
    total = sum(f.stat().st_size for f in config.rutube_ingest_dir.iterdir() if f.is_file())
    return total / (1024 * 1024)


def ingest_has_room(config: Config) -> bool:
    """Не даём ingest/ распухнуть, если RuTube медленно забирает файлы."""
    return ingest_size_mb(config) < config.rutube_ingest_max_mb


def remove_from_ingest(ingest_file: str | None) -> None:
    if not ingest_file:
        return
    p = Path(ingest_file)
    try:
        if p.exists():
            p.unlink()
    except OSError as e:
        logger.warning("Не удалось удалить %s: %s", p, e)


def gc_ingest(config: Config, known_files: set[str]) -> None:
    """Удаляет из ingest/ всё, чего нет в реестре или что старше таймаута."""
    if not config.rutube_ingest_dir.exists():
        return
    cutoff = time.time() - config.rutube_ingest_timeout_h * 3600
    for f in config.rutube_ingest_dir.iterdir():
        if not f.is_file():
            continue
        try:
            if str(f) in known_files and f.stat().st_mtime >= cutoff:
                continue
            f.unlink()
            logger.info("GC ingest: удалён %s", f.name)
        except OSError as e:
            logger.warning("GC ingest: не удалось удалить %s: %s", f, e)


def check_ingest_roundtrip(config: Config) -> tuple[bool, str]:
    """Кладёт тестовый файл в ingest/, скачивает его снаружи и сверяет содержимое."""
    if not config.rutube_public_base_url:
        return False, "Не задан RUTUBE_PUBLIC_BASE_URL"

    config.rutube_ingest_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(16)
    name = f"healthcheck-{token}.txt"
    path = config.rutube_ingest_dir / name
    payload = f"vk2yt healthcheck {token}"
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o644)
    public_url = f"{config.rutube_public_base_url.rstrip('/')}/{name}"

    try:
        resp = requests.get(public_url, timeout=20)
        if not resp.ok:
            return False, f"{public_url} вернул {resp.status_code}"
        if resp.text.strip() != payload:
            return False, f"Содержимое по {public_url} не совпало с тестовым файлом"
        return True, f"Публичный URL работает: {public_url}"
    except requests.RequestException as e:
        return False, f"Не удалось скачать {public_url}: {e}"
    finally:
        if path.exists():
            path.unlink()


def check_auth(config: Config) -> tuple[bool, str]:
    try:
        config.require_rutube()
        client = RutubeClient(config)
        client.login(force=True)
        videos = client.list_channel_videos()
    except Exception as e:  # noqa: BLE001
        return False, str(e)

    where = f"канала {config.rutube_channel_id}" if config.rutube_channel_id else "аккаунта"
    if not videos and not config.rutube_channel_id:
        return False, (
            "Авторизация прошла, но роликов не видно. Аккаунт редакторский — "
            "задайте RUTUBE_CHANNEL_ID (id из ссылки rutube.ru/channel/<id>/)"
        )
    return True, f"Авторизация прошла, роликов у {where}: {len(videos)}"
