---
name: content-factory-mvp
description: Запускать, проверять и сопровождать локальный MVP Content Factory; использовать при работе с API, Worker, схемой базы и документацией проекта.
---

# Сопровождение Content Factory v1

Сначала прочитай корневой README и `docs/final-project-description.md`. API и Worker находятся в `automation/content-factory-v1/`, отдельный Telegram publisher — в `automation/telegram-publisher/`. Рабочие сценарии n8n и доступы не входят в публичный репозиторий.

## Локальный запуск и проверка

Создай `.env` из `.env.example` и задай свои локальные значения, как описано в README. Из корня репозитория запусти `docker compose --env-file .env -p content-factory-v1 -f automation/content-factory-v1/docker-compose.yml up -d --build`. Проверь `http://127.0.0.1:8765/docs`. Тесты Worker: `python -m unittest discover -s automation/content-factory-v1/worker/tests -v`. Для полной генерации нужны отдельно настроенные Codex CLI и рабочее окружение; запуск API сам по себе не запускает Worker или интеграции.

При ошибках проверь состояние контейнеров и их журналы, доступность портов, конфигурацию и зависимости. Не выводи секреты в отчёты и публичные файлы. Не используй `down -v` без отдельного разрешения: это удаляет данные базы.

## Изменения

Перед правкой изучи соответствующий код и схемы, сохраняй ручное утверждение и защиту `MANUAL_LOCK`. Проверяй изменения подходящими тестами; обновляй README, если меняется запуск или поведение. Не запускай публикации и не меняй production-интеграции без явного разрешения владельца.

