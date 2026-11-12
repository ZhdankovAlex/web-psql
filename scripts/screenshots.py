"""Снять скриншоты для README с работающего стенда.

Нужен Playwright (``pip install playwright``) и установленный Google Chrome::

    docker compose up --build -d
    python scripts/screenshots.py

Картинки сохраняются в ``assets/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ASSETS = Path(__file__).resolve().parent.parent / "assets"


def run_sql(page: Page, sql: str, wait_ms: int = 900) -> None:
    """Набрать команду в терминале и нажать Enter.

    :param page: страница с терминалом.
    :param sql: команда psql.
    :param wait_ms: сколько ждать вывода.
    """
    page.locator("#terminal").click()
    page.keyboard.type(sql)
    page.keyboard.press("Enter")
    page.wait_for_timeout(wait_ms)


def open_terminal(page: Page) -> None:
    """Открыть терминал и дождаться, пока psql создаст копию базы.

    :param page: страница.
    """
    page.goto(BASE)
    page.wait_for_selector("#st-db.ok", timeout=15000)
    page.wait_for_timeout(500)


def main() -> None:
    """Снять три скриншота: запрос, «сломанная» база, список сессий."""
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

        # ещё пара вкладок, чтобы в таблице сессий было на что посмотреть
        others = [ctx.new_page() for _ in range(2)]
        for other in others:
            open_terminal(other)
        page.goto(f"{BASE}/sessions")
        page.wait_for_selector("td .badge")
        page.wait_for_timeout(500)
        page.screenshot(path=ASSETS / "sessions.png")

        browser.close()
    print("готово:", ", ".join(f.name for f in sorted(ASSETS.glob("*.png"))))


if __name__ == "__main__":
    main()
