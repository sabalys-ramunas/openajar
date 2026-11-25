"""Interaction on real Camoufox against the local fixture site (run on the node)."""

from __future__ import annotations

import json
import re

import pytest

from openajar.core.errors import InvalidRequest, RefNotFound
from openajar.core.models import ActionKind, Signal
from openajar.node.interaction import PlaywrightInteraction
from openajar.node.snapshot import find_refs

pytestmark = [pytest.mark.node, pytest.mark.asyncio(loop_scope="session")]

POST = "Post by Ada Lovelace"

# Runs in Camoufox's isolated world: the observer and its log live there, invisible to the page.
WATCH_MUTATIONS_JS = """
() => {
  window.__openajarTestMutations = [];
  new MutationObserver((records) => {
    for (const r of records) window.__openajarTestMutations.push(r.type + ':' + (r.attributeName || ''));
  }).observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
}
"""

LIKES_JS = """
() => Array.from(document.querySelectorAll('button.like'), (b) => ({
  owner: b.closest('article').id,
  pressed: b.getAttribute('aria-pressed'),
  count: Number(b.querySelector('.count').textContent),
}))
"""


@pytest.fixture
def ia() -> PlaywrightInteraction:
    return PlaywrightInteraction(allow_private_targets=True)  # fixtures are served on 127.0.0.1


def one_ref(text: str, role: str, name: str, within: str | None = None) -> str:
    refs = find_refs(text, role=role, name=name, within=within)
    assert len(refs) == 1, f"expected one {role} {name!r} within {within!r}, got {refs}\n{text}"
    return refs[0]


async def test_navigate_reports_status_title_and_signal(page, site_url, ia):
    assert await ia.signal(page) is Signal.UNKNOWN  # nothing loaded yet
    result = await ia.navigate(page, f"{site_url}/feed.html")
    assert result.status == 200
    assert result.title == "Fixture feed"
    assert result.url == f"{site_url}/feed.html"
    assert result.signal is Signal.OK


async def test_snapshot_has_refs_and_leaves_the_page_untouched(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/feed.html")
    await page.evaluate(WATCH_MUTATIONS_JS)
    html_before = await page.evaluate("document.documentElement.outerHTML")

    snap = await ia.snapshot(page)

    await page.wait_for_timeout(400)  # give the page's own leak detector a few ticks
    assert await page.evaluate("document.documentElement.outerHTML") == html_before
    assert await page.evaluate("window.__openajarTestMutations") == []
    assert await page.evaluate("document.body.dataset.leak || ''") == ""

    assert snap.title == "Fixture feed" and snap.url.endswith("/feed.html")
    assert not snap.truncated
    assert snap.ref_count == len(re.findall(r"\[ref=", snap.text)) >= 7
    one_ref(snap.text, "button", "Like 12", within=POST)
    one_ref(snap.text, "link", "Home")
    for line in snap.text.splitlines():
        if re.match(r"\s*- (paragraph|heading|article|region|main|banner)\b", line):
            assert "[ref=" not in line, line


async def test_click_post_like_by_ref_changes_the_post_only(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/feed.html")
    snap = await ia.snapshot(page)
    # The post and Grace's comment both show "Like 12"; only the ref tells them apart.
    assert len(find_refs(snap.text, role="button", name="Like 12")) == 2
    post_like = one_ref(snap.text, "button", "Like 12", within=POST)
    before = {b["owner"]: b for b in await page.evaluate(LIKES_JS) if b["owner"].startswith("comment")}

    result = await ia.act(page, post_like, ActionKind.CLICK)

    assert result.ok and result.changed is True
    assert "aria-pressed false -> true" in result.detail
    assert 'name "Like 12" -> "Like 13"' in result.detail
    assert result.signal is Signal.OK
    likes = {b["owner"]: b for b in await page.evaluate(LIKES_JS)}
    assert likes["post-1"] == {"owner": "post-1", "pressed": "true", "count": 13}
    assert likes["post-2"]["pressed"] == "false" and likes["post-2"]["count"] == 5
    for owner, b in before.items():
        assert likes[owner] == b, f"{owner} changed"
        assert likes[owner]["pressed"] == "false"


async def test_type_fill_select_check_and_submit(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/form.html")
    snap = await ia.snapshot(page)
    name = one_ref(snap.text, "textbox", "Name")
    bio = one_ref(snap.text, "textbox", "Bio")
    color = one_ref(snap.text, "combobox", "Favourite color")
    subscribe = one_ref(snap.text, "checkbox", "Subscribe to news")
    submit = one_ref(snap.text, "button", "Submit")

    typed = await ia.act(page, name, ActionKind.TYPE, "Ada L")
    assert typed.changed is True and 'value "" -> "Ada L"' in typed.detail
    assert await page.evaluate("Number(document.body.dataset.trustedKeydowns)") >= len("Ada L")

    filled = await ia.act(page, bio, ActionKind.FILL, "Counts numbers")
    assert filled.changed is True and 'value "Old bio text" -> "Counts numbers"' in filled.detail

    picked = await ia.act(page, color, ActionKind.SELECT, "Green")
    assert picked.ok and picked.changed is True
    assert 'selected "Choose one" -> "Green"' in picked.detail

    with pytest.raises(InvalidRequest):
        await ia.act(page, color, ActionKind.SELECT, "Purple")

    checked = await ia.act(page, subscribe, ActionKind.CLICK)
    assert checked.changed is True and "checked false -> true" in checked.detail

    sent = await ia.act(page, submit, ActionKind.CLICK)
    assert sent.ok and sent.changed is False  # the button's own state does not change
    result = json.loads(await page.evaluate("document.getElementById('result').textContent"))
    assert result == {"name": "Ada L", "bio": "Counts numbers", "color": "g", "subscribe": True}


async def test_press_enter_in_a_field_submits(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/form.html")
    snap = await ia.snapshot(page)
    name = one_ref(snap.text, "textbox", "Name")
    await ia.act(page, name, ActionKind.FILL, "Grace")
    pressed = await ia.act(page, name, ActionKind.PRESS, "Enter")
    assert pressed.ok
    result = json.loads(await page.evaluate("document.getElementById('result').textContent"))
    assert result["name"] == "Grace"


async def test_scroll_to_a_far_target_uses_the_wheel(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/long.html")
    snap = await ia.snapshot(page)
    far = one_ref(snap.text, "button", "Far target")

    scrolled = await ia.act(page, far, ActionKind.SCROLL)

    assert scrolled.ok and scrolled.detail == "in view"
    assert int(await page.evaluate("document.body.dataset.wheels")) >= 5
    assert await page.evaluate("window.scrollY") > 4000
    box = await page.locator("#far").bounding_box()
    height = await page.evaluate("window.innerHeight")
    assert 0 <= box["y"] and box["y"] + box["height"] <= height

    clicked = await ia.act(page, far, ActionKind.CLICK)
    assert clicked.ok
    assert await page.evaluate("document.body.dataset.clicked") == "trusted"


async def test_signal_for_status_codes_and_challenges(page, site_url, ia):
    limited = await ia.navigate(page, f"{site_url}/status/429")
    assert limited.status == 429 and limited.signal is Signal.RATE_LIMITED
    assert await ia.signal(page) is Signal.RATE_LIMITED

    blocked = await ia.navigate(page, f"{site_url}/status/403")
    assert blocked.status == 403 and blocked.signal is Signal.BLOCKED

    challenge = await ia.navigate(page, f"{site_url}/captcha.html")
    assert challenge.status == 200 and challenge.signal is Signal.CAPTCHA

    fine = await ia.navigate(page, f"{site_url}/index.html")
    assert fine.signal is Signal.OK


async def test_stale_refs_raise_ref_not_found(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/feed.html")
    snap = await ia.snapshot(page)
    post_like = one_ref(snap.text, "button", "Like 12", within=POST)

    await ia.navigate(page, f"{site_url}/form.html")
    with pytest.raises(RefNotFound):
        await ia.act(page, post_like, ActionKind.CLICK)

    await ia.snapshot(page)  # a snapshot of the new page does not bring the old ref back
    with pytest.raises(RefNotFound):
        await ia.act(page, post_like, ActionKind.CLICK)
    with pytest.raises(RefNotFound):
        await ia.act(page, "e99999", ActionKind.CLICK)


async def test_screenshot_is_a_png_of_the_viewport(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/feed.html")
    png = await ia.screenshot(page)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 1000


async def test_a_covered_target_is_refused_until_the_overlay_is_gone(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/overlay.html")
    snap = await ia.snapshot(page)
    covered = one_ref(snap.text, "button", "Covered action")
    accept = one_ref(snap.text, "button", "Accept")

    from openajar.node.snapshot import OUTLINES

    OUTLINES.remember(page, "agent-1", page.url, snap.text)  # what browser_snapshot keeps of what the agent saw
    with pytest.raises(InvalidRequest, match="covers the target") as err:
        await ia.act(page, covered, ActionKind.CLICK)
    assert f'dialog "Cookie notice", which has button "Accept" [ref={accept}]' in err.value.message  # what to close
    assert await page.evaluate("document.body.dataset.coveredClicks") == "0"

    gone = await ia.act(page, accept, ActionKind.CLICK)
    assert gone.changed is None and "left the page" in gone.detail  # the banner removed itself
    clicked = await ia.act(page, covered, ActionKind.CLICK)
    assert clicked.ok
    assert await page.evaluate("document.body.dataset.coveredClicks") == "1"


async def test_hover_reads_back_the_targets_own_state(page, site_url, ia):
    await ia.navigate(page, f"{site_url}/overlay.html")
    snap = await ia.snapshot(page)
    menu = one_ref(snap.text, "button", "Menu")
    hovered = await ia.act(page, menu, ActionKind.HOVER)
    assert hovered.ok and hovered.changed is True
    assert "aria-expanded false -> true" in hovered.detail
