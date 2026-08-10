# Развёртывание защищённого входа на BotHost

Этот этап размещает на BotHost административное Mini App, через которое один
из разрешённых сотрудников подключает отдельный пользовательский
Telegram-аккаунт Reader. Браузер получает только состояние входа. Строка
Telethon-сессии шифруется на сервере и не возвращается в Mini App.

## Что где хранится

- в секретных переменных BotHost: Bot Token, API Hash и ключ шифрования;
- в `/app/data/telegram/reader.session.enc`: только шифротекст Telethon-сессии;
- только в оперативной памяти на несколько минут: телефон и данные незавершённого входа;
- нигде не сохраняются: код Telegram и пароль 2FA.

Исходники можно держать в приватном GitHub-репозитории. Сам экран входа должен
отдаваться с HTTPS-домена BotHost, а не с GitHub Pages: так Mini App и API
работают на одном origin без CORS, а секретная операция остаётся у backend.

## 1. Подготовить Telegram

1. Создать отдельного CRM-бота через BotFather и сохранить его токен.
2. Создать Telegram API application на `my.telegram.org/apps`; получить
   `api_id` и `api_hash`.
3. Выбрать пользовательский аккаунт Reader. Желательно отдельный аккаунт с
   отдельной SIM и включённой двухэтапной защитой.
4. Заранее узнать его числовой Telegram ID. Код разрешит сохранить сессию
   только если этот ID совпадёт с `TELEGRAM_EXPECTED_USER_ID`.
5. Узнать числовые ID сотрудников, которым разрешён экран входа. Они попадут
   в `ADMIN_TELEGRAM_IDS`.

Если Mini App открывает тот же пользователь, который будет Reader, его ID
обычно указан и в `ADMIN_TELEGRAM_IDS`, и в `TELEGRAM_EXPECTED_USER_ID`. Если
это разные аккаунты, значения должны быть разными — не угадывайте ID.

## 2. Создать ключ шифрования

После локального `pip install -r requirements.txt` один раз выполнить на своём
компьютере:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Скопировать результат в секретную переменную `SESSION_ENCRYPTION_KEY`. Не
запускать генерацию при каждом деплое и не публиковать ключ: с новым ключом
старый файл сессии открыть невозможно.

## 3. Настроить проект BotHost Pro

В проект загрузить этот репозиторий и указать:

- главный файл: `main.py`;
- Python: 3.11 или новее;
- внутренний порт: тот же, что в `PORT`, например `8080`;
- включить домен BotHost и HTTPS;
- одна реплика/один процесс, без autoscaling;
- не запускать второй проект с той же Telegram-сессией.

Добавить переменные:

```dotenv
APP_ENV=production
APP_HOST=0.0.0.0
PORT=8080

DATA_DIR=/app/data
DATABASE_PATH=/app/data/leads.sqlite3
ENCRYPTED_SESSION_PATH=/app/data/telegram/reader.session.enc

BOT_TOKEN=<токен нового CRM-бота>
ADMIN_TELEGRAM_IDS=<ваш Telegram ID; несколько ID через запятую>
TELEGRAM_API_ID=<api_id с my.telegram.org>
TELEGRAM_API_HASH=<api_hash с my.telegram.org>
TELEGRAM_EXPECTED_USER_ID=<точный ID аккаунта Reader>
SESSION_ENCRYPTION_KEY=<одна сохранённая Fernet-строка>

MINIAPP_INIT_DATA_MAX_AGE_SECONDS=300
LOGIN_CHALLENGE_TTL_SECONDS=300
COLLECTOR_ENABLED=false
READER_CATCHUP_LIMIT=0
```

Телефон, код Telegram, пароль 2FA и `SESSION_STRING` в переменные добавлять не
нужно. Приложение не использует BotHost API и не имеет доступа к панели
управления хостингом.

## 4. Подключить Mini App

1. Выполнить деплой.
2. Проверить `https://<домен-bothost>/health`: ожидается `{"ok": true}`.
3. В BotFather открыть созданного бота: **Bot Settings → Configure Mini App**
   или **Menu Button**.
4. Указать корневой URL `https://<домен-bothost>/`.
5. Открыть Mini App именно из этого бота под аккаунтом, ID которого находится
   в `ADMIN_TELEGRAM_IDS`.
6. Ввести номер Reader, затем код из официального приложения Telegram и, если
   потребуется, пароль 2FA.

Код нельзя отправлять сообщением боту или в рабочий чат. Он вводится только в
HTTPS-форму Mini App и сразу удаляется из поля.

После успеха в `/app/data/telegram/reader.session.enc` появится зашифрованный
файл, а интерфейс покажет «Сессия сохранена». `COLLECTOR_ENABLED` пока остаётся
`false`: этот этап готовит вход, но ещё не запускает чтение сообщений.
Зашифрованное хранилище будет подключено к live Reader на следующем этапе,
после smoke-теста выбранных источников.

Кнопка «Переподключить аккаунт» не удаляет старый файл заранее: прежняя
сессия заменится только после полного успешного входа и сверки ID. Если новый
вход оказался выполнен не в тот аккаунт или файл не удалось сохранить, сервис
отзывает только что созданную сессию и оставляет старую копию нетронутой.

## Если экран показывает блокировку

Сначала проверить, что `SESSION_ENCRYPTION_KEY` не изменился и путь
`ENCRYPTED_SESSION_PATH` указывает на тот же файл. Приложение намеренно не
перезаписывает сессию, которую не смогло расшифровать. Не удаляйте файл до
проверки ключа и резервной копии точного файла.

Если истекли данные Mini App, полностью закрыть окно и открыть его снова из
бота. Если Telegram вернул FloodWait, дождаться указанного времени; повторные
запросы не обходят ограничение.

## Почему такая схема подходит BotHost

BotHost рекомендует StringSession, потому что в контейнере нет интерактивного
терминала. В этой реализации StringSession создаётся через HTTPS Mini App,
сразу шифруется и сохраняется в постоянной `/app/data`. Веб-сервер слушает
`0.0.0.0` и порт из `PORT`, как требует маршрутизация домена BotHost.

Шифрование защищает от случайной публикации файла, утечки бэкапа без ключа и
попадания данных в Git. Оно не защищает от полного захвата работающего
контейнера BotHost, где приложению одновременно доступны и ключ, и шифротекст.
Поэтому доступ к панели и переменным окружения должен быть только у владельца.

## Официальные источники

- [Telegram: проверка Mini App initData](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
- [Telethon 1.44: вход по коду и 2FA](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.auth.AuthMethods.sign_in)
- [Telethon: StringSession и предупреждение о секрете](https://docs.telethon.dev/en/stable/concepts/sessions.html#string-sessions)
- [BotHost: Telegram Userbot](https://bothost.ru/docs/telegram-userbot-setup)
- [BotHost: постоянная папка `/app/data`](https://bothost.ru/docs/database-storage)
- [BotHost: домен, `0.0.0.0` и `PORT`](https://bothost.ru/docs/web-apps-domains)
