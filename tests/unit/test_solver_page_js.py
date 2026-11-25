"""The solver's page scripts in a real Camoufox on this computer (openajar.node.solver.widgets): the widget and its
site key read from the page, the sign-up, login and payment page guard, and the token handed to the widget's field
and the page's own callback from Camoufox's isolated world. Local pages only (set_content): nothing is fetched.

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


async def test_a_contact_form_is_not_a_sign_up_login_or_payment_page(page):
    await page.set_content(CONTACT)
    assert await widgets.sensitive(page, widgets.Widget("turnstile", "k")) == []
    # a login form elsewhere on the page, hidden (a menu's), and not the widget's: not a reason
    await page.set_content(CONTACT + '<form style="display:none"><input type="password" name="p"></form>')
    assert await widgets.sensitive(page, widgets.Widget("turnstile", "k")) == []


@pytest.mark.parametrize(("extra", "why"), [
    ('<input type="password" name="pw">', "a password field"),
    ('<input name="code" autocomplete="one-time-code">', "a field for one-time-code"),
    ('<input name="cardnumber">', "a card field"),
    ('<input name="n" autocomplete="cc-number">', "a field for cc-number"),
])
async def test_a_login_or_card_field_makes_it_one(page, extra, why):
    await page.set_content(CONTACT.replace("<button>", extra + "<button>"))
    assert why in await widgets.sensitive(page, widgets.Widget("turnstile", "k"))


async def test_a_hidden_password_in_the_widgets_own_form_still_counts(page):
    await page.set_content(CONTACT.replace("<button>", '<input type="password" name="p" style="display:none">'
                                                       "<button>"))
    assert "a password field" in await widgets.sensitive(page, widgets.Widget("turnstile", "k"))


async def test_a_form_that_goes_to_a_sign_up_counts(page):
    await page.set_content(CONTACT.replace('action="/contact"', 'action="/users/register"'))
    assert "its form goes to 'register'" in await widgets.sensitive(page, widgets.Widget("turnstile", "k"))


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


# ---- the review of 2026-11-25 (test_solver_review has the rest) ----

async def test_review_2_a_login_form_in_an_open_shadow_root_counts(page):
    await page.set_content('<login-box></login-box>' + CONTACT + '''<script>
      customElements.define('login-box', class extends HTMLElement { constructor() { super();
        this.attachShadow({mode: 'open'}).innerHTML = '<form action="/x"><input name=user>' +
          '<input type=password name=pw autocomplete=current-password></form>'; }});
    </script>''')
    assert "a password field" in await widgets.sensitive(page, widgets.Widget("turnstile", "k"))


async def test_review_2_a_disabled_password_field_counts(page):
    await page.set_content(CONTACT.replace("<button>", '<input type="password" name="pw" disabled><button>'))
    assert "a password field" in await widgets.sensitive(page, widgets.Widget("turnstile", "k"))


async def test_review_2_a_passwordless_login_counts(page):
    await page.set_content(CONTACT.replace('<input type="email" name="email">',
                                           '<input type="email" name="email" autocomplete="username">'))
    assert "a field for username" in await widgets.sensitive(page, widgets.Widget("turnstile", "k"))


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
