"""Environments: each agent's cloud computer, as the app's Environments pages read them.

    GET  /api/orgs/{org}/environments?state=open|all      {"environments": [EnvSummary]}
    GET  /api/orgs/{org}/environments/prepared            {machines, eur_month, eur_per_gb_month, pool, month}:
                                                          the prepared machines, the warm pool with its price
                                                          and worst month, this month's opens
                                                          (openajar.environments.fleet). The owner only
    GET  /api/orgs/{org}/environments/{id}                EnvDetail, with proofs [{id, title, taken_at,
                                                          page_url, width, height, src}], bringup, replay
                                                          and the resume (resumed, resumed_by, owner_woken,
                                                          resumed_at)
    GET  /api/orgs/{org}/environments/{id}/proofs/{proof}.png   a proof's PNG (src above)
    GET  /api/orgs/{org}/environments/{id}/screen.png     the still of its screen (`screen.src` below)
    GET  /api/orgs/{org}/environments/{id}/replay?after=&limit=  the replay of its browser: frames and calls
                                                          by time (openajar.environments.replay). The owner only
    GET  /api/orgs/{org}/environments/{id}/replay/{frame}.jpg    a frame's JPEG (its `src`). The owner only
    POST /api/orgs/{org}/environments/{id}/close          {"discard": false} -> EnvSummary (409 unsaved_work)
    POST /api/orgs/{org}/environments/{id}/extend         {"minutes": 60} -> EnvSummary
    DELETE /api/orgs/{org}/environments/saves/{save_id}   forget a save and its snapshot -> {forgot,
                                                          snapshots_deleted}. The owner in person
    POST /api/orgs/{org}/environments/saves/{save_id}/resume   resume a save, held by no chat
                                                          (openajar.environments.waking) -> EnvSummary with
                                                          opened, resumed and links. The owner in person
    GET  /api/orgs/{org}/environments/{id}/checkpoints/{checkpoint}  one checkpoint against the work now: per
                                                          repository its files and patch (capped). The owner

The detail's `checkpoints` (openajar.environments.checkpoints) carry `diff` in each repository: what
changed since, against the work tree now (files added, changed, removed; lines added, removed).

The detail's `bringup` (openajar.environments.timings) is each step of the bring-up with when it began and
its seconds, the steps running now while it starts, and the work it did not need (a prepared machine's
setup, a warm machine's rent and boot); `replay` is {frames, since, bytes, kept_until} or null.

Each listed summary also has `screen`: a recent still to draw the environment's picture from, or null.
The best is a still of the whole screen (`kind: "screen"`, openajar.environments.stills): about 640 px wide,
held in the node's memory, taken at most once a minute and only while someone looks at a list that shows it
(`?screens=1`, which the Environments page and Home send), never for a machine that is not up and awake,
never while a person has control of its screen, and not counted as the agent's use of the machine. It is
served by the screen route above to whoever may watch that screen's live view. Without one, the newest
landscape screenshot the agent kept as proof (`kind: "page"`, a page from its browser, served by the proof
route above; a video proof is never one).
The listing itself never waits on a machine: a still asked for now is in the next read.

The listing also gives `saved`: [{id, title, repo, branch, head, saved_at, snapshot_gb, files_bytes,
cost_eur_month, note, agent, prune_at, paused, kind, env_id, provider, server_type, price_hourly_eur,
services}], the saves of environments that are not open (openajar.environments.saving, fleet), and
`snapshots_eur_month`, what the kept snapshots cost. Each summary has `keep` (none, live, save).

The shapes are the contract of docs/design/environments.md. These read the environments service
(openajar.environments) themselves and decide who may: reading needs `see` and a close or an extend
`operate` (a member, in workspace `env`). The owner sees every environment; a member only those their
own agents opened (the opener the environment records); a grant only its own family's. Each is also
checked against the caller's grant by its lanes, as the jobs routes check a run's lane. One outside
is not found, so its id tells nothing. There are no live events: the app polls while one is open.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import BaseRoute

from openajar.api.access import Presence
from openajar.api.app import Api, access_of
from openajar.api.errors import error_body
from openajar.api.web.base import org_route
from openajar.core.errors import InvalidRequest, NotFound
from openajar.core.models import Effect
from openajar.environments.caller import grant_family
from openajar.environments.stills import stills_of

WORKSPACE = "env"
NO_ENVIRONMENTS = "This computer has no environments: agents open them with environment_open."
EXTEND_MAX = 24 * 60


def manager(api: Api) -> Any | None:
    mgr = api.optional("environments")
    return mgr if mgr is not None and callable(getattr(mgr, "summary", None)) else None


def environment_lane(api: Api, lane_id: str) -> bool:
    """Is this lane one an environment made (env/<id>, its terminal, or env/<id>-browser)? Those are the
    agent's own computer, shown on the environment's page, not an account."""
    workspace, _, name = str(lane_id).partition("/")
    mgr = manager(api)
    if workspace != WORKSPACE or mgr is None:
        return False
    try:
        return bool(mgr.store.has(name.removesuffix("-browser")))
    except (ValueError, AttributeError):
        return False


def environment_id(lane_id: str | None) -> str | None:
    """The environment a lane belongs to by its id alone (env/<id> or env/<id>-browser), else None."""
    workspace, _, name = str(lane_id or "").partition("/")
    return name.removesuffix("-browser") or None if workspace == WORKSPACE else None


def titles(api: Api, limit: int = 500) -> dict[str, str]:
    """Every recent environment's id to its title: the one name a person knows it by."""
    mgr = manager(api)
    if mgr is None:
        return {}
    try:
        return {env.id: env.title for env in mgr.store.recent(limit) if getattr(env, "title", None)}
    except (AttributeError, ValueError):
        return {}


def without_environments(api: Api, workspaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Workspaces as Accounts and Home see them: an environment's lanes are left out, and workspace env
    with them when nothing else is in it."""
    out: list[dict[str, Any]] = []
    for workspace in workspaces:
        lanes = workspace.get("lanes") or []
        kept = [lane for lane in lanes if not environment_lane(api, lane.get("id", ""))]
        if workspace.get("id") == WORKSPACE and lanes and not kept:
            continue
        out.append({**workspace, "lanes": kept} if len(kept) != len(lanes) else workspace)
    return out


def _visible(access: Any, env: Any) -> bool:
    """Whose environment the caller may see: the owner every one, a member their own agents', and a
    grant its own family's (openajar.environments.manager.reaches)."""
    if not access.lane_allowed(env.terminal_lane):
        return False
    if access.owner and access.member is None:
        return True
    member = access.member
    if member is not None:
        return env.member is not None and env.member == member.id
    grant = getattr(getattr(access, "ctx", None), "grant", None)
    return grant is not None and env.family == grant_family(grant)


def _save_visible(access: Any, m: Any) -> bool:
    """Whose saves the caller may see, as for environments: the owner every one, a member their own
    agents', a grant its own family's."""
    if access.owner and access.member is None:
        return True
    member = access.member
    if member is not None:
        return m.member is not None and m.member == member.id
    grant = getattr(getattr(access, "ctx", None), "grant", None)
    return grant is not None and m.family == grant_family(grant)


SAVED_KEYS = ("id", "title", "repo", "branch", "head", "saved_at", "snapshot_gb", "files_bytes", "cost_eur_month",
              "note", "agent", "prune_at", "paused", "kind", "env_id", "provider", "server_type", "price_hourly_eur",
              "services")


def replay_of(mgr: Any, env: Any) -> dict | None:
    """The detail's `replay`: {frames, since, bytes, kept_until}, or None when the node keeps none."""
    summary = getattr(mgr, "replay_summary", None)
    out = summary(env) if callable(summary) else None
    if out is None:
        return None
    from openajar.core import clock

    kept = getattr(mgr, "replay_kept_until", None)
    return {**out, "kept_until": clock.iso_or_none(kept(env), z=True) if callable(kept) else None}


def resume_of(env: Any) -> dict[str, Any]:
    """The detail's resume: `resumed` (as environment_open's, null when it was not resumed), `resumed_by`
    ("owner" or null), `owner_woken` (the owner resumed it and no chat took it yet) and `resumed_at` (its
    open, which is when the resume began)."""
    from openajar.core import clock
    from openajar.environments.waking import is_owner_woken

    return {"resumed": env.resumed, "resumed_by": env.resumed_by, "owner_woken": is_owner_woken(env),
            "resumed_at": clock.iso_or_none(env.created_at, z=True) if env.resumed else None}


def proofs_of(mgr: Any, env: Any, org: str) -> list[dict[str, Any]]:
    """The screenshots the agent kept as proof (openajar.environments.proofs), each with `src`: the
    app's own route for its PNG, behind the same sign-in as this page."""
    listed = getattr(mgr, "proofs", None)
    if not callable(listed):
        return []
    try:
        records = listed(env, links=False)
    except Exception:  # noqa: BLE001 - the page without its proofs rather than none at all
        return []
    out = []
    for r in records:
        base = f"/api/orgs/{org}/environments/{env.id}/proofs/{r['id']}"
        if r.get("kind") == "video":
            out.append({**r, "src": f"{base}.mp4", **({"gif_src": f"{base}.gif"} if r.get("gif") else {})})
        else:
            out.append({**r, "src": f"{base}.png"})
    return out


def may_watch(access: Any, env: Any) -> bool:
    """May the caller watch this environment's screen? The live view's own rule (its pointer events use it
    too): the screen's lane is in their scope, and for a member a grant's control covers it; the owner may."""
    lane = env.browser_lane
    if not access.lane_allowed(lane):
        return False
    terms = getattr(access, "watch_terms", None)
    return terms(lane) is not None if callable(terms) else bool(access.owner and access.member is None)


def still_of(stills: Any, env: Any, org: str) -> dict[str, Any] | None:
    """The still of the whole screen the node holds for `env`, as the list gives it, or None."""
    still = stills.get(env)
    if still is None:
        return None
    at = still.taken_at.isoformat()
    stamp = int(still.taken_at.timestamp())
    return {"src": f"/api/orgs/{org}/environments/{env.id}/screen.png?at={stamp}", "taken_at": at,
            "kind": "screen", "width": still.width, "height": still.height}


def screen_of(mgr: Any, env: Any, org: str) -> dict[str, Any] | None:
    """The still an environment's picture is drawn from: its newest landscape proof, or None. A phone-shaped
    proof would only be a sliver in a screen's frame, so it is passed over. Read from the node's own
    records (openajar.environments.proofs); the machine is never asked."""
    listed = getattr(mgr, "proofs", None)
    if not callable(listed):
        return None
    try:
        records = listed(env, links=False)
    except Exception:  # noqa: BLE001 - the drawn picture rather than an error in the list
        return None
    wide = [r for r in records if r.get("kind", "screenshot") == "screenshot"  # a video proof has no PNG
            and isinstance(r.get("width"), int) and isinstance(r.get("height"), int) and r["width"] >= r["height"] > 0]
    if not wide:
        return None
    newest = max(wide, key=lambda r: str(r.get("taken_at") or ""))
    return {"src": f"/api/orgs/{org}/environments/{env.id}/proofs/{newest['id']}.png", "taken_at": newest.get("taken_at"),
            "kind": "page", "width": newest["width"], "height": newest["height"]}


def summaries(api: Api, mgr: Any, access: Any, envs: list[Any], request: Request) -> list[dict[str, Any]]:
    """The listed environments, each with the still its picture is drawn from. `screens=1`: the caller shows
    the pictures, so the stills of the open ones it may watch are refreshed (in the background)."""
    org = request.path_params["org"]
    stills = stills_of(api.runtime)
    watched = [e for e in envs if e.open and may_watch(access, e)]
    if request.query_params.get("screens") == "1":
        stills.looked_at(watched)
    shown = {e.id: still_of(stills, e, org) for e in watched}
    return [{**mgr.summary(e), "screen": shown.get(e.id) or screen_of(mgr, e, org)} for e in envs]


async def serve_still(api: Api, access: Any, env: Any) -> Response:
    """The still of `env`'s screen, to whoever may watch its live view; 404 when there is none now (a person
    has control, it sleeps, or none was taken yet)."""
    if not may_watch(access, env):
        raise NotFound(f"Environment {env.id} has no screen to show.")
    await access.authorize(Effect.READ, action="environment.screen", lane_id=env.browser_lane)
    still = stills_of(api.runtime).get(env)
    if still is None:
        raise NotFound(f"Environment {env.id} has no still of its screen now.")
    return Response(still.png, media_type="image/png", headers={"Cache-Control": "private, max-age=300",
                                                                "X-Content-Type-Options": "nosniff"})


async def _body(request: Request) -> dict[str, Any]:
    if not (await request.body()):
        return {}
    try:
        body = await request.json()
    except ValueError:
        raise InvalidRequest("The body is JSON, like {\"discard\": false}.") from None
    if not isinstance(body, dict):
        raise InvalidRequest("The body is a JSON object.")
    return body


def routes(api: Api) -> list[BaseRoute]:
    def find(request: Request, action: str) -> tuple[Any, Any]:
        mgr = manager(api)
        if mgr is None:
            raise NotFound(NO_ENVIRONMENTS)
        env_id = request.path_params["id"]
        try:
            env = mgr.get(env_id)
        except (NotFound, ValueError):
            raise NotFound(f"There is no environment {env_id}.") from None
        access = access_of(request)
        if not _visible(access, env):
            access.trace(action, status="denied", lane=env.terminal_lane, summary="out of scope")
            raise NotFound(f"There is no environment {env_id}.")
        return mgr, env

    async def listing(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None:
            return {"environments": [], "available": False, "reason": NO_ENVIRONMENTS}
        state = request.query_params.get("state") or "open"
        if state not in ("open", "all"):
            raise InvalidRequest("state is open or all.")
        access = access_of(request)
        envs = [e for e in (mgr.store.open() if state == "open" else mgr.store.recent(200)) if _visible(access, e)]
        out: dict[str, Any] = {"environments": summaries(api, mgr, access, envs, request), "available": True}
        saved_rows = getattr(mgr, "saved_rows", None)
        if callable(saved_rows):
            try:
                rows = saved_rows(lambda m: _save_visible(access, m))
            except Exception:  # noqa: BLE001 - the page without its saves rather than none at all
                rows = []
            out["saved"] = [{k: r.get(k) for k in SAVED_KEYS} for r in rows]
            out["snapshots_eur_month"] = mgr.snapshots_eur_month()
        return out

    async def one(request: Request) -> dict[str, Any]:
        mgr, env = find(request, "environment.read")
        await access_of(request).authorize(Effect.READ, action="environment.read", lane_id=env.terminal_lane)
        out = await mgr.detail(env)
        out["proofs"] = proofs_of(mgr, env, request.path_params["org"])
        summaries = getattr(mgr, "checkpoint_summaries", None)
        if out.get("checkpoints") and callable(summaries):
            out["checkpoints"] = await summaries(env, out["checkpoints"])
        out["replay"] = replay_of(mgr, env)
        out.update(resume_of(env))
        return out

    async def close(request: Request) -> Any:
        mgr, env = find(request, "environment.close")
        access = access_of(request)
        access.require("operate", WORKSPACE, action="environment.close", lane=env.terminal_lane)
        body = await _body(request)
        discard = body.get("discard", False)
        if not isinstance(discard, bool):
            raise InvalidRequest("discard is true or false.")
        await access.authorize(Effect.READ, action="environment.close", lane_id=env.terminal_lane,
                               summary=f"Close environment {env.id} ({env.title})"
                               + (", discarding work that is not pushed" if discard else ""))
        from openajar.environments.errors import UnsavedWork

        try:  # the owner's close deletes a kept ("live") machine too
            closed = await mgr.close(env.id, discard=discard, final=True, reason="closed from the app"
                                     + (" (discarded)" if discard else ""))
        except UnsavedWork as e:
            return JSONResponse(error_body("unsaved_work", e.message, unsaved=e.details), status_code=409)
        access.done("environment.close", lane=env.terminal_lane, summary=f"closed environment {env.id}")
        return mgr.summary(closed)

    async def extend(request: Request) -> dict[str, Any]:
        mgr, env = find(request, "environment.extend")
        access = access_of(request)
        access.require("operate", WORKSPACE, action="environment.extend", lane=env.terminal_lane)
        body = await _body(request)
        minutes = body.get("minutes", 60)
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= EXTEND_MAX:
            raise InvalidRequest(f"minutes is a whole number from 1 to {EXTEND_MAX}.")
        await access.authorize(Effect.READ, action="environment.extend", lane_id=env.terminal_lane,
                               summary=f"Keep environment {env.id} ({env.title}) {minutes} min longer")
        out = await mgr.extend(env.id, minutes)
        access.done("environment.extend", lane=env.terminal_lane, summary=f"extended environment {env.id}")
        return mgr.summary(out)

    async def forget_save(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None or not callable(getattr(mgr, "forget", None)):
            raise NotFound(NO_ENVIRONMENTS)
        access = access_of(request)
        save_id = request.path_params["save_id"]
        from openajar.environments.saves import check_save_id

        try:
            m, _ = mgr.saves().find(check_save_id(save_id))
        except (NotFound, InvalidRequest):
            raise NotFound(f"There is no save {save_id}.") from None
        await access.authorize(Effect.READ, action="environment.forget",
                               summary=f"Forget the save {m.id} of environment \"{m.title}\" and its snapshot")
        out = await mgr.forget(None, None, m.id)
        access.done("environment.forget", summary=f"forgot save {m.id} ({m.title})")
        return out

    return [
        org_route(api, "/environments/saves/{save_id}", forget_save, ["DELETE"], owner=True,
                  presence=Presence.PERSON),
        org_route(api, "/environments", listing, ["GET"], cap="see"),
        prepared_route(api),  # before /environments/{id}: "prepared" is no environment's id
        org_route(api, "/environments/{id}", one, ["GET"], cap="see"),
        *_media_routes(api, find),
        *replay_routes(api, find),
        org_route(api, "/environments/{id}/close", close, ["POST"], cap="operate"),
        org_route(api, "/environments/{id}/extend", extend, ["POST"], cap="operate"),
        *_owner_routes(api, find),
    ]


def _media_routes(api: Api, find: Any) -> list[BaseRoute]:
    """What an environment's pictures are served from: a proof's PNG, a video proof's MP4 and GIF, and the
    still of its screen."""

    async def proof_png(request: Request) -> Response:
        mgr, env = find(request, "environment.proof")
        await access_of(request).authorize(Effect.READ, action="environment.proof", lane_id=env.terminal_lane)
        proof_id = request.path_params["proof"]
        read = getattr(mgr, "proof_png", None)
        data = read(env.id, proof_id) if callable(read) else None
        if not data:
            raise NotFound(f"Environment {env.id} has no proof {proof_id}.")
        return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=3600",
                                                               "X-Content-Type-Options": "nosniff"})

    async def screen_png(request: Request) -> Response:
        _mgr, env = find(request, "environment.screen")
        return await serve_still(api, access_of(request), env)

    async def proof_media(request: Request) -> Response:
        """A video proof's MP4 or GIF preview, with range requests (a player seeks)."""
        mgr, env = find(request, "environment.proof")
        await access_of(request).authorize(Effect.READ, action="environment.proof", lane_id=env.terminal_lane)
        proof_id, ext = request.path_params["proof"], request.url.path.rsplit(".", 1)[-1]
        read = getattr(mgr, "proof_path", None)
        path = read(env.id, proof_id, ext) if callable(read) else None
        if path is None:
            raise NotFound(f"Environment {env.id} has no proof {proof_id}.{ext}.")
        return FileResponse(path, media_type="video/mp4" if ext == "mp4" else "image/gif",
                            headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})

    return [
        org_route(api, "/environments/{id}/proofs/{proof}.png", proof_png, ["GET"], cap="see"),
        org_route(api, "/environments/{id}/screen.png", screen_png, ["GET"], cap="see"),
        org_route(api, "/environments/{id}/proofs/{proof}.mp4", proof_media, ["GET"], cap="see"),
        org_route(api, "/environments/{id}/proofs/{proof}.gif", proof_media, ["GET"], cap="see"),
    ]


def prepared_route(api: Api) -> BaseRoute:
    """The owner's prepared machines, the warm pool and this month's opens (openajar.environments.fleet)."""

    async def prepared(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None or not callable(getattr(mgr, "prepared_overview", None)):
            raise NotFound(NO_ENVIRONMENTS)
        await access_of(request).authorize(Effect.READ, action="environment.prepared")
        return await mgr.prepared_overview()

    return org_route(api, "/environments/prepared", prepared, ["GET"], owner=True)


def _owner_routes(api: Api, find: Any) -> list[BaseRoute]:
    """The owner's resume of a save, and one checkpoint's diff (openajar.environments.waking, checkpoints)."""

    async def checkpoint_diff(request: Request) -> dict[str, Any]:
        mgr, env = find(request, "environment.checkpoint")
        raw = request.path_params["checkpoint"]
        if not raw.isdigit() or not 0 < int(raw) < 10**9:
            raise InvalidRequest("a checkpoint is its number, like 3.")
        await access_of(request).authorize(Effect.READ, action="environment.checkpoint", lane_id=env.terminal_lane)
        return await mgr.checkpoint_diff(env, int(raw))

    async def resume_save(request: Request) -> dict[str, Any]:
        mgr = manager(api)
        if mgr is None or not callable(getattr(mgr, "resume_saved", None)):
            raise NotFound(NO_ENVIRONMENTS)
        access = access_of(request)
        save_id = request.path_params["save_id"]
        from openajar.environments.saves import check_save_id

        try:
            m, _ = mgr.saves().find(check_save_id(save_id))
        except (NotFound, InvalidRequest):
            raise NotFound(f"There is no save {save_id}.") from None
        await access.authorize(Effect.READ, action="environment.resume",
                               summary=f"Resume the save {m.id} of environment \"{m.title}\" on a machine")
        env, how = await mgr.resume_saved(m.id)
        access.done("environment.resume", lane=env.terminal_lane,
                    summary=f"resumed save {m.id} ({m.title}) as {env.id}" if how == "resumed" else
                    f"\"{m.title}\" was open already ({env.id})")
        return mgr.owner_view(env, how)

    return [
        org_route(api, "/environments/saves/{save_id}/resume", resume_save, ["POST"], owner=True,
                  presence=Presence.PERSON),
        org_route(api, "/environments/{id}/checkpoints/{checkpoint}", checkpoint_diff, ["GET"], owner=True),
    ]


def replay_routes(api: Api, find: Any) -> list[BaseRoute]:
    """The replay of an environment's browser (openajar.environments.replay), for the owner only: no
    member and no agent's grant reaches it."""

    async def replay(request: Request) -> dict[str, Any]:
        mgr, env = find(request, "environment.replay")
        if not callable(getattr(mgr, "replay_timeline", None)):
            raise NotFound(f"This computer keeps no replay of environment {env.id}.")
        await access_of(request).authorize(Effect.READ, action="environment.replay", lane_id=env.terminal_lane)
        query = request.query_params
        try:
            limit = int(query.get("limit") or 500)
        except ValueError:
            raise InvalidRequest("limit is a whole number from 1 to 2000.") from None
        if not 1 <= limit <= 2000:
            raise InvalidRequest("limit is a whole number from 1 to 2000.")
        src = f"/api/orgs/{request.path_params['org']}/environments/{env.id}/replay"
        return mgr.replay_timeline(env, src=src, after=query.get("after") or None, limit=limit)

    async def replay_jpg(request: Request) -> Response:
        mgr, env = find(request, "environment.replay")
        await access_of(request).authorize(Effect.READ, action="environment.replay", lane_id=env.terminal_lane)
        read = getattr(mgr, "replay_frame", None)
        data = read(env.id, request.path_params["frame"]) if callable(read) else None
        if not data:
            raise NotFound(f"Environment {env.id} has no frame {request.path_params['frame']}.")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600",
                                                                "X-Content-Type-Options": "nosniff"})

    return [org_route(api, "/environments/{id}/replay", replay, ["GET"], owner=True),
            org_route(api, "/environments/{id}/replay/{frame}.jpg", replay_jpg, ["GET"], owner=True)]
