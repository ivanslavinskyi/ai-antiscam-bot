# Контейнерное развёртывание V2

В каталоге находятся переносимые шаблоны отдельного развёртывания на Linux. Они
не выполняют автоматический перенос старой БД и не включают receiver при сборке.
Одноразовые скрипты переключения конкретного VPS, ID групп, снимки и боевые
конфигурации хранятся вне Git. Для уже работающего экземпляра не повторяйте
bootstrap или перенос: обновляйте проверенный образ по локальному ранбуку.

## Образ приложения

Из корня репозитория:

```sh
docker build -f ops/container/Dockerfile \
  --build-arg APP_REVISION="$(git rev-parse HEAD)" \
  -t antiscam-v2:release .
docker image inspect antiscam-v2:release --format '{{.Id}}'
```

Python 3.12 и PostgreSQL 17 взяты из закреплённых образов Debian bookworm.
Образ приложения содержит соответствующие pg_dump/pg_restore, Tesseract и языки
eng/rus/ukr. PostgreSQL-сервер в нём не запускается. Приложение работает как
UID/GID 10001, исходники принадлежат root и должны быть доступны ему для чтения.
При подготовке файлов сборки под umask077 явно назначайте исходникам0644:
COPY сохраняет права, а root0600 делает модуль недоступным приложению.

Dockerfile.dockerignore использует список разрешённых исходников: секреты,
дампы, local-data и .git не попадают в контекст. Проверяйте собранный образ от
UID10001 с read-only filesystem, а не только импорты в рабочем каталоге Windows.

## База и роли

`compose.database.yaml` создаёт отдельный PostgreSQL17, собственные volume и
внутреннюю сеть, без публикации порта. Начальные лимиты:256MiB,0.5CPU,30соединений,
shared_buffers32MB; checksums включены. Лимиты требуют проверки под вашей нагрузкой.
Bootstrap-env `/etc/antiscam-v2/postgres.env` хранится как root0600 и содержит
POSTGRES_USER, POSTGRES_DB и случайный POSTGRES_PASSWORD. Не выводите его содержимое.

На проверенном новом кластере `bootstrap_roles.sql` создаёт БД `antiscam_v2` и
четыре роли без LOGIN/повышенных привилегий: antiscam_migrator, antiscam_runtime,
antiscam_backup, antiscam_health. Скрипт отказывается работать при существующих
ролях/целевой БД; после частичной ошибки требуется проверка состояния.

`activate_credentials.py` включает LOGIN и задаёт SCRAM-пароли из заранее
сохранённого `/etc/antiscam-v2/database-credentials.json`. Каталог должен иметь
root0700, файл root0600. Формат:version1,databaseantiscam_v2,passwords — объект
с четырьмя указанными именами ролей и разными случайными64-символьными URL-safe
паролями. Пароли не передаются аргументами и не печатаются; повторная активация
прежних ролей запрещена. Скрипт использует локальный контейнер antiscam-v2-db-1.

`python -m ops.container.render_database_config` создаёт отдельные файлы из этих
сохранённых реквизитов. Runtime использует рольruntime, миграции — migrator,
backup — backup, независимая проверка — health. Секреты ролей не объединяются.
После явного создания схемы/импорта `production_grants.sql` назначает права на
таблицы и последовательности. Runtime не получает CREATE или запись в ledger
миграций. Bootstrap/grants выполняются через Unix socket от postgres.

## Runtime и конфигурация

`compose.runtime.yaml` требует явный ANTISCAM_V2_IMAGE, без скрытого старого
образа по умолчанию. При подготовке установленной копии
`/opt/antiscam-bot/deploy/compose.runtime.yaml` закрепите в полеimage проверенный
`sha256:...` либо repository@sha256:..., полученный после проверки образа.
Не оставляйте переменную в установленной копии для systemd: controller очищает
ANTISCAM_V2_/COMPOSE_/DOCKER_переопределения и использует локальный Docker socket.
Переменная в исходном шаблоне предназначена для ручной проверки конфигурации.

Конфигурация хранится отдельно, в `/etc/antiscam-v2/service-config` (root0700):

| Файл | Содержимое и доступ |
| --- | --- |
| bot.env | TELEGRAM_BOT_TOKEN,OPENAI_API_KEY,OPENAI_MODEL,OWNER_USER_IDS и лимиты; root0600 |
| runtime-db.env | DB_URL ролиruntime; root0600 |
| migration.env | ANTISCAM_MIGRATION_DATABASE_URL; root0600 |
| backup.env | PGSERVICE/PGSERVICEFILE/PGPASSFILE,каталог/ротация; root0600 |
| health.env | DB_URL ролиhealth,каталог/возраст backup; root0600 |
| pg_service.conf,pgpass | Только backupроль; UID/GID10001,0600 |

bot.env заполняется для конкретного развёртывания по `.env.example`; OPENAI_MODEL
и OWNER_USER_IDS задаются явно. Compose читает env-файлы на хосте. В backup
монтируются только два файла его подключения, не весь каталог конфигурации.
`/var/lib/antiscam-v2/backups` имеет владельца10001 и режим0700; backup получаетRW,
health — RO. Только bot имеет сеть с внешним доступом; operations — внутреннююБД.

Проверка Compose — `config --quiet`. Полный `config` раскрывает значения секретов.
Шаблон runtime:768MiB дляbot,128MiB дляbackup/health, readonlyroot,cap_dropALL,
no-new-privileges и ограниченный tmpfs. Проект приложения — antiscam-v2-app,
контейнер — antiscam-v2-app-bot, внешняя сеть БД — antiscam-v2_database.

## Systemd и включение

`runtime_control.py` устанавливается как `/usr/local/libexec/antiscam-v2-control`
(root0755), unit/timer-файлы — в `/etc/systemd/system`. Hostcontroller работает
как root для обращения к Docker; приложение внутри контейнера — как UID10001.
Процесс Compose остаётся под systemd, Dockerrestart отключён. Receiverзащищён
локальной блокировкой, проверкой старой службы и блокировкой PostgreSQL.

Проверки запуска требуют закрытый маркер `/etc/antiscam-v2/ALLOW_V2_START`
(root0600, содержимое `production-v2-approved` с переводом строки), healthyБД,
остановленный старый `ai-antiscam-bot.service` и отсутствие другого V2контейнера.
`legacy-guard.conf` — шаблон drop-in старой службы, блокирующий её запуск при V2.
Маркер/guard устанавливают только после финального снимка, проверенной миграции,
сверки списков/балансов/истории и Telegrampreflight. Шаблоны не автоматизируют это
решение и не заменяют согласованный план конкретного переноса.

Сначала вручную проверьте backup/health units, затем включайте timers. Backup
планируется ежедневно в03:00UTC с небольшим случайным сдвигом, health — каждую
минуту. Ротация оставляет семь успешных плановых копий; preupdateкопии сохраняются
отдельно. Health пишет результат в journal, самостоятельного внешнего канала
оповещений эти шаблоны не создают.

## Обновление и возврат

Перед обновлением создайте preupdatebackup, сохраните прежний imageID и установленный
compose. Проверьте новый образ, его права и совместимость схемы. Замените только
проверенный imagepin и перезапустите V2; подтвердите свежий heartbeat, обработку
входящих событий, состояние очереди и результат Telegram. Activeпосле10сек не
является достаточной проверкой — receiverможет упасть при следующем сообщении.

Для совместимого исправления кода возможен возврат к прежнему V2образу с той же
БД после оценки изменений. Возврат к legacy после новых V2решений требует
сохранения и сверки новых данных. Не обходите guards и не запускайте два receiver.
Не используйте down-v или удаление volume в процедуре обновления/отката.
Восстановление проверяется отдельно в пустой antiscam_restore_*БД, с явным
ANTISCAM_RESTORE_DATABASE_URL; рабочая БД не перезаписывается.
