#!/usr/bin/env python3
"""Preview or publish an approved AI Content Factory JSON package to Telegram."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from publish_scheduled import (
    PublisherError,
    api_json,
    api_multipart,
    public_url,
    resolve_cover,
)


SCHEMA_VERSION = "agency-content/v2"
DAY_SCHEMA_VERSION = "agency-content/day-v1"
TELEGRAM_TARGET_MIN = 900
TELEGRAM_TARGET_MAX = 1000
TELEGRAM_EDITORIAL_MAX = 1024
MAX_TARGET_MIN = 500
MAX_TARGET_MAX = 800
MAX_EDITORIAL_MAX = 1000
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_HTML_TAGS = {
    "a", "b", "blockquote", "code", "del", "em", "i", "ins", "pre",
    "s", "span", "strike", "strong", "tg-emoji", "tg-spoiler", "u",
}


def save_package(path: Path, package: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def load_package(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise PublisherError(f"JSON-файл не найден: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PublisherError(f"некорректный JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PublisherError("верхний уровень JSON должен быть объектом")
    return data


def as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PublisherError(f"поле {label} должно быть объектом")
    return value


def visible_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]*>", "", value)
    return html.unescape(without_tags)


def character_count(value: str) -> int:
    return len(visible_text(value).strip())


class TelegramHTMLValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in TELEGRAM_HTML_TAGS:
            raise PublisherError(f"Telegram HTML: неподдерживаемый тег <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            raise PublisherError(f"Telegram HTML: некорректно закрыт тег </{tag}>")
        self.stack.pop()

    def close(self) -> None:
        super().close()
        if self.stack:
            raise PublisherError(
                f"Telegram HTML: не закрыт тег <{self.stack[-1]}>"
            )


def validate_telegram_html(value: str) -> None:
    parser = TelegramHTMLValidator()
    parser.feed(value)
    parser.close()


def parse_datetime(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise PublisherError(f"поле {label} обязательно")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublisherError(f"некорректное поле {label}: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublisherError(f"поле {label} должно содержать часовой пояс")
    return parsed


def package_fields(
    package: dict[str, Any], root: Path
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path | None]:
    schema_version = package.get("schema_version")
    if schema_version not in {SCHEMA_VERSION, DAY_SCHEMA_VERSION}:
        raise PublisherError(
            f"ожидается schema_version={SCHEMA_VERSION} или {DAY_SCHEMA_VERSION}"
        )

    if schema_version == DAY_SCHEMA_VERSION:
        if not isinstance(package.get("package_id"), str) or not package["package_id"]:
            raise PublisherError("отсутствует package_id")
        platforms_label = "publications"
        platforms = as_dict(package.get("publications"), platforms_label)
    else:
        if not isinstance(package.get("content_group_id"), str) or not package["content_group_id"]:
            raise PublisherError("отсутствует content_group_id")
        platforms_label = "platforms"
        platforms = as_dict(package.get("platforms"), platforms_label)

    telegram = as_dict(platforms.get("telegram"), f"{platforms_label}.telegram")
    max_post = as_dict(platforms.get("max"), f"{platforms_label}.max")
    for name, post in (("telegram", telegram), ("max", max_post)):
        if not isinstance(post.get("content"), str) or not post["content"].strip():
            raise PublisherError(f"отсутствует {platforms_label}.{name}.content")
        if schema_version == SCHEMA_VERSION:
            if not isinstance(post.get("content_id"), str) or not post["content_id"]:
                raise PublisherError(f"отсутствует platforms.{name}.content_id")

    telegram_count = character_count(telegram["content"])
    max_count = character_count(max_post["content"])
    if telegram_count > TELEGRAM_EDITORIAL_MAX:
        raise PublisherError(
            f"Telegram: {telegram_count} символов, редакционный максимум "
            f"{TELEGRAM_EDITORIAL_MAX}"
        )
    if max_count > MAX_EDITORIAL_MAX:
        raise PublisherError(
            f"MAX: {max_count} символов, редакционный максимум {MAX_EDITORIAL_MAX}"
        )
    if telegram.get("format", "HTML").upper() != "HTML":
        raise PublisherError("platforms.telegram.format должен быть HTML")
    validate_telegram_html(telegram["content"])

    assets = as_dict(package.get("assets", {}), "assets")
    cover_data = as_dict(assets.get("cover", {}), "assets.cover")
    cover = resolve_cover(root, cover_data.get("path"))
    if cover_data.get("required") is True and cover is None:
        raise PublisherError("для обязательной обложки отсутствует assets.cover.path")

    if schema_version == DAY_SCHEMA_VERSION:
        telegram_publication = telegram
    else:
        publication = as_dict(package.get("publication"), "publication")
        telegram_publication = as_dict(
            publication.get("telegram"), "publication.telegram"
        )
    return telegram, max_post, telegram_publication, cover


def validate_package(package: dict[str, Any], root: Path) -> list[str]:
    telegram, max_post, _, cover = package_fields(package, root)
    telegram_count = character_count(telegram["content"])
    max_count = character_count(max_post["content"])
    warnings: list[str] = []
    if not TELEGRAM_TARGET_MIN <= telegram_count <= TELEGRAM_TARGET_MAX:
        warnings.append(
            f"Telegram: {telegram_count} символов; комфортный диапазон "
            f"{TELEGRAM_TARGET_MIN}–{TELEGRAM_TARGET_MAX}"
        )
    if not MAX_TARGET_MIN <= max_count <= MAX_TARGET_MAX:
        warnings.append(
            f"MAX: {max_count} символов; комфортный диапазон "
            f"{MAX_TARGET_MIN}–{MAX_TARGET_MAX}"
        )
    return warnings


def send_telegram(
    token: str,
    chat_id: str,
    content: str,
    cover: Path | None,
    disable_link_preview: bool,
) -> list[int]:
    message_ids: list[int] = []
    if cover and character_count(content) <= TELEGRAM_CAPTION_LIMIT:
        result = api_multipart(
            token,
            "sendPhoto",
            {"chat_id": chat_id, "caption": content, "parse_mode": "HTML"},
            "photo",
            cover,
        )
        return [int(result["message_id"])]

    if cover:
        raise PublisherError(
            f"Telegram: {character_count(content)} символов; текст с обложкой "
            f"должен помещаться в подпись до {TELEGRAM_CAPTION_LIMIT} символов"
        )

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": content,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": disable_link_preview},
    }
    result = api_json(token, "sendMessage", payload)
    message_ids.append(int(result["message_id"]))
    return message_ids


def publish_package(
    path: Path,
    root: Path,
    token: str,
    chat_id: str,
    now: dt.datetime | None = None,
) -> str:
    package = load_package(path)
    telegram, _, publication, cover = package_fields(package, root)
    publication_status = publication.get("status", "")
    if publication_status in {"sending", "ambiguous", "published"}:
        raise PublisherError(
            f"повторная публикация заблокирована: status={publication_status}"
        )
    status = package.get("status")
    if status not in {"approved", "scheduled"}:
        raise PublisherError(
            "для публикации JSON должен иметь status=approved или status=scheduled"
        )

    moment = now or dt.datetime.now(dt.timezone.utc)
    if status == "scheduled":
        scheduled_at = parse_datetime(
            publication.get("scheduled_at"), "publication.telegram.scheduled_at"
        )
        if scheduled_at > moment.astimezone(scheduled_at.tzinfo):
            raise PublisherError("время запланированной публикации ещё не наступило")

    publication.update(
        {
            "status": "sending",
            "attempted_at": moment.isoformat(timespec="seconds"),
        }
    )
    save_package(path, package)
    try:
        message_ids = send_telegram(
            token,
            chat_id,
            telegram["content"],
            cover,
            bool(telegram.get("disable_link_preview", False)),
        )
        published_at = dt.datetime.now(ZoneInfo("Europe/Moscow")).isoformat(
            timespec="seconds"
        )
        url = public_url(chat_id, message_ids[-1])
        publication.update(
            {
                "status": "published",
                "published_at": published_at,
                "published_url": url,
                "message_ids": message_ids,
            }
        )
        publication.pop("error", None)
        package["status"] = "published"
        package["needs_manual_review"] = False
        save_package(path, package)
        return url
    except Exception as exc:
        publication.update({"status": "ambiguous", "error": str(exc)})
        save_package(path, package)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", help="путь к производственному JSON-пакету")
    parser.add_argument("--root", default=".", help="корень AI Content Factory")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="выполнить реальную отправку после явного разрешения Влада",
    )
    args = parser.parse_args()
    root = Path(args.root).resolve()
    path = Path(args.package).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PublisherError("JSON-файл должен находиться внутри проекта") from exc

    package = load_package(path)
    warnings = validate_package(package, root)
    telegram, max_post, _, cover = package_fields(package, root)
    print(
        f"Telegram: {character_count(telegram['content'])} символов; "
        f"MAX: {character_count(max_post['content'])} символов; "
        f"обложка: {cover if cover else 'нет'}"
    )
    for warning in warnings:
        print(f"WARNING: {warning}")
    if not args.publish:
        print("PREVIEW OK: обращения к Telegram не было.")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        raise PublisherError(
            "задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в окружении"
        )
    url = publish_package(path, root, token, chat_id)
    print(f"PUBLISHED: {url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublisherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
