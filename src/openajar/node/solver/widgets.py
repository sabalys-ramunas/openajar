"""What the solver reads on a page, and the one thing it writes there.

Reads (Camoufox's isolated world; nothing on the page changes):

- the widget: its kind (Cloudflare Turnstile, reCAPTCHA v2, hCaptcha), its site key, Turnstile's action and cData,
  whether it is invisible, the name of the page's callback for it, whether it already holds a token. Only a widget
  in the page's own document: its element there (data-sitekey and friends) first, else the address of the vendor's
  frame directly in the page (reCAPTCHA's anchor carries `k=`, hCaptcha's frame `sitekey=`, Turnstile's frame its
  key in the path). A widget inside another site's frame is refused, and so is reCAPTCHA on a page that loads
  reCAPTCHA Enterprise (its script or its frame).
- Cloudflare's full-page check by its own markers (`full_page`).
- the page's kind: a sign-up, login or payment page is never solved (`sensitive`). The reasons are structural,
  never the page's words, read in the document, its open shadow roots and every frame: a password field (a
  disabled one too), a one-time-code, username (a passwordless login) or card field that shows, or any such field
  in the widget's own form; an autocomplete token for one; a payment provider's frame (Stripe, PayPal, Braintree,
  Adyen, Shopify and others); a word for it in the page's address, its query or the form's action (login,
  sign_in, wp-login, register, session, checkout, my-account, payment...). It errs on the side of refusing: a
  contact form next to a visible login box is refused too. It misses a sign-up made only of an email field with
  no autocomplete="username" on a page whose address says nothing, a form in a closed shadow root, and a sign-up
  whose check comes before any of its fields show: docs/ENVIRONMENTS.md says so.

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
                 "authorize.net", "worldpay.com", "mollie.com", "razorpay.com", "2checkout.com", "affirm.com",
                 "shopifycs.com", "checkout.shopify.com", "shop.app", "afterpay.com", "clearpay.co.uk")
# A word for a sign-up, login or payment step: a whole part of the address's path or of a query value
# ("/users/sign_in", "/wp-login.php", "?route=account/register"), or the first or last word of such a part
# ("/user-login", "/signin-oidc"), or the host's first label.
PAGE_WORDS = re.compile(
    r"^(?:log-?in|log-?on|sign-?in|sign-?on|sign-?up|register|registration|join|enrol+|create-?account|"
    r"new-?account|account-?create|my-?account|auth|authenticate|authorize|oauth2?|oidc|sso|saml|session|sessions|"
    r"password|reset-?password|forgot(?:-?password)?|verify-?email|checkout|checkouts|payment|payments|pay|billing|"
    r"purchase|donate)$", re.IGNORECASE)
_EXTENSION = re.compile(r"\.(?:php|html?|aspx?|jsp|cgi|do)$", re.IGNORECASE)
MAX_FRAMES = 50
HOST_WORDS = frozenset({"login", "signin", "signup", "auth", "accounts", "account", "sso", "id", "checkout", "pay",
                        "payment", "payments", "billing", "secure"})

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

# Why this page is a sign-up, login or payment page (structural reads only), for the document it runs in.
_SENSITIVE_JS = """
(fields) => {
  const why = [];
  const shown = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  let form = null;
  for (const name of fields) { const f = document.getElementsByName(name)[0]; if (f && f.form) { form = f.form; break; } }
  const near = (el) => shown(el) || (form && el.form === form);
  // the inputs of the document and of its open shadow roots (a login form in a web component is one too)
  const inputsIn = (root, depth) => {
    let out = Array.from(root.querySelectorAll('input'));
    if (depth < 5) {
      for (const el of Array.from(root.querySelectorAll('*')).slice(0, 5000)) {
        if (el.shadowRoot) out = out.concat(inputsIn(el.shadowRoot, depth + 1));
      }
    }
    return out;
  };
  const inputs = inputsIn(document, 0).slice(0, 1000);
  const ac = /(^|\\s)(current-password|new-password|one-time-code|username|webauthn|cc-number|cc-csc|cc-exp|cc-exp-month|cc-exp-year)(\\s|$)/i;
  const card = /(card.?num|cardnumber|cc-?num|\\bcvv|\\bcvc|security.?code|card.?exp|expiry)/i;
  for (const el of inputs) {  // a disabled one too: a multi-step login shows its password field disabled first
    if (!near(el)) continue;
    if (el.type === 'password') { why.push('a password field'); break; }
  }
  for (const el of inputs) {
    if (!near(el)) continue;
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
    frames = list(getattr(page, "frames", []))[1:MAX_FRAMES]
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


def _word(part: str) -> str | None:
    """`part` (one piece of a path or a query value) when it or its first or last word names such a step."""
    part = _EXTENSION.sub("", part.strip().lower()).replace("_", "-")
    if not part:
        return None
    words = [w for w in part.split("-") if w]
    if PAGE_WORDS.match(part) or (words and (PAGE_WORDS.match(words[0]) or PAGE_WORDS.match(words[-1]))):
        return part
    return None


def _path_words(url: str) -> list[str]:
    try:
        parts = urlsplit(url or "")
        query = parse_qs(parts.query, keep_blank_values=True)
    except ValueError:
        return []
    pieces = (parts.path or "").split("/")
    for key, values in query.items():
        pieces += [p for v in values for p in v.split("/")] + ([key] if values == [""] else [])
    words = [w for w in (_word(p) for p in pieces) if w]
    host = (parts.hostname or "")
    first = host.split(".")[0]
    if first in HOST_WORDS and host.count(".") >= 2:
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
    for frame in list(getattr(page, "frames", []))[1:MAX_FRAMES]:
        host = urlsplit(getattr(frame, "url", "") or "").hostname or ""
        if any(host_is(host, h) for h in PAYMENT_HOSTS):
            why.append(f"a payment frame ({host})")
            break
    for frame in list(getattr(page, "frames", []))[1:MAX_FRAMES]:
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
