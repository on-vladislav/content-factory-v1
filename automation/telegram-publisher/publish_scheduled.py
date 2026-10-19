#!/usr/bin/env python3
"""Publish approved, scheduled Markdown posts to a Telegram channel.

The script is intentionally dependency-free so it can be called by n8n,
Windows Task Scheduler, cron, or a manual dry run.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
DEFAULT_GLOB = "reports/*.md"
ALLOWED_STATUSES = {"scheduled"}


class PublisherError(RuntimeError):
    pass


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value in {"null", "~"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_post(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)\Z", raw, re.S)
    if not match:
        raise PublisherError(f"{path}: отсутствует YAML front matter")

    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("-"):
            continue
        key, separator, value = line.partition(":")
        if separator:
            meta[key.strip()] = parse_scalar(value)
    return meta, match.group(2).strip()


def markdown_to_plain_text(markdown: str) -> str:
    """Convert the small Markdown subset used by this project to Telegram text."""
    lines = []
    for line in markdown.splitlines():
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", line)
        line = re.sub(r"(?<!\\)(\*\*|__)(.+?)\1", r"\2", line)
        line = re.sub(r"(?<!\\)(\*|_)(.+?)\1", r"\2", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def parse_scheduled_at(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise PublisherError("для статуса scheduled обязательно поле scheduled_at")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublisherError(f"некорректное scheduled_at: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublisherError("scheduled_at должно содержать часовой пояс")
    return parsed


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return chunks


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def api_json(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    return execute_request(request)


def api_multipart(
    token: str,
    method: str,
    fields: dict[str, Any],
    file_field: str,
    file_path: Path,
) -> dict[str, Any]:
    boundary = f"----aicf-{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"\r\n'
        ).encode()
    )
    body.extend(f"Content-Type: {mime}\r\n\r\n".encode())
    body.extend(file_path.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    return execute_request(request)


def execute_request(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise PublisherError(f"Telegram API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PublisherError(
            "неоднозначная сетевая ошибка: публикация могла состояться"
        ) from exc
    if not result.get("ok"):
        raise PublisherError(f"Telegram API: {result}")
    return result["result"]


def resolve_cover(root: Path, value: Any) -> Path | None:
    if value in {None, ""}:
        return None
    cover = (root / str(value)).resolve()
    root_resolved = root.resolve()
    try:
        cover.relative_to(root_resolved)
    except ValueError as exc:
        raise PublisherError("cover_path выходит за пределы проекта") from exc
    if not cover.is_file():
        raise PublisherError(f"обложка не найдена: {value}")
    return cover


def publish(
    token: str,
    chat_id: str,
    text: str,
    cover: Path | None,
    disable_link_preview: bool,
) -> list[int]:
    message_ids: list[int] = []
    if cover and len(text) <= CAPTION_LIMIT:
        result = api_multipart(
            token,
            "sendPhoto",
            {"chat_id": chat_id, "caption": text},
            "photo",
            cover,
        )
        return [int(result["message_id"])]

    reply_to: int | None = None
    if cover:
        result = api_multipart(
            token,
            "sendPhoto",
            {"chat_id": chat_id},
            "photo",
            cover,
        )
        reply_to = int(result["message_id"])
        message_ids.append(reply_to)

    for chunk in split_text(text):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "link_preview_options": {"is_disabled": disable_link_preview},
        }
        if reply_to is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to,
                "allow_sending_without_reply": True,
            }
        result = api_json(token, "sendMessage", payload)
        reply_to = int(result["message_id"])
        message_ids.append(reply_to)
    return message_ids


def public_url(chat_id: str, message_id: int) -> str:
    if chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}/{message_id}"
    return f"telegram-message:{message_id}"


def update_front_matter(
    path: Path, published_at: str, published_url: str
) -> None:
    raw = path.read_text(encoding="utf-8")
    match = re.match(r"\A(---\s*\r?\n)(.*?)(\r?\n---\s*\r?\n?.*)\Z", raw, re.S)
    if not match:
        raise PublisherError(f"{path}: невозможно обновить front matter")
    front = match.group(2)
    front = re.sub(r"(?m)^status:\s*scheduled\s*$", "status: published", front)
    additions = []
    for key, value in (
        ("published_at", published_at),
        ("published_url", published_url),
    ):
        pattern = rf"(?m)^{re.escape(key)}:\s*.*$"
        if re.search(pattern, front):
            front = re.sub(pattern, f"{key}: {value}", front)
        else:
            additions.append(f"{key}: {value}")
    if additions:
        front = front.rstrip() + "\n" + "\n".join(additions)
    path.write_text(
        match.group(1) + front + match.group(3),
        encoding="utf-8",
    )


def update_registry(
    path: Path,
    content_id: str,
    published_at: str,
    published_url: str,
) -> None:
    if not path.exists():
        return
    raw = path.read_text(encoding="utf-8")
    entry_pattern = re.compile(
        rf"(?ms)^  - id:\s*{re.escape(content_id)}\s*$.*?(?=^  - id:|\Z)"
    )
    match = entry_pattern.search(raw)
    if not match:
        return
    entry = match.group(0)
    entry = re.sub(r"(?m)^    status:\s*scheduled\s*$", "    status: published", entry)
    for key, value in (
        ("published_at", published_at),
        ("published_url", published_url),
    ):
        pattern = rf"(?m)^    {key}:\s*.*$"
        if re.search(pattern, entry):
            entry = re.sub(pattern, f"    {key}: {value}", entry)
        else:
            entry = entry.rstrip() + f"\n    {key}: {value}\n"
    path.write_text(raw[: match.start()] + entry + raw[match.end() :], encoding="utf-8")


def candidates(root: Path, pattern: str, now: dt.datetime) -> list[dict[str, Any]]:
    result = []
    for path in sorted(root.glob(pattern)):
        try:
            meta, body = read_post(path)
            if meta.get("channel") != "telegram":
                continue
            if meta.get("status") not in ALLOWED_STATUSES:
                continue
            scheduled_at = parse_scheduled_at(meta.get("scheduled_at"))
            if scheduled_at > now.astimezone(scheduled_at.tzinfo):
                continue
            content_id = meta.get("content_id")
            if not isinstance(content_id, str) or not content_id:
                raise PublisherError("отсутствует content_id")
            text = markdown_to_plain_text(body)
            if not text:
                raise PublisherError("пустой текст публикации")
            result.append(
                {
                    "path": path,
                    "meta": meta,
                    "content_id": content_id,
                    "scheduled_at": scheduled_at,
                    "text": text,
                    "cover": resolve_cover(root, meta.get("cover_path")),
                }
            )
        except PublisherError as exc:
            print(f"SKIP {path}: {exc}", file=sys.stderr)
    return result


def run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    state_path = root / args.state
    registry_path = root / args.registry
    state = load_state(state_path)

    if args.unlock:
        previous = state.get(args.unlock)
        if previous and previous.get("status") in {"ambiguous", "sending"}:
            del state[args.unlock]
            save_state(state_path, state)
            print(f"UNLOCKED {args.unlock}")
            return 0
        print(f"NOT UNLOCKED {args.unlock}: нет заблокированной попытки")
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    due = candidates(root, args.glob, now)
    if not due:
        print("Нет публикаций, готовых к отправке.")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not args.dry_run and (not token or not chat_id):
        raise PublisherError(
            "задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в окружении n8n"
        )

    exit_code = 0
    for item in due:
        content_id = item["content_id"]
        previous = state.get(content_id, {})
        if previous.get("status") in {"sending", "ambiguous", "published"}:
            print(f"BLOCKED {content_id}: состояние {previous.get('status')}")
            continue

        if args.dry_run:
            cover_text = str(item["cover"]) if item["cover"] else "нет"
            print(
                f"DRY RUN {content_id}: {len(item['text'])} символов, "
                f"обложка: {cover_text}"
            )
            continue

        attempted_at = dt.datetime.now(dt.timezone.utc).isoformat()
        state[content_id] = {
            "status": "sending",
            "attempted_at": attempted_at,
            "source": str(item["path"].relative_to(root)).replace("\\", "/"),
        }
        save_state(state_path, state)
        try:
            message_ids = publish(
                token,
                chat_id,
                item["text"],
                item["cover"],
                bool(item["meta"].get("disable_link_preview", False)),
            )
            published_at = dt.datetime.now(ZoneInfo("Europe/Moscow")).isoformat(
                timespec="seconds"
            )
            url = public_url(chat_id, message_ids[-1])
            state[content_id] = {
                **state[content_id],
                "status": "published",
                "published_at": published_at,
                "published_url": url,
                "message_ids": message_ids,
            }
            save_state(state_path, state)
            update_front_matter(item["path"], published_at, url)
            update_registry(registry_path, content_id, published_at, url)
            print(f"PUBLISHED {content_id}: {url}")
        except Exception as exc:
            state[content_id] = {
                **state[content_id],
                "status": "ambiguous",
                "error": str(exc),
            }
            save_state(state_path, state)
            print(f"ERROR {content_id}: {exc}", file=sys.stderr)
            exit_code = 1
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="корень проекта")
    parser.add_argument("--glob", default=DEFAULT_GLOB, help="маска Markdown-файлов")
    parser.add_argument(
        "--state",
        default="automation/telegram-publisher/publication-state.json",
        help="файл защиты от повторной публикации",
    )
    parser.add_argument(
        "--registry",
        default="content-registry.yaml",
        help="реестр контента",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="проверить очередь без обращения к Telegram",
    )
    parser.add_argument(
        "--unlock",
        metavar="CONTENT_ID",
        help="разблокировать неоднозначную попытку после ручной проверки канала",
    )
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except PublisherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
