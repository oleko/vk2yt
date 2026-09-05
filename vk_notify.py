# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""Уведомления личным сообщением VK от имени сообщества.

Запасной канал на случай, если Telegram недоступен с сервера (см. notify.py —
на VPS у российских хостеров это реальная и не всегда предсказуемая проблема,
а не баг пайплайна). Нужен отдельный ключ сообщества с правом «Сообщения
сообщества»: VK_TOKEN (сервисный, только для video.get) для messages.send не
подходит — VK отвечает error_code 28 "method is unavailable with service
token" вне зависимости от того, какие права запрошены при создании ключа.

Получателю нужно один раз самому написать сообществу что угодно (аналог
/start у Telegram-бота) — иначе VK не даст сообществу написать первым.
"""
from __future__ import annotations

import logging

import requests

from config import Config

logger = logging.getLogger("vk2yt.vk_notify")

API_URL = "https://api.vk.com/method/messages.send"


def send(config: Config, text: str) -> None:
    if not config.vk_notify_token or not config.vk_notify_user_id:
        return
    try:
        import random
        resp = requests.post(
            API_URL,
            data={
                "access_token": config.vk_notify_token,
                "v": config.vk_api_version,
                "user_id": config.vk_notify_user_id,
                "message": text,
                "random_id": random.randint(1, 2**31 - 1),
            },
            timeout=15,
        )
        data = resp.json()
        if "error" in data:
            err = data["error"]
            logger.warning(
                "VK-уведомление не отправлено: %s %s",
                err.get("error_code"), err.get("error_msg"),
            )
    except requests.RequestException as e:
        logger.warning("VK-уведомление не отправлено: %s", e)
