"""Take the README screenshots from a running stack.

Needs Playwright (``pip install playwright``) and an installed Google Chrome::

    docker compose up --build -d
    python scripts/screenshots.py

Images are saved to ``assets/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def run_sql(page: Page, sql: str, wait_ms: int = 900) -> None:
    """Type a command in the terminal and press Enter.

    :param page: the terminal page.
    :param sql: psql command.
    :param wait_ms: how long to wait for the output.
    """
    page.locator("#terminal").click()
    page.keyboard.type(sql)
    page.keyboard.press("Enter")
    page.wait_for_timeout(wait_ms)


def open_terminal(page: Page) -> None:
    """Open the terminal and wait until the database copy is ready.

    :param page: the page.
    """
    page.goto(BASE)
    page.wait_for_selector("#st-db.ok", timeout=15000)
    page.wait_for_timeout(500)


def main() -> None:
    """Take three screenshots: a query, a "broken" database, the sessions list."""
    ASSETS.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme="dark")

        page = ctx.new_page()
        open_terminal(page)
        run_sql(page, "\\dt")
        run_sql(page, "select * from top_authors limit 6;")
        page.screenshot(path=ASSETS / "terminal.png")

        run_sql(page, "drop table books cascade;")
        run_sql(page, "truncate customers cascade;")
        run_sql(page, "select count(*) from orders;")
        run_sql(page, "\\dt")
        page.screenshot(path=ASSETS / "sandbox.png")

        # a couple more tabs, so the sessions table has something to show
        others = [ctx.new_page() for _ in range(2)]
        for other in others:
            open_terminal(other)
        page.goto(f"{BASE}/sessions")
        page.wait_for_selector("td .badge")
        page.wait_for_timeout(500)
        page.screenshot(path=ASSETS / "sessions.png")

        browser.close()
    print("done:", ", ".join(f.name for f in sorted(ASSETS.glob("*.png"))))


if __name__ == "__main__":
    main()
