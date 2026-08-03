# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""processed.json: состояние заливки каждого ролика на YouTube и RuTube.

Состояния приёмника:
  pending        — ещё не обработан
  waiting_import — только для RuTube: ролик уже на YouTube, ждём их встроенный импорт
  ingesting      — только для RuTube: ссылка отдана, RuTube качает файл
  uploaded       — опубликован (нами или найден сверкой уже существующим)
  error          — ошибка, повтор до MAX_RETRIES
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL_OK = {"uploaded"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def hours_since(value: str | None) -> float | None:
    ts = parse_ts(value)
    if ts is None:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {vk_id: _migrate_entry(entry) for vk_id, entry in data.items()}


def _migrate_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Старый плоский формат ({"youtube_url": ..., "url": ...}) -> вложенный."""
    if "youtube" in entry:
        entry.setdefault("attempts", 0)
        entry.setdefault("rutube", {"state": "pending"})
        return entry

    migrated = {
        "title": entry.get("title", ""),
        "vk_date": entry.get("vk_date") or entry.get("date"),
        "attempts": entry.get("attempts", 1),
        "youtube": {"state": "pending"},
        "rutube": {"state": "pending"},
    }
    yt_url = entry.get("youtube_url") or entry.get("url")
    if yt_url:
        migrated["youtube"] = {
            "state": "uploaded",
            "id": entry.get("youtube_id") or entry.get("id"),
            "url": yt_url,
            "at": entry.get("at") or entry.get("uploaded_at") or _now(),
        }
    return migrated


def save_registry(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".processed.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def get_entry(data: dict[str, Any], vk_id: str, title: str = "", vk_date=None) -> dict[str, Any]:
    entry = data.get(vk_id)
    if entry is None:
        entry = {
            "title": title,
            "vk_date": vk_date,
            "attempts": 0,
            "youtube": {"state": "pending"},
            "rutube": {"state": "pending"},
        }
        data[vk_id] = entry
    return entry


def needs_youtube(entry: dict[str, Any], max_retries: int) -> bool:
    state = entry.get("youtube", {}).get("state", "pending")
    if state in TERMINAL_OK:
        return False
    if state == "error" and entry.get("attempts", 0) >= max_retries:
        return False
    return True


def needs_rutube_upload(
    entry: dict[str, Any],
    max_retries: int,
    import_grace_h: float,
    import_enabled: bool = True,
) -> bool:
    """Нужна ли прямая заливка на RuTube.

    При включённом встроенном импорте RuTube с YouTube прямая заливка работает
    только как дозаливка пропущенного и НЕ должна обгонять YouTube: иначе мы
    зальём ролик напрямую, следом он уедет на YouTube, импортёр утянет его
    оттуда — и на RuTube получится дубль. Поэтому заливаем сами только то, что
    уже лежит на YouTube дольше grace-периода и импортом так и не подхвачено.

    Если импорт выключен (RUTUBE_IMPORT_ENABLED=0), ждать нечего и незачем —
    льём сразу, не оглядываясь на YouTube.
    """
    state = entry.get("rutube", {}).get("state", "pending")
    if state in TERMINAL_OK or state == "ingesting":
        return False
    if state == "error" and entry.get("attempts", 0) >= max_retries:
        return False

    if not import_enabled:
        return True

    yt = entry.get("youtube", {})
    if yt.get("state") != "uploaded":
        # Ролика ещё нет на YouTube — пусть сначала уедет туда, импортёр получит
        # свой шанс. Обгонять нельзя, иначе будет дубль.
        return False

    age_h = hours_since(yt.get("at"))
    if age_h is None:
        return True
    return age_h >= import_grace_h


def is_waiting_import(entry: dict[str, Any], import_grace_h: float) -> bool:
    """Ролик на YouTube, но grace-период импорта ещё не вышел."""
    if entry.get("rutube", {}).get("state") in TERMINAL_OK:
        return False
    yt = entry.get("youtube", {})
    if yt.get("state") != "uploaded":
        return False
    age_h = hours_since(yt.get("at"))
    return age_h is not None and age_h < import_grace_h


def mark_youtube_uploaded(
    entry: dict[str, Any], video_id: str, url: str, source: str | None = None
) -> None:
    entry["youtube"] = {"state": "uploaded", "id": video_id, "url": url, "at": _now()}
    if source:
        entry["youtube"]["source"] = source


def mark_youtube_error(entry: dict[str, Any], message: str) -> None:
    entry["youtube"] = {"state": "error", "error": message, "at": _now()}
    entry["attempts"] = entry.get("attempts", 0) + 1


def mark_rutube_uploaded(
    entry: dict[str, Any], video_id: str, url: str, source: str | None = None
) -> None:
    entry["rutube"] = {"state": "uploaded", "video_id": video_id, "url": url, "at": _now()}
    if source:
        entry["rutube"]["source"] = source


def mark_rutube_ingesting(entry: dict[str, Any], video_id: str, url: str, ingest_file: str) -> None:
    entry["rutube"] = {
        "state": "ingesting",
        "video_id": video_id,
        "url": url,
        "ingest_file": ingest_file,
        "posted_at": _now(),
        "error": None,
    }


def mark_rutube_ingest_done(entry: dict[str, Any]) -> None:
    entry["rutube"]["state"] = "uploaded"
    entry["rutube"]["ingest_file"] = None
    entry["rutube"]["at"] = _now()


def mark_rutube_error(entry: dict[str, Any], message: str) -> None:
    entry["rutube"] = {
        "state": "error",
        "video_id": entry.get("rutube", {}).get("video_id"),
        "error": message,
        "ingest_file": None,
        "at": _now(),
    }
    entry["attempts"] = entry.get("attempts", 0) + 1


def mark_rutube_waiting_import(entry: dict[str, Any]) -> None:
    if entry.get("rutube", {}).get("state") in (*TERMINAL_OK, "ingesting"):
        return
    entry["rutube"] = {"state": "waiting_import"}


def pending_rutube_ingests(data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (vk_id, entry) for vk_id, entry in data.items()
        if entry.get("rutube", {}).get("state") == "ingesting"
    ]
