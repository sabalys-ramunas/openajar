"""MCP tools for disposable cloud VMs. A VM comes back as a terminal: run commands on it with
terminal_exec, shell_open/shell_write/shell_read, or terminal_install, exactly as on any terminal."""

from __future__ import annotations

from typing import Any

from openajar.core.models import Effect
from openajar.core.tools import ToolContext, ToolSpec


def _mgr(ctx: ToolContext):
    return ctx.runtime.require("vms")


def _grant_id(ctx: ToolContext) -> str:
    """Who asked: the caller's grant, else its principal (kept on the VM record)."""
    g = getattr(ctx, "grant", None)
    gid = getattr(g, "id", None)
    if gid:
        return str(gid)
    pid = getattr(getattr(ctx, "principal", None), "id", None)
    return f"agent:{pid}" if pid else "owner"


def _price(plan: dict) -> str:
    price = plan.get("price_hourly_eur")
    if price is None:
        return ""
    hours = -(-plan["ttl_minutes"] // 60)
    worst = max([price, *[q or 0 for _n, q in plan.get("fallbacks") or []]])
    out = f", EUR {price:.4f} an hour (EUR {price * hours:.2f} for {plan['ttl_minutes']} min"
    out += f"; at most EUR {worst * hours:.2f} if only a dearer type has room)" if worst > price else ")"
    if plan.get("fallbacks"):
        out += f"; if it is sold out: {', '.join(n for n, _q in plan['fallbacks'])}"
    return out


def _most(plan: dict) -> float | None:
    """The most the VM can cost for its whole time limit, in EUR (the dearest fallback included)."""
    price = plan.get("price_hourly_eur")
    if price is None:
        return None
    hours = -(-plan["ttl_minutes"] // 60)
    return round(max([price, *[q or 0 for _n, q in plan.get("fallbacks") or []]]) * hours, 2)


def _specs(plan: dict) -> str:
    sp = plan.get("specs")
    if not sp:
        return ""
    return f" ({sp.get('cores')} vCPU, {sp.get('memory_gb'):g} GB, {sp.get('disk_gb')} GB disk, {sp.get('architecture')})"


async def vm_create(ctx: ToolContext, *, on: str, profile: str | None = None, ttl_minutes: int | None = None,
                    server_type: str | None = None, cpu: int | None = None, memory_gb: float | None = None,
                    disk_gb: int | None = None, arch: str | None = None, purpose: str = "",
                    wait_s: float = 150) -> dict:
    """Rent a fresh cloud VM (Ubuntu, root, its own IP) as terminal `on` (workspace/name), for work that
    should not run on the node: builds, tests, untrusted code (your own computer, with a browser, the
    repo and saves, is environment_open). By default the node rents the
    cheapest server that fits: leave the size alone unless the owner said what they need or the
    task clearly needs more (a big build: cpu=8, memory_gb=16). `profile` picks a size the owner
    set up (vms_list shows them); `server_type` (e.g. cpx42) pins an exact type; cpu, memory_gb,
    disk_gb and arch (x86, arm, any) are minimums. `ttl_minutes` is how long it may live (the node
    deletes it then, or sooner when it sits idle at the end of a paid hour). Waits up to wait_s for
    SSH; state "ready" means terminal_exec and shell_open work on `on`. Call vm_destroy when you are
    done: it bills by the hour until deleted."""
    mgr = _mgr(ctx)
    size = {"server_type": server_type, "min_cpu": cpu, "min_memory_gb": memory_gb, "min_disk_gb": disk_gb,
            "arch": arch}
    plan = await mgr.plan(on, profile=profile, ttl_minutes=ttl_minutes, size=size)
    summary = (f"Rent a {plan['server_type']} VM{_specs(plan)} in {plan['location']} as terminal {on} for up to "
               f"{plan['ttl_minutes']} min{_price(plan)}. Asked for: {plan['size']} ({plan['profile']} profile), "
               f"image {plan['image']}" + (f". Purpose given: {purpose[:200]}" if purpose else ""))
    await ctx.authorize(Effect.VM, lane_id=on, summary=summary,
                        content={"create": on, "profile": plan["profile"], "server_type": plan["server_type"],
                                 "ttl_minutes": plan["ttl_minutes"], "price_hourly_eur": plan.get("price_hourly_eur"),
                                 "max_eur": _most(plan)},
                        facts={"profile": plan["profile"]})
    rec = await mgr.create(on, plan=plan, purpose=purpose, created_by=_grant_id(ctx),
                           wait_s=max(0.0, min(float(wait_s), 280.0)))
    ctx.trace("vm_create", lane=on, vm=rec.id, state=rec.state, server_type=rec.server_type)
    out = mgr.brief(rec)
    if rec.state == "ready":
        out["next"] = (f"run commands with terminal_exec(on={on!r}, cmd=...) or shell_open(on={on!r}); "
                       f"vm_clone(on={on!r}, repo=...) puts a private repo on it")
    elif rec.state == "booting":
        out["next"] = "still booting: call vms_list in a minute, then use it"
    return out


async def vm_clone(ctx: ToolContext, *, on: str, repo: str, ref: str | None = None, dest: str | None = None,
                   depth: int | None = None) -> dict:
    """Put a private git repo on the VM behind terminal `on`, at `dest` (default /root/src/<repo>). The
    node clones it with a read-only token the owner stored (`openajar vm repo add`) and copies the
    checkout over; the token never reaches the VM. `repo` is github.com/org/repo (or its https or
    git@ address); `ref` a branch, tag or commit (default: the default branch); `depth` 1 for a
    shallow copy. `origin` on the VM has no credentials, so push from there does not work. A public
    repo needs none of this: run git clone with terminal_exec."""
    from openajar.vms import repos
    mgr = _mgr(ctx)
    rec = mgr.find(on)
    name = repos.parse_repo(repo)
    pattern = await mgr.repo_tokens().pattern_for(name)
    if pattern is None:
        from openajar.core.errors import NotFound
        raise NotFound(f"no token for {name.name} on this node; the owner adds one with "
                       f"`openajar vm repo add {name.name}`. A public repo needs none: run git clone with terminal_exec")
    where = dest or f"/root/src/{name.short}"
    await ctx.authorize(Effect.VM, lane_id=rec.lane_id,
                        summary=(f"Copy private repo {name.name}" + (f" at {ref}" if ref else "")
                                 + f" to VM {rec.id} (lane {rec.lane_id}) at {where}, cloned on the node with the "
                                 f"owner's token for {pattern}. Code on the VM can reach the internet."),
                        content={"clone": name.name, "vm": rec.id, "ref": ref, "dest": where},
                        facts={"repo": name.name})
    out = await mgr.clone(rec.lane_id, name.name, git_ref=ref, dest=dest, depth=depth)
    ctx.trace("vm_clone", lane=rec.lane_id, vm=rec.id, repo=name.name, commit=out["commit"])
    out["next"] = f"terminal_exec(on={rec.lane_id!r}, cmd=..., cwd={out['dest']!r})"
    return out


async def vm_destroy(ctx: ToolContext, *, on: str) -> dict:
    """Delete the VM behind terminal `on` now (everything on it is gone). Do this as soon as the work
    is done and you have copied out what you need."""
    mgr = _mgr(ctx)
    rec = mgr.find(on)
    await ctx.authorize(Effect.VM, lane_id=rec.lane_id,
                        summary=f"Delete VM {rec.id} (lane {rec.lane_id}, {rec.server_type}) and everything on it",
                        content={"destroy": rec.id})
    rec = await mgr.destroy(rec.id, reason=f"vm_destroy by {_grant_id(ctx)}")
    ctx.trace("vm_destroy", lane=rec.lane_id, vm=rec.id, state=rec.state)
    return mgr.brief(rec)


async def vm_extend(ctx: ToolContext, *, on: str, minutes: int = 30) -> dict:
    """Give the VM behind terminal `on` more time (never past the node's maximum lifetime)."""
    mgr = _mgr(ctx)
    rec = mgr.find(on)
    await ctx.authorize(Effect.VM, lane_id=rec.lane_id,
                        summary=f"Keep VM {rec.id} (lane {rec.lane_id}, {rec.server_type}) {minutes} min longer",
                        content={"extend": rec.id, "minutes": minutes})
    rec = await mgr.extend(rec.id, minutes)
    ctx.trace("vm_extend", lane=rec.lane_id, vm=rec.id, expires_at=rec.expires_at.isoformat())
    return mgr.brief(rec)


async def vms_list(ctx: ToolContext, *, include_past: bool = False) -> dict:
    """VMs on this node: state, terminal, time left, cost so far; and the profiles you may ask for."""
    await ctx.authorize(Effect.READ)
    mgr = _mgr(ctx)
    allowed = getattr(ctx, "can_access_lane", None)
    vms = [v for v in mgr.list(include_past=include_past) if allowed is None or allowed(v["lane"])]
    s = mgr.settings
    tokens = [r["pattern"] for r in await mgr.repo_tokens().list()]
    return {"vms": vms, "configured": mgr.configured,
            "profiles": {n: {"size": p.words(), "server_type": p.server_type, "image": p.image,
                             "location": p.location or s.location, "description": p.description}
                         for n, p in s.profiles.items()},
            "sizing": "unless a profile pins a server_type, the node rents the cheapest type that fits",
            "private_repos": tokens,
            "default_profile": s.default_profile, "default_ttl_minutes": s.default_ttl_minutes,
            "max_ttl_minutes": s.max_ttl_minutes, "max_live": s.max_live}


def tool_specs(runtime: Any) -> list[ToolSpec]:
    t = ("vm",)
    return [
        ToolSpec("vm_create", vm_create, Effect.VM, tags=(*t, "terminal"), hidden_without="vms"),
        ToolSpec("vm_destroy", vm_destroy, Effect.VM, tags=t, hidden_without="vms"),
        ToolSpec("vm_extend", vm_extend, Effect.VM, tags=t, hidden_without="vms"),
        ToolSpec("vm_clone", vm_clone, Effect.VM, tags=(*t, "terminal"), hidden_without="vms"),
        ToolSpec("vms_list", vms_list, Effect.READ, lane_arg=None, tags=t, hidden_without="vms"),
    ]
