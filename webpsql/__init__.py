"""web-psql: interactive psql in the browser.

The package is two processes that talk to each other only through Redis Streams:

* :mod:`webpsql.gateway` — web server: serves the pages and holds the WebSocket with the browser;
* :mod:`webpsql.worker` — runs psql in pseudo-terminals, one copy of the demo database per session.
"""
