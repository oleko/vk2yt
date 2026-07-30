"""Сверка плана с тем, что уже опубликовано на YouTube и RuTube.

Оба канала не пустые: на YouTube есть ролики, залитые вручную годами раньше, а на
RuTube — то, что их встроенный импорт утянул с YouTube. Без сверки пайплайн зальёт
это повторно и создаст дубли, поэтому перед каждым прогоном мы забираем инвентарь
обеих площадок и помечаем совпадения в реестре как уже залитые.

Сопоставление идёт по названию — другого общего идентификатора между VK, YouTube и
RuTube нет. При совпадении считаем «уже есть» и пропускаем: осознанный перекос в
сторону «лучше пропустить, чем создать дубль».
"""
from __future__ import annotations

import logging
import re
from typing import Any

import registry
from config import Config

logger = logging.getLogger("vk2yt.dedup")

# Обе площадки режут заголовок до 100 символов — сравниваем по этой длине.
TITLE_CMP_LEN = 100


def normalize_title(title: str) -> str:
    """Приводит название к виду, пригодному для сравнения между площадками."""
    # youtube_target._sanitize_title вырезает <>, повторяем, чтобы названия сошлись
    s = re.sub(r"[<>]", "", title or "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s[:TITLE_CMP_LEN]


def youtube_inventory(config: Config) -> dict[str, dict[str, str]]:
    """{нормализованное название: {id, url, published_at}} по всему каналу."""
    from googleapiclient.discovery import build

    import youtube_target

    yt = build("youtube", "v3", credentials=youtube_target.get_credentials(config))
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    inventory: dict[str, dict[str, str]] = {}
    token = None
    while True:
        resp = yt.playlistItems().list(
            part="snippet", playlistId=uploads, maxResults=50, pageToken=token
        ).execute()
        for item in resp["items"]:
            sn = item["snippet"]
            key = normalize_title(sn["title"])
            video_id = sn["resourceId"]["videoId"]
            # при повторах оставляем самую раннюю публикацию
            prev = inventory.get(key)
            if prev is None or sn["publishedAt"] < prev["published_at"]:
                inventory[key] = {
                    "id": video_id,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "published_at": sn["publishedAt"],
                }
        token = resp.get("nextPageToken")
        if not token:
            break
    return inventory


def rutube_inventory(client) -> dict[str, dict[str, str]]:
    """{нормализованное название: {video_id, url}} по каналу RuTube."""
    inventory: dict[str, dict[str, str]] = {}
    for v in client.list_channel_videos():
        title = v.get("title") or ""
        video_id = str(v.get("id") or v.get("video_id") or "")
        if not video_id:
            continue
        key = normalize_title(title)
        inventory.setdefault(key, {
            "video_id": video_id,
            "url": v.get("video_url") or f"https://rutube.ru/video/{video_id}/",
        })
    return inventory


def reconcile(
    config: Config,
    plan: dict[str, Any],
    reg: dict[str, Any],
    rutube_client=None,
) -> dict[str, int]:
    """Помечает в реестре ролики, которые уже есть на площадках.

    Возвращает счётчики найденного. Реестр меняется на месте, сохранение — снаружи.
    """
    stats = {"youtube": 0, "rutube": 0, "waiting_import": 0, "ambiguous": 0}

    try:
        yt_inv = youtube_inventory(config)
        logger.info("Инвентарь YouTube: %d роликов", len(yt_inv))
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось получить инвентарь YouTube: %s", e)
        yt_inv = {}

    rt_inv: dict[str, dict[str, str]] = {}
    if config.rutube_enabled and rutube_client is not None:
        try:
            rt_inv = rutube_inventory(rutube_client)
            logger.info("Инвентарь RuTube: %d роликов", len(rt_inv))
        except Exception as e:  # noqa: BLE001
            logger.error("Не удалось получить инвентарь RuTube: %s", e)

    seen_titles: set[str] = set()

    for item in plan["items"]:
        key = normalize_title(item["title"])
        existing = reg.get(item["vk_id"])

        if key in seen_titles:
            # Несколько роликов плана с одинаковым названием: первый уже разобран,
            # остальные не трогаем, чтобы не пометить по ошибке.
            if key in yt_inv or key in rt_inv:
                stats["ambiguous"] += 1
                logger.warning(
                    "Неоднозначное название, оставляю как есть: #%s %s",
                    item.get("order"), item["title"][:70],
                )
            continue
        seen_titles.add(key)

        yt_hit = yt_inv.get(key)
        rt_hit = rt_inv.get(key)

        # Пустые записи не заводим: реестр должен содержать только то, с чем
        # реально что-то произошло, иначе он распухнет на весь архив.
        yt_new = yt_hit and (existing or {}).get("youtube", {}).get("state") != "uploaded"
        rt_new = rt_hit and (existing or {}).get("rutube", {}).get("state") != "uploaded"
        waiting = (
            not rt_hit
            and existing is not None
            and registry.is_waiting_import(existing, config.rutube_import_grace_h)
        )
        if not (yt_new or rt_new or waiting):
            continue

        entry = registry.get_entry(reg, item["vk_id"], item["title"], item["vk_date"])

        if yt_new:
            registry.mark_youtube_uploaded(
                entry, yt_hit["id"], yt_hit["url"], source="pre-existing"
            )
            entry["youtube"]["at"] = yt_hit["published_at"]
            stats["youtube"] += 1

        if rt_new:
            registry.mark_rutube_uploaded(
                entry, rt_hit["video_id"], rt_hit["url"], source="import"
            )
            stats["rutube"] += 1
        elif waiting:
            registry.mark_rutube_waiting_import(entry)
            stats["waiting_import"] += 1

    return stats
