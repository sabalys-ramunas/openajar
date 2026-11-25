"""What the solver reads on a page, and the one thing it writes there.

Reads (Camoufox's isolated world; nothing on the page changes):

- the widget: its kind (Cloudflare Turnstile, reCAPTCHA v2, hCaptcha), its site key, Turnstile's action and cData,
  whether it is invisible, the name of the page's callback for it, whether it already holds a token. From the
  widget's element (data-sitekey and friends) first, else from its frame's address (reCAPTCHA's anchor carries
  `k=`, hCaptcha's frame `sitekey=`, Turnstile's frame its key in the path). reCAPTCHA Enterprise is not taken.
- the page's kind: a sign-up, login or payment page is never solved (`sensitive`). The reasons are structural,
  never the page's words: a password, one-time-code or card field that shows (or any in the widget's own form),
  an autocomplete token for one, a payment provider's frame (Stripe, PayPal, Braintree, Adyen and others), or a
  word for it in the page's address or the form's action (login, signup, register, checkout, payment...). It
  errs on the side of refusing: a contact form next to a visible login box is refused too. It misses a sign-up
  made only of an email field on a page whose address says nothing, and a multi-step sign-up whose check comes
  before its password step: docs/ENVIRONMENTS.md says so.

The write: the token goes into the widget's response field (and reCAPTCHA's, which hCaptcha also fills), then the
page's own callback for it is called when the page named one (data-callback, or reCAPTCHA's own client table),
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
PAYMENT_HOSTS = ("js.stripe.com", "checkout.stripe.com", "hooks.stripe.com", "paypal.com", "paypalobjects.com",
                 "braintreegateway.com", "braintree-api.com", "adyen.com", "checkout.com", "pay.google.com",
                 "squareup.com", "squarecdn.com", "klarna.com", "recurly.com", "chargebee.com", "paddle.com",
                 "authorize.net", "worldpay.com", "mollie.com", "razorpay.com", "2checkout.com", "affirm.com")
# a word for a sign-up, login or payment step, as a whole part of the path (or the host's first label)
PAGE_WORDS = re.compile(
    r"^(?:log-?in|log-?on|sign-?in|sign-?on|sign-?up|register|registration|join|enrol+|create-?account|"
    r"new-?account|account-?create|auth|authenticate|oauth2?|sso|saml|password|reset-?password|forgot(?:-?password)?|"
    r"verify-?email|checkout|payment|payments|pay|billing|purchase|donate)$", re.IGNORECASE)
HOST_WORDS = frozenset({"login", "signin", "signup", "auth", "accounts", "account", "sso", "id", "checkout", "pay",
                        "payment", "payments", "billing", "secure"})

# The widgets on the page, from their elements (light DOM), with whether each holds its token already.
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
  return out;
}
"""

# Why this page is a sign-up, login or payment page (structural reads only), for the document it runs in.
_SENSITIVE_JS = """
(fields) => {
  const why = [];
  const shown = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  let form = null;
  for (const name of fields) { const f = document.getElementsByName(name)[0]; if (f && f.form) { form = f.form; break; } }
  const near = (el) => shown(el) || (form && el.form === form);
  const inputs = Array.from(document.querySelectorAll('input')).slice(0, 500);
  const ac = /(^|\\s)(current-password|new-password|one-time-code|cc-number|cc-csc|cc-exp|cc-exp-month|cc-exp-year)(\\s|$)/i;
  const card = /(card.?num|cardnumber|cc-?num|\\bcvv|\\bcvc|security.?code|card.?exp|expiry)/i;
  for (const el of inputs) {
    if (el.disabled || !near(el)) continue;
    if (el.type === 'password') { why.push('a password field'); break; }
  }
  for (const el of inputs) {
    if (el.disabled || !near(el)) continue;
    const auto = el.getAttribute('autocomplete') || '';
    if (ac.test(auto)) { why.push('a field for ' + auto.trim().split(/\\s+/).pop()); break; }
    if (card.test((el.name || '') + ' ' + (el.id || ''))) { why.push('a card field'); break; }
  }
  return {why, action: form ? (form.getAttribute('action') || '') : ''};
}
"""

# The token into the widget's field(s), then the page's callback when it named one. Runs in the isolated world:
# the fields are the page's own DOM, and the callback is reached through the page's window (wrappedJSObject).
_HAND_JS = """
([kind, token, callback]) => {
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


async def find(page: Page) -> tuple[Widget | None, str]:
    """The widget the solver would answer for on this page, and why there is none when there is none."""
    read = await quietly(page.evaluate(_WIDGETS_JS), [], timeout=5.0) or []
    widgets = [Widget(w["kind"], str(w.get("key") or ""), w.get("action") or None, w.get("cdata") or None,
                      (w.get("size") or "") == "invisible", w.get("callback") or None, bool(w.get("passed")))
               for w in read if isinstance(w, dict) and w.get("kind") in FIELDS]
    framed = [w for w in (from_frame_url(getattr(f, "url", "") or "") for f in list(getattr(page, "frames", []))[1:])
              if w is not None]
    enterprise = any("/recaptcha/enterprise/" in (getattr(f, "url", "") or "") for f in getattr(page, "frames", []))
    for w in widgets:
        if w.passed:
            continue
        if not w.site_key:
            match = next((f for f in framed if f.kind == w.kind), None)
            if match is None:
                continue
            w = Widget(w.kind, match.site_key, w.action, w.cdata, w.invisible or match.invisible, w.callback)
        return w, ""
    if widgets and all(w.passed for w in widgets):
        return None, "the widget on this page already holds its token"
    if framed:
        return framed[0], ""
    if enterprise:
        return None, "a reCAPTCHA Enterprise widget, which openajar does not send to a service"
    return None, "no Turnstile, reCAPTCHA v2 or hCaptcha widget with a site key is on this page"


def _path_words(url: str) -> list[str]:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return []
    words = [p for p in re.split(r"[/._]", parts.path or "") if p and PAGE_WORDS.match(p)]
    first = (parts.hostname or "").split(".")[0]
    if first in HOST_WORDS and (parts.hostname or "").count(".") >= 2:
        words.append(f"{first}.")
    return words


async def sensitive(page: Page, widget: Widget | None) -> list[str]:
    """Why this is a sign-up, login or payment page (empty: none of the signs). Errs on the side of refusing."""
    why: list[str] = []
    url = getattr(page, "url", "") or ""
    words = _path_words(url)
    if words:
        why.append(f"its address says {words[0]!r}")
    fields = [FIELDS[widget.kind]] if widget is not None else list(FIELDS.values())
    main = await quietly(page.evaluate(_SENSITIVE_JS, fields), None, timeout=5.0)
    if isinstance(main, dict):
        why += [str(w) for w in main.get("why") or []]
        action = str(main.get("action") or "")
        if action and _path_words(action):
            why.append(f"its form goes to {_path_words(action)[0]!r}")
    elif main is None:
        why.append("the page could not be read to check it is not a sign-up, login or payment page")
    for frame in list(getattr(page, "frames", []))[1:20]:
        host = urlsplit(getattr(frame, "url", "") or "").hostname or ""
        if any(host_is(host, h) for h in PAYMENT_HOSTS):
            why.append(f"a payment frame ({host})")
            break
    for frame in list(getattr(page, "frames", []))[1:10]:
        host = urlsplit(getattr(frame, "url", "") or "").hostname or ""
        if not host or from_frame_url(frame.url) is not None or any(host_is(host, h) for h in
                                                                     ("google.com", "recaptcha.net", "hcaptcha.com",
                                                                      "challenges.cloudflare.com")):
            continue
        inner = await quietly(frame.evaluate(_SENSITIVE_JS, []), None, timeout=3.0)
        if isinstance(inner, dict) and inner.get("why"):
            why.append(f"{inner['why'][0]} in a frame ({host})")
            break
    return list(dict.fromkeys(why))


async def hand(page: Page, widget: Widget, token: str) -> dict[str, Any]:
    """Put the token where the widget would, and call the page's callback when it named one: {filled, called}."""
    out = await quietly(page.evaluate(_HAND_JS, [widget.kind, token, widget.callback or ""]), None, timeout=10.0)
    return out if isinstance(out, dict) else {"filled": 0, "called": "none"}
