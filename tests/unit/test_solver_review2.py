"""The second review of the solver (2026-11-25): each finding as a test that failed before its fix.

1. the page guard refuses a page it could not read in full (more frames than it reads, a frame that does not
   answer, the page script's own limits: test_solver_page_js has those in a real browser);
2. an assumed cost of 0 is not accepted, since such a solve would never reach the cap;
3. the record: a line whose write never finished spends nothing and is removed by the next write, a short write
   goes on to the end, a write that fails sends nothing;
4. the token goes only into the document at the address it was asked for, read in the step that writes it;
5. the record's lock is waited for a short time only;
and the address words the review listed."""

from __future__ import annotations

import errno
import fcntl
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_solver_review import KEY, URL, WIDGET, Ix, Page, entry, env_page, quick  # noqa: F401  (quick: a fixture)

from openajar.node.solver import ledger, services, settings, steps, widgets
from openajar.node.solver.settings import SolverEntry

BASE = ledger.Solve("2026-11-25T10:00:00+00:00", "env/x", "shop.example.com", "turnstile", "main", "command",
                    ledger.PENDING)


async def _clean(script, arg=None):
    return {"why": [], "cut": "", "action": ""}


def _frame(url, evaluate=_clean, detached=False):
    return SimpleNamespace(url=url, evaluate=evaluate, is_detached=lambda: detached)


# ---- 1. the guard and its limits ------------------------------------------------------------------------------

async def test_1_a_page_the_script_could_not_read_in_full_is_refused(monkeypatch):
    monkeypatch.undo()  # the real guard
    page = Page()

    async def main(script, arg=None):
        return {"why": [], "cut": "more elements than openajar reads", "action": ""}
    page.evaluate = main
    assert await widgets.sensitive(page, WIDGET) == [
        "the page could not be read in full (more elements than openajar reads)"]


async def test_1_a_page_with_more_frames_than_are_read_is_refused_and_its_last_frame_still_counts(monkeypatch):
    monkeypatch.undo()
    frames = [_frame(f"https://ads{i}.example.net/") for i in range(widgets.MAX_FRAMES + 5)]
    frames.append(_frame("https://js.stripe.com/v3/elements-inner-card.html"))
    page = Page(frames=frames)
    page.evaluate = _clean
    why = await widgets.sensitive(page, WIDGET)
    assert "a payment frame (js.stripe.com)" in why
    assert any("more frames" in w for w in why), why


async def test_1_a_frame_that_does_not_answer_is_not_clean_and_one_that_left_is(monkeypatch):
    monkeypatch.undo()

    async def broken(script, arg=None):
        raise RuntimeError("Execution context was destroyed")
    page = Page(frames=[_frame("https://id.example.org/", broken)])
    page.evaluate = _clean
    assert await widgets.sensitive(page, WIDGET) == ["a frame (id.example.org) could not be read to check it"]
    page = Page(frames=[_frame("https://ads.example.net/", broken, detached=True)])
    page.evaluate = _clean
    assert await widgets.sensitive(page, WIDGET) == []


@pytest.mark.parametrize("url", [
    "https://a.example.com/userLogin", "https://a.example.com/account/login;jsessionid=AB12CD",
    "https://a.example.com/login.action", "https://a.example.com/#/login", "https://a.example.com/app#!/signup?x=1",
    "https://a.example.com/customer/account/create/",
])
def test_1_address_words(url):
    assert widgets._path_words(url), url


def test_1_address_words_leave_ordinary_pages():
    for url in ("https://a.example.com/posts/create", "https://a.example.com/docs#installation",
                "https://a.example.com/account/settings", "https://a.example.com/blog/how-to-login-faster"):
        assert widgets._path_words(url) == [], url


# ---- 2. the assumed cost --------------------------------------------------------------------------------------

def test_2_an_assumed_cost_of_nothing_is_not_accepted():
    for cost in (0, 0.0001):
        with pytest.raises(ValidationError):
            SolverEntry(name="main", provider="capsolver", key=KEY, monthly_cap=1, sites=["example.com"],
                        assumed_cost=cost)
    assert SolverEntry(name="main", provider="capsolver", key=KEY, monthly_cap=1, sites=["example.com"],
                       assumed_cost=settings.MIN_ASSUMED_COST).assumed_cost == settings.MIN_ASSUMED_COST


# ---- 3. the record --------------------------------------------------------------------------------------------

def test_3_a_line_whose_write_never_finished_spends_nothing_and_goes_with_the_next_write(tmp_path):
    first = ledger.reserve(tmp_path, BASE, assumed=0.003, cap=1.0)
    path = ledger.record_path(tmp_path)
    whole = path.read_bytes()
    path.write_bytes(whole + b'{"at":"2026-11-25T10:00:01+00:00","browser":"env/x","site":"shop.exa')  # the power
    assert [s.id for s in ledger.read_all(tmp_path)] == [first.id]
    assert ledger.spent(tmp_path, "main", month="2026-11") == 0.003
    second = ledger.reserve(tmp_path, BASE, assumed=0.003, cap=1.0)
    assert path.read_bytes().startswith(whole) and path.read_bytes().count(b"\n") == 2
    assert [s.id for s in ledger.read_all(tmp_path)] == [first.id, second.id]
    path.write_bytes(b"\x00" * 40)  # nothing but what a crash left
    ledger.reserve(tmp_path, BASE, assumed=0.003, cap=1.0)
    assert len(ledger.read_all(tmp_path)) == 1


def test_3_a_short_write_goes_on_to_the_end(tmp_path, monkeypatch):
    real = os.write
    monkeypatch.setattr(ledger.os, "write", lambda fd, data: real(fd, data[:7]))
    pending = ledger.reserve(tmp_path, BASE, assumed=0.003, cap=1.0)
    monkeypatch.undo()
    assert [s.id for s in ledger.read_all(tmp_path)] == [pending.id]


def test_3_a_write_the_disk_cut_off_reserves_nothing(tmp_path, monkeypatch):
    real = os.write
    calls = []

    def full(fd, data):
        if calls:
            raise OSError(errno.ENOSPC, "No space left on device")
        calls.append(1)
        return real(fd, data[:20])
    monkeypatch.setattr(ledger.os, "write", full)
    with pytest.raises(ledger.LedgerError, match="cannot be written"):
        ledger.reserve(tmp_path, BASE, assumed=0.003, cap=1.0)
    monkeypatch.undo()
    assert ledger.read_all(tmp_path) == [] and ledger.spent(tmp_path, "main", month="2026-11") == 0
    ledger.reserve(tmp_path, BASE, assumed=0.003, cap=1.0)  # and the half line does not spoil the next one
    assert len(ledger.read_all(tmp_path)) == 1


# ---- 4. the token and the address -----------------------------------------------------------------------------

async def test_4_a_page_that_moved_between_the_guards_and_the_write_gets_no_token(tmp_path, monkeypatch):
    entry(tmp_path, cap=1.0)
    page = env_page()
    asked = []

    async def evaluate(script, arg=None):
        if script == widgets._HAND_JS:
            asked.append(arg[3])
            return {"filled": 0, "called": "none", "moved": True}  # the document's own address was another by then
        return False
    page.evaluate = evaluate

    async def answer(e, job):
        return services.Answer("tok", 0.001)
    monkeypatch.setattr(services, "solve", answer)
    out = await steps.get_past(Ix(), page, browser="env/x", home=tmp_path)
    assert asked == [URL] and out.ended_by == steps.HUMAN
    assert "the token was not used: the page moved on" in out.steps[-2]
    [line] = ledger.read(tmp_path)
    assert (line.result, line.cost) == (ledger.NOT_PASSED, 0.001)


# ---- 5. the lock ----------------------------------------------------------------------------------------------

def test_5_a_lock_nobody_lets_go_refuses_instead_of_hanging(tmp_path, monkeypatch):
    ledger.reserve(tmp_path, BASE, assumed=0.003, cap=1.0)
    monkeypatch.setattr(ledger, "LOCK_WAIT_S", 0.3)
    holder = os.open(tmp_path / ledger.LOCK_FILE, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(ledger.LedgerError, match="does not let it go"):
            ledger.reserve(tmp_path, BASE, assumed=0.003, cap=1.0)
    finally:
        os.close(holder)
    ledger.reserve(tmp_path, BASE, assumed=0.003, cap=1.0)
    assert len(ledger.read_all(tmp_path)) == 2
