# Read-only приёмка пяти Codex-чатов

`chat-audit` проверяет, что пять `chat_id` из `factory/lanes/registry.json`
существуют как пять отдельных инициализированных Codex-сессий. Это узкая
проверка топологии чатов, а не доказательство готовности всей видеофабрики.

## Запуск

Оба источника Codex evidence обязательны и передаются явно; команда не ищет
домашний `.codex` и ничего туда не записывает:

```powershell
$env:PYTHONPATH = (Resolve-Path 'factory/src').Path
python -m video_factory chat-audit `
  --registry factory/lanes/registry.json `
  --session-index <read-only-copy>/session_index.jsonl `
  --sessions-root <read-only-copy>/sessions
```

CLI выводит только JSON в stdout (успех) или stderr (fail-closed, exit code 3).
У команды намеренно нет `--export`: она не создаёт файлы рядом с session
evidence.

## Что считается доказательством

Для каждой из ровно пяти включённых линий одновременно требуются:

1. валидный и уникальный `chat_id` в реестре;
2. точное совпадение `id`, `session_id` или `thread_id` в append-only
   `session_index.jsonl`; последняя по порядку строка этого ID обязана иметь
   непустой `thread_name`;
3. ровно один файл `rollout-*.jsonl`, в имени которого UUID присутствует как
   полный UUID-токен, а не только внутри содержимого;
4. первая явная producer-delegation user-запись (с блоком
   `<codex_delegation>` либо одновременно с producer-agent и глаголом
   делегирования) должна дословно ссылаться на `factory/lanes/<lane_id>`,
   называть `producer-agent`/`продюсер-агент` и явно делегировать работу
   агентам/субагентам; предшествующие ambient/system user-like записи не
   считаются инициализацией;
5. после инициализации должен существовать непустой assistant-ответ с явным
   сигналом готовности (`ready`, `готов`, `приступаю` и эквиваленты).

Поддерживаются штатные rollout-сообщения `response_item` с `payload.role` и
`event_msg` с `payload.type=user_message|agent_message`, а также прямые
`role/content` JSONL-записи. Любая повреждённая JSONL-строка, отсутствующий
файл, неоднозначный rollout, пустое имя или неполная инициализация оставляют
проверку закрытой.

## Результат и граница утверждения

Успешный отчёт содержит:

```json
{
  "ok": true,
  "read_only": true,
  "chat_topology_verified": true,
  "verification_scope": "five_codex_chat_topology_only",
  "production_ready": false
}
```

`chat_topology_verified=true` означает только подтверждённые пять отдельных
чатов и их начальную готовность. `production_ready` всегда остаётся `false`:
приёмка сервера, провайдеров, прав, моделей наблюдения, GPU-render/QC и реальной
партии 10–15 MP4 проводится отдельными гейтами. Ошибки возвращаются массивом
объектов с устойчивым `code`, `message` и, когда применимо, `lane_id`,
`chat_id`, `path`, `line`; тексты приватных сообщений в отчёт не копируются.
