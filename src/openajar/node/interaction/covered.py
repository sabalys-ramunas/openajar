"""What covers a target a click could not reach (human.TargetCovered), in the words an agent acts on.

"another element covers the target" alone left agents stuck behind cookie banners, sign-in promos and location
choosers (the 2026-11-24 sites run: the search step on 5 sites), since they could not tell what to close. The
press already tested a point with elementFromPoint (human._aim_once keeps it on the error); here the element at
that point is read once more, with its ancestors' roles and names, and matched to the refs of the last outline
an agent saw of the tab (snapshot.OUTLINES):

- the covering element's ref, or the ref of its closest ancestor that has one, checked on the page (the ref's
  element is that element or holds it), so a ref that only shares a name is never named;
- else its role and name, and the refs of up to MAX_BUTTONS buttons or links inside the covering layer (the
  dialog, or the fixed or sticky box it sits in) that the outline has, so "Accept" or "Close" is one step away;
- with no outline yet, or nothing that matches: its role and name, and "snapshot again".

The words go on the error's message (what an agent reads) and, as data, on its `details["covered_by"]`.
Nothing here clicks or changes the page: two reads, and at most MAX_CHECKS ref checks.
"""

from __future__ import annotations

import re
from typing import Any

from openajar.node.human import quietly

MAX_CHECKS = 8
MAX_BUTTONS = 3
READ_TIMEOUT_S = 3.0

# The element at (x, y) of the top page (into open shadow roots), and up to 8 ancestors: role and name each, the
# covering layer (a dialog, or the highest fixed or sticky box), and its buttons and links. Roles and names are a
# close guess at the accessibility tree's, enough to find the outline's line, which a ref check then confirms.
_AT_POINT_JS = """
([x, y]) => {
  let hit = document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  if (!hit) return null;
  const tags = {BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox', DIALOG: 'dialog', NAV: 'navigation',
                MAIN: 'main', ASIDE: 'complementary', HEADER: 'banner', FOOTER: 'contentinfo', FORM: 'form',
                IMG: 'img', H1: 'heading', H2: 'heading', H3: 'heading', H4: 'heading', LI: 'listitem',
                IFRAME: 'iframe', SUMMARY: 'button', OPTION: 'option', P: 'paragraph'};
  const inputs = {checkbox: 'checkbox', radio: 'radio', button: 'button', submit: 'button', reset: 'button',
                  image: 'button', search: 'searchbox', range: 'slider', number: 'spinbutton'};
  const role = el => {
    const r = (el.getAttribute('role') || '').trim().split(/\\s+/)[0];
    if (r) return r;
    if (el.tagName === 'A') return el.hasAttribute('href') ? 'link' : 'generic';
    if (el.tagName === 'INPUT') return inputs[(el.type || '').toLowerCase()] || 'textbox';
    return tags[el.tagName] || 'generic';
  };
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
  const named = new Set(['button', 'link', 'checkbox', 'radio', 'tab', 'menuitem', 'option', 'heading', 'cell',
                         'listitem', 'switch', 'treeitem']);
  const name = el => {
    const label = el.getAttribute('aria-label');
    if (label) return clean(label);
    const by = el.getAttribute('aria-labelledby');
    if (by) return clean(by.split(/\\s+/).map(i => (document.getElementById(i) || {}).textContent || '').join(' '));
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') {
      const l = (el.labels && el.labels[0]) ? el.labels[0].textContent : '';
      return clean(l || el.getAttribute('placeholder') || el.getAttribute('title') || '');
    }
    if (el.tagName === 'IMG') return clean(el.getAttribute('alt') || el.getAttribute('title'));
    if (named.has(role(el))) return clean(el.innerText || el.textContent || el.getAttribute('title'));
    return clean(el.getAttribute('title') || '');
  };
  const chain = [];
  let layer = null;
  for (let n = hit, i = 0; n && n.nodeType === 1 && n !== document.body && n !== document.documentElement;
       n = n.parentElement || (n.getRootNode() && n.getRootNode().host), i++) {
    if (i < 8) chain.push({role: role(n), name: name(n)});
    const pos = getComputedStyle(n).position;
    const r = role(n);
    if (r === 'dialog' || r === 'alertdialog' || n.getAttribute('aria-modal') === 'true') { layer = n; break; }
    if (pos === 'fixed' || pos === 'sticky') layer = n;
  }
  const out = {chain, layer: null, buttons: []};
  if (layer) {
    out.layer = {role: role(layer), name: name(layer) || clean(layer.getAttribute('aria-label'))};
    for (const b of layer.querySelectorAll('button, a[href], [role=button], [role=link], input[type=submit], input[type=button]')) {
      if (out.buttons.length >= 12) break;
      const r = b.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      out.buttons.push({role: role(b), name: name(b)});
    }
  }
  return out;
}
"""

_HIT_JS = """
([x, y]) => {
  let hit = document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) {
    const inner = hit.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === hit) break;
    hit = inner;
  }
  return hit;
}
"""

_HOLDS_JS = "(el, hit) => el === hit || el.contains(hit) || (hit.getRootNode && el.contains(hit.getRootNode().host))"


def _fold(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _same_name(outline_name: str | None, page_name: str | None) -> bool:
    a, b = _fold(outline_name), _fold(page_name)
    if a == b:
        return True
    return len(a) >= 3 and len(b) >= 3 and (a.startswith(b) or b.startswith(a))


def last_outline_lines(page: Any) -> list[Any]:
    """The lines with a ref of the newest outline an agent saw of this tab (snapshot.OUTLINES), [] with none."""
    from openajar.node.snapshot import OUTLINES, parse_line

    try:
        seen = OUTLINES._seen.get(page)  # read only: the memory keeps one per agent, newest last
    except TypeError:
        return []
    if not seen:
        return []
    text = list(seen.values())[-1].text
    return [line for line in map(parse_line, text.splitlines()) if line.ref]


def _line_words(role: str, name: str | None, ref: str | None = None) -> str:
    words = role + (f' "{name}"' if name else "")
    return words + (f" [ref={ref}]" if ref else "")


async def _holds(page: Any, ref: str, hit: Any) -> bool:
    return bool(await quietly(page.locator(f"aria-ref={ref}").evaluate(_HOLDS_JS, hit, timeout=1500), False,
                              timeout=2.0))


async def covered_by(page: Any, point: tuple[float, float] | None) -> dict | None:
    """What sits at `point` (the press's point) of the top page: {"role", "name", "ref"?, "inside"?, "buttons"?},
    or None when the page does not answer."""
    if point is None:
        return None
    seen = await quietly(page.evaluate(_AT_POINT_JS, list(point)), None, timeout=READ_TIMEOUT_S)
    if not seen or not seen.get("chain"):
        return None
    first = seen["chain"][0]
    out: dict[str, Any] = {"role": first["role"], "name": first["name"] or None}
    lines = last_outline_lines(page)
    hit = await quietly(page.evaluate_handle(_HIT_JS, list(point)), None, timeout=READ_TIMEOUT_S) if lines else None
    checks = 0
    if hit is not None:
        for node in seen["chain"]:
            same = [ln for ln in lines if ln.role == node["role"]]
            # the same name first; then the outline's unnamed ones (its name can come from what the guess misses)
            for line in [ln for ln in same if _same_name(ln.name, node["name"])] + [ln for ln in same if not ln.name]:
                if checks >= MAX_CHECKS:
                    break
                checks += 1
                if await _holds(page, line.ref, hit):
                    out.update(role=line.role, name=line.name or None, ref=line.ref)
                    break
            if "ref" in out or checks >= MAX_CHECKS:
                break
    layer = seen.get("layer")
    if layer:
        out["inside"] = {"role": layer["role"], "name": layer["name"] or None}
    if "ref" not in out and layer and lines:
        buttons = []
        for b in seen.get("buttons") or []:
            line = next((ln for ln in lines if ln.role == b["role"] and b["name"] and _same_name(ln.name, b["name"])),
                        None)
            if line is not None and all(x["ref"] != line.ref for x in buttons):
                buttons.append({"role": line.role, "name": line.name, "ref": line.ref})
            if len(buttons) >= MAX_BUTTONS:
                break
        if buttons:
            out["buttons"] = buttons
    if hit is not None:
        await quietly(hit.dispose(), None, timeout=1.0)
    return out


def covered_words(found: dict | None, *, outline: bool) -> str:
    """target_covered's message: what covers the target and the one next step."""
    if not found:
        return ("another element covers the target (a banner, dialog or sticky header?); take a snapshot to see "
                "what, close it, then act again")
    inside = found.get("inside")
    if not found.get("ref") and inside:  # what to close is the layer (a dialog, a fixed bar), not the text in it
        what = _line_words(inside["role"], inside.get("name"))
    else:
        what = _line_words(found["role"], found.get("name"), found.get("ref"))
        if inside and (inside["role"], inside.get("name")) != (found["role"], found.get("name")):
            what += f", in {_line_words(inside['role'], inside.get('name'))}"
    head = f"another element covers the target: {what}"
    if found.get("ref"):
        return f"{head}. Close or answer it (a button in it, often), then act again"
    buttons = found.get("buttons") or []
    if buttons:
        listed = ", ".join(_line_words(b["role"], b.get("name"), b["ref"]) for b in buttons)
        return f"{head}, which has {listed}. Close it with one of them, then act again"
    if not outline:
        return f"{head}. Take a snapshot to get its ref, close it, then act again"
    return f"{head}, which has no ref in your last snapshot. Snapshot again, close it, then act again"


async def explain(page: Any, error: Any) -> Any:
    """The TargetCovered error with what covers the target: the words on its message, the data on its details."""
    if error.point is None or error.details.get("covered_by") is not None:
        return error
    found = await covered_by(page, error.point)
    out = type(error)(covered_words(found, outline=bool(last_outline_lines(page))), point=error.point)
    out.details = {**error.details, "covered_by": found}
    return out
