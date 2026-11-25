"""Sites openajar knows, and how a page's state is read from its structure (AUD-06, AUD-12).

Everything here is plain data plus pure functions, so every rule is unit tested without a browser.
interaction.py collects the facts (`PageFacts`) and `classify_page` turns them into a `SignalReport`.

The facts are structural only:
- the page URL and the main document's HTTP status;
- the iframes on the page (URL and whether they render at a real size, on the page) and its script URLs;
- whether a few marker elements exist (by id, never by text);
- which challenge widgets hold their token (whether the field is filled, never the token itself);
- the cookies the browser would send to the URL, read with Playwright's `context.cookies(url)`, which
  the page cannot see.
Page text is never an input. Accounts are run in many languages and the account's language beats the
browser locale, so a rule that read text would break or lie.

Signals (core.models.Signal), first match wins:
1. unknown       nothing loaded (an unreadable page with no status is unknown too, after 3 to 5).
2. captcha       a vendor puzzle that is actually challenging: a challenge frame that renders at a real
   blocked       size, or a challenge page marker. A vendor script that is merely loaded (bot sensors,
                 invisible reCAPTCHA, an Arkose pre-check) never counts, nor a widget that was passed
                 (Turnstile, hCaptcha or reCAPTCHA with its token in its form field: it stays on the
                 page as "Success!"). DataDome's "blocked visitor" frame is `blocked`.
3. per site URL  checkpoint (the site asks the account holder a question), captcha, blocked or
                 rate_limited pages by URL; and login pages (logged_out, see 5).
4. status        site statuses (LinkedIn's 999), then 429 rate_limited and 403 blocked.
5. logged_out    the lane is expected to be signed in here and the site's session cookie is missing,
                 or the page is the site's login URL.
6. ok            anything else that loaded.

A lane is expected to be signed in on a site when its `LaneSpec.account` names that site, or (no
account set) when a dash separated word of the lane name is the site key or an alias
(`biz-a/instagram`, `acme/ig`, `personal/reddit`). See `expectation_for`.

Account identity comes from cookies the site sets for the signed-in user (`c_user`, `ds_user_id`, ...)
and never costs a request. `check_account` compares it with the lane's pinned `AccountRef`:
`match`, `wrong_account`, or `unpinned` (no user id pinned yet; the seen id is reported so the owner can
pin it).

To add a site: append a `Site` to SITES with its hosts, session cookies, identity cookie and URL rules,
and add a row per rule to tests/unit/test_sites.py. Unknown sites fall back to the vendor and status
rules.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from urllib.parse import unquote, urlsplit

from openajar.core.models import AccountRef, LaneSpec, Signal

WRONG_ACCOUNT = "wrong_account"   # an AccountCheck status, and a hold reason (holds.py)
MIN_VISIBLE_PX = 30               # a challenge frame or marker smaller than this is not shown to anyone

Cookies = Sequence[Mapping[str, Any]]   # what context.cookies() returns: name, value, domain, path, ...


@lru_cache(maxsize=512)
def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def host_is(host: str, domain: str) -> bool:
    """`host` is `domain` or one of its subdomains (never a look-alike such as evilinstagram.com)."""
    host, domain = host.lower().rstrip("."), domain.lower()
    return host == domain or host.endswith("." + domain)


def _target(url: str) -> tuple[str, str]:
    """(host, path plus "?query" plus "#fragment") of a URL; empty strings when it is not http(s)."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return "", ""
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return "", ""
    rest = parts.path or "/"
    if parts.query:
        rest += "?" + parts.query
    if parts.fragment:
        rest += "#" + parts.fragment
    return parts.hostname.lower(), rest


# ---- the table types -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UrlRule:
    """A URL that means something on one site. `pattern` is a regular expression searched (case
    insensitive) in the path plus "?query", so anchor it with ^ to match from the path's start.

    `signal` is CHECKPOINT, CAPTCHA, BLOCKED or RATE_LIMITED for a page that always means that, or
    LOGGED_OUT for a login page: that one counts only while the lane is expected to be signed in here.
    `signed_in` is the signal a login page means while the session cookie is still there (Google's
    "Verify it's you" asks a signed-in user for the password again: a checkpoint)."""
    signal: Signal
    pattern: str
    label: str
    host: str | None = None            # only on this host and its subdomains
    signed_in: Signal | None = None

    def matches(self, host: str, rest: str) -> bool:
        if self.host is not None and not host_is(host, self.host):
            return False
        return _rx(self.pattern).search(rest) is not None


@dataclass(frozen=True, slots=True)
class Cookie:
    """A cookie the site sets only for a signed-in browser. A name ending in `*` is a prefix
    (`wordpress_logged_in_<hash>`). A value starting with one of `not_prefixes` does not count (Medium
    gives signed-out visitors a `uid` of `lo_...`)."""
    name: str
    not_prefixes: tuple[str, ...] = ()

    def find(self, cookies: Cookies) -> Mapping[str, Any] | None:
        prefix = self.name.endswith("*")
        stem = self.name.rstrip("*")
        for c in cookies:
            name, value = str(c.get("name", "")), str(c.get("value", ""))
            if (name.startswith(stem) if prefix else name == stem) and value \
                    and not value.startswith(self.not_prefixes):
                return c
        return None


@dataclass(frozen=True, slots=True)
class IdentityCookie:
    """Where a site's stable user id sits: a cookie and how to read the id out of its value."""
    cookie: Cookie
    parse: Callable[[str], str | None]


@dataclass(frozen=True, slots=True)
class Site:
    key: str                              # "instagram"; AccountRef.site uses it
    name: str                             # "Instagram"
    hosts: tuple[str, ...]                # registrable domains; subdomains match too
    session: tuple[Cookie, ...]           # all present while signed in
    identity: IdentityCookie | None       # None: the id is not visible without a request
    urls: tuple[UrlRule, ...] = ()        # first match wins
    aliases: tuple[str, ...] = ()         # lane-name words that mean this site
    paths: str | None = None              # sites without fixed hosts (WordPress): a path regex that marks them
    home: str | None = None               # where account_identity reads cookies when given no URL
    statuses: tuple[tuple[int, Signal], ...] = ()   # site-specific HTTP statuses
    note: str = ""                        # what is not covered, for the next person extending it
    login: str | None = None              # the sign-in page an assisted login opens (openajar.node.login_assist)
    approve: tuple[str, ...] = ()         # path regexes of "approve the sign-in on your phone" pages

    def asks_approval(self, url: str) -> bool:
        """The page waits for the account holder to approve the sign-in on another device."""
        host, rest = _target(url)
        return bool(host) and any(host_is(host, h) for h in self.hosts) and any(
            _rx(p).search(rest) for p in self.approve)

    def missing_session(self, cookies: Cookies) -> str | None:
        """The first session cookie that is missing, or None when the browser is signed in."""
        for c in self.session:
            if c.find(cookies) is None:
                return c.name
        return None


@dataclass(frozen=True, slots=True)
class VendorRule:
    """A challenge vendor's frame, script or marker element.

    `signal` is CAPTCHA or BLOCKED when the resource means the page is challenging right now, or None
    for a passive one (a sensor or an API script that loads on normal pages too). Rules are tried in
    order and the first that matches a resource decides it, so passive exceptions come first.
    Frames and elements with `visible` count only when they render at least MIN_VISIBLE_PX square,
    on the page, not hidden. `host` None matches any host (first-party challenge paths)."""
    vendor: str
    kind: str                       # "frame", "script" or "element"
    signal: Signal | None
    host: str | None = None
    pattern: str = ""               # searched in path?query#fragment (hCaptcha names its frames in the fragment)
    selector: str | None = None     # elements: a CSS selector by id, never by text
    visible: bool = True
    label: str = ""
    # a widget's frame: the form field the widget writes its token into once it is passed (Turnstile,
    # hCaptcha, reCAPTCHA). A passed widget stays on the page ("Success!") and is no longer a challenge.
    passed_field: str = ""

    @property
    def detail(self) -> str:
        return self.label or f"{self.vendor} {self.kind}"

    def matches_url(self, url: str) -> bool:
        host, rest = _target(url)
        if not host or (self.host is not None and not host_is(host, self.host)):
            return False
        return not self.pattern or _rx(self.pattern).search(rest) is not None


# ---- identity parsers ----------------------------------------------------------------------------

def _plain(value: str) -> str | None:
    value = unquote(value).strip().strip('"')
    return value or None


def _twid(value: str) -> str | None:
    """X: `twid=u%3D1234567890` -> 1234567890."""
    value = unquote(value).strip().strip('"')
    if not value.startswith("u="):
        return None
    return value.removeprefix("u=") or None


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if n == 0:
            return out


def _reddit_user(value: str) -> str | None:
    """Reddit: `reddit_session` is a JWT whose `sub` is the account id (t2_...); the older form was
    `<decimal id>,<timestamp>,<hash>`. Read locally; nothing is verified or sent anywhere."""
    value = unquote(value).strip()
    parts = value.split(".")
    if len(parts) == 3:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub")
        except (binascii.Error, ValueError, AttributeError, UnicodeDecodeError):
            return None
        return sub if isinstance(sub, str) and sub.startswith("t2_") else None
    first = value.split(",", 1)[0]
    ok = first.isascii() and first.isdigit()   # str.isdigit() alone accepts "²", which int() refuses
    return f"t2_{_base36(int(first))}" if ok and "," in value else None


def _wordpress_user(value: str) -> str | None:
    """WordPress: `wordpress_logged_in_<hash>` is `user|expiry|token|hmac`; the login name is the id."""
    value = unquote(value)
    if "|" not in value:
        return None
    return value.split("|", 1)[0] or None


# ---- the table -----------------------------------------------------------------------------------

L, C, B, R, A = Signal.LOGGED_OUT, Signal.CHECKPOINT, Signal.BLOCKED, Signal.RATE_LIMITED, Signal.CAPTCHA

SITES: tuple[Site, ...] = (
    Site(
        key="facebook", name="Facebook", hosts=("facebook.com", "fb.com"), aliases=("fb", "meta"),
        session=(Cookie("c_user"), Cookie("xs")),
        identity=IdentityCookie(Cookie("c_user"), _plain),
        home="https://www.facebook.com/",
        urls=(
            UrlRule(C, r"^/checkpoint(?:/|$|\?)", "checkpoint URL"),
            UrlRule(C, r"^/two_step_verification/", "two-step verification URL"),
            UrlRule(C, r"^/auth_platform/", "login approval URL"),
            UrlRule(L, r"^/login(?:\.php|/|$|\?)", "login URL"),
            UrlRule(L, r"^/recover/", "account recovery URL"),
        ),
        note="Temporary feature blocks show as an overlay with no URL of their own, so they are not seen.",
        login="https://www.facebook.com/login/",
        approve=(r"^/auth_platform/",),
    ),
    Site(
        key="instagram", name="Instagram", hosts=("instagram.com",), aliases=("ig", "insta"),
        session=(Cookie("sessionid"),),
        identity=IdentityCookie(Cookie("ds_user_id"), _plain),
        home="https://www.instagram.com/",
        urls=(
            UrlRule(C, r"^/challenge(?:/|$|\?)", "challenge URL"),
            UrlRule(C, r"^/accounts/login/two_factor", "two-factor URL"),
            UrlRule(C, r"^/accounts/(?:suspended|disabled)(?:/|$|\?)", "suspended account URL"),
            UrlRule(L, r"^/accounts/login(?:/|$|\?)", "login URL"),
        ),
        note="'Try again later' limits are text in a dialog with no URL or status, so they are not seen.",
        login="https://www.instagram.com/accounts/login/",
    ),
    Site(
        key="linkedin", name="LinkedIn", hosts=("linkedin.com",), aliases=("li",),
        session=(Cookie("li_at"),),
        identity=None,
        home="https://www.linkedin.com/",
        statuses=((999, B),),
        urls=(
            UrlRule(L, r"^/checkpoint/(?:lg|rm)/", "login URL"),
            UrlRule(C, r"^/checkpoint/challenge", "checkpoint challenge URL"),
            UrlRule(C, r"^/checkpoint/", "checkpoint URL"),
            UrlRule(L, r"^/(?:login|uas/login|authwall)(?:/|$|\?)", "login URL"),
        ),
        note="li_at is opaque and no cookie carries the member id; the id needs /voyager/api/me (a request).",
        login="https://www.linkedin.com/login",
    ),
    Site(
        key="reddit", name="Reddit", hosts=("reddit.com",),
        session=(Cookie("reddit_session"),),
        identity=IdentityCookie(Cookie("reddit_session"), _reddit_user),
        home="https://www.reddit.com/",
        urls=(
            UrlRule(L, r"^/(?:login|account/login)(?:/|$|\?)", "login URL"),
        ),
        note="No checkpoint URL is known; blocks and rate limits arrive as 403 and 429.",
        login="https://www.reddit.com/login/",
    ),
    Site(
        key="medium", name="Medium", hosts=("medium.com",),
        session=(Cookie("sid"), Cookie("uid", not_prefixes=("lo_",))),
        identity=IdentityCookie(Cookie("uid", not_prefixes=("lo_",)), _plain),
        home="https://medium.com/",
        urls=(
            UrlRule(L, r"^/m/signin", "sign-in URL"),
        ),
        note="Publications on their own domains do not get medium.com cookies, so they are generic pages.",
        login="https://medium.com/m/signin",
    ),
    Site(
        key="wordpress", name="WordPress", hosts=("wordpress.com",), aliases=("wp", "blog"),
        paths=r"^/wp-(?:admin(?:/|$|\?)|login\.php)",
        session=(Cookie("wordpress_logged_in*"),),
        identity=IdentityCookie(Cookie("wordpress_logged_in*"), _wordpress_user),
        home=None,
        urls=(
            UrlRule(C, r"^/wp-login\.php\?(?:.*&)?action=(?:validate_2fa|backup_2fa|revalidate_2fa)\b",
                    "two-factor URL"),
            UrlRule(L, r"^/wp-login\.php", "login URL"),
            UrlRule(L, r"^/log-in(?:/|$|\?)", "login URL", host="wordpress.com"),
        ),
        note="Self-hosted blogs are recognized by their /wp-admin and /wp-login.php paths only.",
        login="https://wordpress.com/log-in",
    ),
    Site(
        key="google", name="Google", hosts=("google.com",), aliases=("gmail", "gaccount"),
        session=(Cookie("SID"),),
        identity=None,
        home="https://accounts.google.com/",
        urls=(
            UrlRule(A, r"/challenge/recaptcha", "sign-in reCAPTCHA URL", host="accounts.google.com"),
            UrlRule(L, r"/challenge/pwd", "password page", host="accounts.google.com", signed_in=C),
            UrlRule(C, r"/challenge/", "verification challenge URL", host="accounts.google.com"),
            UrlRule(C, r"^/speedbump/", "verify-it's-you URL", host="accounts.google.com"),
            UrlRule(B, r"^/(?:v3/)?signin/(?:v2/)?(?:rejected|deniedsigninrejected)", "sign-in rejected URL",
                    host="accounts.google.com"),
            UrlRule(L, r"^/(?:v3/signin|signin|ServiceLogin|AccountChooser|InteractiveLogin)(?:/|$|\?)",
                    "sign-in URL", host="accounts.google.com"),
            UrlRule(R, r"^/sorry/", "unusual traffic page"),
        ),
        note="SID is opaque and no cookie carries the account id. YouTube has its own domain and is not covered.",
        login="https://accounts.google.com/",
        approve=(r"/challenge/(?:dp|az)(?:/|$|\?)",),
    ),
    Site(
        key="x", name="X", hosts=("x.com", "twitter.com"), aliases=("twitter",),
        session=(Cookie("auth_token"),),
        identity=IdentityCookie(Cookie("twid"), _twid),
        home="https://x.com/",
        urls=(
            UrlRule(C, r"^/account/access(?:/|$|\?)", "locked account URL"),
            UrlRule(C, r"^/account/login_(?:challenge|verification)", "login challenge URL"),
            UrlRule(L, r"^/i/flow/login", "login URL"),
            UrlRule(L, r"^/login(?:/|$|\?)", "login URL"),
        ),
        login="https://x.com/i/flow/login",
    ),
)

_CF_CHALLENGE = "Cloudflare challenge page"
_HUMAN = "HUMAN (PerimeterX)"
_ARKOSE = "Arkose (FunCaptcha)"
_INVISIBLE_ANCHOR = r"^/recaptcha/(?:api2|enterprise)/anchor\b.*[?&]size=invisible"
_RECAPTCHA_FIELD = "g-recaptcha-response"
_ANCHOR = r"^/recaptcha/(?:api2|enterprise)/anchor\b"
# A check that is a checkbox (one click, then the vendor decides) has a label of its own; the vendor's other
# frames (hCaptcha's and reCAPTCHA's image puzzles) keep "<vendor> frame". CHECKBOX_DETAILS lists them.
HCAPTCHA_CHECKBOX = "hCaptcha checkbox"
RECAPTCHA_CHECKBOX = "reCAPTCHA checkbox"

VENDORS: tuple[VendorRule, ...] = (
    # Google reCAPTCHA: the v3 / invisible badge is a 256x60 anchor frame on normal pages.
    VendorRule("reCAPTCHA", "frame", None, "google.com", _INVISIBLE_ANCHOR, label="invisible reCAPTCHA badge"),
    VendorRule("reCAPTCHA", "frame", None, "recaptcha.net", _INVISIBLE_ANCHOR, label="invisible reCAPTCHA badge"),
    VendorRule("reCAPTCHA", "frame", A, "google.com", _ANCHOR, label=RECAPTCHA_CHECKBOX, passed_field=_RECAPTCHA_FIELD),
    VendorRule("reCAPTCHA", "frame", A, "recaptcha.net", _ANCHOR, label=RECAPTCHA_CHECKBOX,
               passed_field=_RECAPTCHA_FIELD),
    VendorRule("reCAPTCHA", "frame", A, "google.com", r"^/recaptcha/", passed_field=_RECAPTCHA_FIELD),
    VendorRule("reCAPTCHA", "frame", A, "recaptcha.net", r"^/recaptcha/", passed_field=_RECAPTCHA_FIELD),
    VendorRule("reCAPTCHA", "script", None, "google.com", r"^/recaptcha/"),
    VendorRule("reCAPTCHA", "script", None, "gstatic.com", r"^/recaptcha/"),
    VendorRule("reCAPTCHA", "script", None, "recaptcha.net"),
    # hCaptcha
    VendorRule("hCaptcha", "frame", None, "hcaptcha.com", r"frame=checkbox-invisible", label="invisible hCaptcha"),
    VendorRule("hCaptcha", "frame", A, "hcaptcha.com", r"frame=checkbox(?:&|$)", label=HCAPTCHA_CHECKBOX,
               passed_field="h-captcha-response"),
    VendorRule("hCaptcha", "frame", A, "hcaptcha.com", passed_field="h-captcha-response"),
    VendorRule("hCaptcha", "script", None, "hcaptcha.com"),
    # Cloudflare: Turnstile frames; challenge pages ("Just a moment") by their markers and first-party
    # orchestrate scripts. /cdn-cgi/challenge-platform/scripts/jsd/ is bot management on normal pages.
    VendorRule("Cloudflare Turnstile", "frame", A, "challenges.cloudflare.com", passed_field="cf-turnstile-response"),
    VendorRule("Cloudflare Turnstile", "script", None, "challenges.cloudflare.com"),
    VendorRule("Cloudflare", "script", A, None, r"^/cdn-cgi/challenge-platform/h/[^/]+/orchestrate/"
               r"(?:chl_page|managed|jsch|captcha)/", label=_CF_CHALLENGE),
    VendorRule("Cloudflare", "script", None, None, r"^/cdn-cgi/challenge-platform/", label="Cloudflare bot management"),
    VendorRule("Cloudflare", "element", A, selector="#challenge-form", visible=False, label=_CF_CHALLENGE),
    VendorRule("Cloudflare", "element", A, selector="#challenge-stage", visible=False, label=_CF_CHALLENGE),
    VendorRule("Cloudflare", "element", A, selector="#cf-challenge-running", visible=False, label=_CF_CHALLENGE),
    # Arkose Labs (FunCaptcha): LinkedIn, X and others. Detection-only enforcement frames are 0x0.
    VendorRule(_ARKOSE, "frame", A, "arkoselabs.com"),
    VendorRule(_ARKOSE, "frame", A, "funcaptcha.com"),
    VendorRule(_ARKOSE, "script", None, "arkoselabs.com"),
    VendorRule(_ARKOSE, "script", None, "funcaptcha.com"),
    # DataDome: the captcha or device-check frame; t=bv is a banned visitor with nothing to solve.
    VendorRule("DataDome", "frame", B, "captcha-delivery.com", r"[?&]t=bv(?:&|#|$)", label="DataDome block frame"),
    VendorRule("DataDome", "frame", A, "captcha-delivery.com"),
    VendorRule("DataDome", "script", None, "datadome.co", label="DataDome tag"),
    VendorRule("DataDome", "script", None, "captcha-delivery.com"),
    # HUMAN (PerimeterX): the press-and-hold challenge renders into #px-captcha; the sensor loads everywhere.
    VendorRule(_HUMAN, "element", A, selector="#px-captcha", label=f"{_HUMAN} challenge"),
    VendorRule(_HUMAN, "frame", A, "px-cdn.net"),
    VendorRule(_HUMAN, "frame", A, "px-cloud.net"),
    VendorRule(_HUMAN, "script", None, "px-cdn.net"),
    VendorRule(_HUMAN, "script", None, "px-cloud.net"),
    VendorRule(_HUMAN, "script", None, "perimeterx.net"),
    VendorRule(_HUMAN, "script", None, "px-client.net"),
)

# Generic statuses, after the site's own. 5xx is not an account risk and stays ok.
STATUSES: tuple[tuple[int, Signal], ...] = ((429, R), (403, B))

SITES_BY_KEY: dict[str, Site] = {s.key: s for s in SITES}
_BY_WORD: dict[str, Site] = {w: s for s in SITES for w in (s.key, *s.aliases)}
MARKER_SELECTORS: tuple[str, ...] = tuple(dict.fromkeys(r.selector for r in VENDORS if r.selector))
# The token fields of the widgets above: interaction.py reads which of them hold a token (never the token)
PASSED_FIELDS: tuple[str, ...] = tuple(dict.fromkeys(r.passed_field for r in VENDORS if r.passed_field))
# The rules of Cloudflare's own check, which often clears by itself within seconds ("Just a moment",
# "Verifying..."): a navigation gives it that time before it reads the signal (interaction.autocheck)
CLOUDFLARE_DETAILS: frozenset[str] = frozenset(r.detail for r in VENDORS if r.vendor.startswith("Cloudflare")
                                               and r.signal is not None)
# The checks that ask a person for one click on a checkbox and no puzzle: Cloudflare's (its page and Turnstile),
# and the checkbox frames of hCaptcha and reCAPTCHA. A puzzle frame on the page wins over them (_vendor_signal).
CHECKBOX_DETAILS: frozenset[str] = CLOUDFLARE_DETAILS | {HCAPTCHA_CHECKBOX, RECAPTCHA_CHECKBOX}


def site(key: str | None) -> Site | None:
    """A site by key or alias (`instagram`, `ig`)."""
    return _BY_WORD.get((key or "").strip().lower())


def site_key(name: str) -> str:
    """The canonical key for a site name or alias; an unknown name is kept (lowercased)."""
    known = site(name)
    return known.key if known else name.strip().lower()


# The pages where an account edits its own public profile, by site: path prefixes (a prefix ending
# in / is a directory; one with "?" also names query parameters the page must carry). `{id}` is the
# lane's pinned account id: a page that names an account must name the pinned one, and a site whose
# edit page names an account has no edit page until one is pinned. A learn window
# (openajar.node.egress.LaneGuard.open_learn) releases a request only from a page under one of these,
# so a reviewed list keeps it off a composer, a message box or another member's profile. Reviewed by
# hand; a site not listed here has no learn window.
PROFILE_EDIT_PATHS: dict[str, tuple[str, ...]] = {
    "instagram": ("/accounts/edit/",),
    # A best reading of Facebook's web client (Nov 2026), not checked on a real account: the intro is
    # edited in a dialog over the profile page, which names the account only as profile.php?id=<id>
    # (a vanity URL names it by a handle openajar does not pin).
    "facebook": ("/profile.php?id={id}",),
    "linkedin": ("/in/{id}/",),
    "reddit": ("/settings/profile",),
    "medium": ("/me/settings",),
}


def profile_edit_paths(site_name: str, account_id: str | None = None) -> list[str]:
    """The reviewed profile edit page prefixes of a site (by key or alias), with `{id}` filled in
    with the pinned account's id; an entry that names an account is left out without one. [] for a
    site with none."""
    out: list[str] = []
    for p in PROFILE_EDIT_PATHS.get(site_key(site_name or ""), ()):
        if "{id}" in p:
            if not account_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", account_id):
                continue
            p = p.replace("{id}", account_id)
        out.append(p)
    return out


@dataclass(frozen=True, slots=True)
class EditEndpoint:
    """The one request that saves one profile field, as a person reviewed it: `method` to `host`
    (exactly), a path the whole of which `path` (a regex) matches, `op` its operation name (None:
    the request names none), and `body_key` where the field's new value sits (`form:<name>` or
    `json:<a.b>`, the FlowCommit match syntax), `fields` every field the form posts. `form_data`: the (host, path regex, JSON key) of the
    GET whose answer holds the form's current values, which the learn window compares every other
    field of the saved body with (nothing else may change); None means no reviewed way to see them,
    and then nothing is released for the field."""
    method: str
    host: str
    path: str
    body_key: str
    op: str | None = None
    form_data: tuple[str, str, str] | None = None
    fields: tuple[str, ...] = ()          # every field the form posts (a form endpoint): no other may be sent
    note: str = ""


_IG_FORM = ("www.instagram.com", r"/api/v1/accounts/edit/web_form_data/", "form_data")
_IG_FIELDS = ("biography", "chaining_enabled", "email", "external_url", "first_name", "phone_number", "username")
# The requests that save a profile field, by site and field. A field not here cannot be learned. Each
# entry names exactly one endpoint and where the value sits in its body; a request that only carries
# the value somewhere else (a message, another field) is never one of these.
PROFILE_EDIT_ENDPOINTS: dict[str, dict[str, EditEndpoint]] = {
    # Instagram's web "Edit profile" form (www.instagram.com/accounts/edit/): one form post with every
    # field of the form (biography, chaining_enabled, email, external_url, first_name, phone_number,
    # username), whichever changed; the page reads their values first from web_form_data. A reading
    # of the public web client (Nov 2026), to be confirmed in observe mode on a throwaway account.
    "instagram": {
        "bio": EditEndpoint("POST", "www.instagram.com", r"/api/v1/web/accounts/edit/", "form:biography",
                            form_data=_IG_FORM, fields=_IG_FIELDS),
        "website": EditEndpoint("POST", "www.instagram.com", r"/api/v1/web/accounts/edit/", "form:external_url",
                                form_data=_IG_FORM, fields=_IG_FIELDS),
    },
    # Facebook, LinkedIn, Medium and Reddit: no entry yet. Nobody recorded their profile saves (the
    # LinkedIn profile rules are guesses, egress.rules "TO CAPTURE"), so none of their fields can be
    # learned until a person adds a reviewed entry here.
}


def profile_edit_endpoint(site_name: str, field: str) -> EditEndpoint | None:
    """The reviewed endpoint that saves `field` on a site, or None (the field cannot be learned)."""
    return PROFILE_EDIT_ENDPOINTS.get(site_key(site_name or ""), {}).get(field)


def site_for(url: str) -> Site | None:
    """The site a URL belongs to: by host first, then (sites without fixed hosts) by path."""
    host, rest = _target(url)
    if not host:
        return None
    for s in SITES:
        if any(host_is(host, h) for h in s.hosts):
            return s
    for s in SITES:
        if s.paths and _rx(s.paths).search(rest):
            return s
    return None


def vendor_rule(kind: str, url: str) -> VendorRule | None:
    """The first vendor rule of this kind ("frame" or "script") that matches the URL."""
    for rule in VENDORS:
        if rule.kind == kind and rule.matches_url(url):
            return rule
    return None


def is_challenge_frame(url: str) -> bool:
    """A frame URL that only a challenge page or a solvable widget loads (invisible badges excluded)."""
    rule = vendor_rule("frame", url)
    return rule is not None and rule.signal is not None


# ---- what a lane expects -------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LaneExpectation:
    """The sites a lane should be signed into, and the account pinned for it (if any)."""
    lane_id: str | None = None
    sites: frozenset[str] = frozenset()
    account: AccountRef | None = None
    source: str = ""                     # "account", "lane name" or "" (nothing expected)

    def expects(self, site_key: str | None) -> bool:
        return site_key is not None and site_key in self.sites


def expectation_for(spec: LaneSpec) -> LaneExpectation:
    """The account's site when `spec.account` is set; otherwise every dash separated word of the lane
    name that is a site key or alias (`biz-a/instagram` -> instagram, `acme/fb-page` -> facebook)."""
    if spec.account is not None:
        return LaneExpectation(spec.id, frozenset({site_key(spec.account.site)}), spec.account, "account")
    words = frozenset(s.key for s in map(site, spec.name.split("-")) if s is not None)
    return LaneExpectation(spec.id, words, None, "lane name" if words else "")


# ---- account identity ----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Identity:
    site: str
    user_id: str
    source: str                          # "cookie c_user"

    def public(self) -> dict[str, str]:
        return {"site": self.site, "user_id": self.user_id, "source": self.source}


def identity_from_cookies(s: Site, cookies: Cookies) -> Identity | None:
    """The signed-in user's id from the site's own cookie, or None (signed out, or not visible)."""
    if s.identity is None or s.missing_session(cookies) is not None:
        return None
    found = s.identity.cookie.find(cookies)
    user_id = s.identity.parse(str(found.get("value", ""))) if found else None
    return Identity(s.key, user_id, f"cookie {found.get('name')}") if found and user_id else None


async def account_identity(context: Any, which: Site | str, url: str | None = None) -> dict[str, str] | None:
    """`{site, user_id, source}` for the account a browser context is signed into on a site, read from
    the context's cookies (`context.cookies(url)`: no request, invisible to the page). None when signed
    out or when the site shows no id without a request (LinkedIn, Google)."""
    s = which if isinstance(which, Site) else site(which)
    target = url or (s.home if s else None)
    if s is None or not target:
        return None
    cookies = await context.cookies([target])
    found = identity_from_cookies(s, cookies)
    return found.public() if found else None


def _id(value: Any) -> str | None:
    if isinstance(value, Identity):
        return value.user_id
    if isinstance(value, Mapping):
        value = value.get("user_id")
    text = str(value).strip() if value is not None else ""
    return text or None


def account_matches(expected: AccountRef | None, identity: Identity | Mapping[str, Any] | None) -> bool | None:
    """True when the signed-in account is the pinned one, False when it is another, None when it cannot
    be told (nothing pinned, no id visible, or another site)."""
    if expected is None or identity is None:
        return None
    want, seen = _id(expected.user_id), _id(identity)
    seen_site = identity.site if isinstance(identity, Identity) else identity.get("site")
    if want is None or seen is None or site_key(expected.site) != seen_site:
        return None
    return want.casefold() == seen.casefold()


@dataclass(frozen=True, slots=True)
class AccountCheck:
    site: str
    user_id: str
    source: str
    status: str                          # "match", "wrong_account" or "unpinned"
    expected_user_id: str | None = None
    expected_handle: str | None = None

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"site": self.site, "user_id": self.user_id, "source": self.source,
                               "status": self.status}
        if self.expected_user_id or self.expected_handle:
            out["expected"] = {"user_id": self.expected_user_id, "handle": self.expected_handle}
        return out


def check_account(expect: LaneExpectation | None, identity: Identity | None) -> AccountCheck | None:
    """Compare what the session shows with the lane's pinned account."""
    if identity is None:
        return None
    pinned = expect.account if expect is not None else None
    if pinned is not None and site_key(pinned.site) != identity.site:
        pinned = None                    # pinned for another site; nothing to compare here
    verdict = account_matches(pinned, identity)
    status = "match" if verdict else WRONG_ACCOUNT if verdict is False else "unpinned"
    return AccountCheck(identity.site, identity.user_id, identity.source, status,
                        pinned.user_id if pinned else None, pinned.handle if pinned else None)


# ---- page facts and the classifier ---------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Frame:
    url: str
    visible: bool = False                # renders at least MIN_VISIBLE_PX square, on the page, not hidden


@dataclass(frozen=True, slots=True)
class PageFacts:
    """What interaction.py reads from a page. Structural only: there is no field for page text."""
    url: str
    status: int | None = None
    readable: bool = True                # the page's frames, scripts and markers were read
    frames: tuple[Frame, ...] = ()
    scripts: tuple[str, ...] = ()
    elements: Mapping[str, bool] = field(default_factory=dict)   # selector -> visible, for markers found
    cookies: Cookies | None = None       # None: could not be read, so logged_out is never guessed
    passed: frozenset[str] = frozenset()  # PASSED_FIELDS that hold a token: those widgets were passed


@dataclass(frozen=True, slots=True)
class SignalReport:
    signal: Signal
    detail: str | None = None            # which rule matched: "instagram challenge URL", "DataDome frame"
    url: str = ""
    site: str | None = None
    account: AccountCheck | None = None

    @property
    def wrong_account(self) -> bool:
        return self.account is not None and self.account.status == WRONG_ACCOUNT

    def public(self) -> dict[str, Any]:
        """The fields every browser result carries."""
        return {"signal": self.signal, "signal_detail": self.detail,
                "account": self.account.public() if self.account else None}


def _vendor_signal(facts: PageFacts) -> tuple[Signal, str] | None:
    # a puzzle that shows wins over the checkbox it came from: the checkbox frame stays on the page beside it
    ruled = [(f, vendor_rule("frame", f.url)) for f in facts.frames]
    for f, rule in sorted(ruled, key=lambda fr: fr[1] is not None and fr[1].detail in (HCAPTCHA_CHECKBOX,
                                                                                         RECAPTCHA_CHECKBOX)):
        if rule is not None and rule.passed_field and rule.passed_field in facts.passed:
            continue  # the widget was passed (by itself, or by a person): it shows "Success!", nothing to do
        if rule is not None and rule.signal is not None and (f.visible or not rule.visible):
            return rule.signal, rule.detail
    for rule in VENDORS:
        shown = facts.elements.get(rule.selector) if rule.kind == "element" and rule.selector else None
        if rule.signal is not None and shown is not None and (shown or not rule.visible):
            return rule.signal, rule.detail
    for src in facts.scripts:
        rule = vendor_rule("script", src)
        if rule is not None and rule.signal is not None:
            return rule.signal, rule.detail
    return None


def _status_signal(s: Site | None, status: int | None) -> tuple[Signal, str] | None:
    if status is None:
        return None
    for code, sig in (*(s.statuses if s else ()), *STATUSES):
        if status == code:
            return sig, (f"{s.key} HTTP {code}" if s and (code, sig) in s.statuses else f"HTTP {code}")
    return None


def classify_page(facts: PageFacts, expect: LaneExpectation | None = None) -> SignalReport:
    """The page's signal from its facts; deterministic, no model, no page text. See the module doc."""
    url = facts.url or ""
    if not url or url == "about:blank":
        return SignalReport(Signal.UNKNOWN, "nothing is loaded", url)
    s = site_for(url)
    key = s.key if s else None
    cookies = facts.cookies
    signed_in = None if s is None or cookies is None else s.missing_session(cookies) is None
    account = check_account(expect, identity_from_cookies(s, cookies)) if s and cookies is not None else None

    def report(sig: Signal, detail: str | None) -> SignalReport:
        return SignalReport(sig, detail, url, key, account)

    found = _vendor_signal(facts)
    if found:
        return report(*found)
    expected = expect is not None and expect.expects(key)
    if s is not None:
        host, rest = _target(url)
        for rule in s.urls:
            if not rule.matches(host, rest):
                continue
            if rule.signal is not Signal.LOGGED_OUT:
                return report(rule.signal, f"{s.key} {rule.label}")
            if signed_in and rule.signed_in is not None:
                return report(rule.signed_in, f"{s.key} {rule.label} while signed in")
            if expected:
                return report(Signal.LOGGED_OUT, f"{s.key} {rule.label}")
            break                        # a login page on a site this lane does not use: not a problem
    by_status = _status_signal(s, facts.status)
    if by_status:
        return report(*by_status)
    if s is not None and expected and cookies is not None and not signed_in:
        return report(Signal.LOGGED_OUT, f"{s.key} session cookie {s.missing_session(cookies)} missing")
    if not facts.readable and facts.status is None:
        return report(Signal.UNKNOWN, "the page could not be read")
    if account is not None and account.status == WRONG_ACCOUNT:
        handle, uid = account.expected_handle, account.expected_user_id
        want = f"{handle} ({uid})" if handle and uid else handle or uid
        return report(Signal.OK, f"wrong account: {account.site} user {account.user_id} is signed in, "
                                 f"the lane expects {want}")
    return report(Signal.OK, None)


def classify_signal(*, loaded: bool, status: int | None, challenge: bool) -> Signal:
    """The generic rules alone: a challenge wins over the status (a Cloudflare challenge is often a
    403 or 503, and a human can solve it through the live view); then 429 and 403; else ok."""
    if not loaded:
        return Signal.UNKNOWN
    if challenge:
        return Signal.CAPTCHA
    found = _status_signal(None, status)
    return found[0] if found else Signal.OK


def describe(sites: Iterable[Site] = SITES) -> list[dict[str, Any]]:
    """The table as plain dicts (for docs, the panel and the final report)."""
    return [{"site": s.key, "hosts": list(s.hosts), "aliases": list(s.aliases),
             "session_cookies": [c.name for c in s.session],
             "identity": s.identity.cookie.name if s.identity else None,
             "urls": [{"signal": r.signal.value, "pattern": r.pattern, "label": r.label, "host": r.host}
                      for r in s.urls],
             "statuses": {code: sig.value for code, sig in s.statuses}, "login": s.login,
             "approve": list(s.approve), "note": s.note}
            for s in sites]
