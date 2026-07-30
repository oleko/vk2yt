# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""Забор архива видео из VK (video.get) и скачивание роликов через yt-dlp."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

from config import Config

logger = logging.getLogger("vk2yt.vk_source")

VK_API_URL = "https://api.vk.com/method/video.get"
# Несмотря на документацию (максимум 200), video.get реально отдаёт не больше
# 100 записей за вызов — проверено на живом API.
PAGE_SIZE = 100


class VkApiError(RuntimeError):
    pass


def fetch_all_videos(config: Config) -> list[dict[str, Any]]:
    """Весь архив сообщества, отсортированный от старых к новым.

    video.get не всегда отдаёт ровно `count` записей на промежуточных страницах
    (не только на последней) — поэтому ориентируемся на реальный `count` из
    ответа и сдвигаем offset на фактическое число полученных записей, а не на
    PAGE_SIZE, иначе часть архива будет пропущена или обрезана раньше времени.
    """
    owner_id = -abs(config.vk_group_id)
    items: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None

    while total is None or offset < total:
        params = {
            "owner_id": owner_id,
            "count": PAGE_SIZE,
            "offset": offset,
            "access_token": config.vk_token,
            "v": config.vk_api_version,
        }
        resp = requests.get(VK_API_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            err = data["error"]
            raise VkApiError(
                f"VK API error {err.get('error_code')}: {err.get('error_msg')}"
            )

        if total is None:
            total = data["response"]["count"]

        page = data["response"]["items"]
        if not page:
            break

        for v in page:
            vk_id = f"{v['owner_id']}_{v['id']}"
            items.append({
                "vk_id": vk_id,
                "title": v.get("title", ""),
                "description": v.get("description", ""),
                "vk_date": v.get("date"),
                "duration": v.get("duration"),
                "url": v.get("share_url") or video_page_url(vk_id),
            })

        offset += len(page)

    items.sort(key=lambda x: x["vk_date"] or 0)
    return items


def check_access(config: Config) -> tuple[bool, str]:
    owner_id = -abs(config.vk_group_id)
    params = {
        "owner_id": owner_id,
        "count": 1,
        "access_token": config.vk_token,
        "v": config.vk_api_version,
    }
    try:
        resp = requests.get(VK_API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        return False, f"Сетевая ошибка: {e}"

    if "error" in data:
        err = data["error"]
        return False, f"VK API error {err.get('error_code')}: {err.get('error_msg')}"

    total = data["response"]["count"]
    return True, f"Доступ есть, всего видео в архиве: {total}"


def video_page_url(vk_id: str) -> str:
    """Резервный вариант, если API не вернул share_url (см. fetch_all_videos)."""
    return f"https://vkvideo.ru/video{vk_id}"


def download_video(
    vk_id: str, dest_dir: Path, cookies_file: str | None = None, url: str | None = None
) -> Path:
    """Скачивает ролик через yt-dlp, возвращает путь к файлу."""
    import yt_dlp

    dest_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(dest_dir / f"{vk_id.replace('/', '_')}.%(ext)s")

    ydl_opts = {
        "outtmpl": out_template,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    url = url or video_page_url(vk_id)
    last_error: Exception | None = None

    attempts = [None]
    if cookies_file:
        attempts.append(cookies_file)

    for cookies in attempts:
        opts = dict(ydl_opts)
        if cookies:
            opts["cookiefile"] = cookies
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                p = Path(filepath)
                if p.exists():
                    return p
                # merge_output_format may change extension
                mp4_path = p.with_suffix(".mp4")
                if mp4_path.exists():
                    return mp4_path
                raise FileNotFoundError(f"yt-dlp завершился, но файл не найден: {filepath}")
        except Exception as e:  # noqa: BLE001
            last_error = e
            logger.warning("Скачивание %s не удалось (cookies=%s): %s", vk_id, bool(cookies), e)

    raise RuntimeError(f"Не удалось скачать {vk_id}: {last_error}")
