# The sandbox: what you can break and what you cannot

Every session gets **its own copy of the demo database** and **its own PostgreSQL user**.
You can wreck your copy however you like. After “Start over” or closing the tab it is dropped,
and the next session starts with the original data.

## How it is set up

On the first PostgreSQL start
[`db/init/00-sandbox-template.sql`](../db/init/00-sandbox-template.sql) runs:

- it creates the user `sandbox_owner`. Nobody can log in as it; it only exists to own every
  table in the demo database;
- it creates the `bookstore_template` database with the demo data and marks it as a template.
  Nobody can connect to it; it can only be copied;
- it takes away the right to connect to the system databases `postgres` and `template1`,
  which everyone has by default.

When a session opens, the worker ([`webpsql/sandbox.py`](../webpsql/sandbox.py)) runs:

```sql
create role s_<id> login password '<random>' connection limit 3 in role sandbox_owner;
alter role s_<id> set statement_timeout = '30s';                     -- a query longer than 30 s is cancelled
alter role s_<id> set idle_in_transaction_session_timeout = '5min';  -- an abandoned transaction is closed after 5 min
alter role s_<id> set temp_file_limit = '256MB';                     -- limit on temporary files on disk
create database sbx_<id> template bookstore_template owner s_<id>;
revoke connect on database sbx_<id> from public;
```

When the session ends — `drop database sbx_<id> with (force); drop role s_<id>;`.

The session user is a member of `sandbox_owner`, so it can change and drop any table in its
own copy. It cannot connect to any other database: only their owners have the right to connect,
and every copy has its own owner.

## Tested by hand

| What we do | What we get |
|---|---|
| `drop table orders, order_items cascade;` | works — it is your own copy |
| then “Start over” and `select count(*) from books;` | 30 — the data is back |
| `drop database sbx_<own>;` | you cannot drop the database you are connected to, and you cannot connect to any other |
| `drop database bookstore_template;` | `cannot drop a template database` |
| `\c postgres`, `\c sbx_<someone else's>` | `permission denied … User does not have CONNECT privilege` |
| `copy (select 1) to program 'id';` | `permission denied to COPY to or from an external program` |
| `alter system set work_mem = '1GB';` | `permission denied to set parameter` |
| `\! id` | `uid=2000(sandbox)` — not an administrator and not the user the worker runs as |
| `\! env`, `\! cat /proc/1/environ` | the environment holds only your own session's details; the worker's environment cannot be read (`Permission denied`) |

The stack tests in CI check most of these on every push.

## OS commands through `\!`

psql can run operating system commands: `\!`, `\o |command`, `\g |command`,
`\copy … program`. This cannot be turned off, so the user effectively has a shell **inside the
worker container**. To keep that shell harmless:

- psql runs as a separate OS user, `sandbox`, not as the user the worker runs as;
- psql gets only the details for connecting to its own copy. The PostgreSQL administrator
  password and the Redis address are not in its environment;
- the `sandbox` user cannot read the worker's own environment;
- the number of processes for `sandbox` and the container memory are capped: an endless
  process fork bomb hits the limit;
- psql command history is not saved to a file: the file would be shared by all sessions.

## What the sandbox does not cover

The project is meant to be run on your own machine. Before letting other people in, decide on:

- **Network.** From a shell through `\!`, Redis and PostgreSQL are reachable. The user does not
  know the passwords, but the default ones (`web-psql`) are in the repository — set your own in
  `.env`. It is safer to run psql in a separate container with no access to Redis.
- **Shared home directory.** A file one session writes to `/home/sandbox` through `\!`
  is visible to the next one.
- **Load.** The user can lift the 30-second query limit for themselves
  (`set statement_timeout = 0`), and a heavy query will keep the server CPU busy. The size of a
  database copy is limited only by the disk.
- **Login.** There is no password: anyone who opens the page gets a terminal.
