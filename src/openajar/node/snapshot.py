"""Accessibility outline with refs, built on Playwright's AI snapshot.

`page.aria_snapshot(mode="ai")` walks the page in Playwright's utility world (on Camoufox,
an isolated sandbox), never touches the DOM, and keeps a ref -> element map inside that
world. The `aria-ref=eN` selector resolves against the latest map, so refs stay valid until
the next snapshot or navigation and a stale ref simply matches nothing.

Playwright puts a ref on every visible node. This module turns its text into the outline
agents see: refs only on things you can act on, layout boxes dropped, and a size budget
that keeps the part of the page in view when the whole outline is too big.

Icon-only controls have no name, so an unnamed button, link, checkbox, menuitem, tab, option
or textbox gets a hint read from its own attributes (see `hint_from_facts`). Each hint costs
one read of that node, and the reads stop at HINT_LIMIT nodes or HINT_BUDGET_MS, whichever
comes first. A text box shows its value only when that value is known not to be a password.

Line shapes (indented two spaces per level):
    - button "Like 12" [pressed] [ref=e12] [cursor=pointer] [box=277,271,74,35]
    - button [ref=e13] [cursor=pointer] (hint: upvote)
    - heading "Feed" [level=1]
    - paragraph: some text
    - text: loose text
    - /url: /index.html
A key that YAML would misread is wrapped in single quotes, with ' doubled inside.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import weakref
from collections import Counter, OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from openajar.core.models import SnapshotResult
from openajar.node.human import quietly

Page = Any  # playwright.async_api.Page

MAX_CHARS = 40_000
SNAPSHOT_TIMEOUT_MS = 15_000

ACTIONABLE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "switch", "combobox",
    "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "slider",
    "spinbutton", "treeitem",
})

# An image outside anything actionable keeps its ref, so hover reaches one that only reacts to
# the pointer (a caption or a card that shows on hover: QA 2026-11-10 #11). An icon inside a
# button or a link does not: the control's own ref is the one to use.
HOVER_ROLES = frozenset({"img", "image"})

# Unnamed nodes of these roles get a hint. Text boxes also need a check: their value is the
# text after the colon, and it is hidden unless the element is known not to be a password.
HINT_ROLES = frozenset({"button", "link", "checkbox", "menuitem", "tab", "option", "textbox"})
VALUE_ROLES = frozenset({"textbox"})
HINT_LIMIT = 80          # nodes read per snapshot, hints and value checks together
HINT_BUDGET_MS = 1_500   # reads still pending after this are dropped, not awaited
HINT_MAX_CHARS = 40
HINT_PARTS = 3           # distinct words groups joined into one hint

REF_RE = re.compile(r"^(?:f\d+)?e\d+$")
# A site's "are you human" check in an environment's browser (openajar.node.interaction.checks): c1, c2...
CHECK_REF_RE = re.compile(r"^c\d+$")


def is_check_ref(ref: str) -> bool:
    return bool(CHECK_REF_RE.match(ref or ""))
_LINE_RE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_ROLE_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_JSON = json.JSONDecoder()

# Words that name what an element does, for class names like `open-menu` or `vote-up`.
_ACTION_WORDS = frozenset({
    "upvote", "downvote", "vote", "like", "unlike", "share", "save", "reply", "comment", "follow",
    "subscribe", "delete", "remove", "edit", "close", "dismiss", "open", "expand", "collapse",
    "toggle", "menu", "more", "search", "submit", "send", "copy", "download", "print", "play",
    "pause", "mute", "unmute", "next", "previous", "prev", "back", "forward", "refresh", "reload",
    "zoom", "fullscreen", "add", "hide", "show", "info", "help", "settings", "filter", "sort",
    "login", "logout", "cart", "bookmark", "favorite", "favourite", "report", "block", "undo", "redo",
})
# Class-name words that say nothing about the action (`icon-button` is only an icon).
_GENERIC_CLASS_WORDS = frozenset({
    "button", "btn", "icon", "wrapper", "wrap", "container", "action", "actions", "control", "item",
    "root", "inner", "outer", "box", "el", "element", "component", "link", "cta", "trigger", "ui",
})
# Trailing icon style words (`arrow-outline`, `upvote-filled`) that are noise in a hint.
_STYLE_WORDS = frozenset({
    "outline", "outlined", "filled", "fill", "solid", "line", "regular", "bold", "linear", "stroke",
})
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_NOT_WORD = re.compile(r"[\W_]+")
_PLAIN_WORDS = re.compile(r"[a-z]+(?: [a-z]+)*")


@dataclass(frozen=True, slots=True)
class OutlineLine:
    raw: str
    depth: int
    role: str | None = None              # None for text, property and unparsed lines
    name: str | None = None
    attrs: tuple[str, ...] = ()          # e.g. ("pressed", "ref=e12", "cursor=pointer")
    attrs_span: tuple[int, int] | None = None  # where " [a] [b]" sits in raw

    def attr(self, key: str) -> str | None:
        prefix = key + "="
        for a in self.attrs:
            if a.startswith(prefix):
                return a[len(prefix):]
        return None

    @property
    def ref(self) -> str | None:
        return self.attr("ref")

    @property
    def box(self) -> tuple[float, float, float, float] | None:
        value = self.attr("box")
        if not value:
            return None
        try:
            x, y, w, h = (float(v) for v in value.split(","))
        except ValueError:
            return None
        return x, y, w, h

    @property
    def actionable(self) -> bool:
        return self.role in ACTIONABLE_ROLES or "cursor=pointer" in self.attrs


def parse_line(raw: str) -> OutlineLine:
    """Split one outline line into role, name and [attributes], keeping their position in raw."""
    m = _LINE_RE.match(raw)
    if not m:
        return OutlineLine(raw=raw, depth=(len(raw) - len(raw.lstrip(" "))) // 2)
    depth = len(m["indent"]) // 2
    pos = m.start("body")
    quoted = raw.startswith("'", pos)
    if quoted:
        pos += 1
    role_m = _ROLE_RE.match(raw, pos)
    if not role_m or raw.startswith("text:", pos) or raw.startswith("/", pos):
        return OutlineLine(raw=raw, depth=depth)
    role = role_m.group(0)
    pos = role_m.end()
    name = None
    if raw.startswith(' "', pos):
        try:
            name, end = _JSON.raw_decode(raw, pos + 1)
        except ValueError:
            return OutlineLine(raw=raw, depth=depth, role=role)
        if quoted:
            name = name.replace("''", "'")
        pos = end
    attrs: list[str] = []
    start = pos
    while raw.startswith(" [", pos):
        close = raw.find("]", pos)
        if close < 0:
            break
        attrs.append(raw[pos + 2:close])
        pos = close + 1
    return OutlineLine(raw=raw, depth=depth, role=role, name=name, attrs=tuple(attrs),
                       attrs_span=(start, pos))


def render_line(line: OutlineLine, hint: str | None = None, *, value: bool = True, ref: bool | None = None) -> str:
    """The line as agents see it: no layout box, a ref only if the node is actionable (or `ref`
    says to keep it), the hint after the attributes, and the text after the attributes (a text
    box's value) only if `value`."""
    if line.attrs_span is None:
        return line.raw
    show_ref = line.actionable if ref is None else ref
    keep = [a for a in line.attrs
            if not a.startswith("box=") and (show_ref or not a.startswith("ref="))]
    start, end = line.attrs_span
    suffix = f" (hint: {hint})" if hint else ""
    tail = line.raw[end:] if value else ""
    return line.raw[:start] + "".join(f" [{a}]" for a in keep) + suffix + tail


def find_refs(text: str, role: str | None = None, name: str | None = None,
              within: str | None = None) -> list[str]:
    """Refs in an outline, filtered by role, exact name, and the exact name of an ancestor
    (`within`). Only direct content of that ancestor counts: a match inside a nested node with
    the same role as the ancestor (for example a comment article inside a post article) is
    excluded, so `within` never reaches into nested posts or comments."""
    lines = [parse_line(raw) for raw in text.splitlines()]
    out: list[str] = []
    stack: list[OutlineLine] = []
    for line in lines:
        while stack and stack[-1].depth >= line.depth:
            stack.pop()
        if line.role is not None:
            ok = line.ref is not None
            ok = ok and (role is None or line.role == role) and (name is None or line.name == name)
            if ok and within is not None:
                owners = [i for i, a in enumerate(stack) if a.name == within]
                if not owners:
                    ok = False
                else:
                    owner = stack[owners[-1]]
                    ok = not any(a.role == owner.role for a in stack[owners[-1] + 1:])
            if ok:
                out.append(line.ref)
            stack.append(line)
    return out


@dataclass(slots=True)
class _Node:
    line: OutlineLine
    parent: int | None
    in_frame: int | None = None  # index of the nearest iframe ancestor
    y: tuple[float, float] | None = None  # top and bottom, viewport relative


def _nodes(lines: list[OutlineLine]) -> list[_Node]:
    nodes: list[_Node] = []
    stack: list[int] = []
    for i, line in enumerate(lines):
        while stack and lines[stack[-1]].depth >= line.depth:
            stack.pop()
        parent = stack[-1] if stack else None
        in_frame = None
        if parent is not None:
            in_frame = parent if lines[parent].role == "iframe" else nodes[parent].in_frame
        box = line.box
        nodes.append(_Node(line=line, parent=parent, in_frame=in_frame,
                           y=(box[1], box[1] + box[3]) if box else None))
        stack.append(i)
    return nodes


def _vertical_span(nodes: list[_Node], i: int) -> tuple[float, float] | None:
    """Where the node sits vertically in the main viewport. Lines inside an iframe use the
    iframe's own box (their boxes are relative to the frame); lines without a box use their
    nearest ancestor's."""
    node = nodes[i]
    if node.in_frame is not None:
        return _vertical_span(nodes, node.in_frame)
    while node.y is None and node.parent is not None:
        node = nodes[node.parent]
    return node.y


def _select(nodes: list[_Node], spans: list[tuple[float, float] | None], lo: float, hi: float) -> list[bool]:
    keep = [False] * len(nodes)
    for i, span in enumerate(spans):
        if span is None:
            hit = True
        else:
            top, bottom = span
            hit = (lo <= top < hi) if bottom <= top else (top < hi and bottom > lo)
        if hit and not keep[i]:
            j: int | None = i
            while j is not None and not keep[j]:
                keep[j] = True
                j = nodes[j].parent
    return keep


def _cost(keep: list[bool], costs: list[int]) -> int:
    return sum(c for k, c in zip(keep, costs, strict=True) if k)


def _hintable(line: OutlineLine) -> bool:
    return line.ref is not None and line.role in HINT_ROLES and not line.name


def _has_value(line: OutlineLine) -> bool:
    """A text box's value is the text after its attributes: `textbox "Pin" [ref=e2]: hunter2`."""
    return line.attrs_span is not None and line.raw[line.attrs_span[1]:].startswith(":")


def _probed(line: OutlineLine) -> bool:
    """Nodes whose facts are read in a snapshot: unnamed hint targets and text boxes with a value."""
    return line.ref is not None and (_hintable(line) or (line.role in VALUE_ROLES and _has_value(line)))


def _hover_refs(lines: list[OutlineLine]) -> list[bool]:
    """Which lines keep a ref: the actionable ones, and images with nothing actionable above them."""
    out: list[bool] = []
    stack: list[tuple[int, bool]] = []  # (depth, actionable or inside something actionable)
    for line in lines:
        while stack and stack[-1][0] >= line.depth:
            stack.pop()
        inside = bool(stack) and stack[-1][1]
        out.append(line.actionable or (line.role in HOVER_ROLES and line.ref is not None and not inside))
        stack.append((line.depth, inside or line.actionable))
    return out


def _render_for(line: OutlineLine, facts: Mapping[str, Mapping[str, Any]] | None, ref: bool | None = None) -> str:
    if line.role in VALUE_ROLES and line.attrs_span is None:
        return line.raw.split(":", 1)[0]  # could not split the line: keep only what precedes a colon
    known = facts.get(line.ref) if facts and line.ref else None
    hint = hint_from_facts(known) if _hintable(line) else None
    hides_value = (line.role in VALUE_ROLES and _has_value(line)
                   and (known is None or known.get("input_type") == "password"))
    return render_line(line, hint, value=not hides_value, ref=ref)


def _outline(raw: str, *, viewport_height: float | None, max_chars: int,
             facts: Mapping[str, Mapping[str, Any]] | None) -> tuple[str, int, bool, list[OutlineLine]]:
    """The outline, its ref count, whether it was truncated, and the lines it shows."""
    lines = [parse_line(r) for r in raw.splitlines() if r.strip()]
    with_ref = _hover_refs(lines)
    rendered = [_render_for(line, facts, ref) for line, ref in zip(lines, with_ref, strict=True)]
    costs = [len(r) + 1 for r in rendered]

    budget = max_chars * 0.97  # room for the "not shown" lines
    if sum(costs) <= max_chars:
        keep = [True] * len(lines)
        truncated = False
    else:
        truncated = True
        allowed = [True] * len(lines)
        keep = None
        if viewport_height and viewport_height > 0:
            nodes = _nodes(lines)
            spans = [_vertical_span(nodes, i) for i in range(len(nodes))]
            vh = float(viewport_height)
            for factor in (0, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32):
                candidate = _select(nodes, spans, -vh * factor / 2, vh * (1 + factor))
                if _cost(candidate, costs) > budget:
                    if keep is None:
                        allowed = candidate  # even the view alone is too big: cut it, top first
                    break
                keep = candidate
        if keep is None:
            keep, used = [False] * len(lines), 0
            for i, c in enumerate(costs):
                if not allowed[i]:
                    continue
                if used + c > budget:
                    break
                keep[i], used = True, used + c

    out: list[str] = []
    shown: list[OutlineLine] = []
    refs = 0
    skipped, skipped_depth = 0, 0

    def flush() -> None:
        nonlocal skipped
        if skipped:
            out.append(f"{'  ' * skipped_depth}- ... {skipped} lines not shown (scroll, then snapshot again)")
            skipped = 0

    for line, text, k, has_ref in zip(lines, rendered, keep, with_ref, strict=True):
        if not k:
            if not skipped:
                skipped_depth = line.depth
            skipped += 1
            continue
        flush()
        out.append(text)
        shown.append(line)
        if has_ref and line.ref:
            refs += 1
    flush()
    return "\n".join(out), refs, truncated, shown


def build_outline(raw: str, *, viewport_height: float | None = None,
                  max_chars: int = MAX_CHARS,
                  facts: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[str, int, bool]:
    """Playwright AI snapshot text -> (outline, ref count, truncated).

    Over budget, keep what is in view and grow a window around the viewport (more below than
    above) while it fits; ancestors of kept lines always stay so the structure reads right.
    Skipped runs become one `- ... N lines not shown` line. Without a viewport height (or if
    even the viewport alone is too big) the outline is cut at the budget, top first.

    `facts` maps refs to what `collect_facts` read from the page. Without them an unnamed
    node has no hint, and a text box's value is hidden."""
    text, refs, truncated, _ = _outline(raw, viewport_height=viewport_height, max_chars=max_chars, facts=facts)
    return text, refs, truncated


# ---- main content only ---------------------------------------------------------------------

# The page's frame around its content: site header, menus, side columns and footer. With
# main_only each becomes one line with a count (QA 2026-11-10 #15: a Reddit profile's outline was
# about 40 KB, most of it navigation). The default outline keeps them whole.
FRAME_LANDMARKS = frozenset({"banner", "navigation", "complementary", "contentinfo"})
MAIN_ONLY_NOTE = "main content only: page header, menus, side columns and footer are folded to one line each"


def fold_landmarks(text: str) -> tuple[str, int, int]:
    """(the outline with each outermost frame landmark as one line, lines folded, refs folded).
    A folded line keeps the landmark's role and name and says what it holds; snapshot again
    without main_only to see it."""
    out: list[str] = []
    folded_lines = folded_refs = 0
    fold: tuple[int, int, int, OutlineLine] | None = None  # (depth, lines, refs, the landmark line)

    def close() -> None:
        nonlocal fold
        if fold is None:
            return
        depth, n, r, head = fold
        name = f" {json.dumps(head.name, ensure_ascii=False)}" if head.name else ""
        what = f"{n} line{'s' if n != 1 else ''}" + (f", {r} ref{'s' if r != 1 else ''}" if r else "")
        out.append(f"{'  ' * depth}- {head.role}{name} (folded: {what})")
        fold = None

    for raw in text.splitlines():
        line = parse_line(raw)
        if fold is not None:
            if raw.strip() and line.depth > fold[0]:
                fold = (fold[0], fold[1] + 1, fold[2] + (1 if line.ref else 0), fold[3])
                folded_lines += 1
                folded_refs += 1 if line.ref else 0
                continue
            close()
        if line.role in FRAME_LANDMARKS:
            fold = (line.depth, 0, 1 if line.ref else 0, line)
            folded_refs += 1 if line.ref else 0
            continue
        out.append(raw)
    close()
    return "\n".join(out), folded_lines, folded_refs


# ---- hints ---------------------------------------------------------------------------------

# Read-only facts about one node: its own attributes, its open shadow root (a closed root reads
# as null) and a bounded part of its subtree. Runs in the isolated world, so the page never sees
# it. A password input returns its type and nothing else: its attributes and text are not read.
_FACTS_JS = """
(el) => {
  const type = (el.getAttribute('type') || '').trim().toLowerCase();
  if (type === 'password') return {input_type: type};
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  const facts = {input_type: type};

  const root = el.shadowRoot;
  if (root) {
    for (const node of Array.from(root.querySelectorAll('[aria-label], [aria-labelledby]')).slice(0, 30)) {
      let label = clean(node.getAttribute('aria-label'));
      if (!label && node.getAttribute('aria-labelledby')) {
        label = clean(node.getAttribute('aria-labelledby').split(/\\s+/).map((id) => {
          const target = root.getElementById(id);
          return target ? target.textContent : '';
        }).join(' '));
      }
      if (label) { facts.shadow_label = label; break; }
    }
  }

  facts.title = clean(el.getAttribute('title')) || null;
  const svgTitle = el.querySelector('svg title');
  facts.svg_title = (svgTitle && clean(svgTitle.textContent)) || null;
  const hidden = el.querySelector('.sr-only, .visually-hidden, .visuallyhidden, .screen-reader-text');
  facts.hidden = (hidden && clean(hidden.textContent)) || null;
  facts.classes = Array.from(el.classList).slice(0, 20);

  const icons = {};
  const nodes = [el];
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT);
  while (nodes.length < 200 && walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const formField = ['input', 'textarea', 'select'].includes(node.tagName.toLowerCase());
    for (const attr of ['icon-name', 'data-icon', 'name', 'data-testid', 'data-action']) {
      if (attr === 'name' && formField) continue;
      const value = node.getAttribute(attr);
      if (value && !(attr in icons)) icons[attr] = clean(value);
    }
  }
  facts.icons = icons;

  const standard = new Set(['allowfullscreen', 'async', 'autofocus', 'autoplay', 'checked', 'contenteditable',
    'controls', 'default', 'defer', 'disabled', 'draggable', 'formnovalidate', 'hidden', 'inert', 'ismap',
    'itemscope', 'loop', 'multiple', 'muted', 'novalidate', 'open', 'playsinline', 'readonly', 'required',
    'reversed', 'selected', 'spellcheck', 'translate']);
  facts.flags = Array.from(el.attributes)
    .filter((a) => a.value === '' && /^[a-z][a-z-]{1,23}$/.test(a.name) && !standard.has(a.name)
      && !/^(aria-|data-|on)/.test(a.name))
    .map((a) => a.name)
    .slice(0, 8);
  return facts;
}
"""


def _normalize(value: object) -> str:
    """A label in lowercase words: `upVote-outline` -> `up vote`. Empty for anything that is not
    words: no letters, a token-like word (a long random string or a hash), or nothing at all."""
    if not isinstance(value, str):
        return ""
    words = [w for w in _NOT_WORD.sub(" ", _CAMEL.sub(" ", value).lower()).split() if not _looks_secret(w)]
    while len(words) > 1 and words[-1] in _STYLE_WORDS:
        words.pop()
    if not any(c.isalpha() for c in " ".join(words)):
        return ""
    return " ".join(words)


def _looks_secret(word: str) -> bool:
    """Tokens and IDs are not labels: very long words, or long mixes of letters and digits."""
    if len(word) > 24:
        return True
    return len(word) >= 12 and any(c.isdigit() for c in word) and any(c.isalpha() for c in word)


def _merge(values: Iterable[object]) -> str:
    """Join the distinct labels of one source, in order. A label that repeats a word already
    taken is dropped, so `upvote` and `upvote-outline` make one `upvote`, not two."""
    seen: set[str] = set()
    parts: list[str] = []
    for value in values:
        text = _normalize(value)
        words = text.split()
        if not words or any(w in seen for w in words):
            continue
        seen.update(words)
        parts.append(text)
    return " / ".join(parts[:HINT_PARTS])


def _action_class(classes: Iterable[object]) -> str | None:
    """The first class that names an action in plain words (`open-menu`), not a hash or a utility."""
    for name in classes:
        words = [w for w in _normalize(name).split() if w not in _GENERIC_CLASS_WORDS]
        text = " ".join(words)
        if 0 < len(words) <= 4 and _PLAIN_WORDS.fullmatch(text) and _ACTION_WORDS.intersection(words):
            return text
    return None


def _action_flags(flags: Iterable[object]) -> list[str]:
    """Boolean attributes that name an action (`upvote`). Design-system and state flags
    (Reddit's `rpl`, `actioned`, `leading`) say nothing about what the button does."""
    return [f for f in flags if isinstance(f, str) and _ACTION_WORDS.intersection(_normalize(f).split())]


def _fit(text: str) -> str:
    """At most HINT_MAX_CHARS, cut at a word boundary when one is near."""
    if len(text) <= HINT_MAX_CHARS:
        return text
    head = text[:HINT_MAX_CHARS]
    if text[HINT_MAX_CHARS] != " ":
        head = head.rpartition(" ")[0] or head
    return head.rstrip(" /")


def hint_from_facts(facts: Mapping[str, Any] | None) -> str | None:
    """The hint for one unnamed node, from what `_FACTS_JS` read. Sources run in priority order,
    and the first one with a usable value gives the hint: the open shadow root's label, the
    title, an svg title, icon attributes with the element's own boolean attributes (`upvote`),
    a class that names an action, then visually hidden text. Never a password, and never the
    value of an input: the facts do not carry one."""
    if not facts or facts.get("input_type") == "password":
        return None
    icons = facts.get("icons") or {}
    sources = (
        (facts.get("shadow_label"),),
        (facts.get("title"),),
        (facts.get("svg_title"),),
        (icons.get("icon-name"), icons.get("data-icon"), icons.get("name"),
         *_action_flags(facts.get("flags") or ()), icons.get("data-testid"), icons.get("data-action")),
        (_action_class(facts.get("classes") or ()),),
        (facts.get("hidden"),),
    )
    for values in sources:
        text = _merge(values)
        if text:
            return _fit(text)
    return None


async def _read_facts(page: Page, ref: str, deadline: float) -> dict[str, Any] | None:
    loop = asyncio.get_running_loop()
    timeout_ms = max(1.0, (deadline - loop.time()) * 1000)
    return await quietly(page.locator(f"aria-ref={ref}").evaluate(_FACTS_JS, timeout=timeout_ms))


async def collect_facts(page: Page, refs: Sequence[str], *,
                        budget_ms: float = HINT_BUDGET_MS) -> dict[str, dict[str, Any]]:
    """Read the facts of up to HINT_LIMIT nodes, all in flight at once, within `budget_ms`.

    Each read is a locator evaluate, which only reads. Reads still pending when the budget runs
    out are cancelled and not awaited, so those nodes simply get no hint (and a text box with a
    value keeps it hidden). Refs that cannot be read are left out the same way."""
    targets = list(dict.fromkeys(refs))[:HINT_LIMIT]
    if not targets:
        return {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_ms / 1000
    tasks = {ref: asyncio.ensure_future(_read_facts(page, ref, deadline)) for ref in targets}
    await asyncio.wait(tasks.values(), timeout=max(0.0, deadline - loop.time()))
    for task in tasks.values():
        if not task.done():
            task.cancel()
    return {ref: task.result() for ref, task in tasks.items()
            if task.done() and not task.cancelled() and task.result() is not None}


async def take_snapshot(page: Page, *, max_chars: int = MAX_CHARS) -> SnapshotResult:
    """Snapshot the page without changing it. Refs from earlier snapshots stop resolving."""
    raw = await page.aria_snapshot(mode="ai", boxes=True, timeout=SNAPSHOT_TIMEOUT_MS) or ""
    viewport_height = await quietly(page.evaluate("window.innerHeight"))
    text, ref_count, truncated, shown = _outline(raw, viewport_height=viewport_height,
                                                 max_chars=max_chars, facts=None)
    targets = [line.ref for line in shown if _probed(line)]
    if targets:
        facts = await collect_facts(page, targets)
        if facts:
            text, ref_count, truncated, _ = _outline(raw, viewport_height=viewport_height,
                                                     max_chars=max_chars, facts=facts)
    title = await quietly(page.title(), "")
    return SnapshotResult(url=page.url, title=title, text=text, ref_count=ref_count, truncated=truncated)


# ---- deltas --------------------------------------------------------------------------------
#
# A delta is what changed between the outline an agent last saw of a tab and a fresh one, so an
# action can report its effect in a few lines instead of a whole page. Playwright keeps an
# element's ref while its role and name stay the same, so unchanged lines carry the refs the
# agent already has; a line whose element changed name or was re-rendered gets a new ref, and
# the delta shows it.

DELTA_MAX_SHARE = 0.6     # a delta longer than this share of the full outline falls back to it
DELTA_CONTEXT = 2         # named ancestors shown above a run of changes
DELTA_GONE_RUN = 8        # a longer run of lines that went away is told as a count
MEMORY_PER_PAGE = 8       # agents remembered per tab
_HINT_RE = re.compile(r" \(hint: [^()]*\)")
_ACTIVE = " [active]"
_DELTA_LINE_CHARS = 400


@dataclass(frozen=True, slots=True)
class OutlineDelta:
    text: str      # the change lines, grouped under their context; empty when nothing changed
    changed: int
    added: int
    removed: int

    @property
    def empty(self) -> bool:
        return not (self.changed or self.added or self.removed)

    def summary(self) -> str:
        if self.empty:
            return "nothing changed"
        return f"{self.changed} changed, {self.added} new, {self.removed} gone"


@dataclass(frozen=True, slots=True)
class _Entry:
    mark: str                   # "+" new, "-" gone, "~" changed
    line: OutlineLine           # the line as it is now (as it was, for "-")
    index: int                  # its position in its own outline
    was: OutlineLine | None = None
    folded: int = 0             # "-" only: the lines of a long run this one stands for


def _compare_key(raw: str) -> str:
    """Hints depend on a time-boxed read, and focus ([active]) moves with every click, so a line
    that only gained or lost one of them is not a change."""
    return _HINT_RE.sub("", raw).replace(_ACTIVE, "")


def _parents(lines: Sequence[OutlineLine]) -> list[int | None]:
    out: list[int | None] = []
    stack: list[int] = []
    for i, line in enumerate(lines):
        while stack and lines[stack[-1]].depth >= line.depth:
            stack.pop()
        out.append(stack[-1] if stack else None)
        stack.append(i)
    return out


def _pair(old: Sequence[OutlineLine], i1: int, i2: int, new: Sequence[OutlineLine], j1: int,
          j2: int) -> list[_Entry]:
    """Inside a replaced block, a new line and an old one are the same element when they share a
    ref, or else the same depth and role (in order). The rest appeared or went away."""
    taken: set[int] = set()
    entries: list[_Entry] = []
    floor = i1
    for j in range(j1, j2):
        line = new[j]
        match = None
        if line.ref is not None:
            match = next((i for i in range(i1, i2) if i not in taken and old[i].ref == line.ref), None)
        if match is None:
            match = next((i for i in range(floor, i2) if i not in taken and old[i].depth == line.depth
                          and old[i].role == line.role), None)
        if match is None:
            entries.append(_Entry("+", line, j))
        else:
            taken.add(match)
            floor = max(floor, match + 1)
            entries.append(_Entry("~", line, j, was=old[match]))
    entries += [_Entry("-", old[i], i) for i in range(i1, i2) if i not in taken]
    return entries


def _fold_gone(entries: Sequence[_Entry]) -> list[_Entry]:
    """Scrolling a long page moves the part of it the outline shows, so whole screens of lines
    leave it at once. They are not news, and repeating them cost a feed more than what was new:
    a long run of "-" lines becomes one line with its count. A short run is what the agent did
    (a button that went away), so it stays."""
    out: list[_Entry] = []
    run: list[_Entry] = []

    def flush() -> None:
        if len(run) > DELTA_GONE_RUN:
            out.append(replace(run[0], folded=len(run)))
        else:
            out.extend(run)
        run.clear()

    for e in entries:
        if e.mark != "-" or (run and e.index != run[-1].index + 1):
            flush()
        if e.mark == "-":
            run.append(e)
        else:
            out.append(e)
    flush()
    return out


def _body(line: OutlineLine) -> str:
    text = line.raw.strip()
    text = text.removeprefix("- ")
    return text if len(text) <= _DELTA_LINE_CHARS else text[:_DELTA_LINE_CHARS - 3] + "..."


def _label(line: OutlineLine) -> str:
    if line.role is None:
        return _body(line).rstrip(":")
    return line.role + (f" {json.dumps(line.name, ensure_ascii=False)}" if line.name else "")


def _context(lines: Sequence[OutlineLine], parents: Sequence[int | None], index: int, skip: set[int]) -> str:
    """Where a change sits: its nearest named ancestors that are not part of the change."""
    named: list[str] = []
    nearest: str | None = None
    at = parents[index]
    while at is not None and len(named) < DELTA_CONTEXT:
        if at not in skip:
            line = lines[at]
            nearest = nearest or _label(line)
            if line.name:
                named.append(_label(line))
        at = parents[at]
    if named:
        return " > ".join(reversed(named))
    return nearest or "page"


def outline_delta(old: str, new: str, *, max_share: float = DELTA_MAX_SHARE) -> OutlineDelta | None:
    """The lines of `new` that are new or changed since `old`, and the lines of `old` that are
    gone, grouped under the named ancestors they sit in. None when the delta would be longer
    than `max_share` of `new`: then the full outline is the better answer.

        in article "Post by Ada Lovelace":
        ~ button "Like 13" [pressed] [ref=e40] [cursor=pointer] (was: button "Like 12" [ref=e12] ...)
        + paragraph: Liked by you
        in main:
        - button "Load more" [ref=e33]
    """
    a = [parse_line(raw) for raw in old.splitlines() if raw.strip()]
    b = [parse_line(raw) for raw in new.splitlines() if raw.strip()]
    matcher = difflib.SequenceMatcher(None, [_compare_key(x.raw) for x in a], [_compare_key(x.raw) for x in b],
                                      autojunk=False)
    entries: list[_Entry] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete":
            entries += [_Entry("-", a[i], i) for i in range(i1, i2)]
        elif tag == "insert":
            entries += [_Entry("+", b[j], j) for j in range(j1, j2)]
        elif tag == "replace":
            entries += _pair(a, i1, i2, b, j1, j2)
    if not entries:
        return OutlineDelta("", 0, 0, 0)

    pa, pb = _parents(a), _parents(b)
    gone = {e.index for e in entries if e.mark == "-"}
    added = {e.index for e in entries if e.mark == "+"}
    out: list[str] = []
    last_context: str | None = None
    run_mark, run_depth = "", 0
    for e in _fold_gone(entries):
        lines, parents, skip = (a, pa, gone) if e.mark == "-" else (b, pb, added)
        context = _context(lines, parents, e.index, skip)
        if context != last_context:
            out.append(f"in {context}:")
            last_context, run_mark = context, ""
        if e.mark != run_mark:
            run_mark, run_depth = e.mark, e.line.depth
        indent = "  " * max(0, e.line.depth - run_depth) if e.mark != "~" else ""
        text = f"{e.mark} {indent}{_body(e.line)}"
        if e.folded:
            text = f"- ... {e.folded} lines left the outline (scrolled out of it, or gone)"
        if e.was is not None:
            text += f" (was: {_body(e.was)})"
        out.append(text)
    text = "\n".join(out)
    if len(text) > max_share * max(len(new), 1):
        return None
    counts = Counter(e.mark for e in entries)
    return OutlineDelta(text, changed=counts["~"], added=counts["+"], removed=counts["-"])


@dataclass(frozen=True, slots=True)
class SeenOutline:
    url: str
    text: str  # the page part of the outline (no dialog or tab notices)


class OutlineMemory:
    """The last outline each agent was shown of each tab, so its next one can be a delta. Keyed
    weakly by the page, so a closed tab (or a lane that went cold) forgets; at most
    MEMORY_PER_PAGE agents per tab, the least recent dropped first."""

    def __init__(self, per_page: int = MEMORY_PER_PAGE):
        self.per_page = per_page
        self._seen: weakref.WeakKeyDictionary[Any, OrderedDict[str, SeenOutline]] = weakref.WeakKeyDictionary()

    def get(self, page: Any, who: str) -> SeenOutline | None:
        try:
            return (self._seen.get(page) or {}).get(who)
        except TypeError:  # not weak-referenceable (a test double)
            return None

    def remember(self, page: Any, who: str, url: str, text: str) -> None:
        try:
            seen = self._seen.setdefault(page, OrderedDict())
        except TypeError:
            return
        seen.pop(who, None)
        seen[who] = SeenOutline(url=url, text=text)
        while len(seen) > self.per_page:
            seen.popitem(last=False)

    def forget(self, page: Any) -> None:
        try:
            self._seen.pop(page, None)
        except TypeError:
            pass


OUTLINES = OutlineMemory()


# ---- text in the tree (waits) ----------------------------------------------------------------

# Roles whose text after the colon is what was typed or picked (a password, a search), not
# what the page says: never matched, never shown.
_INPUT_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton", "slider"})


def fold(text: str) -> str:
    """Text as waits compare it: spacing collapsed, case folded."""
    return " ".join(text.split()).casefold()


def outline_texts(raw: str) -> list[tuple[str, str]]:
    """Every accessible name and text in an AI snapshot as (folded text, the line as an agent sees
    it: no box, no field value). Link targets (`/url:`) are not text."""
    out: list[tuple[str, str]] = []
    for raw_line in raw.splitlines():
        if not raw_line.strip():
            continue
        line = parse_line(raw_line)
        texts: list[str] = []
        if line.role is None:
            body = raw_line.strip()
            body = body.removeprefix("- ")
            if body.startswith("text:"):
                texts.append(body[5:])
            elif not body.startswith("/"):
                texts.append(body)
            shown = body
        else:
            if line.name:
                texts.append(line.name)
            keep_value = line.role not in _INPUT_ROLES
            tail = raw_line[line.attrs_span[1]:] if line.attrs_span is not None else ""
            if keep_value and tail.startswith(":"):
                texts.append(tail[1:])
            rendered = render_line(line, value=keep_value)
            if tail.strip() == ":":  # a container: its children follow, the colon says nothing
                rendered = rendered.rstrip().removesuffix(":")
            shown = _body(OutlineLine(raw=rendered, depth=0))
        for text in texts:
            folded = fold(text)
            if folded:
                out.append((folded, shown[:_DELTA_LINE_CHARS]))
    return out


def find_text(texts: Sequence[tuple[str, str]], want: str, *, exact: bool = False) -> str | None:
    """The first line whose name or text contains `want` (equals it, with `exact`), case and
    spacing ignored."""
    target = fold(want)
    for folded, shown in texts:
        if folded == target or (not exact and target in folded):
            return shown
    return None
