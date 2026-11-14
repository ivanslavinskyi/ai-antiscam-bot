# Разработка V2

Рабочий код новой версии находится в `app/v2`. `python -m app.main` запускает только V2. Старые handlers сохранены для сравнения, но не импортируются точкой входа. Не запускать старые и новые обработчики одновременно с одним токеном.

## Окружение

Цель: Linux, Python 3.12, PostgreSQL 17 с актуальными исправлениями, Tesseract и языки `rus`, `ukr`, `eng`. Для локальных транзакционных тестов подходит Windows с PostgreSQL; OCR и systemd дополнительно проверяются на целевой Linux-системе.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

Runtime-версии закреплены в `requirements.txt` и `constraints.txt`, инструменты разработки — в `requirements-dev.txt`. Тесты не читают `.env`, не используют production-токен и не обращаются к Telegram/OpenAI.

## Проверки

Создать отдельную локальную базу с именем `antiscam_test_*`; установить `ANTISCAM_TEST_DATABASE_URL`. Фикстуры очищают **только эту тестовую БД**, проверяя имя и localhost. Исходная production-копия для миграционной репетиции не должна использовать это имя.

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check app/v2 app/main.py migrations tests tools ops
.venv/bin/ruff format --check app/v2 app/main.py migrations tests tools ops
.venv/bin/python -m pip check
```

На Windows для psycopg используются selector event loops. При наличии системного `SSLKEYLOGFILE`, не доступного текущему процессу, удалить эту переменную только из окружения тестового запуска; проверку TLS не отключать.

GitHub Actions выполняет те же проверки на чистом PostgreSQL 17 и Python 3.12. Workflow добавлен локально; успешный запуск GitHub CI утверждается только после фактического выполнения.

## Миграция

Обычный старт не создаёт таблицы. Alembic сначала проверяет четыре исходные таблицы, затем добавляет новые. DDL `0002_schema.sql` зафиксирован отдельно от runtime-моделей, чтобы следующие изменения моделей не меняли старую миграцию.

`ANTISCAM_MIGRATION_DATABASE_URL` задаётся явно вне Git. Инструмент не читает `.env` автоматически. Для репетиции использовать новую восстановленную копию, отдельный manifest с тестовым bot/admin ID и `purpose: rehearsal`.

```sh
.venv/bin/python -m app.v2.migration /private/migration.json --report /private/preflight.json
.venv/bin/alembic upgrade head
.venv/bin/python -m app.v2.migration /private/migration.json --apply --report /private/import.json
```

Предварительное чтение должно выполняться до изменения схемы. Снимок исходных строк, явная карта primary/archived, настройки, владелец и роли проверяются до импорта. Импорт выполняется одной транзакцией; повтор того же manifest не возвращает позднее снятое доверие. Импорт не вызывает Telegram/OpenAI. Значения manifest для боевой системы нельзя брать из примера.

Старые четыре таблицы остаются базой сверки; новые оперативные данные ведутся в новых таблицах. После появления V2-записей запуск старого кода не является корректным откатом: он не знает новых ролей, решений и локальных списков.

## Архитектура

- `queue.py`, `runtime.py`: inbox и offset в одной транзакции, один receiver, отдельный worker приёма/команд, фоновые проверки/доставка, lease и heartbeat.
- `auth.py`, `operator.py`: группа + человек + актуальная связь + индивидуальная роль; подтверждения с ограниченным сроком и версией объекта.
- `state.py`: локальные циклы доверия, кредиты и страйки. Порядок блокировок: настройки группы → участник → инцидент. Глобальная аварийная блокировка проверяется перед переходом действия в `in_flight`.
- `pipeline.py`, `content.py`, `llm.py`: оригиналы, правки, альбомы, OCR, структурированный ответ, бюджет. Сеть и OCR выполняются вне транзакций с блокировками.
- `actions.py`, `delivery.py`: отдельные факты планирования, отправки, успеха, отказа и неизвестного результата. При неоднозначном исходе необратимое действие не повторяется автоматически.
- `onboarding.py`, `ui.py`: выбор групп и проверка прав в личке; публикация итоговой карточки только в админской группе.
- `maintenance.py`, `controls.py`: здоровье, сводки, сроки доверия, тихие часы, системная пауза и heartbeat.

Порог доверия по умолчанию 5, настраивается на группу. Понижение порога не запускает массовую выдачу; повышение и отключение не отзывают выданное доверие. Отзыв/истечение начинает новый цикл, старые сообщения не возвращают доверие. Blacklist и whitelist не снимают друг друга или ограничения Telegram автоматически.

## Нагрузка и модель

`tools/load_test.py` использует настоящую PostgreSQL и контролируемые задержки заглушек. Только пустая локальная `antiscam_test_*`; отчёт сохраняет длительность, число updates, p95 кнопок и команд, задержки проверок каждой группы от исходной публикации, очередь, память и ошибки. Приёмочный прогон — 1800 секунд при 10 публикациях/с и 60 секунд при 20/с, 50 групп; дополнительно каждые 10 секунд отправляются callback и команда в админку самой активной группы. Она получает 60% публикаций и весь синтетический SCAM (20% общего потока). Число работников задаётся `--workers`, по умолчанию 4. После потока отдельно измеряется разбор остатка очереди. Короткий smoke не считается выполнением этого критерия. Результат на ноутбуке не заменяет проверку ресурсов VPS и реальных API.

```sh
.venv/bin/python -m tools.load_test --output local-data/load-report.json
```

`tools/evaluate_models.py` работает офлайн с отдельно полученными предсказаниями. Требует независимую человеческую разметку, отдельный tuning, не менее 200 SCAM и 300 NOT_SCAM в holdout, группировку похожих примеров и обязательные регрессии. В контрольном наборе обязательны защищаемые невиновные авторы цитат, ответов и законных вакансий. Считает FPR, recall, precision, доверительные интервалы, ошибки и стоимость; не принимает старые модельные ответы за независимую разметку. Сырые наборы и отчёты с данными хранить в `local-data`, не в Git.

Формат строки dataset: `id`, `text`, `label` (SCAM/NOT_SCAM), `split` (tuning/holdout), `reviewed_by`, `review_source: independent_human`, `near_duplicate_group`, `tags`, необязательное `must_protect_author`. Формат predictions: `id`, `label` (SCAM/OK), `confidence`, `cost_usd`, необязательное `error`.

```sh
.venv/bin/python -m tools.evaluate_models local-data/dataset.jsonl local-data/baseline.jsonl local-data/candidate.jsonl --output local-data/model-report.json
```

Тарифы и возможности модели — версионируемый allowlist в `llm.py`. Фактическая смена `OPENAI_MODEL` допускается после оценки качества/стоимости и проверки доступности в аккаунте. Никакого автоматического платного прогона production-истории эти инструменты не делают.

## Источники интерфейсов

- [Telegram: выбор групп в личных чатах](https://core.telegram.org/bots/api#keyboardbuttonrequestchat).
- [PostgreSQL: согласованный custom dump](https://www.postgresql.org/docs/17/app-pgdump.html) и [восстановление](https://www.postgresql.org/docs/17/app-pgrestore.html).
- [age: шифрование резервных копий](https://github.com/FiloSottile/age#usage).
- CI закрепляет проверенные коммиты [checkout](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1) и [setup-python](https://github.com/actions/setup-python/commit/6d4fd22b20465ccc0f7354476588e8df7c9ca0e0).
