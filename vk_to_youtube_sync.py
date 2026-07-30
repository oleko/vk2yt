# Copyright (C) 2026 oleko
# Это свободное ПО под лицензией GNU GPL v3 или новее — см. LICENSE.
# Распространяется без каких-либо гарантий.

"""Оркестратор: инвентаризация архива VK, ежедневная заливка на YouTube и RuTube."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import dedup
import plan_store
import registry
import rutube_target
import vk_source
import youtube_target
from config import Config, load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("vk2yt")


def _rutube_client(config: Config):
    if not config.rutube_enabled:
        return None
    try:
        config.require_rutube()
    except RuntimeError as e:
        logger.warning("RuTube отключён: %s", e)
        return None
    return rutube_target.RutubeClient(config)


def cmd_check(config: Config) -> int:
    ok = True

    try:
        config.require_vk()
        success, msg = vk_source.check_access(config)
    except Exception as e:  # noqa: BLE001
        success, msg = False, str(e)
    print(f"[{'OK' if success else 'FAIL'}] VK: {msg}")
    ok = ok and success

    success, msg = youtube_target.check(config)
    print(f"[{'OK' if success else 'FAIL'}] YouTube: {msg}")
    ok = ok and success

    if not config.rutube_enabled:
        print("[SKIP] RuTube отключён (RUTUBE_ENABLED=0)")
        return 0 if ok else 1

    success, msg = rutube_target.check_auth(config)
    print(f"[{'OK' if success else 'FAIL'}] RuTube auth: {msg}")
    ok = ok and success

    success, msg = rutube_target.check_ingest_roundtrip(config)
    print(f"[{'OK' if success else 'FAIL'}] RuTube ingest: {msg}")
    ok = ok and success

    return 0 if ok else 1


def cmd_plan(config: Config) -> int:
    config.require_vk()
    print("Забираю архив VK...")
    items = vk_source.fetch_all_videos(config)
    plan = plan_store.build_or_update_plan(
        config.plan_path, config.vk_group_id, config.daily_limit, items, config.new_first
    )
    reg = registry.load_registry(config.registry_path)
    _print_summary(plan_store.summarize(plan, reg))
    return 0


def _print_summary(s: dict) -> None:
    print(f"Всего роликов в архиве: {s['total']}")
    print(f"Залито на YouTube: {s['youtube_done']}")
    print(
        f"Залито на RuTube: {s['rutube_done']} "
        f"(в обработке: {s['rutube_ingesting']}, ждут импорта: {s['rutube_waiting']})"
    )
    print(f"Осталось: {s['remaining']}, дней: {s['days_left']}, финиш: {s['eta']}")


def cmd_reconcile(config: Config) -> int:
    plan = plan_store.load_plan(config.plan_path)
    if plan is None:
        print("plan.json не найден — сначала выполните --plan")
        return 1

    reg = registry.load_registry(config.registry_path)
    stats = dedup.reconcile(config, plan, reg, _rutube_client(config))
    registry.save_registry(config.registry_path, reg)

    print(f"Найдено уже залитым на YouTube: {stats['youtube']}")
    print(f"Найдено уже залитым на RuTube: {stats['rutube']}")
    print(f"Помечено «ждёт импорта»: {stats['waiting_import']}")
    if stats["ambiguous"]:
        print(f"Неоднозначных названий (пропущены): {stats['ambiguous']}")
    print()
    _print_summary(plan_store.summarize(plan, reg))
    return 0


def cmd_dry_run(config: Config) -> int:
    plan = plan_store.load_plan(config.plan_path)
    if plan is None:
        print("plan.json не найден — сначала выполните --plan")
        return 1
    reg = registry.load_registry(config.registry_path)
    batch = plan_store.next_batch(
        plan, reg, config.daily_limit, config.max_retries,
        config.rutube_enabled, config.rutube_import_grace_h,
    )
    print(f"Сегодня будет обработано {len(batch)} роликов:")
    for item in batch:
        entry = registry.get_entry(reg, item["vk_id"])
        need_yt = registry.needs_youtube(entry, config.max_retries)
        need_rt = config.rutube_enabled and registry.needs_rutube_upload(
            entry, config.max_retries, config.rutube_import_grace_h
        )
        targets = ",".join(t for t, need in (("YT", need_yt), ("RT", need_rt)) if need)
        print(f"  #{item['order']:<5} [{targets:<5}] {item['vk_id']}  {item['title'][:60]}")
    return 0


def _reconcile_ingests(config: Config, client, reg: dict) -> None:
    """Проверяет ролики, которые RuTube ещё качает у нас по ссылке."""
    pending = registry.pending_rutube_ingests(reg)
    if not pending:
        return
    logger.info("Проверяю статус %d роликов в обработке у RuTube", len(pending))

    for vk_id, entry in pending:
        rt = entry["rutube"]
        try:
            ready, _ = client.is_ready(rt["video_id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("Статус RuTube %s недоступен: %s", rt["video_id"], e)
            continue

        if ready:
            registry.mark_rutube_ingest_done(entry)
            rutube_target.remove_from_ingest(rt.get("ingest_file"))
            logger.info("RuTube готов: %s -> %s", vk_id, rt.get("url"))
            continue

        age_h = registry.hours_since(rt.get("posted_at"))
        if age_h is not None and age_h > config.rutube_ingest_timeout_h:
            registry.mark_rutube_error(entry, "Таймаут ожидания обработки RuTube")
            rutube_target.remove_from_ingest(rt.get("ingest_file"))
            logger.warning("RuTube таймаут: %s", vk_id)

    registry.save_registry(config.registry_path, reg)


def _upload_rutube(config: Config, client, entry: dict, item: dict, local_path: Path) -> None:
    size_mb = local_path.stat().st_size / (1024 * 1024)
    if size_mb > config.rutube_max_file_mb:
        registry.mark_rutube_error(
            entry, f"Файл {size_mb:.0f} МБ больше лимита RuTube {config.rutube_max_file_mb} МБ"
        )
        return

    ingest_path, public_url = rutube_target.place_in_ingest(config, local_path)
    try:
        video_id = client.upload_by_url(
            public_url, item["title"], item["description"],
            config.rutube_category_id, config.rutube_is_hidden,
        )
    except Exception:
        rutube_target.remove_from_ingest(str(ingest_path))
        raise

    client.patch_video(
        video_id,
        title=item["title"],
        description=item["description"],
        category_id=config.rutube_category_id,
        is_hidden=config.rutube_is_hidden,
    )
    registry.mark_rutube_ingesting(
        entry, video_id, rutube_target.video_url(video_id), str(ingest_path)
    )


def cmd_run(config: Config, limit: int | None, only: str | None) -> int:
    config.require_vk()
    config.ensure_dirs()

    plan = plan_store.load_plan(config.plan_path)
    if plan is None:
        print("plan.json не найден — сначала выполните --plan")
        return 1

    reg = registry.load_registry(config.registry_path)
    client = _rutube_client(config) if only != "youtube" else None

    if client is not None:
        _reconcile_ingests(config, client, reg)

    # Сверка с площадками: что уже залито вручную или утянуто импортом RuTube
    stats = dedup.reconcile(config, plan, reg, client)
    if any(stats.values()):
        logger.info(
            "Сверка: YouTube +%d, RuTube +%d, ждут импорта %d, неоднозначных %d",
            stats["youtube"], stats["rutube"], stats["waiting_import"], stats["ambiguous"],
        )
    registry.save_registry(config.registry_path, reg)

    if client is not None:
        known = {
            str(e["rutube"]["ingest_file"])
            for e in reg.values()
            if e.get("rutube", {}).get("ingest_file")
        }
        rutube_target.gc_ingest(config, known)

    batch = plan_store.next_batch(
        plan, reg, config.daily_limit, config.max_retries,
        config.rutube_enabled, config.rutube_import_grace_h,
    )
    if limit:
        batch = batch[:limit]

    if not batch:
        logger.info("Нечего обрабатывать сегодня")
        return 0

    logger.info("В обработке %d роликов", len(batch))

    for item in batch:
        vk_id = item["vk_id"]
        entry = registry.get_entry(reg, vk_id, item["title"], item["vk_date"])

        need_yt = only != "rutube" and registry.needs_youtube(entry, config.max_retries)
        need_rt = (
            only != "youtube"
            and client is not None
            and registry.needs_rutube_upload(
                entry, config.max_retries, config.rutube_import_grace_h
            )
        )
        if not need_yt and not need_rt:
            continue

        logger.info("Обрабатываю %s: %s", vk_id, item["title"])
        local_path = None
        try:
            local_path = vk_source.download_video(
                vk_id, config.downloads_dir, config.vk_cookies_file, item.get("url")
            )
        except Exception as e:  # noqa: BLE001
            registry.mark_youtube_error(entry, f"Скачивание не удалось: {e}")
            registry.save_registry(config.registry_path, reg)
            logger.error("Скачивание %s не удалось: %s", vk_id, e)
            continue

        try:
            if need_yt:
                try:
                    video_id, url = youtube_target.upload_video(
                        config, local_path, item["title"], item["description"]
                    )
                    registry.mark_youtube_uploaded(entry, video_id, url)
                    registry.save_registry(config.registry_path, reg)
                    logger.info("YouTube OK: %s -> %s", vk_id, url)
                except youtube_target.QuotaExceededError:
                    logger.error("Квота YouTube исчерпана, прерываю прогон")
                    registry.save_registry(config.registry_path, reg)
                    return 0
                except Exception as e:  # noqa: BLE001
                    registry.mark_youtube_error(entry, str(e))
                    registry.save_registry(config.registry_path, reg)
                    logger.error("YouTube ошибка для %s: %s", vk_id, e)

            if need_rt:
                try:
                    _upload_rutube(config, client, entry, item, local_path)
                    logger.info(
                        "RuTube в обработке: %s -> %s", vk_id, entry["rutube"].get("video_id")
                    )
                except Exception as e:  # noqa: BLE001
                    registry.mark_rutube_error(entry, str(e))
                    logger.error("RuTube ошибка для %s: %s", vk_id, e)
                registry.save_registry(config.registry_path, reg)
        finally:
            if local_path and local_path.exists():
                local_path.unlink()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="VK -> YouTube + RuTube sync")
    parser.add_argument("--check", action="store_true", help="Самопроверка без заливок")
    parser.add_argument("--plan", action="store_true", help="Построить/обновить plan.json")
    parser.add_argument(
        "--reconcile", action="store_true",
        help="Сверить план с тем, что уже есть на YouTube и RuTube",
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать порцию без скачивания")
    parser.add_argument("--limit", type=int, default=None, help="Ограничить число роликов")
    parser.add_argument("--only", choices=["youtube", "rutube"], default=None)
    args = parser.parse_args()

    config = load_config()

    if args.check:
        return cmd_check(config)
    if args.plan:
        return cmd_plan(config)
    if args.reconcile:
        return cmd_reconcile(config)
    if args.dry_run:
        return cmd_dry_run(config)
    return cmd_run(config, args.limit, args.only)


if __name__ == "__main__":
    sys.exit(main())
