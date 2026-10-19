#!/usr/bin/env python3
"""Проверить метаданные и базовые редакционные правила контента."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path


CHANNELS = {"telegram", "max", "website"}
STATUSES = {"draft", "review", "approved", "scheduled", "published", "sending", "ambiguous"}
HASHTAGS = {
    "#разбор_процессов", "#кейсы", "#управляемость_бизнеса",
    "#автоматизация_бизнеса", "#потери_в_бизнесе",
}
HASHTAG_EXEMPT = {"pinned-post", "personal-brand", "story"}
REQUIRED = {"content_id", "channel", "content_type", "status", "timezone", "needs_manual_review"}
JSON_SCHEMA = "agency-content/v2"
TELEGRAM_TARGET = (900, 1000)
TELEGRAM_MAX = 1024
MAX_TARGET = (500, 800)
MAX_MAX = 1000


def scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return {}, text
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if line and not line[0].isspace() and ":" in line:
            key, value = line.split(":", 1)
            data[key.strip()] = scalar(value)
    return data, "\n".join(lines[end + 1:])


def validate_file(path: Path, root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    data, body = frontmatter(path.read_text(encoding="utf-8-sig"))
    if not data.get("content_id"):
        return errors, warnings

    channel, status = data.get("channel", ""), data.get("status", "")
    missing = sorted(REQUIRED - data.keys())
    if missing:
        message = f"отсутствуют поля: {', '.join(missing)}"
        if status == "published":
            warnings.append(message)
        else:
            errors.append(message)
    if channel and channel not in CHANNELS:
        errors.append(f"недопустимый channel: {channel}")
    if status and status not in STATUSES:
        errors.append(f"недопустимый status: {status}")
    if data.get("timezone") and data["timezone"] != "Europe/Moscow":
        errors.append("timezone должен быть Europe/Moscow")
    if status == "scheduled" and not data.get("scheduled_at"):
        errors.append("для status=scheduled требуется scheduled_at")
    if channel and f"-{channel}-" not in data.get("content_id", ""):
        warnings.append("content_id не содержит обозначение канала")
    if status in {"draft", "review", "approved"} and data.get("needs_manual_review") != "true":
        warnings.append("для рабочего материала ожидается needs_manual_review: true")
    if re.search(r"^Сегодня (расскажу|покажу|разбер[её]м)", body.strip(), re.I):
        warnings.append("шаблонное вступление «Сегодня…»")

    found = set(re.findall(r"(?<!\w)#[\w_]+", body, re.UNICODE))
    unknown = sorted(found - HASHTAGS)
    if unknown:
        warnings.append(f"неутверждённые хештеги: {', '.join(unknown)}")
    if channel in {"telegram", "max"} and data.get("content_type") not in HASHTAG_EXEMPT:
        if not 1 <= len(found) <= 2:
            warnings.append("для основного экспертного поста требуется 1–2 хештега")

    cover = data.get("cover_path", "")
    if cover and not Path(cover).is_absolute() and not (root / cover).exists():
        warnings.append(f"cover_path не найден: {cover}")
    return errors, warnings


def visible_count(value: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]*>", "", value)).strip())


def validate_json(path: Path, root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return [f"некорректный JSON: {exc}"], warnings
    if not isinstance(data, dict):
        return ["верхний уровень JSON должен быть объектом"], warnings
    if data.get("schema_version") != JSON_SCHEMA:
        warnings.append("устаревший JSON-пакет: строгая проверка длины не применена")
        return errors, warnings

    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        return ["отсутствует объект platforms"], warnings
    for channel, maximum, target in (
        ("telegram", TELEGRAM_MAX, TELEGRAM_TARGET),
        ("max", MAX_MAX, MAX_TARGET),
    ):
        post = platforms.get(channel)
        if not isinstance(post, dict):
            errors.append(f"отсутствует объект platforms.{channel}")
            continue
        if not post.get("content_id"):
            errors.append(f"отсутствует platforms.{channel}.content_id")
        content = post.get("content")
        if not isinstance(content, str) or not content.strip():
            errors.append(f"отсутствует platforms.{channel}.content")
            continue
        count = visible_count(content)
        if count > maximum:
            message = f"{channel}: {count} символов, максимум {maximum}"
            if data.get("status") == "published":
                warnings.append(message)
            else:
                errors.append(message)
        elif not target[0] <= count <= target[1]:
            warnings.append(
                f"{channel}: {count} символов, комфортно {target[0]}–{target[1]}"
            )

    telegram = platforms.get("telegram", {})
    if isinstance(telegram, dict) and telegram.get("format", "HTML").upper() != "HTML":
        errors.append("platforms.telegram.format должен быть HTML")
    assets = data.get("assets", {})
    cover = assets.get("cover", {}) if isinstance(assets, dict) else {}
    cover_path = cover.get("path", "") if isinstance(cover, dict) else ""
    if cover_path and not Path(cover_path).is_absolute() and not (root / cover_path).exists():
        warnings.append(f"обложка не найдена: {cover_path}")
    if data.get("status") == "scheduled":
        publication = data.get("publication", {})
        telegram_publication = (
            publication.get("telegram", {}) if isinstance(publication, dict) else {}
        )
        if not isinstance(telegram_publication, dict) or not telegram_publication.get("scheduled_at"):
            errors.append("для status=scheduled требуется publication.telegram.scheduled_at")
    return errors, warnings


def registry_items(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*-\s+id:\s*(.*)$", line)
        if match:
            if current:
                items.append(current)
            current = {"id": scalar(match.group(1))}
        elif current:
            match = re.match(r"\s{4}([a-z_]+):\s*(.*)$", line)
            if match:
                current[match.group(1)] = scalar(match.group(2))
    if current:
        items.append(current)
    return items


def validate_registry(root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for item in registry_items(root / "content-registry.yaml"):
        item_id = item.get("id", "")
        if item_id in seen:
            errors.append(f"реестр: повторяющийся id {item_id}")
        seen.add(item_id)
        raw_path = item.get("path", "")
        if not raw_path or re.match(r"^[A-Za-z]:[/\\]", raw_path):
            continue
        path = root / raw_path
        if not path.exists():
            warnings.append(f"реестр: локальный файл не найден: {raw_path}")
            continue
        data, _ = frontmatter(path.read_text(encoding="utf-8-sig"))
        if data.get("content_id") and data["content_id"] != item_id:
            errors.append(f"{raw_path}: content_id не совпадает с реестром")
        if data.get("channel") and data["channel"] != item.get("channel", ""):
            errors.append(f"{raw_path}: channel не совпадает с реестром")
        if data.get("status") and data["status"] != item.get("status", ""):
            warnings.append(f"{raw_path}: status не совпадает с реестром")
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Markdown-файлы для проверки")
    parser.add_argument("--root", default=".", help="Корень AI Content Factory")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    paths = [Path(p).resolve() for p in args.paths]
    if not paths:
        paths = sorted((root / "reports").glob("*.md"))
        paths += sorted((root / "reports").glob("*.json"))

    errors: list[str] = []
    warnings: list[str] = []
    for path in paths:
        if not path.exists():
            errors.append(f"{path}: файл не найден")
            continue
        if path.suffix.lower() == ".json":
            file_errors, file_warnings = validate_json(path, root)
        else:
            file_errors, file_warnings = validate_file(path, root)
        label = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        errors += [f"{label}: {message}" for message in file_errors]
        warnings += [f"{label}: {message}" for message in file_warnings]
    reg_errors, reg_warnings = validate_registry(root)
    errors += reg_errors
    warnings += reg_warnings

    for message in errors:
        print(f"ERROR: {message}")
    for message in warnings:
        print(f"WARNING: {message}")
    print(f"Проверено файлов: {len(paths)}; ошибок: {len(errors)}; предупреждений: {len(warnings)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
