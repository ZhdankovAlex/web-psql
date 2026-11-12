-- Выполняется один раз при первом старте контейнера postgres (от имени суперпользователя).
--
-- Готовит шаблон, из которого worker копирует базу для каждой сессии:
--   * sandbox_owner  — роль без входа, владелец всех таблиц шаблона;
--   * bookstore_template — шаблонная база, подключаться к ней нельзя.
-- Роль каждой сессии входит в sandbox_owner, поэтому в своей копии может делать что угодно
-- (в том числе DROP TABLE), а к чужим копиям и служебным базам подключиться не может.

create role sandbox_owner nologin;
create database bookstore_template owner sandbox_owner;

\connect bookstore_template
set role sandbox_owner;
\i /sql/schema.sql
\i /sql/data.sql
reset role;

\connect postgres
alter database bookstore_template with is_template true allow_connections false;

-- По умолчанию подключаться к любой базе может PUBLIC. Закрываем служебные базы:
-- роль сессии должна видеть только свою копию.
revoke connect on database postgres  from public;
revoke connect on database template1 from public;
