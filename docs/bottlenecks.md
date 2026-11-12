# What limits scale

Everything was measured on a laptop with Docker Desktop (WSL2), PostgreSQL 17. The demo
database template is 9.4 MB.

## What one session costs

| What | How much | How it was measured |
|---|---|---|
| Creating the database copy | 40–60 ms for a single session; up to ~200 ms when 10 open at once | `\timing` in psql and the `sandbox_ms` field in `/api/sessions` |
| From opening the page to the `bookstore=>` prompt | ~160 ms for a single session; up to ~480 ms when 10 open at once | 12 connections from the browser at the same time |
| Processes | one psql in the worker and one PostgreSQL process | `ps` and `pg_stat_activity` |
| Memory | ~2–3 MB in PostgreSQL and ~2 MB in the worker | `docker stats` with no sessions and with 10: PostgreSQL 181 → 205 MB, worker 25 → 44 MB |
| Disk | ~9.4 MB — a full copy of the template | `pg_database_size` |
| Redis connections | about three | `INFO clients`: 34 connections with 10 sessions |

## Weak spots — in the order we will hit them

### 1. PostgreSQL connections run out first

PostgreSQL starts a separate process for every connection. Each session has at least one
connection, and the session user can open up to three. By default PostgreSQL accepts 100
connections, so the limit is 30 to 90 sessions at the same time.

A connection pooler like PgBouncer does not help here: it saves connections when many clients
use one database as one user. Here every session has its own database and its own user, so
there is nothing to share.

**What is done:** each worker runs at most 10 sessions (`MAX_SESSIONS`). The eleventh user
immediately sees "too many open terminals" instead of an obscure error from psql.

**How to go further:** several PostgreSQL servers with sessions spread between them.

### 2. Creating copies when many people arrive at once

PostgreSQL can copy a database in two ways:

| Method | 9.4 MB template | How it works |
|---|---|---|
| `WAL_LOG` — the default since PostgreSQL 15 | 40–60 ms | copies the database page by page and writes each page to the write-ahead log; time grows with the template size |
| `FILE_COPY` | 100–480 ms | copies whole files, but first flushes all server changes to disk twice; slower for a small database |

For our small template the default is faster. But the worker creates at most four copies at a
time (that is how many admin connections to PostgreSQL it has), the rest wait their turn. So
when many sessions open together, the wait grows.

**How to go further:** create a few copies in advance and hand out a ready one instantly,
making the next one in the background. If the template grows to gigabytes, switch to
`FILE_COPY` or to file system snapshots (ZFS, btrfs).

### 3. Huge output with a slow browser

The worker reads everything psql prints right away and puts it into Redis. Redis keeps the last
~2,000 pieces of output for each session and deletes older ones. If the browser takes data
slower than psql prints it, the oldest pieces can be deleted before the gateway sends them, and
the browser never gets them. Nothing reports this.

Paging usually saves the day: psql shows a long result through `less`, and the output waits
until the user scrolls. But if paging is turned off (`\pset pager off`) and you run
`select * from generate_series(1, 1000000)`, the output pours out non-stop.

**How to fix:** when too much unread output piles up in Redis, the worker stops reading from
psql. psql fills the terminal buffer and waits until it is drained, so nothing is lost.

### 4. Redis connections

While a service waits for new messages in Redis, it holds a connection. Right now each session
needs two of them: the gateway waits for output, the worker waits for input. Plus short requests.
By default Redis accepts 10,000 connections — about 3,000 sessions, far more than PostgreSQL
can handle.

**How to go further:** wait for messages from all of a service's sessions with a single
request — Redis supports that. Then each service needs one connection for all its sessions.

### 5. What is not a weak spot

- **A single thread in the worker.** The worker hardly computes anything, it only moves data
  between psql and Redis, so one asynchronous thread is enough. Output is sent in portions:
  everything psql printed between two sends goes to Redis as one message.
- **The gateway** keeps nothing in memory, so you can run as many as you like behind a load
  balancer (it has to support WebSocket).
- **Workers** can also run in several copies: `docker compose up --scale worker=3`.
  Each request for a new session goes to exactly one of them.
