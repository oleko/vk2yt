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
import descriptions
import notify
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
        config.plan_path, config.vk_group_id, config.daily_limit,
        items, config.new_first, config,
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


def _batches(config: Config, plan: dict, reg: dict) -> tuple[list, list]:
    """Очереди считаются раздельно: у YouTube квота, у RuTube её нет."""
    yt_batch = plan_store.next_batch_youtube(
        plan, reg, config.daily_limit, config.max_retries
    )
    rt_batch = []
    if config.rutube_enabled:
        rt_batch = plan_store.next_batch_rutube(
            plan, reg, config.rutube_daily_limit, config.max_retries,
            config.rutube_import_grace_h, config.rutube_import_enabled,
        )
    return yt_batch, rt_batch


def cmd_dry_run(config: Config) -> int:
    plan = plan_store.load_plan(config.plan_path)
    if plan is None:
        print("plan.json не найден — сначала выполните --plan")
        return 1
    reg = registry.load_registry(config.registry_path)
    yt_batch, rt_batch = _batches(config, plan, reg)

    rt_ids = {i["vk_id"] for i in rt_batch}
    print(f"YouTube: {len(yt_batch)} роликов (лимит {config.daily_limit}, квота API)")
    for item in yt_batch:
        both = " +RT" if item["vk_id"] in rt_ids else ""
        print(f"  #{item['order']:<5} {item['title'][:60]}{both}")

    only_rt = [i for i in rt_batch if i["vk_id"] not in {x["vk_id"] for x in yt_batch}]
    print(f"\nRuTube: {len(rt_batch)} роликов (лимит {config.rutube_daily_limit}), "
          f"из них только на RuTube: {len(only_rt)}")
    for item in only_rt[:15]:
        print(f"  #{item['order']:<5} {item['title'][:60]}")
    if len(only_rt) > 15:
        print(f"  ... и ещё {len(only_rt) - 15}")

    downloads = len({i["vk_id"] for i in yt_batch} | rt_ids)
    print(f"\nБудет скачано из VK: {downloads} роликов (каждый — один раз)")
    return 0


def cmd_preview_desc(config: Config, limit: int | None) -> int:
    """Показать, что получится в названии и описании, ничего не меняя."""
    plan = plan_store.load_plan(config.plan_path)
    if plan is None:
        print("plan.json не найден — сначала выполните --plan")
        return 1

    items = plan["items"]
    # берём разные случаи: своё описание / только пост / пусто / заглушка
    buckets: dict[str, list] = {"описание": [], "пост": [], "пусто": [], "заглушка": []}
    for i in items:
        if descriptions.PLACEHOLDER_TITLE.match((i.get("title") or "").strip()):
            key = "заглушка"
        elif descriptions.is_meaningful(i.get("description") or "", i.get("title") or ""):
            key = "описание"
        elif descriptions.is_meaningful(i.get("wall_text") or "", i.get("title") or ""):
            key = "пост"
        else:
            key = "пусто"
        if len(buckets[key]) < (limit or 3):
            buckets[key].append(i)

    for key, group in buckets.items():
        print(f"\n{'=' * 70}\nИСТОЧНИК: {key}  ({len(group)} примеров)\n{'=' * 70}")
        for i in group:
            print(f"\n--- #{i['order']} {i['vk_id']}")
            print(f"название VK : {i.get('title', '')[:80]!r}")
            print(f"название -> : {descriptions.pick_title(i, config)[:80]!r}")
            print("описание ->")
            for line in descriptions.build_description(i, config).splitlines():
                print(f"    {line}")
    return 0


def cmd_update_meta(config: Config, limit: int | None, only: str | None) -> int:
    """Проставить собранные название и описание уже залитым роликам."""
    plan = plan_store.load_plan(config.plan_path)
    if plan is None:
        print("plan.json не найден — сначала выполните --plan")
        return 1

    reg = registry.load_registry(config.registry_path)
    by_id = {i["vk_id"]: i for i in plan["items"]}
    client = _rutube_client(config) if only != "youtube" else None

    limit = limit or config.meta_update_limit
    units = 0
    changed_yt = changed_rt = skipped = 0

    # Лимит ограничивает именно изменения: уже совпадающие ролики почти
    # ничего не стоят (1 юнит на чтение) и не должны съедать порцию, иначе
    # повторный прогон будет топтаться на начале реестра.
    for vk_id, entry in reg.items():
        if changed_yt + changed_rt >= limit:
            break
        item = by_id.get(vk_id)
        if item is None:
            continue

        title = descriptions.pick_title(item, config)
        description = descriptions.build_description(item, config)

        if only != "rutube" and entry.get("youtube", {}).get("state") == "uploaded":
            vid = entry["youtube"].get("id")
            if vid:
                try:
                    changed, spent = youtube_target.update_video_meta(
                        config, vid, title, description
                    )
                    units += spent
                    if changed:
                        changed_yt += 1
                        logger.info("YouTube обновлён: %s — %s", vid, title[:50])
                    else:
                        skipped += 1
                except Exception as e:  # noqa: BLE001
                    logger.error("YouTube: не удалось обновить %s: %s", vid, e)

        if client is not None and entry.get("rutube", {}).get("state") == "uploaded":
            vid = entry["rutube"].get("video_id")
            if vid:
                try:
                    if client.update_meta_if_changed(vid, title, description):
                        changed_rt += 1
                        logger.info("RuTube обновлён: %s — %s", vid, title[:50])
                    else:
                        skipped += 1
                except Exception as e:  # noqa: BLE001
                    logger.error("RuTube: не удалось обновить %s: %s", vid, e)

    registry.save_registry(config.registry_path, reg)
    print(f"Обновлено на YouTube : {changed_yt}")
    print(f"Обновлено на RuTube  : {changed_rt}")
    print(f"Уже совпадало        : {skipped}")
    print(f"Потрачено юнитов YouTube: {units} из 10 000 суточных")
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


def _upload_rutube(
    config: Config, client, entry: dict, item: dict, local_path: Path,
    title: str, description: str,
) -> None:
    size_mb = local_path.stat().st_size / (1024 * 1024)
    if size_mb > config.rutube_max_file_mb:
        registry.mark_rutube_error(
            entry, f"Файл {size_mb:.0f} МБ больше лимита RuTube {config.rutube_max_file_mb} МБ"
        )
        return

    ingest_path, public_url = rutube_target.place_in_ingest(config, local_path)
    try:
        video_id = client.upload_by_url(
            public_url, title, description,
            config.rutube_category_id, config.rutube_is_hidden,
        )
    except Exception:
        rutube_target.remove_from_ingest(str(ingest_path))
        raise

    client.patch_video(
        video_id,
        title=title,
        description=description,
        category_id=config.rutube_category_id,
        is_hidden=config.rutube_is_hidden,
    )
    registry.mark_rutube_ingesting(
        entry, video_id, rutube_target.video_url(video_id), str(ingest_path)
    )


def _format_report(report: dict) -> str:
    lines = []
    if report["new_from_vk"]:
        lines.append(f"Новых роликов из VK: {report['new_from_vk']}")

    yt = f"YouTube: +{report['yt_uploaded']}"
    if report["yt_errors"]:
        yt += f" (ошибок: {report['yt_errors']})"
    if report["yt_quota"]:
        yt += " — квота исчерпана"
    lines.append(yt)

    rt = f"RuTube: +{report['rt_uploaded']}"
    if report["rt_errors"]:
        rt += f" (ошибок: {report['rt_errors']})"
    if report["rt_quota"]:
        rt += " — суточный лимит исчерпан"
    lines.append(rt)

    if report["download_errors"]:
        lines.append(f"Не удалось скачать из VK: {report['download_errors']}")

    ok = not report["yt_errors"] and not report["rt_errors"] and not report["download_errors"]
    icon = "✅" if ok else "⚠️"
    return f"{icon} vk2yt\n" + "\n".join(lines)


def cmd_run(config: Config, limit: int | None, only: str | None) -> int:
    report = {
        "new_from_vk": 0,
        "yt_uploaded": 0, "yt_errors": 0, "yt_quota": False,
        "rt_uploaded": 0, "rt_errors": 0, "rt_quota": False,
        "download_errors": 0,
    }
    try:
        code = _cmd_run(config, limit, only, report)
    except Exception as e:  # noqa: BLE001
        logger.exception("Прогон упал с необработанной ошибкой")
        notify.send(config, f"🔴 vk2yt: прогон упал с ошибкой\n{e}")
        raise
    notify.send(config, _format_report(report))
    return code


def _cmd_run(config: Config, limit: int | None, only: str | None, report: dict) -> int:
    config.require_vk()
    config.ensure_dirs()

    plan = plan_store.load_plan(config.plan_path)

    # Свежий срез сообщества: подхватываем ролики, появившиеся со вчера. Иначе
    # пайплайн встанет, как только закончится архив.
    if config.auto_refresh_plan or plan is None:
        before = plan["total"] if plan else 0
        try:
            items = vk_source.fetch_all_videos(config)
            plan = plan_store.build_or_update_plan(
                config.plan_path, config.vk_group_id, config.daily_limit,
                items, config.new_first, config,
            )
            added = plan["total"] - before
            report["new_from_vk"] = added
            if added:
                where = "в начало очереди" if config.new_first else "в конец очереди"
                logger.info("В сообществе новых роликов: %d — добавлены %s", added, where)
        except Exception as e:  # noqa: BLE001
            logger.error("Не удалось обновить план из VK: %s", e)
            if plan is None:
                return 1

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

    yt_batch, rt_batch = _batches(config, plan, reg)
    if only == "youtube":
        rt_batch = []
    if only == "rutube":
        yt_batch = []

    # Объединяем в порядке плана: ролик, нужный обеим площадкам, скачивается один раз
    order = {}
    for item in yt_batch + rt_batch:
        order.setdefault(item["vk_id"], item)
    batch = sorted(order.values(), key=lambda i: i.get("order", 0))
    if limit:
        batch = batch[:limit]

    if not batch:
        logger.info("Нечего обрабатывать сегодня")
        return 0

    yt_ids = {i["vk_id"] for i in yt_batch}
    rt_ids = {i["vk_id"] for i in rt_batch}
    logger.info(
        "В обработке %d роликов (YouTube: %d, RuTube: %d)",
        len(batch), len(yt_ids & {i["vk_id"] for i in batch}),
        len(rt_ids & {i["vk_id"] for i in batch}),
    )

    for item in batch:
        vk_id = item["vk_id"]
        entry = registry.get_entry(reg, vk_id, item["title"], item["vk_date"])

        need_yt = vk_id in yt_ids
        need_rt = client is not None and vk_id in rt_ids
        if need_rt and not rutube_target.ingest_has_room(config):
            logger.warning(
                "ingest/ упёрся в лимит %d МБ — RuTube для %s отложен до следующего прогона",
                config.rutube_ingest_max_mb, vk_id,
            )
            need_rt = False
        if not need_yt and not need_rt:
            continue

        # Название и описание собираются один раз и одинаково едут на обе площадки
        title = descriptions.pick_title(item, config)
        description = descriptions.build_description(item, config)

        logger.info("Обрабатываю %s: %s", vk_id, title)
        local_path = None
        try:
            local_path = vk_source.download_video(
                vk_id, config.downloads_dir, config.vk_cookies_file, item.get("url")
            )
        except Exception as e:  # noqa: BLE001
            registry.mark_download_error(
                entry, f"Скачивание не удалось: {e}", need_yt, need_rt
            )
            registry.save_registry(config.registry_path, reg)
            logger.error("Скачивание %s не удалось: %s", vk_id, e)
            report["download_errors"] += 1
            continue

        try:
            if need_yt:
                try:
                    video_id, url = youtube_target.upload_video(
                        config, local_path, title, description
                    )
                    registry.mark_youtube_uploaded(entry, video_id, url)
                    registry.save_registry(config.registry_path, reg)
                    logger.info("YouTube OK: %s -> %s", vk_id, url)
                    report["yt_uploaded"] += 1
                except youtube_target.QuotaExceededError:
                    logger.error("Квота YouTube исчерпана, прерываю прогон")
                    registry.save_registry(config.registry_path, reg)
                    report["yt_quota"] = True
                    return 0
                except Exception as e:  # noqa: BLE001
                    registry.mark_youtube_error(entry, str(e))
                    registry.save_registry(config.registry_path, reg)
                    logger.error("YouTube ошибка для %s: %s", vk_id, e)
                    report["yt_errors"] += 1

            if need_rt:
                try:
                    _upload_rutube(
                        config, client, entry, item, local_path, title, description
                    )
                    logger.info(
                        "RuTube в обработке: %s -> %s", vk_id, entry["rutube"].get("video_id")
                    )
                    report["rt_uploaded"] += 1
                except rutube_target.RutubeQuotaExceeded as e:
                    # Не наша ошибка и не ошибка ролика: до завтра RuTube всё
                    # равно откажет, поэтому ничего не помечаем и не тратим
                    # попытки — просто перестаём его дёргать в этом прогоне.
                    logger.warning("Суточный лимит загрузок RuTube исчерпан (%s)", e)
                    report["rt_quota"] = True
                    rt_ids.clear()
                except Exception as e:  # noqa: BLE001
                    registry.mark_rutube_error(entry, str(e))
                    logger.error("RuTube ошибка для %s: %s", vk_id, e)
                    report["rt_errors"] += 1
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
    parser.add_argument(
        "--preview-desc", action="store_true",
        help="Показать, какие получатся названия и описания (ничего не меняет)",
    )
    parser.add_argument(
        "--update-meta", action="store_true",
        help="Проставить названия и описания уже залитым роликам",
    )
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
    if args.preview_desc:
        return cmd_preview_desc(config, args.limit)
    if args.update_meta:
        return cmd_update_meta(config, args.limit, args.only)
    if args.dry_run:
        return cmd_dry_run(config)
    return cmd_run(config, args.limit, args.only)


if __name__ == "__main__":
    sys.exit(main())
