"""Render og.html to ../public/og.png at 1200 by 630, with the Google Chrome installed on this computer.

    uv run --no-project --with playwright python site/og/render.py

First it serves ../public on a free local port and photographs the page's scene (the agent's computer, in its
finished state, as a visitor who asks for less motion sees it) to og/scene.png; og.html places that picture.
"""

import asyncio
import functools
import http.server
import threading
from pathlib import Path

from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
PUBLIC = HERE.parent / "public"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: object) -> None:
        pass


def serve() -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = functools.partial(Quiet, directory=str(PUBLIC))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


async def main() -> None:
    server, port = serve()
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="chrome")
            page = await browser.new_page(
                viewport={"width": 1440, "height": 1100}, device_scale_factor=1.5, reduced_motion="reduce"
            )
            await page.goto(f"http://127.0.0.1:{port}/")
            await page.evaluate("document.fonts.ready")
            await page.wait_for_timeout(600)
            await page.locator("#scene").screenshot(path=str(HERE / "scene.png"), omit_background=True)

            page = await browser.new_page(viewport={"width": 1200, "height": 630})
            await page.goto((HERE / "og.html").as_uri())
            await page.evaluate("document.fonts.ready")
            await page.evaluate("Promise.all([...document.images].map(i => i.decode()))")
            await page.screenshot(path=str(PUBLIC / "og.png"))
            await browser.close()
    finally:
        server.shutdown()


asyncio.run(main())
