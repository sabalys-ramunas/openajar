"""An element's own state, read before and after an action (aria-pressed, value, ...), and the
markers a signal reads. Password values are never read back, only their length.
"""

from __future__ import annotations

import json
from typing import Any

from openajar.node.human import quietly

ElementHandle = Any  # playwright.async_api.ElementHandle


# The target's own state. Password values are never read back, only their length.
_STATE_JS = """
(el) => {
  const s = {};
  for (const n of ['aria-pressed', 'aria-checked', 'aria-expanded', 'aria-selected', 'aria-current',
                   'aria-disabled', 'aria-valuenow']) {
    const v = el.getAttribute(n);
    if (v !== null) s[n] = v;
  }
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (tag === 'input' && (type === 'checkbox' || type === 'radio')) {
    s.checked = String(el.checked);
  } else if (tag === 'input' && type === 'password') {
    s['value length'] = String(el.value.length);
  } else if (tag === 'input' || tag === 'textarea') {
    s.value = el.value;
  } else if (tag === 'select') {
    s.selected = Array.from(el.selectedOptions, (o) => (o.label || o.textContent || '').trim()).join(', ');
  } else if (el.isContentEditable) {
    s.text = (el.innerText || el.textContent || '').trim();
  }
  if ('disabled' in el && el.disabled) s.disabled = 'true';
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
  const byIds = (ids) => clean(ids.split(/\\s+/).map((id) => {
    const node = el.getRootNode().getElementById ? el.getRootNode().getElementById(id) : document.getElementById(id);
    return node ? node.textContent : '';
  }).join(' '));
  let name = '';
  if (el.getAttribute('aria-labelledby')) name = byIds(el.getAttribute('aria-labelledby'));
  if (!name) name = clean(el.getAttribute('aria-label'));
  if (!name && el.labels && el.labels.length) name = clean(Array.from(el.labels, (l) => l.textContent).join(' '));
  if (!name && tag === 'img') name = clean(el.getAttribute('alt'));
  if (!name && !['input', 'textarea', 'select'].includes(tag)) name = clean(el.innerText || el.textContent);
  if (!name) name = clean(el.getAttribute('title') || el.getAttribute('placeholder'));
  s.name = name.slice(0, 200);
  return s;
}
"""

# Page facts for the signal (sites.PageFacts): iframe sources (absolute, via the src property) with
# whether they render at a real size on the page (not hidden, not transparent, not parked off the page
# like reCAPTCHA's idle challenge frame), script sources, and which marker elements (by id, passed in
# from sites.MARKER_SELECTORS) exist and show, and which widget token fields (sites.PASSED_FIELDS) hold a token
# (whether one does, never the token). Reads only; never the page's text.
_SHOWN_JS = """
(el, minPx) => {
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  return r.width >= minPx && r.height >= minPx && st.visibility !== 'hidden' && st.display !== 'none'
    && Number(st.opacity) > 0.05 && r.right + window.scrollX > 0 && r.bottom + window.scrollY > 0;
}
""".strip()
_MARKERS_JS = """
([selectors, minPx, fields]) => {
  const shown = (el) => (SHOWN)(el, minPx);
  const frames = Array.from(document.querySelectorAll('iframe[src], frame[src]')).slice(0, 300)
    .map((f) => ({src: f.src, visible: shown(f)}));
  const scripts = Array.from(document.querySelectorAll('script[src]')).slice(0, 300).map((s) => s.src);
  const elements = {};
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) elements[sel] = shown(el);
  }
  const passed = (fields || []).filter((name) =>
    Array.from(document.getElementsByName(name)).slice(0, 20).some((f) => typeof f.value === 'string' && f.value.length > 0));
  return {frames, scripts, elements, passed};
}
""".replace("SHOWN", _SHOWN_JS)

# Read-back keys in the order they are reported.
_STATE_ORDER = ("aria-pressed", "aria-checked", "checked", "aria-selected", "selected", "aria-expanded",
                "value", "value length", "text", "aria-valuenow", "aria-current", "aria-disabled",
                "disabled", "name")
_QUOTED = {"value", "text", "selected", "name"}


def _show(key: str, value: str | None) -> str:
    if value is None:
        return "(none)"
    if key in _QUOTED:
        if len(value) > 60:
            value = value[:57] + "..."
        return json.dumps(value, ensure_ascii=False)
    return value


def diff_state(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Human-readable changes of the target's own state, e.g. `aria-pressed false -> true`."""
    keys = [k for k in _STATE_ORDER if k in before or k in after]
    keys += sorted(k for k in set(before) | set(after) if k not in _STATE_ORDER)
    return [f"{k} {_show(k, before.get(k))} -> {_show(k, after.get(k))}"
            for k in keys if before.get(k) != after.get(k)]


async def _state(target: ElementHandle) -> dict[str, str] | None:
    """The target's own state, or None if it is gone (detached, or its document navigated away)."""
    if not await quietly(target.evaluate("(el) => el.isConnected"), False):
        return None
    return await quietly(target.evaluate(_STATE_JS))
