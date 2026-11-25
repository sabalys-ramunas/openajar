"""Cloudflare's automatic check gets the seconds a person gives it before the signal is read (autocheck)."""

from __future__ import annotations

import pytest

from openajar.core.models import Signal
from openajar.node.interaction import autocheck
from openajar.node.interaction.autocheck import cloudflare_check, past_automatic_check
from openajar.node.sites import CLOUDFLARE_DETAILS, SignalReport

CHALLENGE = SignalReport(Signal.CAPTCHA, "Cloudflare challenge page", "https://www.glassdoor.com/")
WIDGET = SignalReport(Signal.CAPTCHA, "Cloudflare Turnstile frame", "https://example.com/form")
OK = SignalReport(Signal.OK, None, "https://www.glassdoor.com/")


class Page:
    def __init__(self):
        self.loads = 0
        self.closed = False

    def is_closed(self):
        return self.closed

    async def wait_for_load_state(self, state, timeout=None):
        self.loads += 1


def reader(*reports):
    seen = list(reports)
    calls = []

    async def read(page):
        calls.append(1)
        return seen.pop(0) if len(seen) > 1 else seen[0]
    return read, calls


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(autocheck, "AUTO_POLL_S", 0.0)


def test_only_cloudflares_own_check_waits():
    assert cloudflare_check(CHALLENGE) and cloudflare_check(WIDGET)
    assert {"Cloudflare challenge page", "Cloudflare Turnstile frame"} <= CLOUDFLARE_DETAILS
    assert not cloudflare_check(SignalReport(Signal.CAPTCHA, "hCaptcha frame"))
    assert not cloudflare_check(SignalReport(Signal.CAPTCHA, "DataDome frame"))
    assert not cloudflare_check(SignalReport(Signal.BLOCKED, "HTTP 403"))
    assert not cloudflare_check(OK)


async def test_a_page_with_no_check_is_read_once():
    read, calls = reader(OK)
    assert await past_automatic_check(Page(), OK, read) is OK
    assert calls == []


async def test_a_check_that_clears_by_itself_reports_the_page_it_let_through():
    read, calls = reader(CHALLENGE, CHALLENGE, OK)
    page = Page()
    assert await past_automatic_check(page, CHALLENGE, read) is OK
    assert len(calls) == 3 and page.loads == 0


async def test_mid_way_to_the_page_it_waits_for_that_page_to_load():
    unknown = SignalReport(Signal.UNKNOWN, "nothing is loaded", "")
    read, calls = reader(unknown, OK)
    page = Page()
    assert await past_automatic_check(page, CHALLENGE, read) is OK
    assert page.loads == 1 and len(calls) == 2


async def test_a_check_that_wants_a_person_is_reported_after_the_wait():
    read, calls = reader(CHALLENGE)
    got = await past_automatic_check(Page(), CHALLENGE, read, wait_s=0.05)
    assert got.signal is Signal.CAPTCHA and got.detail == "Cloudflare challenge page"
    assert calls


async def test_a_closed_tab_stops_the_wait():
    read, calls = reader(CHALLENGE)
    page = Page()
    page.closed = True
    assert await past_automatic_check(page, CHALLENGE, read) is CHALLENGE
    assert calls == []
