"""The solver's page scripts in a real Camoufox on this computer (openajar.node.solver.widgets): the widget and its
site key read from the page, Cloudflare's full-page markers, and the token handed to the widget's field and the
page's own callback from Camoufox's isolated world. Local pages only (set_content): nothing is fetched.

Skipped where Camoufox's browser is not installed."""

from __future__ import annotations

import pytest
import pytest_asyncio

from openajar.node.solver import widgets


def _camoufox_ready() -> bool:
    try:
        from camoufox.pkgman import installed_verstr

        return bool(installed_verstr())
    except Exception:  # noqa: BLE001 - not installed, or no browser fetched yet
        return False


pytestmark = [pytest.mark.skipif(not _camoufox_ready(), reason="needs Camoufox's browser on this computer"),
              pytest.mark.asyncio(loop_scope="module")]  # one browser for the module

CONTACT = """<form action="/contact" method="post">
<input type="email" name="email"><textarea name="message"></textarea>
<div class="cf-turnstile" data-sitekey="0x4AAAAAAAexample00000" data-action="contact" data-cdata="c-1"
     data-callback="onPass" style="width:300px;height:65px"><input type="hidden" name="cf-turnstile-response" value="">
</div><button>Send</button></form>
<script>window.onPass = (t) => { document.title = 'called:' + t.length; };</script>"""


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(headless=True, i_know_what_im_doing=True) as b:
        yield b


@pytest_asyncio.fixture(loop_scope="module")
async def page(browser):
    p = await browser.new_page()
    yield p
    await p.close()


async def test_the_widget_and_its_site_key_are_read_from_the_page(page):
    await page.set_content(CONTACT)
    widget, why = await widgets.find(page)
    assert widget == widgets.Widget("turnstile", "0x4AAAAAAAexample00000", "contact", "c-1", False, "onPass", False)
    await page.set_content('<div class="g-recaptcha" data-sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI" '
                           'data-size="invisible"><textarea name="g-recaptcha-response"></textarea></div>')
    widget, _ = await widgets.find(page)
    assert (widget.kind, widget.invisible) == ("recaptcha_v2", True)
    await page.set_content('<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001">'
                           '<textarea name="h-captcha-response">already</textarea></div>')
    assert await widgets.find(page) == (None, "the widget on this page already holds its token")
    await page.set_content("<p>nothing here</p>")
    widget, why = await widgets.find(page)
    assert widget is None and "no Turnstile, reCAPTCHA v2 or hCaptcha widget" in why


async def test_the_token_goes_into_the_field_and_the_pages_callback(page):
    await page.set_content(CONTACT)
    widget, _ = await widgets.find(page)
    out = await widgets.hand(page, widget, "XXXX.DUMMY.TOKEN.XXXX")
    assert out == {"filled": 1, "called": "called"}
    assert await page.title() == "called:21"
    value = await page.evaluate("() => document.getElementsByName('cf-turnstile-response')[0].value")
    assert value == "XXXX.DUMMY.TOKEN.XXXX"
    assert (await widgets.find(page))[0] is None  # passed now


async def test_recaptchas_own_client_table_gives_the_callback_when_the_page_passed_a_function(page):
    await page.set_content('<div class="g-recaptcha" data-sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI">'
                           '<textarea name="g-recaptcha-response"></textarea></div><script>window.___grecaptcha_cfg = '
                           '{clients: {0: {aa: {bb: {sitekey: "x", callback: (t) => { document.title = "cfg:" + t; }}}}}};'
                           '</script>')
    widget, _ = await widgets.find(page)
    assert await widgets.hand(page, widget, "03AF-token") == {"filled": 1, "called": "called"}
    assert await page.title() == "cfg:03AF-token"


# ---- the reviews of 2026-11-25 (test_solver_review has the rest) ----

async def test_review_6_a_page_that_loads_recaptcha_enterprise_is_refused(page):
    await page.route("**/*", lambda route: route.abort())  # nothing is fetched
    await page.set_content('<script src="https://www.google.com/recaptcha/enterprise.js" async></script>'
                           '<div class="g-recaptcha" data-sitekey="6LcEnterpriseKey000000000000000000000000" '
                           'data-action="LOGIN"><textarea name="g-recaptcha-response"></textarea></div>')
    widget, why = await widgets.find(page)
    assert widget is None and "Enterprise" in why


async def test_review_9_the_full_page_markers_are_read(page):
    await page.set_content('<div id="challenge-stage"></div>')
    assert await widgets.full_page(page)
    await page.set_content(CONTACT)
    assert not await widgets.full_page(page)


async def test_review2_the_token_goes_only_into_the_page_it_was_asked_for(page):
    await page.set_content(CONTACT)
    widget, _ = await widgets.find(page)
    out = await widgets.hand(page, widget, "XXXX.DUMMY.TOKEN.XXXX", "https://shop.example.com/contact")
    assert out == {"filled": 0, "called": "none", "moved": True}  # the document is about:blank, not that address
    assert await page.evaluate("() => document.getElementsByName('cf-turnstile-response')[0].value") == ""
    assert await page.title() == ""
    here = await page.evaluate("() => location.href")
    assert (await widgets.hand(page, widget, "XXXX.DUMMY.TOKEN.XXXX", here + "#top"))["filled"] == 1
