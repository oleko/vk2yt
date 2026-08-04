# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""Сборка названия и описания ролика для площадок.

Поле `description` у видео в VK почти всегда пустое: на живом архиве
содержательный текст нашёлся у 15% роликов. Поэтому текст собирается слоями:
описание видео → текст поста со стены (если он не повторяет название) → и в
любом случае блок с реальными фактами (дата первой публикации, ссылка на
оригинал).

Ничего не выдумываем: если исходного текста нет, описание состоит только из
проверяемых данных. Придумывать содержание по одному заголовку нельзя — для
архива городского телевидения это означало бы ложные имена, даты и события.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from config import Config

TITLE_MAX = 100
DESC_MAX = 5000

# Автозаглушки, которые VK подставляет вместо названия
PLACEHOLDER_TITLE = re.compile(
    r"^\s*(video by|live:|видео от|без названия|untitled)\b", re.I
)

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

DEFAULT_TEMPLATE = """{text}

Из архива сообщества «{channel}».
Опубликовано в сообществе: {date}
Оригинал: {vk_url}

{hashtags}"""


def _norm(s: str) -> str:
    """Нормализация для сравнения текста с названием (как в dedup.py)."""
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def is_meaningful(text: str, title: str) -> bool:
    """Полезен ли текст, или это пересказ названия."""
    t = _norm(text)
    if not t:
        return False
    n = _norm(title)
    if t == n:
        return False
    # текст ненамного длиннее названия — считаем, что смысла не добавляет
    return len(t) > len(n) + 10


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _format_date(vk_date) -> str:
    if not vk_date:
        return ""
    try:
        dt = datetime.fromtimestamp(int(vk_date), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return ""
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year} г."


def vk_video_url(vk_id: str) -> str:
    return f"https://vkvideo.ru/video{vk_id}"


def source_text(item: dict) -> str:
    """Исходный текст ролика: своё описание, иначе текст поста со стены."""
    title = item.get("title") or ""
    desc = (item.get("description") or "").strip()
    if is_meaningful(desc, title):
        return desc
    wall = (item.get("wall_text") or "").strip()
    if is_meaningful(wall, title):
        return wall
    return ""


def pick_title(item: dict, config: Config) -> str:
    """Название для площадок; заглушки VK заменяются текстом из поста."""
    title = (item.get("title") or "").strip()
    wall = (item.get("wall_text") or "").strip()

    if config.fix_placeholder_titles and wall and (not title or PLACEHOLDER_TITLE.match(title)):
        candidate = _first_line(wall)
        if candidate:
            title = candidate

    title = re.sub(r"[<>]", "", title).strip()
    if not title:
        title = "Без названия"
    return title[:TITLE_MAX]


def _load_template(config: Config) -> str:
    path = config.desc_template_file
    if path and path.exists():
        try:
            tpl = path.read_text(encoding="utf-8").strip()
            if tpl:
                return tpl
        except OSError:
            pass
    return DEFAULT_TEMPLATE


def build_description(item: dict, config: Config) -> str:
    """Итоговое описание: исходный текст (если есть) + блок фактов."""
    rendered = _load_template(config).format(
        text=source_text(item),
        title=(item.get("title") or "").strip(),
        channel=config.desc_channel_name or "",
        date=_format_date(item.get("vk_date")),
        vk_url=item.get("url") or vk_video_url(item.get("vk_id", "")),
        hashtags=config.desc_hashtags or "",
    )

    # Схлопываем пустоты, оставшиеся от незаполненных плейсхолдеров
    lines = [ln.rstrip() for ln in rendered.splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln and (not out or not out[-1]):
            continue
        out.append(ln)
    return "\n".join(out).strip()[:DESC_MAX]


def needs_wall_text(item: dict) -> bool:
    """Стоит ли тянуть для ролика текст поста со стены."""
    if item.get("wall_text") is not None:
        return False  # уже пробовали, второй раз VK не дёргаем
    if not item.get("wall_post_id"):
        return False
    title = item.get("title") or ""
    if is_meaningful(item.get("description") or "", title):
        return False  # своё описание есть, стена не нужна
    return True
