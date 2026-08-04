# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""Минимальный веб-дашборд для мониторинга заливки VK -> YouTube."""
from __future__ import annotations

import subprocess
import sys
from functools import wraps
from pathlib import Path

from flask import Flask, Response, make_response, redirect, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import plan_store
import registry
import youtube_target
from config import load_config

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
config = load_config()
app.secret_key = config.dash_password or "vk2yt-dev-secret"

QUEUE_PAGE_SIZE = 10


def _paginate(items: list, page_param: str) -> tuple[list, int, int, int]:
    """Режет список на страницы по параметру из query string (?<page_param>=N).

    Возвращает (роликов на странице, номер страницы, всего страниц, всего роликов).
    """
    total = len(items)
    total_pages = max(1, (total + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
    try:
        page = int(request.args.get(page_param, 1))
    except ValueError:
        page = 1
    page = min(max(page, 1), total_pages)
    start = (page - 1) * QUEUE_PAGE_SIZE
    return items[start:start + QUEUE_PAGE_SIZE], page, total_pages, total


def requires_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not config.dash_password:
            return f(*args, **kwargs)
        supplied = request.args.get("password") or request.cookies.get("dash_password")
        if supplied != config.dash_password:
            return Response(
                '<form method="get">'
                '<input type="password" name="password" placeholder="Пароль">'
                '<button type="submit">Войти</button></form>',
                mimetype="text/html",
            )
        resp = make_response(f(*args, **kwargs))
        resp.set_cookie("dash_password", supplied)
        return resp
    return wrapper


def _tail_log(n: int = 200) -> str:
    if not config.log_path.exists():
        return "(лог пуст)"
    lines = config.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


def _render(
    summary: dict,
    yt_queue: tuple[list, int, int, int],
    rt_queue: tuple[list, int, int, int],
    recent: list[dict],
    log_tail: str,
    yt_status: str,
) -> str:
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def pager(page_param: str, page: int, total_pages: int) -> str:
        if total_pages <= 1:
            return ""
        other = "rt_page" if page_param == "yt_page" else "yt_page"
        other_val = request.args.get(other)
        other_qs = f"&{other}={other_val}" if other_val else ""
        links = []
        for p in range(1, total_pages + 1):
            if p == page:
                links.append(f"<b>{p}</b>")
            else:
                links.append(f'<a href="/?{page_param}={p}{other_qs}">{p}</a>')
        return f'<div class="pager">Стр.: {" ".join(links)}</div>'

    def queue_section(title: str, page_param: str, queue: tuple[list, int, int, int]) -> str:
        items, page, total_pages, total = queue
        rows = "".join(f"<li>#{i.get('order', '?')} {esc(i['title'])}</li>" for i in items)
        return f"""
        <div class="queue-col">
          <h3>{title} <span class="count">(всего {total})</span></h3>
          <ul>{rows or '<li style="list-style:none">(пусто)</li>'}</ul>
          {pager(page_param, page, total_pages)}
        </div>
        """

    def cell(target: dict) -> str:
        state = target.get("state", "-")
        if state == "uploaded":
            src = target.get("source")
            mark = {"import": " (импорт)", "pre-existing": " (было)"}.get(src, "")
            return f'<a href="{esc(target.get("url"))}" target="_blank">открыть</a>{mark}'
        label = {
            "ingesting": "в обработке",
            "waiting_import": "ждёт импорта",
            "pending": "в очереди",
            "error": "ошибка",
        }.get(state, state)
        return f'<span title="{esc(target.get("error", ""))}">{esc(label)}</span>'

    rows = ""
    for r in recent:
        rows += (
            f"<tr><td>{esc(r['title'])}</td>"
            f"<td>{cell(r.get('youtube', {}))}</td>"
            f"<td>{cell(r.get('rutube', {}))}</td></tr>"
        )

    return f"""
    <html><head><meta charset="utf-8"><title>VK2YT</title>
    <style>
      body {{ font-family: sans-serif; max-width: 900px; margin: 2em auto; }}
      table {{ border-collapse: collapse; width: 100%; }}
      td, th {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
      pre {{ background: #111; color: #ddd; padding: 1em; overflow-x: auto; max-height: 400px; }}
      .stats span {{ margin-right: 1.5em; }}
      button {{ padding: 8px 16px; }}
      .queues {{ display: flex; gap: 2em; flex-wrap: wrap; }}
      .queue-col {{ flex: 1; min-width: 280px; }}
      .queue-col h3 {{ margin-bottom: 0.3em; }}
      .queue-col .count {{ font-weight: normal; color: #666; font-size: 0.85em; }}
      .queue-col ol {{ padding-left: 1.5em; margin-top: 0.3em; }}
      .queue-col li {{ margin-bottom: 0.2em; }}
      .pager {{ margin-top: 0.5em; }}
      .pager a {{ margin-right: 0.5em; }}
      .pager b {{ margin-right: 0.5em; }}
    </style></head>
    <body>
      <h1>VK &rarr; YouTube + RuTube</h1>
      <div class="stats">
        <span>Всего: <b>{summary['total']}</b></span>
        <span>YouTube: <b>{summary['youtube_done']}</b></span>
        <span>RuTube: <b>{summary['rutube_done']}</b>
          (в обработке: {summary['rutube_ingesting']}, ждут импорта: {summary['rutube_waiting']})</span>
        <span>Осталось: <b>{summary['remaining']}</b></span>
        <span>Дней: <b>{summary['days_left']}</b></span>
        <span>Финиш: <b>{summary['eta'] or '-'}</b></span>
      </div>

      <form method="post" action="/run">
        <button type="submit">Запустить сейчас</button>
      </form>

      <h2>YouTube</h2>
      <p>{esc(yt_status)}</p>
      <p><a href="/oauth2start">Авторизовать / переавторизовать YouTube</a></p>

      <h2>Сегодняшняя порция</h2>
      <div class="queues">
        {queue_section("YouTube", "yt_page", yt_queue)}
        {queue_section("RuTube", "rt_page", rt_queue)}
      </div>

      <h2>Последние обработанные</h2>
      <table>
        <tr><th>Название</th><th>YouTube</th><th>RuTube</th></tr>
        {rows or '<tr><td colspan="3">(пока пусто)</td></tr>'}
      </table>

      <h2>Лог</h2>
      <pre>{esc(log_tail)}</pre>
    </body></html>
    """


@app.route("/")
@requires_auth
def index():
    plan = plan_store.load_plan(config.plan_path)
    reg = registry.load_registry(config.registry_path)

    if plan is None:
        return "<h1>plan.json не найден</h1><p>Выполните: python vk_to_youtube_sync.py --plan</p>"

    summary = plan_store.summarize(plan, reg)
    yt_batch = plan_store.next_batch_youtube(
        plan, reg, config.daily_limit, config.max_retries
    )
    rt_batch = []
    if config.rutube_enabled:
        rt_batch = plan_store.next_batch_rutube(
            plan, reg, config.rutube_daily_limit, config.max_retries,
            config.rutube_import_grace_h, config.rutube_import_enabled,
        )
    yt_queue = _paginate(yt_batch, "yt_page")
    rt_queue = _paginate(rt_batch, "rt_page")

    recent_ids = [
        vk_id for vk_id, e in reg.items()
        if e.get("youtube", {}).get("state") == "uploaded"
    ]
    recent_ids.sort(key=lambda vk_id: reg[vk_id].get("youtube", {}).get("at", ""), reverse=True)
    recent = [reg[vk_id] for vk_id in recent_ids[:20]]

    _, yt_status = youtube_target.check(config)

    return _render(summary, yt_queue, rt_queue, recent, _tail_log(), yt_status)


@app.route("/run", methods=["POST"])
@requires_auth
def run_now():
    script = Path(__file__).parent / "vk_to_youtube_sync.py"
    subprocess.Popen(
        ["flock", "-n", str(config.lock_path), sys.executable, str(script)],
        stdout=open(config.log_path, "a"),
        stderr=subprocess.STDOUT,
    )
    return redirect(url_for("index"))


@app.route("/oauth2start")
@requires_auth
def oauth2start():
    auth_url, state, code_verifier = youtube_target.get_authorization_url(config)
    session["oauth_state"] = state
    session["oauth_code_verifier"] = code_verifier
    return redirect(auth_url)


@app.route("/oauth2callback")
@requires_auth
def oauth2callback():
    state = session.get("oauth_state")
    code_verifier = session.get("oauth_code_verifier")
    if not state or not code_verifier or request.args.get("state") != state:
        return "Ошибка: state/code_verifier не совпадает, начните заново через /oauth2start", 400

    try:
        youtube_target.exchange_code(config, state, code_verifier, request.url)
    except Exception as e:  # noqa: BLE001
        app.logger.exception("oauth2callback: exchange_code failed")
        return f"Ошибка обмена кода на токен: {e}", 400

    ok, msg = youtube_target.check(config)
    return (
        f"<h1>{'Готово' if ok else 'Авторизовано, но есть проблема'}</h1>"
        f"<p>{msg}</p><p><a href='/'>Вернуться на дашборд</a></p>"
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=config.dash_port)
