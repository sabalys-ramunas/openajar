"""A click on a covered element says what covers it (openajar.node.interaction.covered): the covering element's
ref, or the ref of its closest ancestor that has one, else the covering layer with the buttons in it, so the
agent can close it in one step. Real pages in system Chrome (skipped where Chrome cannot start), and the words."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from openajar.core.models import ActionKind
from openajar.node.human import TargetCovered
from openajar.node.interaction import PlaywrightInteraction
from openajar.node.interaction.covered import covered_words
from openajar.node.snapshot import OUTLINES, find_refs, take_snapshot

pw = pytest.importorskip("playwright.async_api")

OVERLAY = (Path(__file__).parent.parent / "fixtures" / "site" / "overlay.html").read_text()
HOST = "https://covered.fixture.example"
STICKY = """<!doctype html><title>Sticky</title>
<style>#chat { position: fixed; left: 0; top: 0; width: 100%; height: 200px; }
#target { position: absolute; top: 80px; left: 40%; }</style>
<button id="target">Buy now</button><button id="chat"><span>Chat with us</span></button>"""
NESTED = """<!doctype html><title>Nested</title>
<style>#bar { position: fixed; left: 0; top: 0; width: 100%; height: 200px; background: #eee; }
#target { position: absolute; top: 80px; left: 40%; }</style>
<button id="target">Search</button><a id="bar" href="/promo"><div><p>Get the app</p></div></a>"""


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def browser():
    async with pw.async_playwright() as p:
        try:
            b = await p.chromium.launch(channel="chrome", headless=True)
        except Exception as e:  # noqa: BLE001 - no Chrome here: the words' tests still run
            pytest.skip(f"no Chrome: {e}")
        yield b
        await b.close()


@pytest_asyncio.fixture(loop_scope="module")
async def page(browser):
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    bodies = {"/overlay.html": OVERLAY, "/sticky.html": STICKY, "/nested.html": NESTED}

    async def serve(route):
        path = route.request.url.removeprefix(HOST)
        await route.fulfill(status=200, content_type="text/html", body=bodies.get(path, "<p>gone</p>"))

    await page.route(f"{HOST}/**", serve)
    yield page
    await context.close()


async def _seen(page, path: str) -> str:
    """Open the page and take the outline an agent sees (the tool layer remembers it, as browser_snapshot does)."""
    await page.goto(f"{HOST}{path}")
    snap = await take_snapshot(page)
    OUTLINES.remember(page, "agent-1", page.url, snap.text)
    return snap.text


async def _covered(page, ref: str) -> TargetCovered:
    with pytest.raises(TargetCovered) as err:
        await PlaywrightInteraction(allow_private_targets=True).act(page, ref, ActionKind.CLICK)
    return err.value


pytestmark = pytest.mark.asyncio(loop_scope="module")


async def test_a_dialog_with_text_over_the_target_names_the_dialog_and_its_button(page):
    """The 2026-11-24 sites run: cookie boxes, sign-in promos and location choosers stopped the search on 5 sites,
    since "another element covers the target" did not say what to close."""
    text = await _seen(page, "/overlay.html")
    [covered] = find_refs(text, role="button", name="Covered action")
    [accept] = find_refs(text, role="button", name="Accept")
    err = await _covered(page, covered)
    assert err.message.startswith('another element covers the target: dialog "Cookie notice", which has button '
                                  f'"Accept" [ref={accept}]. Close it with one of them'), err.message
    found = err.details["covered_by"]
    assert found["inside"] == {"role": "dialog", "name": "Cookie notice"}
    assert found["buttons"] == [{"role": "button", "name": "Accept", "ref": accept}]
    assert "—" not in err.message


async def test_a_covering_button_is_named_by_its_own_ref(page):
    text = await _seen(page, "/sticky.html")
    [target] = find_refs(text, role="button", name="Buy now")
    [chat] = find_refs(text, role="button", name="Chat with us")
    err = await _covered(page, target)
    assert err.message.startswith(f'another element covers the target: button "Chat with us" [ref={chat}]. '
                                  "Close or answer it"), err.message
    assert err.details["covered_by"]["ref"] == chat


async def test_text_inside_a_link_gets_the_links_ref_its_closest_ancestor_with_one(page):
    text = await _seen(page, "/nested.html")
    [target] = find_refs(text, role="button", name="Search")
    [bar] = find_refs(text, role="link")  # Chrome gives this link no name: the ref check on the page finds it
    err = await _covered(page, target)
    assert f"covers the target: link [ref={bar}]" in err.message, err.message
    assert err.details["covered_by"]["ref"] == bar


async def test_without_a_snapshot_it_names_what_covers_it_and_asks_for_one(page):
    await page.goto(f"{HOST}/sticky.html")
    snap = await take_snapshot(page)  # refs exist, but no agent saw this outline
    OUTLINES.forget(page)
    [target] = find_refs(snap.text, role="button", name="Buy now")
    err = await _covered(page, target)
    assert err.message == ('another element covers the target: button "Chat with us". Take a snapshot to get its '
                           "ref, close it, then act again")


def test_the_words_when_nothing_could_be_read_keep_the_old_hint():
    words = covered_words(None, outline=True)
    assert words.startswith("another element covers the target (a banner, dialog or sticky header?)")
    assert covered_words({"role": "dialog", "name": "Sign in", "inside": {"role": "dialog", "name": "Sign in"}},
                         outline=True) == ('another element covers the target: dialog "Sign in", which has no ref in '
                                           "your last snapshot. Snapshot again, close it, then act again")
