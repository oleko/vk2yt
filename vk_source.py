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
VK_WALL_URL = "https://api.vk.com/method/wall.getById"
WALL_BATCH = 100
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
                # пост, к которому приложено видео: у большинства роликов своё
                # описание пустое, и текст поста — единственный источник
                "wall_post_id": v.get("wall_post_id"),
            })

        offset += len(page)

    items.sort(key=lambda x: x["vk_date"] or 0)
    return items


def fetch_wall_texts(config: Config, post_ids: list[int]) -> dict[int, str]:
    """Тексты постов со стены сообщества: {wall_post_id: text}.

    wall.getById принимает до 100 постов за вызов, так что весь архив
    обходится примерно за 15 запросов. Ошибки не фатальны: без текста поста
    описание просто соберётся из одного шаблона.
    """
    owner_id = -abs(config.vk_group_id)
    texts: dict[int, str] = {}

    for start in range(0, len(post_ids), WALL_BATCH):
        chunk = post_ids[start:start + WALL_BATCH]
        params = {
            "posts": ",".join(f"{owner_id}_{pid}" for pid in chunk),
            "access_token": config.vk_token,
            "v": config.vk_api_version,
        }
        try:
            resp = requests.get(VK_WALL_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning("Не удалось прочитать посты со стены: %s", e)
            continue

        if "error" in data:
            err = data["error"]
            logger.warning(
                "VK API (wall.getById) error %s: %s",
                err.get("error_code"), err.get("error_msg"),
            )
            continue

        response = data.get("response")
        posts = response.get("items", []) if isinstance(response, dict) else (response or [])
        for p in posts:
            pid = p.get("id")
            if pid is not None:
                texts[pid] = (p.get("text") or "").strip()

    return texts


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
        # Часть роликов в VK — это встроенные видео с YouTube (кто-то когда-то
        # запостил чужую ссылку вместо своего файла). yt-dlp честно идёт качать
        # с CDN YouTube, а он бывает медленным для не-жилых IP; дефолтные 20с
        # этого не переживают. Ссылки на реально удалённое/приватное исходное
        # видео это не спасёт — тут только больше времени на разовую задержку.
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
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
