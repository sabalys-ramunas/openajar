"""Computers of this computer's earlier agent grants, given to its current one (`openajar environments adopt`).

Environments, saves and title-bound logins belong to a grant family (openajar.environments.caller). Since
0.8.1 a local agent grant that replaces an earlier one keeps its family (openajar.policy.families), so
nothing is left behind. Before, each new grant had a family of its own: after an expiry, a freeze or new
default terms the agent saw `saved: []`, and its title opened a new, empty machine while the old one kept
billing (audit S7-01). This gives those computers to the current grant, once, by the owner's word:

- Whose: the families of local agent grants of this node (issued by `openajar local`) for the same subject
  as the current one, revoked or expired, that no live grant has. Another subject's (a computer renamed,
  another agent) is never taken, and the dry run lists it as left alone; a grant `openajar connect` issued
  for another computer is never one of them.
- What: the open environments (let go, so their title takes them back at once: the chat that held each
  one had the old grant, which no longer works), the saves (folder and manifest) and the title scopes of
  logins. Closed environment records stay as they are: they are the history of what that grant did.
- A title the current family has too stays the current one's; the moved computer is renamed
  "<title> (earlier)" (then "(earlier 2)"), so neither is resumed in place of the other, nor removed by the
  other's keep_last. Several earlier families with one title: the newest keeps it.
- Nothing is deleted. The move is a row of the action log (`environments_adopted`) per run.

The open environments move in one transaction of environments.db; each save is a folder rename then its
manifest, and each login one vault write, so a run that stops halfway moves the rest when run again. Why a
command and not the service's start: which agent reaches which computer is the owner's decision (the CLI is
a person, the service is not), a dry run only helps when someone reads it, and a title both families have
is renamed, which the owner should see before it happens.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.core.errors import InvalidRequest, NotFound
from openajar.environments.saves import SaveStore, title_key

EARLIER = "earlier"
TITLE_MAX = 80  # openajar.environments.manager.TITLE_MAX


@contextlib.contextmanager
def node_policy(home: Path) -> Iterator[Any]:
    """A node's policy, for the CLI to read its grants (None on a node that has none: then every caller is the
    owner, who reaches every computer)."""
    if not (Path(home) / "policy.db").exists():
        yield None
        return
    from openajar.core.config import load_config
    from openajar.policy.service import PolicyService

    policy = PolicyService(home, node_config=load_config(home))
    try:
        yield policy
    finally:
        policy.close()


def left_behind(policy: Any) -> set[str]:
    """The families earlier agent grants of this computer left behind, which its agent no longer reaches."""
    current = policy.local_agent_grant() if policy is not None else None
    if current is None:
        return set()
    return {f["family"] for f in policy.earlier_families(current["subject"], current["family"],
                                                         owner=current.get("owner"))}


def earlier_titles(store: Any, saves: SaveStore, families: set[str]) -> list[str]:
    """The titles of the open environments and saves of these families (sorted, each once)."""
    if not families:
        return []
    found = {title_key(e.title): e.title for e in (store.open() if store is not None else []) if e.family in families}
    for m in saves.all():
        if m.family in families:
            found.setdefault(m.title_key, m.title)
    return sorted(found.values(), key=str.lower)


# ---------------------------------------------------------------- what the agent is told (environment_open, environment())
EARLIER_WORDS = ("the owner runs `openajar environments adopt --yes` on this computer to give them to this "
                 "grant (`openajar environments adopt` lists them first)")


def earlier_of(ctx: Any) -> list[dict]:
    """The families this agent had under earlier grants of this computer that its grant does not reach
    (openajar.policy.families): what openajar 0.8.0 and before left behind. None for any other caller."""
    from openajar.environments.caller import grant_of

    found = getattr(ctx.runtime.get("policy"), "earlier_families_of", None)
    grant = grant_of(ctx)
    if grant is None or not callable(found):
        return []
    try:
        return found(grant)
    except Exception:  # noqa: BLE001 - only words for the agent: the call goes on without them
        return []


def refuse_earlier(ctx: Any, mgr: Any, title: str) -> None:
    """A title this agent's computer has under an earlier grant of this computer is named
    (saved_by_earlier_grant), never opened as a new, empty machine beside it (audit S7-01)."""
    earlier = {f["family"]: f for f in earlier_of(ctx)}
    if not earlier:
        return
    key = title_key(title)
    saved = [m for m in mgr.saves().all() if m.family in earlier and m.title_key == key]
    live = [e for e in mgr.store.open() if e.family in earlier and title_key(e.title) == key]
    if not saved and not live:
        return
    from openajar.environments.errors import SavedByEarlierGrant

    fam = earlier[(saved or live)[0].family]
    raise SavedByEarlierGrant(
        f"\"{title}\" is a computer this agent {'saved' if saved else 'still has open'} under an earlier agent "
        f"grant of this computer ({fam['grants'][0]}, {fam['state']}), which this grant does not reach yet. Nothing "
        f"was opened: a new machine would start empty. Tell the user: {EARLIER_WORDS}. Then "
        f"environment_open(title=\"{title}\") resumes it.")


def earlier_listing(ctx: Any, mgr: Any) -> dict | None:
    """environment()'s `earlier`: the titles this agent has under earlier grants of this computer, and what to
    tell the user; None when there are none."""
    earlier = earlier_of(ctx)
    titles = earlier_titles(mgr.store, mgr.saves(), {f["family"] for f in earlier}) if earlier else []
    if not titles:
        return None
    return {"titles": titles, "note": (
        "your computers under an earlier agent grant of this computer, which this grant does not reach yet, so "
        f"environment_open with one of these titles is refused. Tell the user: {EARLIER_WORDS}. Then each title "
        "resumes")}


# ---------------------------------------------------------------- the owner's move
def _renamed(base: str, taken: set[str]) -> str:
    """`<base> (earlier)`, else `(earlier 2)` and on, not in `taken` (title keys), within TITLE_MAX."""
    n = 1
    while True:
        tail = f" ({EARLIER})" if n == 1 else f" ({EARLIER} {n})"
        name = base[:TITLE_MAX - len(tail)].rstrip() + tail
        if title_key(name) not in taken:
            return name
        n += 1


@dataclass
class Adoption:
    """One node's earlier families and the current local agent grant (`plan`), and the move (`apply`).
    `store` is environments.db's store (None: a node that never opened one), `vault` the node's vault for the
    logins (None: they are not read, and the plan says so)."""

    policy: Any
    store: Any
    saves: SaveStore
    vault: Any = None

    def _current(self) -> dict:
        current = self.policy.local_agent_grant()
        if current is None:
            raise InvalidRequest("this node has no live agent grant of `openajar local` to give the computers to: run "
                                 "`openajar setup agents` (or `openajar local start`) first, then this again")
        return current

    async def plan(self) -> dict:
        """What would move: {to, from, environments, saves, logins, left, logins_unread}. Changes nothing."""
        current = self._current()
        to_family = current["family"]
        earlier = self.policy.earlier_families(current["subject"], to_family, owner=current.get("owner"))
        sources = {f["family"] for f in earlier}
        opened = self.store.open() if self.store is not None else []
        envs = [e for e in opened if e.family in sources]
        saves = [m for m in self.saves.all() if m.family in sources]
        names = self._names(to_family, envs, saves)
        logins, unread = await self._logins(sources)
        holding = {x.family for x in [*envs, *saves]} | {sc["family"] for _, sc in logins}
        others = self.policy.others_left(current["subject"])
        kept_by = {e.family for e in opened} | {m.family for m in self.saves.all()}
        return {
            "to": {"grant": current["id"], "family": to_family, "agent": current["subject"]},
            "from": [f for f in earlier if f["family"] in holding],
            "environments": [{"id": e.id, "title": e.title, "as": names.get((e.family, title_key(e.title))),
                              "family": e.family, "state": e.state} for e in envs],
            "saves": [{"id": m.id, "title": m.title, "as": names.get((m.family, m.title_key)), "family": m.family,
                       "saved_at": m.saved_at.isoformat(), "snapshot": m.snapshot is not None} for m in saves],
            "logins": [{"id": b.ref, "name": b.name, "site": b.site, "title": s["title"],
                        "as": names.get((s["family"], title_key(str(s["title"])))), "family": s["family"]}
                       for b, s in logins],
            "left": [{**f, "why": f"issued for another agent ({f['subject']}), not {current['subject']}"}
                     for f in others if f["family"] in kept_by],
            "logins_unread": unread,
        }

    def _names(self, to_family: str, envs: list, saves: list) -> dict[tuple[str, str], str]:
        """(family, title key) -> its new title, for the moved computers whose title is taken there."""
        taken = {title_key(e.title) for e in (self.store.open() if self.store is not None else [])
                 if e.family == to_family}
        taken |= {m.title_key for m in self.saves.all() if m.family == to_family}
        newest: dict[tuple[str, str], tuple[datetime, str]] = {}
        for fam, key, when, title in [*((e.family, title_key(e.title), e.created_at, e.title) for e in envs),
                                      *((m.family, m.title_key, m.saved_at, m.title) for m in saves)]:
            if (fam, key) not in newest or when > newest[(fam, key)][0]:
                newest[(fam, key)] = (when, title)
        out: dict[tuple[str, str], str] = {}
        # newest first: of several earlier families with one title (and none in the current one), it keeps it
        for (fam, key), (_when, title) in sorted(newest.items(), key=lambda kv: kv[1][0], reverse=True):
            if key in taken:
                out[(fam, key)] = _renamed(title, taken)
                taken.add(title_key(out[(fam, key)]))
            else:
                taken.add(key)
        return out

    async def _logins(self, sources: set[str]) -> tuple[list[tuple[Any, dict]], str | None]:
        """(login, its title scope) for each scope of these families, and why the logins were not read."""
        if not sources:
            return [], None
        if self.vault is None:
            return [], "this node's vault was not opened, so its logins were not read"
        from openajar.environments import logins

        unsealed = getattr(self.vault, "_unsealed", None)
        try:
            if callable(unsealed):
                await unsealed()
            bound = await logins.all_bound(self.vault)
        except Exception as e:  # noqa: BLE001 - a sealed vault: the computers move, its logins wait
            return [], f"the vault did not open ({getattr(e, 'message', None) or type(e).__name__})"
        return [(b, s) for b in bound for s in b.scopes if s.get("title") and s.get("family") in sources], None

    async def apply(self, *, by: str = "owner") -> dict:
        """Move what plan() lists, and record it. Returns the plan, with moved True."""
        plan = await self.plan()
        to, sources = plan["to"], {f["family"] for f in plan["from"]}
        moved_envs: list[str] = []
        if self.store is not None and plan["environments"]:
            moves = {e["id"]: e["as"] or e["title"] for e in plan["environments"]}
            moved_envs = [e.id for e in self.store.adopt(moves, sources=sources, family=to["family"])]
        moved_saves = []
        for m in plan["saves"]:
            with contextlib.suppress(NotFound):  # forgotten since the plan: nothing to move
                self.saves.move(m["id"], family=to["family"], title=m["as"])
                moved_saves.append(m["id"])
        moved_logins = await self._move_logins(plan["logins"], sources, to)
        renamed = {x["title"]: x["as"] for x in [*plan["environments"], *plan["saves"]] if x["as"]}
        if moved_envs or moved_saves or moved_logins:
            self._trace(plan, moved_envs, moved_saves, moved_logins, renamed, by)
        return {**plan, "moved": True}

    async def _move_logins(self, rows: list[dict], sources: set[str], to: dict) -> list[str]:
        if not rows or self.vault is None:
            return []
        from openajar.environments import logins

        renames = {(r["family"], title_key(str(r["title"]))): r["as"] for r in rows if r["as"]}
        out = []
        for b in await logins.all_bound(self.vault):
            if not any(s.get("title") and s.get("family") in sources for s in b.scopes):
                continue
            scopes = []
            for s in b.scopes:
                if s.get("title") and s.get("family") in sources:
                    s = {**s, "title": renames.get((s["family"], title_key(str(s["title"])))) or s["title"],
                         "family": to["family"]}
                scopes.append(s)
            config = dict(b.meta.config or {})
            config["envs"] = {**config.get("envs", {}), "scopes": scopes}
            await self.vault.set_config(b.ref, config)
            out.append(b.ref)
        return out

    def _trace(self, plan: dict, envs: list[str], saves: list[str], logins: list[str], renamed: dict,
               by: str) -> None:
        from openajar.policy.trace_models import TraceEvent

        to = plan["to"]
        what = ", ".join(w for w in (f"{len(saves)} saves" if saves else "",
                                     f"{len(envs)} open environments" if envs else "",
                                     f"{len(logins)} logins" if logins else "") if w)
        self.policy.traces.write(TraceEvent(
            kind="environments_adopted", tool="environments.adopt", principal=by, principal_kind="owner",
            grant_id=to["grant"], status="ok",
            summary=f"gave {what} of earlier agent grants of {to['agent']} to grant {to['grant']}",
            args={"from": [f["family"] for f in plan["from"]], "to": to["family"], "grant": to["grant"],
                  "environments": envs, "saves": saves, "logins": logins, "renamed": renamed}))
