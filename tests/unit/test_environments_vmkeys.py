"""Real key presses on an environment's machine (openajar.environments.vmkeys): the layout follows the
browser's language, the helper's answer decides what is typed as a key, and a helper that fails is not asked
again. The helper itself runs on the machine: test_environments_vmbrowser_docker.py."""

from __future__ import annotations

import asyncio
import types

import pytest

from openajar.environments import vmkeys
from openajar.node import human


@pytest.mark.parametrize(("locale", "layout"), [
    (["lt-LT", "lt", "en-US", "en"], "lt"),
    ("de-DE", "de"), ("pt-BR", "br"), ("pt-PT", "pt"), ("fr-CA", "ca"), ("cs", "cz"), ("et-EE", "ee"),
    ("en-US", None), (["en-GB", "en"], None), ("ja-JP", None), ("", None), (None, None),
])
def test_the_layout_follows_the_browsers_first_language(locale, layout):
    assert vmkeys.layout_for(locale) == layout


def test_the_launch_asks_for_the_helper_with_its_layout_or_the_us_one():
    assert vmkeys.request({"locale": ["lt-LT", "lt", "en-US", "en"]}, "en-US") == {"keys": {"layout": "lt"}}
    assert vmkeys.request({}, "de-DE") == {"keys": {"layout": "de"}}  # no exit language: the lane's own
    assert vmkeys.request({}, "en-US") == {"keys": {"layout": None}}  # the US layout, and the hand


class _Pipe:
    def __init__(self, answers: list[bytes]):
        self.answers, self.sent = answers, []

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        pass

    async def readline(self) -> bytes:
        return self.answers.pop(0) if self.answers else b""


def _proc(answers: list[bytes]):
    pipe = _Pipe(answers)
    return types.SimpleNamespace(stdin=pipe, stdout=pipe, terminate=lambda: None, wait=lambda: asyncio.sleep(0))


async def test_a_letter_is_one_line_to_the_helper_and_a_failure_stops_asking():
    proc = _proc([b"ok\n", b"no\n"])
    kb = vmkeys.VmKeys("ąčęėįšųūž", proc)
    assert kb.has("ą") and not kb.has("a") and not kb.has("é")
    assert await kb.press("ą", 92.4) is True
    assert proc.stdin.sent == [f"{ord('ą')} 92\n".encode()]
    assert await kb.press("č", 80) is False  # the helper could not: the caller inserts it
    assert kb.broken and not kb.has("ą") and await kb.press("ą", 80) is False
    assert len(proc.stdin.sent) == 2  # never asked again


async def test_no_helper_on_the_machine_leaves_the_letters_as_text():
    context = object.__new__(type("Ctx", (), {}))
    for started in ({}, {"keys": {"error": "libXtst.so.6: cannot open shared object file"}},
                    {"keys": {"socket": "/run/x", "chars": ""}}, None):
        assert await vmkeys.attach("e1", types.SimpleNamespace(remote=None), context, started) is None
    assert context not in human.X_KEYBOARDS


async def test_the_hand_glides_and_presses_on_the_machine_in_one_line_each():
    proc = _proc([b"ok\n", b"ok\n", b"no\n"])
    kb = vmkeys.VmKeys("", proc)  # an English browser: no letters to type, a hand all the same
    assert not kb.has("a")
    assert await kb.glide((100.4, 200.6), (831, 584), 412.7) is True
    assert await kb.press_button(96.2) is True
    assert proc.stdin.sent == [b"glide 100 201 831 584 413\n", b"press 96\n"]
    assert await kb.glide((831, 584), (835, 580), 120) is False  # the machine could not: the caller falls back


async def test_every_environment_browser_on_its_machine_may_click_a_check():
    from openajar.node.interaction import checks

    context = object.__new__(type("Ctx", (), {}))
    assert not checks.allowed(types.SimpleNamespace(context=context))
    assert await vmkeys.attach("e1", types.SimpleNamespace(remote=None), context, {}) is None
    assert checks.allowed(types.SimpleNamespace(context=context))  # with or without the helper
