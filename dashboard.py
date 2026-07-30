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


def _render(summary: dict, batch: list[dict], recent: list[dict], log_tail: str, yt_status: str) -> str:
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

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

    batch_rows = "".join(f"<li>{esc(b['title'])}</li>" for b in batch)

    return f"""
    <html><head><meta charset="utf-8"><title>VK2YT</title>
    <style>
      body {{ font-family: sans-serif; max-width: 900px; margin: 2em auto; }}
      table {{ border-collapse: collapse; width: 100%; }}
      td, th {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
      pre {{ background: #111; color: #ddd; padding: 1em; overflow-x: auto; max-height: 400px; }}
      .stats span {{ margin-right: 1.5em; }}
      button {{ padding: 8px 16px; }}
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
      <ul>{batch_rows or '<li>(пусто)</li>'}</ul>

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
    batch = plan_store.next_batch(
        plan, reg, config.daily_limit, config.max_retries,
        config.rutube_enabled, config.rutube_import_grace_h,
    )

    recent_ids = [
        vk_id for vk_id, e in reg.items()
        if e.get("youtube", {}).get("state") == "uploaded"
    ]
    recent_ids.sort(key=lambda vk_id: reg[vk_id].get("youtube", {}).get("at", ""), reverse=True)
    recent = [reg[vk_id] for vk_id in recent_ids[:20]]

    _, yt_status = youtube_target.check(config)

    return _render(summary, batch, recent, _tail_log(), yt_status)


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
