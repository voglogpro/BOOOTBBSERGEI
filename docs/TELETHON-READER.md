# Контракт Telethon Reader

Административная часть этапа 2 реализована командами `authorize` и
`resolve-sources`. Live-reader остаётся следующим отдельным этапом.

## Зафиксированная версия

Для пилота используется стабильный `Telethon==1.44.0`. Предварительная ветка
2.x не используется. Параметры клиента сверяются с
[официальной справкой TelegramClient](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.telegrambaseclient.TelegramBaseClient).

## Одноразовый вход

1. Создать собственные `api_id` и `api_hash` в
   [my.telegram.org](https://core.telegram.org/api/obtaining_api_id).
2. На доверенном локальном компьютере запустить отдельный интерактивный script.
3. Ввести телефон, одноразовый код и при наличии пароль 2FA только в prompt.
4. Сохранить `user_id`, вернуть его в `TELEGRAM_EXPECTED_USER_ID` и при каждом
   запуске сверять с `client.get_me()`.
5. Перенести только session-файл в отдельный закрытый persistent volume.

Пошаговая инструкция: [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md).

Телефон, код и пароль 2FA не сохраняются в `.env`. Утечка session равносильна
доступу к аккаунту; подробности — в документации
[Telethon Session Files](https://docs.telethon.dev/en/stable/concepts/sessions.html).

## Профили клиента

Реализованные административные команды всегда используют
`receive_updates=False`. Ниже показан будущий профиль этапа 3 для live-reader:

```python
client = TelegramClient(
    session_path,
    api_id,
    api_hash,
    receive_updates=True,
    catch_up=False,             # включается только после проверки outbox
    sequential_updates=True,
    flood_sleep_threshold=0,    # admin/runtime сами останавливаются на FloodWait
    request_retries=5,
    connection_retries=5,
    auto_reconnect=True,
)
client.session.save_entities = False
```

`save_entities=False` обязателен: стандартная SQLite-session умеет запоминать
встреченных пользователей и username, а проект не должен превращать session в
базу участников.

Один session-файл использует ровно один процесс. При штатной остановке вызывается
`disconnect()`, но не `log_out()`: logout отзывает авторизацию и удаляет session.

## Разрешённый путь события

Reader регистрирует только два whitelist-handler:

- `events.NewMessage(chats=SOURCE_CHAT_IDS, incoming=True, forwards=False)`;
- `events.MessageEdited(chats=SOURCE_CHAT_IDS, incoming=True, forwards=False)`.

Из события без дополнительных запросов берутся только:

- `event.chat_id`;
- `event.id`;
- `event.raw_text`;
- `event.date`;
- `event.edit_date`;
- ID темы после отдельной реализации и тестирования forum-событий.

Reader не вызывает `get_sender`, `get_participants`, `iter_participants`,
`get_entity` на каждое сообщение, методы отправки, реакции или вступления.
Фильтрация выполняется по заранее сохранённым числовым chat ID.

Callback не классифицирует сообщение и не ждёт HTTP. Он быстро пишет минимальное
событие в локальный durable inbox. Отдельный worker доставляет его Core.

## Catch-up и FloodWait

Первый пилот принимает только live-события с `catch_up=False` и
`READER_CATCHUP_LIMIT=0`. Catch-up не включается до отдельного проектирования
account-specific input peer, проверки идемпотентности и локального inbox.

Любой `FloodError` останавливает пакет, а `retry_not_before` сохраняется рядом с
session и проверяется до следующего подключения. Нельзя задавать бесконечные
retries или многократно разрешать username. См.
[iter_messages](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.messages.MessageMethods.iter_messages)
и [Telethon entities](https://docs.telethon.dev/en/stable/concepts/entities.html).

## Ссылки

Допускаются только источники с публичным username. Базовая ссылка строится без
API-запроса:

```text
https://t.me/<username>/<message_id>
```

Будущий формат для forum topic после добавления `topic_id` в событие:

```text
https://t.me/<username>/<topic_id>/<message_id>
```

До этого момента resolver возвращает `unsupported_forum`, чтобы не создавать
неверные ссылки. Ссылки `t.me/c/...` считаются приватными и в пилот не
принимаются. Официальный синтаксис:
[Telegram message links](https://core.telegram.org/api/links#message-links).

## Gate этапа 2A

Административная часть считается пройденной, когда новый тестовый аккаунт:

- успешно авторизуется один раз и повторно стартует без запроса кода;
- совпадает с обязательным `TELEGRAM_EXPECTED_USER_ID`;
- вручную состоит хотя бы в одном выбранном публичном источнике;
- получает для него `ready` и числовой ID без чтения сообщений;
- регистрируется в Core выключенным из свежего resolver-отчёта;
- не сохраняет entities пользователей;
- не имеет в коде путей отправки, вступления или чтения участников.

## Gate этапа 3 — будущий live-reader

Live-reader считается готовым, когда:

- принимает live-сообщение и edit в локальный inbox;
- соблюдает `COLLECTOR_ENABLED` как kill switch;
- работает только с проверенным включённым allowlist;
- безопасно останавливается и снова запускается без дублей.
