# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""plan.json: инвентарь всего архива VK и фиксированный порядок выгрузки на YouTube."""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("vk2yt.plan")


def load_plan(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_plan(path: Path, plan: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".plan.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2, sort_keys=False)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def build_or_update_plan(
    path: Path,
    group_id: int,
    daily_limit: int,
    vk_items: list[dict[str, Any]],
    new_first: bool = False,
    config=None,
) -> dict[str, Any]:
    """vk_items уже отсортированы от старых к новым (см. vk_source.fetch_all_videos)."""
    existing = load_plan(path)
    known_ids = {item["vk_id"] for item in existing["items"]} if existing else set()

    ordered_existing = existing["items"] if existing else []

    # Освежаем у уже известных роликов поля, приходящие из VK: название и
    # описание там могли поправить, а wall_post_id мог просто отсутствовать в
    # плане, собранном более старой версией скрипта.
    fresh = {i["vk_id"]: i for i in vk_items}
    for item in ordered_existing:
        src = fresh.get(item["vk_id"])
        if not src:
            continue
        for key in ("title", "description", "vk_date", "duration", "url", "wall_post_id"):
            if src.get(key) is not None:
                item[key] = src[key]
    new_items = [
        {
            "vk_id": item["vk_id"],
            "title": item["title"],
            "description": item["description"],
            "vk_date": item["vk_date"],
            "duration": item.get("duration"),
            "url": item.get("url"),
            "wall_post_id": item.get("wall_post_id"),
        }
        for item in vk_items
        if item["vk_id"] not in known_ids
    ]

    if new_first:
        combined = new_items + ordered_existing
    else:
        combined = ordered_existing + new_items

    items = []
    for i, item in enumerate(combined, start=1):
        item["order"] = i
        item["planned_day"] = math.ceil(i / daily_limit)
        items.append(item)

    if config is not None:
        _fill_wall_texts(items, config)

    plan = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "group_id": group_id,
        "daily_limit": daily_limit,
        "total": len(items),
        "items": items,
    }
    save_plan(path, plan)
    return plan


def _fill_wall_texts(items: list[dict[str, Any]], config) -> None:
    """Подтягивает тексты постов для роликов без собственного описания.

    Результат оседает в plan.json, поэтому за стеной ходим один раз на ролик,
    а не на каждом прогоне. Пустая строка тоже сохраняется — как отметка
    «пробовали, там ничего нет».
    """
    import descriptions
    import vk_source

    need = [i for i in items if descriptions.needs_wall_text(i)]
    if not need:
        return

    logger.info("Тяну тексты постов со стены для %d роликов", len(need))
    texts = vk_source.fetch_wall_texts(config, [i["wall_post_id"] for i in need])
    for item in need:
        item["wall_text"] = texts.get(item["wall_post_id"], "")


def summarize(plan: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    total = plan["total"]

    def count(target: str, state: str) -> int:
        return sum(
            1 for item in plan["items"]
            if registry.get(item["vk_id"], {}).get(target, {}).get("state") == state
        )

    yt_done = count("youtube", "uploaded")
    rt_done = count("rutube", "uploaded")
    remaining = total - yt_done
    days_left = math.ceil(remaining / plan["daily_limit"]) if remaining > 0 else 0
    eta = (
        (datetime.now(timezone.utc) + timedelta(days=days_left)).date().isoformat()
        if days_left > 0 else None
    )
    return {
        "total": total,
        "youtube_done": yt_done,
        "rutube_done": rt_done,
        "rutube_ingesting": count("rutube", "ingesting"),
        "rutube_waiting": count("rutube", "waiting_import"),
        "remaining": remaining,
        "days_left": days_left,
        "eta": eta,
    }


def next_batch_youtube(
    plan: dict[str, Any],
    registry_data: dict[str, Any],
    daily_limit: int,
    max_retries: int,
) -> list[dict[str, Any]]:
    """Порция на YouTube — упирается в суточную квоту API."""
    from registry import get_entry, needs_youtube

    batch = []
    for item in plan["items"]:
        entry = get_entry(registry_data, item["vk_id"], item["title"], item["vk_date"])
        if needs_youtube(entry, max_retries):
            batch.append(item)
        if len(batch) >= daily_limit:
            break
    return batch


def next_batch_rutube(
    plan: dict[str, Any],
    registry_data: dict[str, Any],
    daily_limit: int,
    max_retries: int,
    import_grace_h: float,
    import_enabled: bool,
) -> list[dict[str, Any]]:
    """Порция на RuTube — квоты нет, поэтому лимит свой и обычно выше."""
    from registry import get_entry, needs_rutube_upload

    batch = []
    for item in plan["items"]:
        entry = get_entry(registry_data, item["vk_id"], item["title"], item["vk_date"])
        if needs_rutube_upload(entry, max_retries, import_grace_h, import_enabled):
            batch.append(item)
        if len(batch) >= daily_limit:
            break
    return batch
