# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""Заливка на YouTube: веб-OAuth (Flow) + resumable upload."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from config import Config

logger = logging.getLogger("vk2yt.youtube")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    # нужен, чтобы править название и описание уже залитых роликов
    # (videos.update); одного youtube.upload для этого не хватает
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
TITLE_MAX = 100
DESC_MAX = 5000


class QuotaExceededError(RuntimeError):
    pass


class NotAuthorizedError(RuntimeError):
    """Нет валидного token.json — нужно пройти веб-авторизацию через /oauth2start."""


class AuthExpiredError(NotAuthorizedError):
    """Refresh-токен истёк или отозван (invalid_grant) — не проблема ролика.

    Если не отличать это от обычной ошибки, каждый ролик в этом прогоне
    получает +1 к attempts за чужую проблему и рано или поздно уходит
    в терминальную ошибку сам по себе, хотя с файлом всё в порядке.
    """


def _sanitize_title(title: str) -> str:
    title = re.sub(r"[<>]", "", title or "").strip()
    if not title:
        title = "Без названия"
    return title[:TITLE_MAX]


def _sanitize_description(description: str) -> str:
    return (description or "")[:DESC_MAX]


def build_flow(config: Config, **kwargs) -> Flow:
    if not config.youtube_client_secret.exists():
        raise RuntimeError(
            f"Не найден {config.youtube_client_secret} — см. README, раздел "
            "«Получение доступа к YouTube Data API v3»"
        )
    if not config.youtube_redirect_uri:
        raise RuntimeError("Не задан YOUTUBE_REDIRECT_URI в .env")
    return Flow.from_client_secrets_file(
        str(config.youtube_client_secret),
        scopes=SCOPES,
        redirect_uri=config.youtube_redirect_uri,
        **kwargs,
    )


def get_authorization_url(config: Config) -> tuple[str, str, str]:
    flow = build_flow(config)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    return auth_url, state, flow.code_verifier


def exchange_code(
    config: Config, state: str, code_verifier: str, authorization_response: str
) -> Credentials:
    flow = build_flow(config, code_verifier=code_verifier, autogenerate_code_verifier=False)
    flow.state = state
    flow.fetch_token(authorization_response=authorization_response)
    creds = flow.credentials
    config.youtube_token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_credentials(config: Config) -> Credentials:
    if not config.youtube_token_path.exists():
        raise NotAuthorizedError(
            "Нет token.json — пройдите авторизацию через дашборд (/oauth2start)"
        )
    creds = Credentials.from_authorized_user_file(str(config.youtube_token_path), SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise AuthExpiredError(
                    f"Токен YouTube истёк или отозван (invalid_grant) — "
                    f"авторизуйтесь заново через /oauth2start: {e}"
                ) from e
            config.youtube_token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise NotAuthorizedError(
                "token.json невалиден и не может быть обновлён — авторизуйтесь заново через /oauth2start"
            )

    return creds


def get_my_channel(config: Config) -> dict:
    creds = get_credentials(config)
    youtube = build("youtube", "v3", credentials=creds)
    resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise RuntimeError("У авторизованного аккаунта нет ни одного YouTube-канала")
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def upload_video(config: Config, file_path: Path, title: str, description: str) -> tuple[str, str]:
    creds = get_credentials(config)
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": _sanitize_title(title),
            "description": _sanitize_description(description),
        },
        "status": {"privacyStatus": "public"},
    }

    media = MediaFileUpload(str(file_path), chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    try:
        while response is None:
            status, response = request.next_chunk()
            if status:
                logger.info("YouTube upload %s: %d%%", file_path.name, int(status.progress() * 100))
    except HttpError as e:
        reason = ""
        try:
            reason = e.error_details[0].get("reason", "") if e.error_details else ""
        except Exception:  # noqa: BLE001
            pass
        if e.resp.status in (403, 400) and ("quota" in str(e).lower() or reason == "quotaExceeded"):
            raise QuotaExceededError(str(e)) from e
        raise

    video_id = response["id"]
    url = f"https://www.youtube.com/watch?v={video_id}"
    return video_id, url


def update_video_meta(
    config: Config, video_id: str, title: str, description: str
) -> tuple[bool, int]:
    """Обновляет название и описание уже залитого ролика.

    Возвращает (было ли изменение, потрачено юнитов). Сначала читаем текущий
    snippet: videos.update требует categoryId, и без него ролику собьётся
    категория, а заодно потерялись бы теги. Если менять нечего — запрос не
    отправляется, чтобы не жечь квоту (обновление стоит 50 юнитов из 10 000).
    """
    creds = get_credentials(config)
    youtube = build("youtube", "v3", credentials=creds)

    resp = youtube.videos().list(part="snippet", id=video_id).execute()
    units = 1
    items = resp.get("items", [])
    if not items:
        raise RuntimeError(f"Ролик {video_id} не найден на канале")

    snippet = items[0]["snippet"]
    new_title = _sanitize_title(title)
    new_desc = _sanitize_description(description)

    if snippet.get("title") == new_title and snippet.get("description") == new_desc:
        return False, units

    snippet["title"] = new_title
    snippet["description"] = new_desc
    youtube.videos().update(part="snippet", body={"id": video_id, "snippet": snippet}).execute()
    return True, units + 50


def check(config: Config) -> tuple[bool, str]:
    if not config.youtube_client_secret.exists():
        return False, f"Не найден файл {config.youtube_client_secret}"
    try:
        channel = get_my_channel(config)
    except NotAuthorizedError as e:
        return False, f"{e} (URL: {config.youtube_redirect_uri.rsplit('/', 1)[0] if config.youtube_redirect_uri else '?'}/oauth2start)"
    except Exception as e:  # noqa: BLE001
        return False, f"Ошибка авторизации YouTube: {e}"

    msg = f"Авторизован канал «{channel['title']}» ({channel['id']})"
    if config.youtube_expected_channel_id and channel["id"] != config.youtube_expected_channel_id:
        return False, (
            f"{msg} — но ожидался канал {config.youtube_expected_channel_id}! "
            "Перепройдите авторизацию тем аккаунтом, который управляет нужным каналом."
        )
    return True, msg
