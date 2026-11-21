"""Render og.html to ../public/og.png at 1200 by 630, with the Google Chrome installed on this computer.

    uv run --no-project --with playwright python site/og/render.py
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome")
        page = await browser.new_page(viewport={"width": 1200, "height": 630})
        await page.goto((HERE / "og.html").as_uri())
        await page.evaluate("document.fonts.ready")
        await page.evaluate("Promise.all([...document.images].map(i => i.decode()))")
        await page.screenshot(path=str(HERE.parent / "public" / "og.png"))
        await browser.close()


asyncio.run(main())
