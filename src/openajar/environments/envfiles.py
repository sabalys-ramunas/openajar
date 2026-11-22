"""A repository's local env files on an environment's machine, without its secrets
(`openajar environments env push`, openajar.environments.envfiles_cli).

A `.env` file holds perhaps a hundred values: a few secrets and many settings (URLs, flags, ports,
names). This module reads one (dotenv syntax), sorts each key into plain or secret, and plans what
reaches the machine:

- a plain value is copied as it is written in the file;
- a secret that a placeholder can carry (openajar.environments.appsecrets, swapped in by the node,
  openajar.environments.swap) is bound to its host in the vault, and the machine's file gets
  `KEY=${KEY}`, which reads the placeholder the machine's environment holds;
- any other secret is not sent at all: the machine's file gets a comment naming the key, and the
  table says why and what the person can do.

It errs toward secret: a key it is not sure about is a secret. A secret's value never leaves this
module except into the vault (`Row.entry.value`, read only by the binding step): not into the
machine's file (`render`), the table (`table`) or the call summary (`summary`).

What a placeholder can carry (swap.py): an HTTPS request, over HTTP/1.1, to the host it is bound to,
in a header (a Basic credential's user or password included) or the URL's query. Not a body, not
a value the machine uses itself, not a database or mail connection, not a secret inside a URL.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

# ---------------------------------------------------------------- parsing

FILE_MAX_BYTES = 256 * 1024
KEYS_MAX = 2000

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class Entry:
    key: str
    value: str          # the value as the app reads it (quotes removed, escapes in "..." applied)
    raw: str            # the value as written (quotes kept), for copying a plain value exactly
    export: bool = False
    line: int = 0


@dataclass
class Parsed:
    entries: list[Entry] = field(default_factory=list)
    bad_lines: list[int] = field(default_factory=list)   # line numbers only: a bad line may hold a secret


_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), "\\" + m.group(1)), s, flags=re.DOTALL)


def _quoted(text: str, start: int, quote: str) -> int:
    """The index of the quote that closes the one at `start`, or -1. In "..." a backslash escapes."""
    i = start + 1
    while i < len(text):
        c = text[i]
        if c == "\\" and quote == '"':
            i += 2
            continue
        if c == quote:
            return i
        i += 1
    return -1


def parse(text: str) -> Parsed:
    """dotenv syntax: `KEY=value`, `export KEY=value`, `# comments`, empty values, '...' and `...` taken
    as written, "..." with escapes, quoted values over several lines, and `value # comment`. A line that
    is none of these is left out and its number kept (never its text). A key given twice: the last wins."""
    out = Parsed()
    pos, n = 0, len(text)
    seen: dict[str, int] = {}
    while pos < n:
        end = text.find("\n", pos)
        end = n if end < 0 else end
        line_no = text.count("\n", 0, pos) + 1
        line = text[pos:end]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            pos = end + 1
            continue
        body_at = pos + (len(line) - len(line.lstrip()))
        export = False
        if text.startswith("export ", body_at) or text.startswith("export\t", body_at):
            export = True
            body_at += 7
            while body_at < end and text[body_at] in " \t":
                body_at += 1
        m = _KEY_RE.match(text, body_at, end)
        eq = m.end() if m else end
        while eq < end and text[eq] in " \t":
            eq += 1
        if m is None or eq >= end or text[eq] != "=":
            out.bad_lines.append(line_no)
            pos = end + 1
            continue
        key = m.group(0)
        v = eq + 1
        while v < end and text[v] in " \t":
            v += 1
        if v < n and text[v] in "'\"`":
            close = _quoted(text, v, text[v])
            if close < 0:
                out.bad_lines.append(line_no)
                pos = end + 1
                continue
            raw = text[v:close + 1]
            inner = raw[1:-1]
            value = _unescape(inner) if raw[0] == '"' else inner
            rest_end = text.find("\n", close)
            rest_end = n if rest_end < 0 else rest_end
            rest = text[close + 1:rest_end].strip()
            if rest and not rest.startswith("#"):
                out.bad_lines.append(line_no)
                pos = rest_end + 1
                continue
            pos = rest_end + 1
        else:
            unquoted = text[v:end]
            hash_at = re.search(r"\s#", unquoted)
            raw = (unquoted[:hash_at.start()] if hash_at else unquoted).strip()
            value = raw
            pos = end + 1
        if key in seen:
            out.entries[seen[key]] = Entry(key, value, raw, export, line_no)
        else:
            seen[key] = len(out.entries)
            out.entries.append(Entry(key, value, raw, export, line_no))
        if len(out.entries) > KEYS_MAX:
            raise ValueError(f"has more than {KEYS_MAX} keys")
    return out


# ---------------------------------------------------------------- classifying

PLAIN, SECRET = "plain", "secret"
# How a secret is used, which decides whether a placeholder can carry it.
HEADER = "header"               # sent to an API in a header or the query: a placeholder works
LOCAL = "local"                 # used on the machine itself: signing, verifying, encrypting
BODY = "body"                   # sent in a request body: OAuth token requests
CONNECTION = "connection"       # a database, cache, queue or mail server: not HTTPS
IN_URL = "in_url"               # the secret is part of a URL (user:password@, or a webhook URL's path)

# Why a placeholder cannot carry it, and what the person can do instead ({key} is the key).
UNSUPPORTED: dict[str, tuple[str, str]] = {
    LOCAL: (("the app uses it on the machine itself (to sign, verify or encrypt), so a placeholder there is "
            "just a wrong key"),
            ("give the machine a value of its own (a test key, or a fresh random one: openssl rand -hex 32), "
            "or --plain {key} if this file's value is a dev-only one")),
    BODY: (("it is sent in a request body (an OAuth token request), and the node puts values into headers "
           "and the URL query only"),
           "--plain {key} with a test account's value, or --host {key}=HOST if the app sends it in a header"),
    CONNECTION: (("it is for a database, cache, queue or mail server, and the node only carries HTTPS "
                 "requests"),
                 ("a server on the machine itself: its dev password is fine to copy with --plain {key}; a "
                 "remote one: give the machine a test server of its own")),
    IN_URL: ("the secret is part of a URL, and the node puts values into headers and the URL query only",
             "put the secret in a variable of its own, or --plain {key} if it is a dev-only URL"),
}
NEEDS_HOST = ("needs a host: the node puts a placeholder's value in only for the host it is bound to",
              "--host {key}=api.example.com binds it, or --plain {key} if it is a dev-only value")
NOT_A_NAME = ("its name is not one the node can bind (capitals, digits and _ only)",
              "rename it, or --plain {key} if it is a dev-only value")

_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")
# Beyond policy.redact's secret words (password, token, secret, api_key, auth, session, ...).
_MORE_SECRET_WORDS = {"pass", "key", "dsn", "salt", "pepper", "license", "licence", "webhook", "hook"}
_LOCAL_WORDS = {"webhook", "signing", "jwt", "session", "cookie", "encryption", "encrypt", "cipher", "hmac",
                "salt", "pepper", "nextauth", "csrf", "xsrf", "signature", "private", "master"}
_LOCAL_NAMES = {"SECRET", "SECRET_KEY", "SECRET_KEY_BASE", "APP_KEY", "APP_SECRET_KEY", "AUTH_SECRET"}
_CONNECTION_WORDS = {"db", "database", "postgres", "postgresql", "pg", "mysql", "mariadb", "mongo", "mongodb",
                     "redis", "rabbitmq", "rabbit", "amqp", "smtp", "imap", "pop3", "mail", "ldap", "ftp", "sftp",
                     "ssh", "kafka", "nats", "memcached", "valkey", "mssql", "oracle", "clickhouse"}
# The last word of a setting about a secret, not the secret: AUTH_URL, SESSION_TTL, JWT_EXPIRES_IN.
_SETTING_LAST = {"url", "uri", "host", "hostname", "port", "endpoint", "domain", "issuer", "audience", "path",
                 "callback", "redirect", "enabled", "disabled", "enable", "disable", "ttl", "timeout", "expiry",
                 "expires", "expire", "in", "age", "lifetime", "length", "size", "limit", "name", "mode", "type",
                 "provider", "strategy", "region", "header", "prefix", "file", "dir", "algorithm", "alg", "version"}
_BOOLS = {"true", "false", "yes", "no", "on", "off", "y", "n", "none", "null", "nil"}


def words(key: str) -> list[str]:
    return [w.lower() for w in _SPLIT.split(key) if w]


@dataclass(frozen=True)
class Verdict:
    cls: str                 # PLAIN or SECRET
    use: str | None = None   # for a secret: HEADER, LOCAL, BODY, CONNECTION or IN_URL
    why: str = ""            # why it is in that class, in a few words


def _secret_name(key: str) -> str | None:
    from openajar.policy.redact import secret_key_kind

    kind = secret_key_kind(key)
    if kind:
        return kind
    return next((w for w in words(key) if w in _MORE_SECRET_WORDS), None)


def _secret_shape(value: str) -> bool:
    """A value policy.redact would hide: a key's known prefix (sk_live_, ghp_, AKIA...), a JWT, a URL's
    password, a private key, a long random string."""
    from openajar.policy.redact import redact_text

    return redact_text(value, max_len=0) != value


def _url(value: str) -> tuple[str, str, bool, str] | None:
    """(scheme, host, has a user or password, path) of a URL, else None."""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        return None
    try:
        u = urlsplit(value)
        host = u.hostname or ""
    except ValueError:
        return None
    return u.scheme.lower(), host, bool(u.username or u.password), u.path


def _plain_word(w: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9*][A-Za-z0-9._:/*+@-]*", w) or len(w) > 40:
        return False
    # a run of 8 or more letters and digits mixed (abc123def, a UUID's part) may be a key: not sure
    return not any(re.search(r"[A-Za-z]", run) and re.search(r"\d", run)
                   for run in re.findall(r"[A-Za-z0-9]{8,}", w))


def plain_shaped(value: str) -> bool:
    """A value that reads as a setting: empty, a flag, a number (3000, 1.5, 7d), a URL with no user or
    password, an email address, a path, or a few short words (production, eu-west-1, info,debug)."""
    v = value.strip()
    if not v or v.lower() in _BOOLS or re.fullmatch(r"-?\d+(\.\d+)?[A-Za-z]{0,2}", v):
        return True
    if len(v) > 200 or "\n" in v:
        return False
    u = _url(v)
    if u is not None:
        return not u[2] and all(_plain_word(s) for s in u[3].split("/") if s)
    if re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", v) or re.fullmatch(r"(\.{0,2}/|~/)[\w./@+-]*", v):
        return True
    return all(_plain_word(w) for w in re.split(r"[,\s]+", v) if w)


def _use(key: str, value: str) -> str:
    """How a secret is used, from its value first, then its name."""
    w = words(key)
    ws = set(w)
    v = value.strip()
    if v.startswith(("-----BEGIN", "whsec_", "{")):
        return LOCAL
    u = _url(v)
    if u is not None:
        if u[0] not in ("http", "https"):
            return CONNECTION
        if u[2] or ws & {"webhook", "hook"}:
            return IN_URL
    if ("refresh" in ws and "token" in ws) or ("client" in ws and "secret" in ws):
        return BODY
    if key.upper() in _LOCAL_NAMES or ws & _LOCAL_WORDS or {"secret", "access", "key"} <= ws:
        return LOCAL  # the last: AWS's secret access key signs each request on the machine
    if ws & _CONNECTION_WORDS:
        return CONNECTION
    return HEADER


def classify(key: str, value: str) -> Verdict:
    """Plain or secret, and for a secret how it is used. When in doubt, a secret."""
    if value == "":
        return Verdict(PLAIN, why="empty")
    named = _secret_name(key)
    if _secret_shape(value):
        return Verdict(SECRET, _use(key, value), "its value looks like a key or a password")
    if named:
        last = words(key)[-1]
        if last in _SETTING_LAST and plain_shaped(value) and not ({"webhook", "hook"} & set(words(key))):
            return Verdict(PLAIN, why=f"a setting about a {named}, not the {named} itself")
        return Verdict(SECRET, _use(key, value), f"its name says so ({named})")
    if {"webhook", "hook"} & set(words(key)) and _url(value) is not None:
        return Verdict(SECRET, IN_URL, "a webhook URL is its own secret")
    if plain_shaped(value):
        return Verdict(PLAIN)
    return Verdict(SECRET, _use(key, value), "not sure, so a secret")


# ---------------------------------------------------------------- hosts

# A key's host when its name or value says which API it is for: (name prefixes, value prefixes, host).
KNOWN_HOSTS: tuple[tuple[tuple[str, ...], tuple[str, ...], str], ...] = (
    (("STRIPE_",), ("sk_live_", "sk_test_", "rk_live_", "rk_test_"), "api.stripe.com"),
    (("OPENAI_",), ("sk-proj-", "sk-svcacct-"), "api.openai.com"),
    (("ANTHROPIC_",), ("sk-ant-",), "api.anthropic.com"),
    (("OPENROUTER_",), ("sk-or-",), "openrouter.ai"),
    (("GITHUB_", "GH_"), ("ghp_", "github_pat_"), "api.github.com"),
    (("SENDGRID_",), ("SG.",), "api.sendgrid.com"),
    (("RESEND_",), ("re_",), "api.resend.com"),
    (("POSTMARK_",), (), "api.postmarkapp.com"),
    (("MAILGUN_",), (), "api.mailgun.net"),
    (("TWILIO_",), (), "api.twilio.com"),
    (("GROQ_",), ("gsk_",), "api.groq.com"),
    (("MISTRAL_",), (), "api.mistral.ai"),
    (("REPLICATE_",), ("r8_",), "api.replicate.com"),
    (("HUGGINGFACE_", "HF_"), ("hf_",), "huggingface.co"),
)
_URL_LAST = {"url", "uri", "endpoint", "host", "base"}
_SECRET_TAIL = {"key", "token", "secret", "password", "pass", "passwd", "pwd", "apikey", "api", "access", "auth",
                "bearer", "credential", "credentials", "user", "username", "login"}


def _public_host(host: str) -> bool:
    import ipaddress

    if not host or "." not in host or host.endswith((".local", ".localhost", ".internal")) or host == "localhost":
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def infer_host(key: str, value: str, entries: list[Entry]) -> tuple[str, str] | None:
    """(host, how it was found) for a secret: a key of a known API, else the one HTTPS host of the
    settings that share its prefix (DATAFORSEO_PASSWORD next to DATAFORSEO_API_URL=https://...)."""
    up = key.upper()
    for names, prefixes, host in KNOWN_HOSTS:
        if up.startswith(names) or (prefixes and value.startswith(prefixes)):
            return host, "its name or value (a known API)"
    w = key.split("_")
    while w and w[-1].lower() in _SECRET_TAIL:
        w.pop()
    prefix = "_".join(w)
    if not prefix:
        return None
    hosts: dict[str, str] = {}
    for e in entries:
        if e.key == key or not e.key.startswith(prefix + "_") or words(e.key)[-1] not in _URL_LAST:
            continue
        u = _url(e.value) or (("https", e.value, False, "") if re.fullmatch(r"[\w.-]+", e.value) else None)
        if u is not None and u[0] == "https" and not u[2] and _public_host(u[1].lower()):
            hosts.setdefault(u[1].lower(), e.key)
    if len(hosts) == 1:
        host, source = next(iter(hosts.items()))
        return host, source
    return None


# ---------------------------------------------------------------- the plan

COPY, PLACEHOLDER, BIND, HOLD, SKIP = "copy", "placeholder", "bind", "not sent", "left out"
PLAIN_SAID = "you said --plain"


@dataclass
class Overrides:
    """The person's word on keys, as fnmatch patterns (NEXT_PUBLIC_*)."""
    plain: list[str] = field(default_factory=list)
    secret: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    hosts: dict[str, list[str]] = field(default_factory=dict)

    @staticmethod
    def hit(key: str, patterns: list[str]) -> bool:
        return any(fnmatch.fnmatchcase(key, p) for p in patterns)

    def host_for(self, key: str) -> list[str]:
        return [h for p, hs in self.hosts.items() if fnmatch.fnmatchcase(key, p) for h in hs]


@dataclass
class Row:
    entry: Entry
    cls: str
    action: str
    note: str = ""                       # what happens, in a few words (never a secret's value)
    hosts: tuple[str, ...] = ()
    fix: str = ""                        # for HOLD: what the person can do
    use: str | None = None


def plan_file(entries: list[Entry], *, overrides: Overrides, bound: dict[str, tuple[str, ...]],
              live: set[str], bindable: re.Pattern[str]) -> list[Row]:
    """What happens to each key. `bound`: the variables already bound for this environment's repository
    (name: hosts); `live`: those whose placeholder the open machine holds now."""
    rows = []
    for e in entries:
        rows.append(_row(e, entries, overrides, bound, live, bindable))
    return rows


def _row(e: Entry, entries: list[Entry], ov: Overrides, bound: dict[str, tuple[str, ...]], live: set[str],
         bindable: re.Pattern[str]) -> Row:
    key = e.key
    if ov.hit(key, ov.skip):
        return Row(e, "-", SKIP, "you said --skip")
    if ov.hit(key, ov.plain):
        return Row(e, PLAIN, COPY, PLAIN_SAID)
    if e.value == "":
        return Row(e, PLAIN, COPY, "empty")
    hosts = ov.host_for(key)
    if hosts or ov.hit(key, ov.secret):
        v = classify(key, e.value)
        use = (v.use or HEADER) if v.cls == SECRET else HEADER
        why = "you said --host" if hosts else "you said --secret"
    else:
        v = classify(key, e.value)
        if v.cls == PLAIN:
            return Row(e, PLAIN, COPY, v.why)
        use, why = v.use or HEADER, v.why
    when = "on the machine now" if key in live else "on the machine from its next open"
    if key in bound:
        note = f"placeholder, bound to {', '.join(bound[key])} ({when})"
        if use != HEADER and not hosts:
            note += f"; it may not work: {UNSUPPORTED[use][0]}"
        return Row(e, SECRET, PLACEHOLDER, note, bound[key], use=use)
    if use != HEADER and not hosts:
        reason, fix = UNSUPPORTED[use]
        return Row(e, SECRET, HOLD, f"not supported as a placeholder: {reason}", fix=fix.format(key=key), use=use)
    if not bindable.match(key):
        return Row(e, SECRET, HOLD, NOT_A_NAME[0], fix=NOT_A_NAME[1].format(key=key), use=use)
    if not hosts:
        found = infer_host(key, e.value, entries)
        if found is None:
            return Row(e, SECRET, HOLD, f"{why}; {NEEDS_HOST[0]}", fix=NEEDS_HOST[1].format(key=key), use=use)
        hosts, how = [found[0]], found[1]
        why = f"{why}; host from {how}"
    return Row(e, SECRET, BIND, f"bind to {', '.join(hosts)}, placeholder {when} ({why})", tuple(hosts), use=use)


def across_files(plans: list[list[Row]]) -> None:
    """The vault holds one value per name: a secret bound from one file is a placeholder in the next with
    the same value, and is not sent from a file that has another value for it."""
    first: dict[str, Row] = {}
    for rows in plans:
        for i, r in enumerate(rows):
            if r.action != BIND:
                continue
            had = first.get(r.entry.key)
            if had is None:
                first[r.entry.key] = r
            elif had.entry.value == r.entry.value:
                rows[i] = Row(r.entry, SECRET, PLACEHOLDER, f"placeholder, bound with the same value from "
                              f"another file to {', '.join(had.hosts)}", had.hosts, use=r.use)
            else:
                rows[i] = Row(r.entry, SECRET, HOLD, "another file has a different value for it, and the node "
                              "binds one value per name", fix=f"--skip {r.entry.key} in one of the files' pushes",
                              use=r.use)


# ---------------------------------------------------------------- what the person and the machine see

SHOWN = 32


def shown(value: str) -> str:
    """A plain value for the table: one line, at most SHOWN characters."""
    v = value.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    v = "".join(c if c.isprintable() else "?" for c in v)
    return v if len(v) <= SHOWN else v[:SHOWN - 3] + "..."


def table(rows: list[Row]) -> list[str]:
    """KEY  CLASS  WHAT HAPPENS, one line per key. A plain value is shown cut short; a secret's never."""
    cells = [("KEY", "CLASS", "WHAT HAPPENS")]
    for r in rows:
        if r.action == COPY and r.note == PLAIN_SAID:
            what = f"copy as it is ({PLAIN_SAID}; the value is not shown)"
        elif r.action == COPY:
            what = f'copy "{shown(r.entry.value)}"' + (f" ({r.note})" if r.note and r.note != "empty" else "")
        elif r.action in (HOLD, SKIP):
            what = f"{r.action}: {r.note}"
        else:
            what = r.note
        cells.append((r.entry.key, r.cls, what))
    width = [max(len(c[i]) for c in cells) for i in range(2)]
    return [f"{k:<{width[0]}}  {c:<{width[1]}}  {w}".rstrip() for k, c, w in cells]


def fixes(rows: list[Row]) -> list[str]:
    return [f"{r.entry.key}: {r.fix}" for r in rows if r.action == HOLD and r.fix]


HEADER_LINES = ("# Written by openajar (openajar environments env push) from the file of the same name on the",
                "# owner's computer. Secrets are not in it: KEY=${KEY} reads the placeholder that the machine's",
                "# environment holds, and the node puts the real value in for the bound host only.")


def render(rows: list[Row]) -> str:
    """The machine's file: plain values as written, bound secrets as KEY=${KEY}, the rest as a comment.
    Never a secret's value."""
    out = list(HEADER_LINES)
    for r in rows:
        e = r.entry
        lead = "export " if e.export else ""
        if r.action == COPY:
            out.append(f"{lead}{e.key}={e.raw}")
        elif r.action in (PLACEHOLDER, BIND):
            out.append(f"{lead}{e.key}=${{{e.key}}}")
        elif r.action == HOLD:
            out.append(f"# {e.key}: a secret, not sent by openajar (the owner's env push table says why)")
        else:
            out.append(f"# {e.key}: left out")
    return "\n".join(out) + "\n"


def counts(rows: list[Row]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r.action] = out.get(r.action, 0) + 1
    return out


def summary(path: str, rows: list[Row]) -> str:
    """One line for the environment's last calls: the file and how many keys went which way (key names
    of the secrets not sent, never a value)."""
    c = counts(rows)
    parts = [f"{n} {a}" for a, n in c.items()]
    held = [r.entry.key for r in rows if r.action == HOLD]
    tail = f"; not sent: {', '.join(held[:8])}" if held else ""
    return f"env push {path}: {', '.join(parts)}{tail}"
