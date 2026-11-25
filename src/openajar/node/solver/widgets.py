"""What the solver reads on a page, and the one thing it writes there.

Reads (Camoufox's isolated world; nothing on the page changes):

- the widget: its kind (Cloudflare Turnstile, reCAPTCHA v2, hCaptcha), its site key, Turnstile's action and cData,
  whether it is invisible, the name of the page's callback for it, whether it already holds a token. Only a widget
  in the page's own document: its element there (data-sitekey and friends) first, else the address of the vendor's
  frame directly in the page (reCAPTCHA's anchor carries `k=`, hCaptcha's frame `sitekey=`, Turnstile's frame its
  key in the path). A widget inside another site's frame is refused, and so is reCAPTCHA on a page that loads
  reCAPTCHA Enterprise (its script or its frame).
- Cloudflare's full-page check by its own markers (`full_page`).
- an image puzzle left open after the token went in (`close_puzzle`): reCAPTCHA keeps its puzzle (the bframe, 400 by
  580) over the page even once its field holds a token, and it then blocks the next click on the form. Its
  container's first child is a transparent fixed overlay across the viewport, and one click on that overlay closes
  the puzzle, as a person's click outside it does. Only a point where the document's own hit test answers that
  overlay is clicked, so the click cannot land on anything of the page's. hCaptcha's challenge is not handled:
  its markup was not seen with such an overlay.

The write: when the document is still at the address the token was asked for, the token goes into the widget's
response field (and reCAPTCHA's, which hCaptcha also fills), then the page's own callback for it is called when the page named one (data-callback, or reCAPTCHA's own client table),
the way the widget does when a person passes it. The page then sends the token with its form, and the site checks
it with the vendor: openajar cannot know the answer before that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

from openajar.node.human import quietly
from openajar.node.sites import host_is

Page = Any

FIELDS = {"turnstile": "cf-turnstile-response", "recaptcha_v2": "g-recaptcha-response",
          "hcaptcha": "h-captcha-response"}
_TURNSTILE_KEY = re.compile(r"/(0x[0-9A-Za-z_-]{16,}|[123]x0{20}[A-F]{2})(?:/|$)")
# The widgets in the page's own document, from their elements (light DOM), with whether each holds its token already,
# and whether the page loads reCAPTCHA Enterprise (its script, or its frame).
_WIDGETS_JS = """
() => {
  const kindOf = (el) => {
    const cls = String(el.className || '');
    if (/(^|\\s)cf-turnstile(\\s|$)/.test(cls) || el.querySelector('input[name="cf-turnstile-response"]')) return 'turnstile';
    if (/(^|\\s)h-captcha(\\s|$)/.test(cls) || el.querySelector('textarea[name="h-captcha-response"]')) return 'hcaptcha';
    if (/(^|\\s)g-recaptcha(\\s|$)/.test(cls) || el.querySelector('textarea[name="g-recaptcha-response"]')) return 'recaptcha_v2';
    return null;
  };
  const fields = {turnstile: 'cf-turnstile-response', recaptcha_v2: 'g-recaptcha-response', hcaptcha: 'h-captcha-response'};
  const out = [];
  for (const el of Array.from(document.querySelectorAll('[data-sitekey]')).slice(0, 10)) {
    const kind = kindOf(el);
    if (!kind) continue;
    const field = el.querySelector(`[name="${fields[kind]}"]`);
    const r = el.getBoundingClientRect();
    out.push({kind, key: el.getAttribute('data-sitekey') || '', action: el.getAttribute('data-action'),
              cdata: el.getAttribute('data-cdata'), size: el.getAttribute('data-size'),
              callback: el.getAttribute('data-callback'), passed: !!(field && field.value),
              shown: r.width > 0 && r.height > 0});
  }
  const enterprise = Array.from(document.querySelectorAll('script[src], iframe[src]')).slice(0, 500)
    .some((el) => /\\/recaptcha\\/enterprise(\\.js|\\/)/.test(el.getAttribute('src') || ''));
  return {widgets: out, enterprise};
}
"""

# Cloudflare's full-page check by its own markers, whatever the frames say.
_FULL_PAGE_JS = """() => !!(document.getElementById('challenge-form') || document.getElementById('challenge-stage') ||
  Array.from(document.scripts).slice(0, 200).some((s) =>
    /\\/cdn-cgi\\/challenge-platform\\/h\\/[^/]+\\/orchestrate\\//.test(s.getAttribute('src') || '')))"""

# reCAPTCHA's image puzzle when it still shows: whether one is open, and a point in the viewport where the hit test
# answers its container's fixed overlay (the click there closes it and reaches nothing of the page's).
_PUZZLE_JS = """
() => {
  const frames = Array.from(document.querySelectorAll('iframe')).slice(0, 200)
    .filter((f) => /\\/recaptcha\\/(api2|enterprise)\\/bframe/.test(f.getAttribute('src') || ''));
  for (const f of frames) {
    const box = f.parentElement && f.parentElement.parentElement;
    if (!box || getComputedStyle(box).visibility === 'hidden') continue;
    const r = f.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom <= 0 || r.right <= 0 || r.top >= innerHeight || r.left >= innerWidth) {
      continue;
    }
    const overlay = box.firstElementChild;
    if (!overlay || overlay.contains(f) || getComputedStyle(overlay).position !== 'fixed') return {open: true, point: null};
    const w = innerWidth, h = innerHeight;
    for (const [x, y] of [[4, 4], [w - 5, 4], [4, h - 5], [w - 5, h - 5], [w / 2, 4], [w / 2, h - 5], [4, h / 2],
                          [w - 5, h / 2]]) {
      if (document.elementFromPoint(x, y) === overlay) return {open: true, point: [x, y]};
    }
    return {open: true, point: null};
  }
  return {open: false};
}
"""
CLOSE_WAIT_S = 2.0

# The token into the widget's field(s), then the page's callback when it named one. Runs in the isolated world:
# the fields are the page's own DOM, and the callback is reached through the page's window (wrappedJSObject).
_HAND_JS = """
([kind, token, callback, expected]) => {
  // the address is read here, in the same step that writes the token: a page that moved since the guards ran gets none
  if (expected && location.href.split('#')[0] !== expected.split('#')[0]) return {filled: 0, called: 'none', moved: true};
  const names = {turnstile: ['cf-turnstile-response'], recaptcha_v2: ['g-recaptcha-response'],
                 hcaptcha: ['h-captcha-response', 'g-recaptcha-response']}[kind] || [];
  let filled = 0;
  for (const name of names) {
    for (const f of Array.from(document.getElementsByName(name)).slice(0, 10)) {
      if (f.value) continue;
      f.value = token; filled += 1;
      try { f.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {}
    }
  }
  const win = window.wrappedJSObject || window;
  const find = (path) => { let o = win; for (const p of String(path).split('.')) { if (o == null) return null; o = o[p]; }
    return typeof o === 'function' ? o : null; };
  let called = 'none';
  let fn = callback ? find(callback) : null;
  if (!fn && kind === 'recaptcha_v2') {
    // reCAPTCHA keeps a widget's callback in its own client table when the page passed a function, not a name
    const seen = new Set();
    const walk = (o, depth) => {
      if (fn || o == null || typeof o !== 'object' || depth > 4 || seen.has(o)) return;
      seen.add(o);
      for (const k of Object.keys(o).slice(0, 50)) {
        const v = o[k];
        if (k === 'callback') { if (typeof v === 'function') { fn = v; return; } if (typeof v === 'string') { fn = find(v); if (fn) return; } }
        walk(v, depth + 1);
      }
    };
    try { const cfg = win.___grecaptcha_cfg; if (cfg && cfg.clients) walk(cfg.clients, 0); } catch (e) {}
  }
  if (fn) {
    try { fn(token); called = 'called'; } catch (e) { called = 'threw'; }
  }
  return {filled, called};
}
"""


@dataclass(frozen=True, slots=True)
class Widget:
    kind: str                   # "turnstile", "recaptcha_v2" or "hcaptcha"
    site_key: str
    action: str | None = None
    cdata: str | None = None
    invisible: bool = False
    callback: str | None = None
    passed: bool = False


def from_frame_url(url: str) -> Widget | None:
    """A widget read from its frame's address, or None (reCAPTCHA Enterprise and invisible badges included)."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    host = parts.hostname or ""
    if (host_is(host, "google.com") or host_is(host, "recaptcha.net")) and re.match(r"^/recaptcha/api2/anchor\b",
                                                                                    parts.path):
        query = parse_qs(parts.query)
        key = (query.get("k") or [""])[0]
        return Widget("recaptcha_v2", key, invisible=(query.get("size") or [""])[0] == "invisible") if key else None
    if host_is(host, "hcaptcha.com"):
        frag = parse_qs(parts.fragment)
        if (frag.get("frame") or [""])[0] not in ("checkbox", "challenge"):
            return None
        key = (frag.get("sitekey") or [""])[0]
        return Widget("hcaptcha", key) if key else None
    if host_is(host, "challenges.cloudflare.com"):
        found = _TURNSTILE_KEY.search(parts.path)
        return Widget("turnstile", found.group(1)) if found else None
    return None


def _direct(page: Page, frame: Any) -> bool:
    """`frame` sits directly in the page's own document (its parent is the main frame)."""
    main = getattr(page, "main_frame", None)
    parent = getattr(frame, "parent_frame", None)
    return main is not None and parent is main


async def find(page: Page) -> tuple[Widget | None, str]:
    """The widget the solver would answer for on this page, and why there is none when there is none. Only a widget
    in the page's own document: its element there, or the vendor's frame directly in it. A widget inside another
    site's frame is refused (its key would be sent under this page's address, and its token could not reach it), and
    so is any reCAPTCHA on a page that loads reCAPTCHA Enterprise."""
    read = await quietly(page.evaluate(_WIDGETS_JS), {}, timeout=5.0) or {}
    read = read if isinstance(read, dict) else {}
    widgets = [Widget(w["kind"], str(w.get("key") or ""), w.get("action") or None, w.get("cdata") or None,
                      (w.get("size") or "") == "invisible", w.get("callback") or None, bool(w.get("passed")))
               for w in read.get("widgets") or [] if isinstance(w, dict) and w.get("kind") in FIELDS]
    frames = list(getattr(page, "frames", []))[1:]
    vendor = [(f, from_frame_url(getattr(f, "url", "") or "")) for f in frames]
    framed = [w for f, w in vendor if w is not None and _direct(page, f)]
    nested = [f for f, w in vendor if w is not None and not _direct(page, f)]
    enterprise = bool(read.get("enterprise")) or any("/recaptcha/enterprise" in (getattr(f, "url", "") or "")
                                                     for f in frames)
    found: Widget | None = None
    for w in widgets:
        if w.passed:
            continue
        if not w.site_key:
            match = next((f for f in framed if f.kind == w.kind), None)
            if match is None:
                continue
            w = Widget(w.kind, match.site_key, w.action, w.cdata, w.invisible or match.invisible, w.callback)
        found = w
        break
    if found is None and not (widgets and all(w.passed for w in widgets)) and framed:
        found = framed[0]
    if found is not None and found.kind == "recaptcha_v2" and enterprise:
        return None, "a reCAPTCHA Enterprise widget, which openajar does not send to a service"
    if found is not None:
        return found, ""
    if widgets and all(w.passed for w in widgets):
        return None, "the widget on this page already holds its token"
    if nested:
        host = urlsplit(getattr(nested[0], "url", "") or "").hostname or "another site"
        return None, (f"the widget ({host}) is inside a frame of another site, so its key would go under this page's "
                      "address and its token could not reach this page")
    if enterprise:
        return None, "a reCAPTCHA Enterprise widget, which openajar does not send to a service"
    return None, "no Turnstile, reCAPTCHA v2 or hCaptcha widget with a site key is on this page"


async def full_page(page: Page) -> bool:
    """Cloudflare's full-page check shows its own markers (its form, its stage, its orchestrate script)."""
    return bool(await quietly(page.evaluate(_FULL_PAGE_JS), False, timeout=5.0))


async def hand(page: Page, widget: Widget, token: str, expected_url: str = "") -> dict[str, Any]:
    """Put the token where the widget would, and call the page's callback when it named one: {filled, called}.
    With `expected_url`, only when the document is still at that address (`moved` when it is not)."""
    out = await quietly(page.evaluate(_HAND_JS, [widget.kind, token, widget.callback or "", expected_url]), None,
                        timeout=10.0)
    return out if isinstance(out, dict) else {"filled": 0, "called": "none"}


async def close_puzzle(page: Page) -> str:
    """Close reCAPTCHA's image puzzle when it is still open: "none" (no puzzle open), "closed", or "open" (no point
    where only its overlay would take the click, or it stayed open after the one click; nothing else is clicked)."""
    import asyncio

    from openajar.node.interaction import checks

    seen = await quietly(page.evaluate(_PUZZLE_JS), None, timeout=5.0)
    if not isinstance(seen, dict) or not seen.get("open"):
        return "none"
    point = seen.get("point")
    if not (isinstance(point, list) and len(point) == 2):
        return "open"
    await checks._page_click(page, (float(point[0]), float(point[1])))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CLOSE_WAIT_S
    while True:
        seen = await quietly(page.evaluate(_PUZZLE_JS), None, timeout=5.0)
        if isinstance(seen, dict) and not seen.get("open"):
            return "closed"
        if loop.time() >= deadline:
            return "open"
        await asyncio.sleep(0.2)
