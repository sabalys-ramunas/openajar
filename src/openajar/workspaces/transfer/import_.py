"""Importing a workspace export (`openajar workspace import`): plan it, then apply it as one unit.

`plan_import(contents, src, options)` looks at this node and says what would happen: every lane,
proxy, vault entry, flow, schedule and grant, plus blockers (nothing is applied while there is
one), warnings and notes. It changes nothing; `--dry-run` prints it and stops.

`apply_import(plan, contents, src)` adds, in this order: proxies (carried over, or a placeholder
that points nowhere), the lanes (cold, through the lane manager's import_profile, so the node's
own checks run), the workspace record and readiness, the workspace's memory (format 2; its hash
chain must verify here), the vault entries (sealed into this node's vault in one transaction),
the re-issued grants, the flows, the schedules, the agenda entries (format 3). If any step fails,
everything the import added is removed again, newest first, and the error says so (naming
anything that could not be undone). The export's files were verified before the plan was made.

Rules:
- An existing lane id or workspace is refused: an import never merges. `rename_to` (--as) brings the
  workspace in under a new id, and everything follows: lane ids, flow lanes, flow ids that start
  with the old id, vault bindings, grant scopes, schedule lanes.
- A lane is never created without its proxy, because it would then use this node's own IP. A label
  this node lacks must be added first, mapped (proxy_map), carried in the export (with_proxies),
  or, with allow_missing_proxy, created as a placeholder that points at an address that never
  resolves, so the lane fails closed until `openajar proxy add <label> ...` sets the real one.
- Grant tokens never travel. With reissue_grants each grant definition is minted again here, with
  the time it had left; the new tokens are in the result, once.
- Schedules come in turned off unless enable_schedules: the source copy may still run them. One
  whose grant is not on this node stays off either way. Webhooks get new tokens and secrets.
- Agenda entries come in as drafts, whatever they were on the source: an approval binds to the source
  node's grants and is used up there, so approvals never travel. Each one is proposed again here.
  An entry on a connector operation this node has not installed is left out, with a note.
- Memory keeps its trust and labels (the file is encrypted to the owner) and forgotten entries stay
  forgotten. Under the same id every version hash is the source's; under --as the keys, decision
  scopes and lanes move to the new id and the chain is rebuilt, each version naming its old hash.
- Nothing on the source node is touched. The plan warns, loudly, that running both copies of a lane
  looks to the site like one device in two places.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from openajar.core.aio import maybe_await
from openajar.core.config import load_proxies, save_proxies
from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.core.models import LaneSpec, ProxyConfig
from openajar.policy.families import LOCAL_AGENT_NOTE
from openajar.workspaces.models import Readiness, check_workspace_id, unique_workspace_id
from openajar.workspaces.transfer.export import endpoint_digest, workspace_of
from openajar.workspaces.transfer.format import ExportError, ExportRefused
from openajar.workspaces.transfer.read import ExportContents
from openajar.workspaces.transfer.sources import Sources

log = logging.getLogger("openajar.ops")

VAULT_CONFLICTS = ("error", "rename", "skip")
PLACEHOLDER_HOST = "proxy-missing.invalid"   # .invalid never resolves (RFC 2606)
PLACEHOLDER_PORT = 9
AS_HINT = "Import it under a new workspace id with --as NEW."
PAGE_HINT = "Type another name for it, then check again."  # the form on this computer: its own box, no command


class ImportFailed(ExportError):
    code = "import_failed"


@dataclass
class ImportOptions:
    rename_to: str | None = None          # --as
    reissue_grants: bool = False
    allow_missing_proxy: bool = False
    proxy_map: dict[str, str] = field(default_factory=dict)  # source label -> label on this node
    vault_conflict: str = "error"         # a vault entry with the same name here: error, rename, skip
    enable_schedules: bool = False
    by: str = "owner"
    on_page: bool = False                 # the form on this computer: a clash asks for another name, never --as
    display_name: str | None = None       # the form on this computer: the name it shows; its id is made from it


# ---- renaming (--as) ------------------------------------------------------------------------

def rename_lane(value: str, old: str, new: str) -> str:
    """A lane id or pattern in workspace `old` moved to `new`; anything else unchanged."""
    ws, sep, rest = value.partition("/")
    return f"{new}/{rest}" if sep and ws == old else value


def rename_flow_id(flow_id: str, old: str, new: str) -> str:
    """'acme.instagram.post' -> 'acme2.instagram.post' (only a first segment equal to the workspace)."""
    first, sep, rest = flow_id.partition(".")
    return f"{new}{sep}{rest}" if first == old else flow_id


def _rename_specials(specials: Any, old: str, new: str) -> Any:
    if not isinstance(specials, dict):
        return specials
    out = json.loads(json.dumps(specials))
    for cap in ("secret", "exec"):
        if isinstance(out.get(cap), dict) and isinstance(out[cap].get("lanes"), list):
            out[cap]["lanes"] = [rename_lane(p, old, new) for p in out[cap]["lanes"]]
    return out


# ---- the plan -------------------------------------------------------------------------------

@dataclass
class ImportPlan:
    """What an import would do. `blockers` must be empty before it can be applied. Never holds a
    secret in what to_dict() returns."""
    source: dict[str, Any]
    workspace_from: str
    workspace_to: str
    record: dict[str, Any] | None
    lanes: list[dict[str, Any]] = field(default_factory=list)
    devices: list[dict[str, Any]] = field(default_factory=list)
    proxies: list[dict[str, Any]] = field(default_factory=list)
    vault: dict[str, Any] = field(default_factory=dict)
    flows: list[dict[str, Any]] = field(default_factory=list)
    schedules: list[dict[str, Any]] = field(default_factory=list)
    grants: list[dict[str, Any]] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    agenda: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    options: ImportOptions = field(default_factory=ImportOptions)
    # what apply needs, kept out of to_dict()
    _entries: list[Any] = field(default_factory=list, repr=False)
    _flows: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _schedules: list[Any] = field(default_factory=list, repr=False)
    _grants: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _agenda: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _proxy_to: dict[str, str | None] = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "source": self.source, "workspace": {"from": self.workspace_from,
                                                                    "to": self.workspace_to,
                                                                    "record": self.record is not None},
                "lanes": self.lanes, "devices": self.devices, "proxies": self.proxies, "vault": self.vault, "flows": self.flows,
                "schedules": self.schedules, "grants": self.grants, "memory": self.memory, "agenda": self.agenda,
                "blockers": self.blockers,
                "warnings": self.warnings, "notes": self.notes}


async def vault_state(vault: Any) -> str:
    """missing (no vault service), uninitialized, sealed, unsealed, or error."""
    from openajar.vault.errors import VaultSealed

    if vault is None:
        return "missing"
    try:
        if vault.store.header() is None:
            return "uninitialized"
        await vault.ensure_unsealed()
    except VaultSealed:
        return "sealed"
    except OpenajarError:
        return "error"
    return "unsealed"


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {many or word + 's'}"


async def plan_import(contents: ExportContents, src: Sources, options: ImportOptions | None = None, *,
                      now: datetime | None = None) -> ImportPlan:
    o = options or ImportOptions()
    old = contents.workspace_id
    new = check_workspace_id(o.rename_to) if o.rename_to else old
    if o.display_name:  # a name typed on the page is for people: the id is made from it, and is one nothing here has
        new = unique_workspace_id(o.display_name, lambda wid: _taken_here(src, wid))
    if o.vault_conflict not in VAULT_CONFLICTS:
        raise InvalidRequest(f"the vault conflict rule must be one of {', '.join(VAULT_CONFLICTS)}")
    record = contents.workspace.get("record")
    plan = ImportPlan(source={**(contents.header.get("source") or {}), "created_at": contents.header.get("created_at"),
                              "agentos_version": contents.header.get("agentos_version")},
                      workspace_from=old, workspace_to=new, record=record, options=o)
    when = now or datetime.now(UTC)
    _plan_workspace(plan, contents, src)
    _plan_proxies(plan, contents, src)
    _plan_lanes(plan, contents)
    _plan_devices(plan, contents, src)
    await _plan_vault(plan, contents, src)
    _plan_grants(plan, contents, src, when)
    _plan_flows(plan, contents, src)
    _plan_schedules(plan, contents, src)
    _plan_agenda(plan, contents, src)
    _plan_memory(plan, contents, src)
    _plan_device_warning(plan, contents, src)
    return plan


def _taken_here(src: Sources, workspace_id: str) -> bool:
    """Whether a workspace id is already on this node, as a workspace or as the first part of a lane's id."""
    if src.workspaces is not None and src.workspaces.get_workspace(workspace_id) is not None:
        return True
    return any(i.spec.id.startswith(f"{workspace_id}/") for i in src.lanes.list())


def _plan_workspace(plan: ImportPlan, contents: ExportContents, src: Sources) -> None:
    new = plan.workspace_to
    here = {i.spec.id for i in src.lanes.list()}
    collide = [f"{new}/{n}" for n in contents.lane_names if f"{new}/{n}" in here]
    in_use = sorted(lane for lane in here if lane.startswith(f"{new}/"))
    record_here = src.workspaces.get_workspace(new) if src.workspaces is not None else None
    hint = PAGE_HINT if plan.options.on_page else AS_HINT
    if collide:
        plan.blockers.append(f"lane id(s) {', '.join(collide)} already exist on this node. {hint}")
    elif in_use or record_here is not None:
        plan.blockers.append(f"workspace {new!r} already exists on this node ({_plural(len(in_use), 'lane')}); an "
                             f"import never merges into a workspace. {hint}")
    rows = [lane.get("readiness") for lane in contents.workspace["lanes"].values()]
    if src.workspaces is None and (plan.record is not None or any(rows)):
        plan.blockers.append("the workspaces service is not on this node, so the workspace record and readiness "
                             "cannot be imported")
    for name, lane in contents.workspace["lanes"].items():
        state = (lane.get("readiness") or {}).get("readiness")
        if state is not None and state not in {r.value for r in Readiness}:
            plan.blockers.append(f"lane {name} has an unknown readiness {state!r} in the export")


def _plan_proxies(plan: ImportPlan, contents: ExportContents, src: Sources) -> None:
    o = plan.options
    try:
        here = load_proxies(src.config)
    except ValueError as e:
        raise ExportError(str(e)) from None
    exported = {p["label"]: p for p in contents.proxies if isinstance(p, dict) and isinstance(p.get("label"), str)}
    users: dict[str, list[str]] = {}
    for name, lane in contents.workspace["lanes"].items():
        label = lane["spec"].get("proxy_label")
        if label:
            users.setdefault(label, []).append(f"{plan.workspace_to}/{name}")
    for label in sorted(set(o.proxy_map) - set(users)):
        plan.warnings.append(f"--map-proxy {label}=...: no lane in the export uses proxy {label!r}")
    for label, lanes in sorted(users.items()):
        p = exported.get(label, {})
        lanes_text = ", ".join(lanes)
        to: str | None = label
        if label in o.proxy_map:
            to, action = o.proxy_map[label], "mapped"
            if to not in here:
                plan.blockers.append(f"--map-proxy {label}={to}: there is no proxy {to!r} on this node (add it with: "
                                     f"openajar proxy add {to} host:port:user:pass)")
        elif label in here:
            action = "exists"
            if p.get("endpoint") and endpoint_digest(here[label]) != p["endpoint"]:
                action = "differs"
                plan.warnings.append(
                    f"proxy {label!r} on this node is not the same proxy as {label!r} on the source, so {lanes_text} "
                    "will reach their sites from a new IP (a new location, which sites may challenge). If that is "
                    f"wrong, map it to the right one with --map-proxy {label}=OTHER")
        elif p.get("host") and p.get("port"):
            action = "create"
            plan.notes.append(f"proxy {label!r} is added to this node from the export (it was exported with "
                              "--with-proxies)")
        elif o.allow_missing_proxy:
            action = "placeholder"
            plan.warnings.append(
                f"proxy {label!r} is not on this node: {lanes_text} are created with it pointing nowhere "
                f"({PLACEHOLDER_HOST}), so they fail closed instead of using this node's IP. Set the real one "
                f"with: openajar proxy add {label} host:port:user:pass")
        else:
            to, action = None, "missing"
            plan.blockers.append(
                f"proxy {label!r} (used by {lanes_text}) is not on this node. Add it (openajar proxy add {label} "
                f"host:port:user:pass), map it to one that is (--map-proxy {label}=OTHER), or pass "
                "--allow-missing-proxy to create the lanes with a placeholder that fails closed. A lane never "
                "falls back to this node's own IP")
        plan._proxy_to[label] = to
        plan.proxies.append({"label": label, "to": to, "action": action, "lanes": lanes})
    default = (plan.record or {}).get("default_proxy")
    if default and default not in users:
        mapped = o.proxy_map.get(default, default)
        if mapped not in here:
            plan.notes.append(f"the workspace's default proxy {default!r} is not on this node; it is left unset "
                              "(it only shapes lanes made from templates later)")
            mapped = None
        plan._proxy_to[default] = mapped


def _plan_lanes(plan: ImportPlan, contents: ExportContents) -> None:
    copied = {lane.get("name"): lane for lane in contents.manifest.get("lanes", []) if isinstance(lane, dict)}
    for name in contents.lane_names:
        lane = contents.workspace["lanes"][name]
        spec = lane["spec"]
        label = spec.get("proxy_label")
        info = copied.get(name, {})
        plan.lanes.append({
            "from": spec["id"], "to": f"{plan.workspace_to}/{name}", "proxy_from": label,
            "proxy": plan._proxy_to.get(label) if label else None, "fingerprint": contents.fingerprint(name) is not None,
            "device": spec.get("device"), "files": info.get("files", 0), "bytes": info.get("bytes", 0),
            "readiness": (lane.get("readiness") or {}).get("readiness") or Readiness.NEEDS_LOGIN.value})
        if contents.fingerprint(name) is None:
            plan.notes.append(f"lane {name} had no pinned fingerprint on the source (never launched there); it "
                              "gets a new one on its first launch here")


def _plan_devices(plan: ImportPlan, contents: ExportContents, src: Sources) -> None:
    """Lanes that shared a device (AUD-02) come in on one device again: the device is
    made from the lane whose pin the export carries (every lane's copy of it must be the same), so
    each lane is still the computer its site knows, and the others join it."""
    groups: dict[str, list[str]] = {}
    for name in contents.lane_names:
        device = contents.workspace["lanes"][name]["spec"].get("device")
        if device:
            groups.setdefault(device, []).append(name)
    if not groups:
        return
    lanes = src.lanes
    if not callable(getattr(lanes, "create_device", None)):
        plan.blockers.append(f"the export's lanes run on devices ({', '.join(sorted(groups))}) and this node's "
                             "lane manager has no devices")
        return
    store = getattr(lanes, "store", None)
    for device, names in sorted(groups.items()):
        pins: dict[bytes, list[str]] = {}
        for name in names:
            path = contents.fingerprint(name)
            if path is not None:
                pins.setdefault(path.read_bytes(), []).append(name)
        if len(pins) > 1:
            plan.blockers.append(f"the lanes of device {device} carry pins that disagree "
                                 f"({'; '.join(', '.join(n) for n in pins.values())}), so the device the sites know "
                                 "cannot be told apart. Fix it on the source (openajar lane audit) and export again")
            continue
        if store is not None and store.device(device) is not None:
            plan.blockers.append(f"device {device} already exists on this node, and it is another computer (or this "
                                 f"one already). Lanes of {plan.workspace_to} cannot join it: remove or rename it "
                                 "first, or import on another node")
            continue
        source = next(iter(pins.values()))[0] if pins else None
        label = contents.workspace["lanes"][names[0]]["spec"].get("proxy_label")
        plan.devices.append({"name": device, "proxy": plan._proxy_to.get(label) if label else None,
                             "lanes": [f"{plan.workspace_to}/{n}" for n in names],
                             "from_lane": f"{plan.workspace_to}/{source}" if source else None})
        plan.notes.append(f"device {device} is made again for {', '.join(names)}"
                          + (f", from {source}'s pin (the computer their sites know)" if source else
                             " (it was never pinned: its first launch here pins it)"))


async def _plan_vault(plan: ImportPlan, contents: ExportContents, src: Sources) -> None:
    from openajar.vault.errors import EntryInvalid
    from openajar.vault.models import NewEntry, validate_new_entry
    from openajar.vault.secret import Secret

    o = plan.options
    n = len(contents.vault)
    plan.vault = {"entries": n, "state": None, "conflicts": [], "on_conflict": o.vault_conflict}
    if not n:
        return
    state = await vault_state(src.vault)
    plan.vault["state"] = state
    what = _plural(n, "vault entry", "vault entries")
    if state in ("missing", "uninitialized"):
        plan.blockers.append(f"this node has no vault yet and the export carries {what}. Create it with "
                             "`openajar vault init`, then import again")
        return
    if state == "sealed":
        plan.blockers.append(f"the vault on this node is sealed, so the export's {what} cannot be sealed into it. "
                             "Unseal it (openajar vault unseal) and import again")
        return
    if state != "unsealed":
        plan.blockers.append("the vault on this node cannot be opened (see `openajar vault status`)")
        return
    old, new = plan.workspace_from, plan.workspace_to

    def secret(value: Any) -> Secret | None:
        return Secret(value) if isinstance(value, str) and value else None

    for e in contents.vault:
        try:
            if not isinstance(e, dict):
                raise EntryInvalid("malformed")
            entry = validate_new_entry(NewEntry(
                name=e.get("name"), kind=e.get("kind"), domain=e.get("domain"), hosts=list(e.get("hosts") or []),
                lanes=[rename_lane(p, old, new) for p in e.get("lanes") or []], username=secret(e.get("username")),
                secret=secret(e.get("secret")), totp=secret(e.get("totp")), config=e.get("config") or {}))
        except (OpenajarError, TypeError, ValueError) as exc:
            name = e.get("name") if isinstance(e, dict) else "?"
            message = exc.message if isinstance(exc, OpenajarError) else type(exc).__name__
            plan.blockers.append(f"vault entry {name!r} in the export cannot be used here: {message}")
            continue
        plan._entries.append(entry)
    names_here = {meta.name for meta in await src.vault.list_entries()}
    conflicts = sorted({entry.name for entry in plan._entries} & names_here)
    plan.vault["conflicts"] = conflicts
    if conflicts:
        shown = ", ".join(repr(c) for c in conflicts[:5]) + (" ..." if len(conflicts) > 5 else "")
        if o.vault_conflict == "error":
            plan.blockers.append(f"vault entries named {shown} already exist on this node. Pass --on-vault-conflict "
                                 "rename (keep both, the imported one gets a suffix) or skip (keep this node's)")
        else:
            verb = "imported with a suffix" if o.vault_conflict == "rename" else "skipped (this node's are kept)"
            plan.notes.append(f"vault entries named {shown} already exist here; the imported ones are {verb}")


def _plan_grants(plan: ImportPlan, contents: ExportContents, src: Sources, now: datetime) -> None:
    o = plan.options
    old, new = plan.workspace_from, plan.workspace_to
    max_ttl = getattr(getattr(src.policy, "cfg", None), "max_grant_ttl_s", None)
    for g in contents.grants:
        if not isinstance(g, dict) or not isinstance(g.get("id"), str) or not isinstance(g.get("subject"), dict):
            plan.blockers.append("a grant definition in the export is malformed")
            continue
        exp = g.get("exp")
        left = int(exp - now.timestamp()) if isinstance(exp, int | float) else None
        action = "reissue" if o.reissue_grants else "not_reissued"
        if left is not None and left <= 0:
            action = "expired"
        definition = {
            "subject": g["subject"], "workspaces": [new if w == old else w for w in g.get("workspaces") or []] or None,
            "lanes": [rename_lane(p, old, new) for p in g.get("lanes") or []] or None, "domains": g.get("domains"),
            "ceiling": g.get("ceiling") or "read", "specials": _rename_specials(g.get("specials"), old, new),
            "budget": g.get("budget"), "ttl_s": left, "not_before": g.get("nbf"), "depth": g.get("depth"),
            "fanout": g.get("fanout"), "pins": g.get("pins"), "egress": g.get("egress"),
            # the note that marks this computer's own agent grant (openajar.policy.families) never comes from a file
            "admin": bool(g.get("admin")), "note": None if g.get("note") == LOCAL_AGENT_NOTE else g.get("note")}
        if action == "reissue" and max_ttl and left and left > max_ttl:
            definition["ttl_s"] = max_ttl
            plan.notes.append(f"grant {g['id']} had more time left than this node allows; it is re-issued for "
                              f"{max_ttl // 86400} days")
        row = {"from": g["id"], "subject": g["subject"].get("id"), "kind": g["subject"].get("kind", "agent"),
               "workspaces": definition["workspaces"], "lanes": definition["lanes"], "ceiling": definition["ceiling"],
               "admin": definition["admin"], "expires_at": g.get("expires_at"), "action": action}
        plan.grants.append(row)
        if action == "reissue":
            plan._grants.append({"from": g["id"], **definition})
    if plan._grants and src.policy is None:
        plan.blockers.append("grants cannot be re-issued: the policy service is not on this node")
    waiting = sum(1 for g in plan.grants if g["action"] == "not_reissued")
    if waiting:
        plan.notes.append(f"{_plural(waiting, 'grant definition')} came with the export but are not re-issued: tokens "
                          "never travel. Pass --reissue-grants to mint new tokens for them (shown once)")
    expired = sum(1 for g in plan.grants if g["action"] == "expired")
    if expired:
        plan.notes.append(f"{_plural(expired, 'grant')} in the export had expired and are not re-issued")


def _plan_flows(plan: ImportPlan, contents: ExportContents, src: Sources) -> None:
    from openajar.runner.models import FlowDefinition, is_connector_id

    old, new = plan.workspace_from, plan.workspace_to
    if contents.flows and src.flows is None:
        plan.blockers.append("flows cannot be imported: the runner is not on this node")
        return
    for f in contents.flows:
        if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not isinstance(f.get("versions"), list):
            plan.blockers.append("a flow in the export is malformed")
            continue
        to = rename_flow_id(f["id"], old, new)
        versions, head = [], None
        try:
            for v in f["versions"]:
                d = dict(v["definition"])
                d["id"], d["lane"] = to, rename_lane(d["lane"], old, new)
                defn = FlowDefinition.model_validate(d)
                versions.append({"from": v["version"], "version": defn.content_hash(), "definition": defn,
                                 "by": v.get("saved_by") or v.get("created_by")})
                if v["version"] == f.get("head"):
                    head = versions[-1]
        except (KeyError, TypeError, ValueError) as e:
            plan.blockers.append(f"flow {f['id']} in the export cannot be used here ({type(e).__name__})")
            continue
        if head is None or not versions:
            plan.blockers.append(f"flow {f['id']} in the export has no current version")
            continue
        existing = src.flows.store.get_flow(to) if src.flows is not None else None
        action = "create"
        if existing is not None:
            have = {fv.version for fv in src.flows.versions(to)}
            if all(v["version"] in have for v in versions):
                action = "exists"
                if existing.version != head["version"]:
                    plan.notes.append(f"flow {to} is already here; its current version here is kept")
            elif is_connector_id(to):
                action = "conflict"
                plan.blockers.append(f"flow {to} is an installed connector operation in another version; install "
                                     "the connector here, an import cannot.")
            else:
                action = "conflict"
                why = AS_HINT if workspace_of(f.get("lane") or "") == old and to.split(".")[0] == old else (
                    "Its id does not start with the workspace id, so --as cannot rename it; rename or remove the "
                    "flow here first.")
                plan.blockers.append(f"flow {to} already exists on this node with other versions; an import never "
                                     f"changes another flow. {why}")
        elif is_connector_id(to):
            action = "conflict"
            plan.blockers.append(f"flow {to} is an installed connector operation; install the connector here first, "
                                 "an import cannot.")
        plan.flows.append({"from": f["id"], "to": to, "lane": rename_lane(f.get("lane") or "", old, new),
                           "versions": len(versions), "action": action, "shared": bool(f.get("shared"))})
        ordered = [v for v in versions if v is not head] + [head]
        plan._flows.append({"from": f["id"], "to": to, "action": action, "versions": ordered,
                            "map": {v["from"]: v["version"] for v in versions}})


def _plan_schedules(plan: ImportPlan, contents: ExportContents, src: Sources) -> None:
    from openajar.runner.models import Schedule

    o = plan.options
    old, new = plan.workspace_from, plan.workspace_to
    if contents.schedules and src.scheduler is None:
        plan.blockers.append("schedules cannot be imported: the runner is not on this node")
        return
    flows = {f["from"]: f for f in plan._flows}
    reissued = {g["from"] for g in plan._grants}
    exported_grants = {g["id"] for g in contents.grants if isinstance(g, dict)}
    held = 0
    for data in contents.schedules:
        try:
            s = Schedule.model_validate(data)
        except (TypeError, ValueError):
            plan.blockers.append("a schedule in the export is malformed")
            continue
        flow = flows.get(s.flow_id)
        if flow is None:
            plan.blockers.append(f"schedule {s.id} runs flow {s.flow_id}, which is not in the export")
            continue
        grant = s.grant_id
        usable = grant in (None, "owner") or grant in reissued
        if not usable:
            why = " (pass --reissue-grants)" if grant in exported_grants else " (a narrowed grant, or one beyond this workspace)"
            plan.notes.append(f"schedule {s.id} runs under grant {grant}, which is not re-issued here{why}; it comes in "
                              "turned off")
        enabled = o.enable_schedules and s.enabled and usable
        if s.enabled and not enabled and usable:
            held += 1
        version = flow["map"].get(s.flow_version) if s.flow_version else None
        if s.flow_version and version is None:
            plan.blockers.append(f"schedule {s.id} pins flow version {s.flow_version}, which is not in the export")
            continue
        moved = s.model_copy(update={"lane": rename_lane(s.lane, old, new) if s.lane else None, "flow_id": flow["to"],
                                     "flow_version": version})
        plan._schedules.append({"schedule": moved, "enabled": enabled, "grant": grant})
        plan.schedules.append({"id": s.id, "kind": s.kind.value, "flow": flow["to"], "lane": moved.lane,
                               "was_enabled": s.enabled, "enabled": enabled, "grant": grant,
                               "webhook": s.webhook is not None})
    if held:
        plan.notes.append(f"{_plural(held, 'schedule')} come in turned off, so the two copies do not both fire. Turn each "
                          "on with `openajar schedules enable <id>` once the source copy is stopped, or import with "
                          "--enable-schedules")
    if any(row["webhook"] for row in plan.schedules):
        plan.notes.append("webhook schedules get a new URL and secret on this node (shown once after the import); "
                          "update the senders")


AGENDA_DRAFT_NOTE = ("approvals do not travel between nodes, so propose each one again here (`openajar agenda "
                     "propose <id>`, or agenda_propose) once the source copy is cancelled")


def _plan_agenda(plan: ImportPlan, contents: ExportContents, src: Sources) -> None:
    from openajar.agenda.models import ActionKind, Entry, EntryState

    if not contents.agenda:
        return
    if src.agenda is None:
        plan.blockers.append("agenda entries cannot be imported: the agenda is not on this node")
        return
    old, new = plan.workspace_from, plan.workspace_to
    flows = {f["from"]: f for f in plan._flows}
    reissued = {g["from"] for g in plan._grants}
    node = (contents.header.get("source") or {}).get("node") or "the source node"
    for data in contents.agenda:
        try:
            e = Entry.model_validate({**data, "state": EntryState.DRAFT.value})
        except (TypeError, ValueError):
            plan.blockers.append("an agenda entry in the export is malformed")
            continue
        a = e.action
        if a.kind == ActionKind.FLOW:
            flow = flows.get(a.target)
            if flow is None:
                plan.blockers.append(f"agenda entry {e.id} runs flow {a.target}, which is not in the export")
                continue
            target, version = flow["to"], flow["map"].get(a.version) if a.version else None
            if a.version and version is None:
                plan.blockers.append(f"agenda entry {e.id} pins flow version {a.version}, which is not in the export")
                continue
        elif a.kind == ActionKind.CONNECTOR:
            here = src.flows.store.get_flow(a.target) if src.flows is not None else None
            if here is None:
                plan.notes.append(f"agenda entry {e.id} ({e.title}) uses connector operation {a.target}, which is not "
                                  "installed on this node; it is left out")
                continue
            target, version = a.target, a.version
            if version not in {v.version for v in src.flows.versions(a.target)}:
                version = here.version
                plan.notes.append(f"agenda entry {e.id} ({e.title}) plans the version of {a.target} installed here")
        else:  # the export leaves trigger entries out; a hand-edited file may not
            plan.notes.append(f"agenda entry {e.id} ({e.title}) fires a trigger, which is not imported; it is left out")
            continue
        grant = e.grant_id
        usable = grant in (None, "owner") or grant in reissued
        reason = f"imported from {node}: approvals do not travel, so propose it again"
        if not usable:
            reason += (f". It was drafted under grant {grant}, which is not re-issued here, so it cannot be proposed "
                       "until it is drafted again under a grant this node knows")
        moved = e.model_copy(update={"workspace": new, "reason": reason, "action": a.model_copy(update={
            "target": target, "version": version, "on": rename_lane(a.on, old, new) if a.on else None,
            "lane": rename_lane(a.lane, old, new) if a.lane else None,
            "surface": rename_lane(a.surface, old, new) if a.surface else None})})
        plan._agenda.append({"entry": moved, "grant": grant, "was": data.get("state")})
        plan.agenda.append({"id": e.id, "title": e.title, "action": target, "lane": moved.action.acts_on,
                            "was": data.get("state"), "state": "draft", "grant": grant, "grant_here": usable})
    if plan.agenda:
        plan.notes.append(f"{_plural(len(plan.agenda), 'agenda entry', 'agenda entries')} come in as drafts: "
                          f"{AGENDA_DRAFT_NOTE}")


def _plan_memory(plan: ImportPlan, contents: ExportContents, src: Sources) -> None:
    records = contents.memory
    if not records:
        plan.memory = {"entries": 0, "versions": 0, "forgotten": 0, "action": "none"}
        return
    count = {kind: sum(1 for r in records if r.get("record") == kind) for kind in ("entry", "version", "tombstone")}
    plan.memory = {"entries": count["entry"], "versions": count["version"], "forgotten": count["tombstone"],
                   "action": "import" if plan.workspace_to == plan.workspace_from else "import_renamed"}
    if src.memory is None:
        plan.blockers.append("the export carries workspace memory, but memory is not on this node")
        return
    if src.memory.has_memory(plan.workspace_to):
        plan.blockers.append(f"workspace {plan.workspace_to!r} already has memory on this node; an import never "
                             f"merges. {AS_HINT}")
    if plan.workspace_to != plan.workspace_from:
        plan.notes.append(f"memory moves to {plan.workspace_to}: keys, decision scopes and lanes follow, so its "
                          "versions get new hashes; each one records the hash it had on the source")
    plan.notes.append("memory evidence names traces on the source node; their snapshots come along, the traces "
                      "do not")


def _plan_device_warning(plan: ImportPlan, contents: ExportContents, src: Sources) -> None:
    if not contents.lane_names:
        return
    source = contents.header.get("source") or {}
    node, host = source.get("node") or "the source node", source.get("hostname") or "?"
    same = source.get("node") == src.config.name and source.get("hostname") == src.host.hostname()
    n = len(contents.lane_names)
    warm = [lane.get("id") for lane in contents.manifest.get("lanes", []) if isinstance(lane, dict) and lane.get("was_warm")]
    if same and plan.workspace_to != plan.workspace_from:
        plan.warnings.insert(0, (
            f"SAME DEVICE TWICE: this makes a second copy of {_plural(n, 'lane')} of {plan.workspace_from} on this node, "
            f"with the same cookies and the same pinned fingerprint. Never warm both copies: to the sites it looks "
            f"like one device in two places, which can trigger checkpoints and locked accounts."))
    elif not same:
        warm_text = (f" {_plural(len(warm), 'lane')} of them were warm there when exported and were warmed again "
                     "after the copy.") if warm else ""
        plan.warnings.insert(0, (
            f"ONE DEVICE, TWO PLACES: these {_plural(n, 'lane')} still exist on node {node} (host {host}); an export "
            f"never deletes anything there.{warm_text} Running both copies at once looks to the sites like one device "
            "in two places and can trigger checkpoints and locked accounts. Delete or archive the lanes on "
            f"{node} (`openajar lane delete <lane>` once it exists), or at least keep them cold, before you open "
            "them here."))


# ---- applying ---------------------------------------------------------------------------------

@dataclass
class ImportResult:
    workspace: str
    lanes: list[str]
    vault: dict[str, int]
    flows: list[str]
    schedules: list[dict[str, Any]]
    grants: list[dict[str, Any]]           # with the new tokens: show once
    webhooks: list[dict[str, Any]]         # with the new secrets: show once
    proxies_added: list[str]
    warnings: list[str]
    notes: list[str]
    memory: dict[str, Any] = field(default_factory=dict)
    agenda: list[dict[str, Any]] = field(default_factory=list)  # every entry comes in as a draft

    def to_dict(self) -> dict[str, Any]:
        return {"workspace": self.workspace, "lanes": self.lanes, "vault": self.vault, "flows": self.flows,
                "schedules": self.schedules, "grants": self.grants, "webhooks": self.webhooks,
                "proxies_added": self.proxies_added, "memory": self.memory, "agenda": self.agenda,
                "warnings": self.warnings,
                "notes": self.notes}




async def remove_lane(src: Sources, lane_id: str) -> None:
    """Undo a lane the import made (cold: the import never opened it). The lane manager's delete
    moves a lane to the trash for the owner to recover; a lane the import itself just made leaves
    nothing behind, so its trash copy and the workspace's empty directory go too."""
    lane_dir = src.config.lane_dir(lane_id)
    remover = getattr(src.lanes, "delete", None)
    if callable(remover):
        trash = await maybe_await(remover(lane_id, by="import rollback"))
        if trash:
            shutil.rmtree(trash, ignore_errors=True)
        shutil.rmtree(lane_dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            lane_dir.parent.rmdir()
        return
    store = getattr(src.lanes, "store", None)
    if store is not None and hasattr(store, "delete"):
        store.delete(lane_id)
    shutil.rmtree(lane_dir, ignore_errors=True)
    with contextlib.suppress(OSError):
        lane_dir.parent.rmdir()  # the workspace's directory, when this was its last lane


async def _remove_device(src: Sources, name: str) -> None:
    """Undo a device the import made: its lanes leave it (they are removed next), and its pin
    directory, which only the import wrote, goes too."""
    store = src.lanes.store
    for rec in store.list():
        if rec.spec.device == name:
            store.update_spec(rec.spec.model_copy(update={"device": None}))
    trash = await maybe_await(src.lanes.remove_device(name, by="import rollback"))
    if trash:
        shutil.rmtree(trash, ignore_errors=True)


def _remove_proxies(src: Sources, labels: list[str]) -> None:
    proxies = load_proxies(src.config)
    for label in labels:
        proxies.pop(label, None)
    save_proxies(src.config, proxies)


async def _rollback(undo: list[tuple[str, Callable[[], Any]]]) -> list[str]:
    problems = []
    for what, action in reversed(undo):
        try:
            await maybe_await(action())
        except Exception as e:  # noqa: BLE001  (keep undoing the rest)
            log.warning("import rollback: could not remove %s: %s", what, type(e).__name__)
            problems.append(what)
    return problems


async def apply_import(plan: ImportPlan, contents: ExportContents, src: Sources, *,
                       now: datetime | None = None) -> ImportResult:
    if not plan.ok:
        raise ExportRefused("the import has blockers; nothing was changed: " + "; ".join(plan.blockers))
    when = (now or datetime.now(UTC)).isoformat()
    new, o = plan.workspace_to, plan.options
    undo: list[tuple[str, Callable[[], Any]]] = []
    step = "proxies"
    result = ImportResult(workspace=new, lanes=[], vault={"added": 0, "renamed": 0, "skipped": 0}, flows=[],
                          schedules=[], grants=[], webhooks=[], proxies_added=[], warnings=list(plan.warnings),
                          notes=list(plan.notes))
    try:
        # proxies the lanes need: carried in the export, or placeholders that fail closed
        exported = {p["label"]: p for p in contents.proxies if isinstance(p, dict) and isinstance(p.get("label"), str)}
        adding = {}
        for row in plan.proxies:
            label = row["label"]
            if row["action"] == "create":
                p = exported[label]
                adding[label] = ProxyConfig(label=label, host=p["host"], port=int(p["port"]),
                                            username=p.get("username"), password=p.get("password"))
            elif row["action"] == "placeholder":
                adding[label] = ProxyConfig(label=label, host=PLACEHOLDER_HOST, port=PLACEHOLDER_PORT)
        if adding:
            proxies = load_proxies(src.config)
            proxies.update(adding)
            save_proxies(src.config, proxies)
            added = sorted(adding)
            undo.append(("proxies " + ", ".join(added), lambda: _remove_proxies(src, added)))
            result.proxies_added = added

        step = "lanes"
        devices = {d["name"]: d for d in plan.devices}
        lenders = {d["from_lane"]: d for d in plan.devices if d["from_lane"]}
        for d in plan.devices:  # a device never pinned: made first, its lanes join it
            if d["from_lane"] is None:
                await maybe_await(src.lanes.create_device(d["name"], proxy_label=d["proxy"], by=o.by))
                undo.append((f"device {d['name']}", lambda name=d["name"]: _remove_device(src, name)))
        # the lane whose pin a device takes comes in first, as itself; the device is made from it
        rows = sorted(plan.lanes, key=lambda r: r["to"] not in lenders)
        for row in rows:
            name = row["to"].split("/", 1)[1]
            device = row.get("device")
            lends = row["to"] in lenders
            spec = LaneSpec.model_validate({**contents.workspace["lanes"][name]["spec"], "id": row["to"],
                                            "proxy_label": row["proxy"], "device": None if lends else device})
            pin = contents.fingerprint(name) if (lends or device not in devices) else None
            await src.lanes.import_profile(spec, contents.profile_dir(name), pin)
            undo.append((f"lane {spec.id}", lambda lane=spec.id: remove_lane(src, lane)))
            result.lanes.append(spec.id)
            if lends and device:
                await maybe_await(src.lanes.create_device(device, from_lane=spec.id, by=o.by))
                undo.append((f"device {device}", lambda name=device: _remove_device(src, name)))
        result.lanes.sort()

        step = "the workspace record"
        _apply_workspace(plan, contents, src, when, undo)

        step = "the memory"
        if contents.memory:
            undo.append((f"the memory of {new}", lambda: src.memory.drop_workspace(new)))
            result.memory = src.memory.import_records(new, contents.memory, source={
                "export_created_at": contents.header.get("created_at")})

        step = "the vault entries"
        if plan._entries:
            await _apply_vault(plan, src, result, undo)

        step = "the grants"
        grant_map = {"owner": "owner"}
        for g in plan._grants:
            definition = {k: v for k, v in g.items() if k != "from"}
            granted, token = src.policy.issue(**definition, by=o.by)
            undo.append((f"grant {granted.id}", lambda gid=granted.id: src.policy.revoke(
                gid, by=o.by, reason="the workspace import that made it was rolled back")))
            grant_map[g["from"]] = granted.id
            result.grants.append({"from": g["from"], "id": granted.id, "subject": g["subject"].get("id"),
                                  "token": token})

        step = "the flows"
        for f in plan._flows:
            if f["action"] == "exists":
                continue
            for v in f["versions"]:
                src.flows.save(v["definition"], by=v["by"])
            undo.append((f"flow {f['to']}", lambda fid=f["to"]: src.flows.delete(fid)))
            result.flows.append(f["to"])

        step = "the schedules"
        for row in plan._schedules:
            s = row["schedule"]
            principal = dict(s.principal or {})
            if principal.get("grant_id") in grant_map:
                principal["grant_id"] = grant_map[principal["grant_id"]]
            moved = s.model_copy(update={"grant_id": grant_map.get(s.grant_id, s.grant_id) if s.grant_id else None,
                                         "principal": principal})
            stored, extras = src.scheduler.adopt(moved, enabled=row["enabled"])
            undo.append((f"schedule {stored.id}", lambda sid=stored.id: src.scheduler.remove(sid)))
            result.schedules.append({"from": s.id, "id": stored.id, "flow": stored.flow_id, "lane": stored.lane,
                                     "enabled": stored.enabled})
            if extras:
                result.webhooks.append({"schedule": stored.id, **extras})

        step = "the agenda entries"
        for row in plan._agenda:
            _apply_agenda_entry(row, src, grant_map, result, undo)
    except BaseException as e:
        left = await _rollback(undo)
        message = e.message if isinstance(e, OpenajarError) else type(e).__name__
        tail = ("everything it had added was removed again" if not left else
                f"it could not remove: {', '.join(left)}; remove those by hand")
        if isinstance(e, Exception):
            raise ImportFailed(f"the import stopped while adding {step} ({message}); {tail}") from e
        raise
    return result


def _apply_agenda_entry(row: dict[str, Any], src: Sources, grant_map: dict[str, str], result: ImportResult,
                        undo: list[tuple[str, Callable[[], Any]]]) -> None:
    """One entry, as a draft: no approval, no run, a fresh history, the grant it maps to here."""
    from openajar.agenda.models import EntryState
    from openajar.runner.util import new_id

    e = row["entry"]
    now = datetime.now(UTC)
    principal = dict(e.principal or {})
    if principal.get("grant_id") in grant_map:
        principal["grant_id"] = grant_map[principal["grant_id"]]
    entry = e.model_copy(update={
        "id": new_id("ag") if src.agenda.has(e.id) else e.id, "state": EntryState.DRAFT, "approval": None,
        "approved_hash": None, "run_id": None, "event_id": None, "retry_at": None, "proof": None, "finished_at": None,
        "principal": principal, "grant_id": grant_map.get(e.grant_id, e.grant_id) if e.grant_id else None,
        "history": [], "updated_at": now})
    entry.note(now, f"imported as a draft (it was {row.get('was') or 'planned'} on the source node)")
    src.agenda.insert(entry)
    undo.append((f"agenda entry {entry.id}", lambda eid=entry.id: src.agenda.delete(eid)))
    result.agenda.append({"from": e.id, "id": entry.id, "title": entry.title, "lane": entry.action.acts_on,
                          "state": "draft"})


def _apply_workspace(plan: ImportPlan, contents: ExportContents, src: Sources, when: str,
                     undo: list[tuple[str, Callable[[], Any]]]) -> None:
    store, new = src.workspaces, plan.workspace_to
    rows = {name: lane.get("readiness") for name, lane in contents.workspace["lanes"].items()}
    if store is None or (plan.record is None and not any(rows.values())):
        return
    undo.append((f"workspace {new}", lambda: store.delete_workspace(new)))
    if plan.record is not None:
        r = plan.record
        default = r.get("default_proxy")
        store.insert_workspace({"id": new, "name": plan.options.display_name or r.get("name") or new, "color": r.get("color"),
                                "default_proxy": plan._proxy_to.get(default) if default else None,
                                "notes": r.get("notes") or "", "created_at": r.get("created_at") or when,
                                "updated_at": when})
    for name, row in sorted(rows.items()):
        if not row:
            continue
        lane_id = f"{new}/{name}"
        platform = row.get("platform") or name
        store.record_lane({"id": lane_id, "workspace": new, "surface": row.get("surface"), "platform": platform,
                           "template": row.get("template"), "home_url": row.get("home_url"),
                           "login_url": row.get("login_url"), "created_at": row.get("created_at") or when})
        state = row.get("readiness") or Readiness.NEEDS_LOGIN.value
        if state != Readiness.NEEDS_LOGIN.value or row.get("note") or row.get("updated_at"):
            store.set_readiness(lane_id, workspace=new, platform=platform, readiness=state, note=row.get("note"),
                                by=row.get("updated_by") or "import", now=row.get("updated_at") or when)


async def _apply_vault(plan: ImportPlan, src: Sources, result: ImportResult,
                       undo: list[tuple[str, Callable[[], Any]]]) -> None:
    added = await src.vault.import_entries(plan._entries, on_conflict=plan.options.vault_conflict)
    metas = added["entries"]
    for meta in metas:
        undo.append((f"vault entry {meta.name!r}", lambda ref=meta.id: src.vault.remove_entry(ref)))
    result.vault = {"added": added["added"], "renamed": added["renamed"], "skipped": added["skipped"]}
    if plan.options.vault_conflict != "rename" or not added["renamed"]:
        return
    renamed = {e.name: m.name for e, m in zip(plan._entries, metas, strict=True) if e.name != m.name}
    for meta in metas:  # a code catcher names its mailbox entry; follow that entry's new name
        catcher = meta.config.get("catcher")
        if isinstance(catcher, dict) and catcher.get("account") in renamed:
            await src.vault.set_config(meta.id, {**meta.config, "catcher": {**catcher,
                                                                             "account": renamed[catcher["account"]]}})
