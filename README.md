# Ритм трейдера — Telegram Mini App

Совместный журнал для двух трейдеров. Каждый участник входит через Telegram, отмечает состояние, записывает сделки и видит календарь дисциплины. Запись можно оставить личной или показать напарнику.

## Что уже работает

- серверная проверка `Telegram.WebApp.initData` по подписи бота;
- отдельное хранение данных по Telegram ID;
- команда на двух участников с приватным восьмизначным кодом;
- дневная оценка настроения, энергии, уверенности и дисциплины;
- сделки с сетапом, ценами, риском, P/L, R, эмоциями и ошибками;
- личные и командные записи;
- месячный календарь результатов и настроения;
- статистика прибыли, винрейта, соблюдения плана и стабильности;
- адаптация к светлой/тёмной теме и safe-area Telegram;
- SQLite в постоянной директории хостинга.

## Локальный запуск

Требуется Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

В `.env` для локального просмотра:

```dotenv
DEV_MODE=true
DATABASE_PATH=./data/trader_journal.sqlite3
PORT=8080
```

Запуск:

```powershell
python main.py
```

Открыть `http://localhost:8080`. `DEV_MODE` нельзя включать на публичном сервере.

## Тесты

```powershell
python -m unittest discover -s tests -v
```

Подробное подключение бота и размещение описаны в [docs/DEPLOY.md](docs/DEPLOY.md).

## Структура

```text
journal/            backend, Telegram auth, SQLite и API
static/             стили, клиентский JavaScript и иконка
tests/              тесты подписи, базы и HTTP API
docs/DEPLOY.md      настройка BotFather и хостинга
main.py             серверная точка запуска Python
index.html          основная страница Mini App
Dockerfile          однозначный запуск Python-контейнера
```

## Приватность

Приложение не запрашивает номер телефона, пароль Telegram или доступ к сообщениям. Сервер получает только подписанные Telegram данные Mini App. Друг видит лишь записи с видимостью `team` и не может их изменять.
