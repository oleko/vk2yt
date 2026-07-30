"""plan.json: инвентарь всего архива VK и фиксированный порядок выгрузки на YouTube."""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


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
) -> dict[str, Any]:
    """vk_items уже отсортированы от старых к новым (см. vk_source.fetch_all_videos)."""
    existing = load_plan(path)
    known_ids = {item["vk_id"] for item in existing["items"]} if existing else set()

    ordered_existing = existing["items"] if existing else []
    new_items = [
        {
            "vk_id": item["vk_id"],
            "title": item["title"],
            "description": item["description"],
            "vk_date": item["vk_date"],
            "duration": item.get("duration"),
            "url": item.get("url"),
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

    plan = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "group_id": group_id,
        "daily_limit": daily_limit,
        "total": len(items),
        "items": items,
    }
    save_plan(path, plan)
    return plan


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


def next_batch(
    plan: dict[str, Any],
    registry_data: dict[str, Any],
    daily_limit: int,
    max_retries: int,
    rutube_enabled: bool = True,
    import_grace_h: float = 72.0,
) -> list[dict[str, Any]]:
    from registry import get_entry, needs_any_processing

    batch = []
    for item in plan["items"]:
        entry = get_entry(registry_data, item["vk_id"], item["title"], item["vk_date"])
        if needs_any_processing(entry, max_retries, rutube_enabled, import_grace_h):
            batch.append(item)
        if len(batch) >= daily_limit:
            break
    return batch
