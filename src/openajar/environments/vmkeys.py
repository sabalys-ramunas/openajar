"""Real key presses for letters the US layout has no key for, on an environment's browser on its machine
(browser = "vm"): what openajar.node.xkeyboard does for a lane on the node.

Playwright knows only the US layout, so it inserts ą, é or ж as text with no keydown at all, which no keyboard
does (the red team's `kbd.keyless_text`, caught on every visit of the machine's browser). So the machine's
screen gets the keyboard layout of the browser's language (the exit country's by default, browserproxy) and
those letters go through the X server: a helper of the control script's (vmbrowser_ctl KEYS) that the
browser's launch starts and its stop ends, sending XTest key events. It tells the node which characters the
layout types; the node keeps one ssh session to it open (a relay to its socket), so a letter costs one round
trip, not a command. Everything else stays on Playwright, whose keys do not read the X layout.

A machine where the helper cannot start (no libXtst, no setxkbmap, a language with no layout here) answers
with an error or no characters, and those letters go in as text, as before. A helper that stops answering is
not asked again on that launch (human.type_text then types the rest as text).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from typing import Any

log = logging.getLogger("openajar.environments")

# A language (the browser's first one) -> the X keyboard layout (xkeyboard-config) a person there types on.
LAYOUTS = {
    "lt": "lt", "lv": "lv", "et": "ee", "pl": "pl", "cs": "cz", "sk": "sk", "hu": "hu", "ro": "ro", "hr": "hr",
    "sl": "si", "bs": "ba", "sr": "rs", "de": "de", "fr": "fr", "es": "es", "it": "it", "pt": "pt", "ca": "es",
    "nl": "nl", "da": "dk", "sv": "se", "nb": "no", "nn": "no", "no": "no", "fi": "fi", "is": "is", "tr": "tr",
    "el": "gr", "ru": "ru", "uk": "ua", "be": "by", "bg": "bg", "mk": "mk",
}
REGION_LAYOUTS = {"pt-BR": "br", "fr-CA": "ca", "de-CH": "ch", "fr-CH": "ch", "de-AT": "at", "fr-BE": "be",
                  "nl-BE": "be", "es-419": "latam", "es-MX": "latam", "es-AR": "latam"}
RELAY = ("import socket,sys\ns=socket.socket(socket.AF_UNIX)\ns.connect(sys.argv[1])\nf=s.makefile('rwb',buffering=0)\n"
         "for line in sys.stdin.buffer:\n  f.write(line)\n  sys.stdout.buffer.write(f.readline() or b'no\\n')\n"
         "  sys.stdout.flush()\n")
PRESS_TIMEOUT_S = 5.0
_BY_ENV: dict[str, VmKeys] = {}


def layout_for(locale: Any) -> str | None:
    """The layout for the browser's first language ("lt-LT", ["lt-LT", "lt", "en-US"], "lt-LT,lt"), or None
    (English, or a language with no layout here: the US layout stays)."""
    first = locale[0] if isinstance(locale, list | tuple) and locale else locale
    tag = str(first or "").split(",")[0].strip().replace("_", "-")
    if not tag:
        return None
    lang = tag.split("-")[0].lower()
    region = f"{lang}-{tag.split('-')[1].upper()}" if "-" in tag else ""
    return REGION_LAYOUTS.get(region) or LAYOUTS.get(lang)


def request(options: dict[str, Any], lane_locale: str | None) -> dict[str, Any]:
    """What the control script's browser action gets for the helper: the layout of the launch's language
    (options["locale"] from the exit, else the lane's own), or nothing."""
    layout = layout_for(options.get("locale") or lane_locale)
    return {"keys": {"layout": layout}} if layout else {}


class VmKeys:
    """xkeyboard.XKeyboard's interface (has, press) over a relay to the machine's helper."""

    def __init__(self, chars: str, proc: Any):
        self.chars, self.proc = set(chars), proc
        self.broken = False
        self._lock = asyncio.Lock()

    def has(self, ch: str) -> bool:
        return not self.broken and ch in self.chars

    async def press(self, ch: str, hold_ms: float) -> bool:
        """Press and release the key for `ch` on the machine, held `hold_ms`. False when it could not be sent;
        the caller then inserts the letter, and this keyboard is not tried again."""
        async with self._lock:
            if self.broken:
                return False
            answer = b""
            try:
                self.proc.stdin.write(f"{ord(ch)} {hold_ms:.0f}\n".encode())
                await self.proc.stdin.drain()
                answer = await asyncio.wait_for(self.proc.stdout.readline(), PRESS_TIMEOUT_S + hold_ms / 1000)
            except (OSError, TimeoutError, ValueError, AttributeError):
                pass
            if answer.strip() != b"ok":
                self.broken = True
                log.warning("the machine's keyboard helper did not press a key (%s); letters go in as text from now on",
                            answer.strip().decode(errors="replace") or "no answer")
                return False
            return True

    async def close(self) -> None:
        with contextlib.suppress(ProcessLookupError, OSError):
            self.proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.proc.wait(), 5)


async def attach(env_id: str, tunnel: Any, context: Any, started: Any) -> VmKeys | None:
    """After the browser started: when the helper runs (`started["keys"]`, the control script's answer), open
    the relay to it and make it the lane's X keyboard. Never raises: without it letters go in as text."""
    from openajar.node import human
    from openajar.vms.ssh import ssh_base

    await detach(env_id)
    keys = started.get("keys") if isinstance(started, dict) else None
    if not isinstance(keys, dict):
        return None
    if keys.get("error") or not keys.get("chars") or not keys.get("socket"):
        log.info("environment %s: the machine's keyboard helper is not running (%s); letters the US layout lacks "
                 "go in as text", env_id, str(keys.get("error") or "the layout types no such letter")[-300:])
        return None
    try:
        command = shlex.join(["/usr/bin/python3", "-I", "-c", RELAY, str(keys["socket"])])
        proc = await asyncio.create_subprocess_exec(*ssh_base(tunnel.remote), command, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except (OSError, AttributeError) as e:
        log.warning("environment %s: no relay to the machine's keyboard helper (%s); letters go in as text", env_id, e)
        return None
    kb = VmKeys(str(keys["chars"]), proc)
    _BY_ENV[env_id] = kb
    human.X_KEYBOARDS[context] = kb
    return kb


async def detach(env_id: str) -> None:
    """The browser stops: its relay goes (the helper stops with the browser on the machine)."""
    kb = _BY_ENV.pop(env_id, None)
    if kb is not None:
        await kb.close()
