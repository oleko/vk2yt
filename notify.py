# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""Уведомления о результатах прогона в Telegram.

Работает только если заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID — без них
send() молча ничего не делает, остальной пайплайн эту разницу не замечает.
Сбой самой отправки (сеть, неверный токен) — не повод ронять прогон, поэтому
здесь только логируем предупреждение, а не бросаем исключение наружу.
"""
from __future__ import annotations

import logging

import requests

from config import Config

logger = logging.getLogger("vk2yt.notify")

API = "https://api.telegram.org/bot{token}/sendMessage"


def send(config: Config, text: str) -> None:
    if not config.telegram_bot_token or not config.telegram_chat_id:
        return
    try:
        resp = requests.post(
            API.format(token=config.telegram_bot_token),
            json={
                "chat_id": config.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not resp.ok:
            logger.warning(
                "Telegram-уведомление не отправлено (%s): %s", resp.status_code, resp.text[:200]
            )
    except requests.RequestException as e:
        logger.warning("Telegram-уведомление не отправлено: %s", e)
