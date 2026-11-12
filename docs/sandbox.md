# Песочница: что можно сломать и что нельзя

Каждая сессия получает **свою копию учебной базы** и **свою роль PostgreSQL**. Копию можно
разнести как угодно — после «Начать заново» (или закрытия вкладки) она удаляется,
а следующая сессия снова начинается с исходных данных.

## Как устроено

При первом старте PostgreSQL ([`db/init/00-sandbox-template.sql`](../db/init/00-sandbox-template.sql)):

- роль `sandbox_owner` (без входа) — владелец всех таблиц шаблона;
- база `bookstore_template`: `is_template = true`, `allow_connections = false`;
- `REVOKE CONNECT … FROM PUBLIC` на базах `postgres` и `template1`.

На каждую сессию worker ([`webpsql/sandbox.py`](../webpsql/sandbox.py)) выполняет:

```sql
create role s_<sid> login password '<случайный>' connection limit 3 in role sandbox_owner;
alter role s_<sid> set statement_timeout = '30s';
alter role s_<sid> set idle_in_transaction_session_timeout = '5min';
alter role s_<sid> set temp_file_limit = '256MB';
create database sbx_<sid> template bookstore_template owner s_<sid>;
revoke connect on database sbx_<sid> from public;
```

а в конце — `drop database sbx_<sid> with (force); drop role s_<sid>;`.

Роль сессии входит в `sandbox_owner`, поэтому в своей копии может менять и удалять любые
объекты. При этом подключиться она может только к своей базе: у остальных `CONNECT`
отозван у `PUBLIC`, а владелец у каждой копии свой.

## Проверено руками

| Что делаем | Результат |
|---|---|
| `drop table orders, order_items cascade;` | работает — это своя копия |
| новая сессия после этого: `select count(*) from books;` | 30 — исходные данные на месте |
| `drop database bookstore;` / `drop database sbx_<своя>;` | нельзя: к другим базам не подключиться, а текущую удалить нельзя |
| `drop database bookstore_template;` | `cannot drop a template database` |
| `\c postgres`, `\c sbx_<чужая>` | `permission denied … User does not have CONNECT privilege` |
| `copy (select 1) to program 'id';` | `permission denied to COPY to or from an external program` |
| `alter system set work_mem = '1GB';` | `permission denied to set parameter` |
| `\! id` | `uid=2000(sandbox)` — не root и не пользователь worker'а |
| `\! env`, `\! cat /proc/1/environ` | в окружении только данные своей роли; окружение worker'а закрыто (`Permission denied`) |

## Shell через `\!`

psql умеет запускать команды ОС (`\!`, `\o |cmd`, `\g |cmd`, `\copy … program`). Отключить
это нельзя, поэтому пользователь фактически получает shell **в контейнере worker'а**.
Что сделано, чтобы этот shell был безвредным:

- psql запускается от отдельного пользователя ОС `sandbox` (uid 2000), а не от worker'а;
- окружение psql собирается с нуля: в нём нет `ADMIN_DSN`, `REDIS_URL` и паролей;
- `/proc/<pid>/environ` worker'а (root) пользователю `sandbox` недоступен;
- `RLIMIT_NPROC` для `sandbox` и `pids_limit` / `mem_limit` контейнера — fork-бомба упрётся в лимит;
- файл истории psql не пишется (`PSQL_HISTORY=/dev/null`): иначе он был бы общим на всех.

## Чего песочница не закрывает

Это демо для запуска у себя. Перед тем как выставить его наружу, нужно решить:

- **Сеть из shell.** Пользователь `sandbox` видит ту же сеть, что и worker: Redis и
  PostgreSQL. Пароли он не знает, но пароли по умолчанию (`web-psql`) записаны в репозитории —
  задайте свои в `.env`. Надёжнее запускать psql в отдельном контейнере без доступа к Redis.
- **Общий домашний каталог.** Файлы, которые одна сессия запишет через `\!` в `/home/sandbox`,
  увидит следующая.
- **Ресурсы.** `statement_timeout` роль может переопределить у себя (`set statement_timeout = 0`),
  и тяжёлый запрос займёт CPU сервера. Размер копии базы ничем не ограничен, кроме диска.
- **Аутентификации нет.** Любой, кто открыл страницу, получает терминал.
