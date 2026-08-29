# Codex headless worker smoke test

Дата: 29.08.2026. Среда: Windows workstation, авторизация Codex CLI через
ChatGPT account.

## Результат

- Установленный глобально `codex-cli 0.98.0` не прошёл production preflight:
  модель из текущей конфигурации требовала более новый клиент.
- Pinned `@openai/codex 0.151.0` успешно выполнил read-only non-interactive turn
  через `codex exec` с `--ephemeral`, `--ignore-user-config`, `--ignore-rules` и
  `--output-schema`.
- Модель была зафиксирована явно: `gpt-5.4`.
- Схемный результат сохранён в `codex-worker-smoke-20260829.json`; SHA-256:
  `659a34bcd7b8c73bc887b4508b83ef3c1c107941a25e57cfd52b7e1f065395ce`.
- Изменений workspace от агента не требовалось и не выполнялось.

## Измеренное потребление одного пустого turn

```text
input_tokens:            16,265
cached_input_tokens:      1,792
output_tokens:              133
reasoning_output_tokens:    106
```

Это нижняя граница, а не оценка полноценного исследования. Один Codex turn на
каждое из 10–15 видео означает примерно 162,650–243,975 input tokens в сутки до
добавления source pack. Два независимых turn на ролик — 325,300–487,950 input
tokens в сутки. Поэтому production-профиль должен объединять лёгкие
редакционные этапы в один schema-bound dossier turn и оставлять второй turn
только для медицинского/privacy/sensitivity review.

ChatGPT-подписка может использоваться локальным Codex CLI, но точный доступный
лимит зависит от плана/workspace и не является гарантированным серверным SLA.
Для предсказуемого unattended production официальный предпочтительный вариант
— отдельный API key с лимитом расходов; для ChatGPT-managed auth на headless
сервере возможен device-code flow или защищённый auth cache.

Официальные разделы:

- https://learn.chatgpt.com/docs/non-interactive-mode
- https://learn.chatgpt.com/docs/auth
- https://learn.chatgpt.com/docs/pricing
