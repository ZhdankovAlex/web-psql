-- Runs once, on the first start of the postgres container (as the superuser).
--
-- Prepares the template the worker copies for every session:
--   * sandbox_owner      — a user that cannot log in; owns every table in the template;
--   * bookstore_template — the template database; nobody can connect to it.
-- Each session user is a member of sandbox_owner, so it can do anything in its own copy
-- (including DROP TABLE), but cannot connect to other copies or to the system databases.

create role sandbox_owner nologin;
create database bookstore_template owner sandbox_owner;

\connect bookstore_template
set role sandbox_owner;
\i /sql/schema.sql
\i /sql/data.sql
reset role;

\connect postgres
alter database bookstore_template with is_template true allow_connections false;

-- By default anyone (PUBLIC) may connect to any database. Close the system databases:
-- a session user should only see its own copy.
revoke connect on database postgres  from public;
revoke connect on database template1 from public;
