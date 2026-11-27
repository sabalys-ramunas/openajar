"""The one wrapper every MCP tool call goes through.

The MCP layer does, per request:

    with bind_request_token(bearer, session=mcp_session_id):
        result = await wrapped(**arguments)

and registers tools with:

    for spec in specs:
        fn = wrap_tool(spec, runtime, error_factory=mcp_ToolError)  # SDK error class, optional
        server.add_tool(fn, name=spec.name, description=spec.description)

and lists them per token with `visible_tools(token, specs, runtime)`.

For each call the wrapper: resolves the principal, enforces tool-manifest pins, prechecks
the spec's effect and lane scope, writes a start trace, runs the handler with a
PolicyContext, turns ApprovalPending into a structured pending result, turns OpenajarError
into "<code>: <message>", and writes an end trace (duration, outcome, redacted args, cost).
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from openajar.core import logs
from openajar.core.errors import ApprovalPending, NotFound, OpenajarError, PolicyDenied
from openajar.core.presence import Presence
from openajar.core.tools import ToolSpec
from openajar.environments import routing
from openajar.policy.context import CallMeta
from openajar.policy.errors import Denied
from openajar.policy.redact import redact_text
from openajar.policy.service import PolicyService
from openajar.policy.traces import TraceEvent

log = logging.getLogger("openajar.policy.wrap")

request_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("openajar_request_token", default=None)
request_meta: contextvars.ContextVar[CallMeta | None] = contextvars.ContextVar("openajar_request_meta", default=None)


@contextmanager
def bind_request_token(token: str | None, *, session: str | None = None, run_id: str | None = None,
                       parent_id: str | None = None) -> Iterator[None]:
    """Bind the caller's bearer token (and optional MCP session, run and parent trace ids)
    for the duration of one request. The MCP layer sets this; nothing else should."""
    t1 = request_token.set(token)
    t2 = request_meta.set(CallMeta(session=session, run_id=run_id, parent_id=parent_id))
    try:
        yield
    finally:
        request_token.reset(t1)
        request_meta.reset(t2)


class ToolCallError(Exception):
    """A tool error whose text is "<code>: <message>". Pass `error_factory` to wrap_tool to
    raise the MCP SDK's own error class instead (so the text reaches the client)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _policy(runtime: Any) -> PolicyService:
    return runtime.require("policy")


@contextmanager
def _freeze_guard(runtime: Any, tool: str, lane_id: str | None, grant: Any,
                  workspace: str | None = None) -> Iterator[None]:
    """openajar.freeze: refuse a call on a frozen scope (its lane, or the workspace it names), and let a
    freeze interrupt it in flight. Every MCP caller is an agent here, the owner's token included,
    whatever its grant reaches."""
    freeze = runtime.get("freeze")
    if freeze is None:
        yield
        return
    extra = {"workspace": workspace} if isinstance(workspace, str) and workspace else {}
    with freeze.guard(tool=tool, lane_id=lane_id, chain=getattr(grant, "chain_ids", ()), **extra):
        yield


async def _frozen_aware(runtime: Any, ctx: Any, call: Any) -> Any:
    """Run a handler and account for the frozen scopes it was held off (PolicyContext.frozen_scope,
    asked by every scope helper): a "not found" or "not covered" it raised because of one is the
    `frozen` it really is, and a listing that left their rows out says so (`frozen`). A result that
    is not a dict (a bare list) cannot carry the note; its rows are left out all the same."""
    from openajar.core.errors import Frozen
    from openajar.core.tools import frozen_error
    from openajar.policy.freeze import frozen_note

    try:
        result = await call
    except (NotFound, PolicyDenied) as e:
        hits = getattr(ctx, "frozen_hits", None)
        if hits and not isinstance(e, Frozen):
            raise frozen_error(runtime.get("freeze"), hits) from None
        raise
    hits = getattr(ctx, "frozen_hits", None)
    if hits and isinstance(result, dict) and "frozen" not in result:
        result = {**result, "frozen": frozen_note(runtime.get("freeze"), hits)}
    return result


def _scrub_result(value: Any, token: str | None) -> Any:
    if not token or len(token) < 6:
        return value
    if isinstance(value, str):
        return value.replace(token, "[redacted]")
    if isinstance(value, dict):
        return {k: _scrub_result(v, token) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_result(v, token) for v in value]
    return value


def wrap_tool(spec: ToolSpec, runtime: Any, *,
              error_factory: Callable[[str], Exception] | None = None) -> Callable[..., Any]:
    """Wrap a ToolSpec handler into `async fn(**arguments)` with the handler's signature
    minus `ctx`, so an MCP SDK can build the input schema from it."""
    sig = inspect.signature(spec.handler, eval_str=True)
    params = list(sig.parameters.values())[1:]
    public_sig = sig.replace(parameters=params, return_annotation=inspect.Signature.empty)

    def fail(code: str, message: str, token: str | None) -> Exception:
        msg = redact_text(message, extra=[token] if token else [], max_len=500)
        if error_factory is not None:
            return error_factory(f"{code}: {msg}")
        return ToolCallError(code, msg)

    async def call(**kwargs: Any) -> Any:
        try:
            return await _call(**kwargs)
        finally:  # the call's audit rows are tried (lock wait included, off the loop) before it returns
            traces = getattr(runtime.get("policy") if hasattr(runtime, "get") else None, "traces", None)
            if getattr(traces, "deferred", False):
                await traces.settled()

    async def _call(**kwargs: Any) -> Any:
        policy = _policy(runtime)
        token = request_token.get()
        meta = request_meta.get() or CallMeta()
        scrub = [token] if token else []
        if routing.is_handle(kwargs.get("on")):  # an environment's handle never reaches a trace or the audit log
            scrub.append(" ".join(kwargs["on"].split()))
        started = time.monotonic()
        lane_id = kwargs.get(spec.lane_arg) if spec.lane_arg else None
        lane_id = lane_id if isinstance(lane_id, str) else None
        try:
            principal, grant = policy.resolve(token)
        except OpenajarError as e:
            policy.traces.write(TraceEvent(kind="tool_end", tool=spec.name, lane=lane_id, effect=spec.effect.value,
                                           status="denied", summary=e.message, args=kwargs, run_id=meta.run_id,
                                           duration_ms=int((time.monotonic() - started) * 1000)), extra_scrub=scrub)
            raise fail(e.code, e.message, token) from None
        logs.update(grant=grant.id, run=meta.run_id)  # the rest of this call's log lines carry them
        picked = None  # the environment route() chose: the result names it (openajar.environments.routing)
        if routing.routes(spec.name) and routing.wants_route(kwargs.get("on"), spec.name):
            try:
                picked = routing.route(runtime, spec.name, kwargs, session=meta.session, grant_id=grant.id,
                                       family=grant.env_family)
            except OpenajarError as e:
                policy.traces.write(TraceEvent(kind="tool_end", tool=spec.name, effect=spec.effect.value,
                                               status="error", error=f"{e.code}: {e.message}", args=kwargs,
                                               principal=principal.id, grant_id=grant.id, run_id=meta.run_id,
                                               duration_ms=int((time.monotonic() - started) * 1000)),
                                    extra_scrub=scrub)
                raise fail(e.code, e.message, token) from None
            lane_id = kwargs.get(spec.lane_arg) if isinstance(kwargs.get(spec.lane_arg), str) else None
        ctx = policy.context(grant, principal=principal, tool=spec.name, tags=spec.tags, lane_id=lane_id,
                             meta=meta, token=token)
        # every MCP caller is below a person: the node token is NODE_TOKEN, any grant AGENT (docs/THREAT-MODEL.md)
        ctx.presence = Presence.NODE_TOKEN if getattr(grant, "owner_token", False) else Presence.AGENT
        ctx.mcp = True  # an agent's call: another family's environment is out of reach (openajar.environments)
        start = policy.traces.write(TraceEvent(
            kind="tool_start", tool=spec.name, lane=lane_id, effect=spec.effect.value, status="started",
            principal=principal.id, principal_kind=principal.kind, grant_id=grant.id, grant_path=grant.path,
            args=kwargs, run_id=meta.run_id, parent_id=meta.parent_id), extra_scrub=scrub)
        ctx.start_id = start.id

        env_lane: list[str | None] = [lane_id]  # the environment lane this call counts on (a studio check sets it)

        def end(status: str, summary: str | None = None, error: str | None = None, result: Any = None) -> None:
            if status in ("ok", "error", "denied"):
                routing.after(runtime, spec.name, env_lane[0], kwargs, ok=status == "ok", scrub=scrub,
                              result=result, error="denied" if status == "denied" else error)
            eff = ctx.effect.value if ctx.effect is not None else spec.effect.value
            policy.traces.write(TraceEvent(
                kind="tool_end", tool=spec.name, lane=lane_id, effect=eff, status=status, summary=summary,
                error=error, principal=principal.id, principal_kind=principal.kind, grant_id=grant.id,
                grant_path=grant.path, args=kwargs, cost=ctx.cost, run_id=meta.run_id, parent_id=start.id,
                duration_ms=int((time.monotonic() - started) * 1000)), extra_scrub=scrub)

        try:
            policy.check_pins(grant, spec)
            ctx.precheck(spec.effect, lane_id=lane_id, lane_later=spec.name in routing.PICKS_LANE)
            with _freeze_guard(runtime, spec.name, lane_id, grant, kwargs.get("workspace")), \
                    routing.studio_proxy(runtime, spec.name, kwargs, session=meta.session,
                                         grant_id=grant.id, family=grant.env_family) as studio_lane:
                env_lane[0] = env_lane[0] or studio_lane
                since = routing.now(runtime) if spec.name in routing.NETWORK_TOOLS else None
                result = await _frozen_aware(runtime, ctx, spec.handler(ctx, **kwargs))
        except ApprovalPending as e:
            summary = e.message.removeprefix("waiting for the owner to approve: ")
            end("pending", summary=f"approval {e.approval_id}")
            out = {"status": "approval_pending", "approval_id": e.approval_id,
                   "summary": redact_text(summary, extra=scrub, max_len=1000), "check_with": "approval_wait",
                   "then": f"call {spec.name} again with the same arguments once approved"}
            if getattr(e, "why", None):
                out["why"] = redact_text(e.why, extra=scrub, max_len=1000)
            out.update(getattr(e, "confirm", None) or {})
            return out
        except PolicyDenied as e:
            end("denied", error=e.message)
            raise fail(e.code, e.message, token) from None
        except OpenajarError as e:
            end("error", error=f"{e.code}: {e.message}")
            raise fail(e.code, e.message, token) from None
        except asyncio.CancelledError:
            end("error", error="cancelled")
            raise
        except Exception as e:
            log.exception("tool %s failed", spec.name)
            end("error", error=f"{type(e).__name__}: {e}")
            raise fail("error", f"{spec.name} failed unexpectedly ({type(e).__name__})", token) from None
        tag_labels = {lbl for t in spec.tags for lbl in policy.cfg.taint_tags.get(t, [])}
        if tag_labels:
            try:
                ctx.taint(*tag_labels, source=spec.name)
            except OpenajarError:
                log.warning("bad taint_tags config for %s", spec.name)
        end("ok", result=result)
        return _scrub_result(routing.finish(runtime, spec.name, result, picked, since), token)

    call.__signature__ = public_sig  # type: ignore[attr-defined]
    call.__name__ = spec.name
    call.__qualname__ = spec.name
    call.__doc__ = spec.description
    call.spec = spec  # type: ignore[attr-defined]
    return call


def visible_tools(token: str | None, specs: list[ToolSpec], runtime: Any) -> list[ToolSpec]:
    """The subset of specs this token's grant may see and call."""
    return _policy(runtime).visible_tools(token, specs, runtime=runtime)


__all__ = ["Denied", "ToolCallError", "bind_request_token", "request_meta", "request_token", "visible_tools",
           "wrap_tool"]
